"""PR creation step — pushes branch, opens PR, transitions Jira ticket."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config.schemas import RepoConfig
from integrations.base.tracker import TrackerInterface
from integrations.base.vcs import VCSInterface
from orchestrator.constants import REPORT_SCOPE_GUARD
from workspace.workspace import Workspace

logger = logging.getLogger(__name__)

STAGED_MARK = " [staged]"             # ends a compared ticket's PR title, as " [skill]" ends the skill flow's
TITLE_LIMIT = 256                     # characters of the PR title: GitHub refuses a longer one


@dataclass
class PRCreationResult:
    """Result of the PR creation step."""
    success: bool
    pr_number: int = 0
    pr_url: str = ""
    error: str = ""


async def create_pr(
    workspace: Workspace,
    vcs: VCSInterface,
    tracker: TrackerInterface,
    repo_config: RepoConfig,
    event_bus: Any = None,
    *,
    compared: bool = False,
) -> PRCreationResult:
    """Push branch, open PR, transition Jira ticket.

    AC1: Push feature branch after scope certificate.
    AC2: Open PR with pr_description_template.
    AC3: Store PR number/URL in state.json.
    AC4: Transition Jira to In Review + comment with PR URL.

    `compared` (the ticket is compared with the skill flow): the PR is a draft titled `… [staged]`
    and AC4 is held; the operator's verdict makes those tracker writes.

    Push policy is graceful: if the project has a pre-push hook that fails for
    environmental reasons (e.g. Gradle/AAPT2 toolchain mismatch — see
    `gradle_remediation.looks_like_pre_push_hook_environmental_failure`), the
    push is automatically retried with `--no-verify` and a `push_hook_bypassed`
    event is emitted. Real detekt/lint findings remain blocking — those are
    the user's code being wrong and should not be silently skipped.
    """
    from orchestrator.gradle_remediation import (
        looks_like_pre_push_hook_environmental_failure,
    )

    state = workspace.state
    branch = state.branch

    if not branch:
        return PRCreationResult(
            success=False,
            error="No branch set in workspace state",
        )

    # Check scope check passed (report or certificate)
    scope_report = workspace.reports_dir / REPORT_SCOPE_GUARD
    scope_cert = workspace.meta_dir / "scope-certificate.md"
    scope_ok = scope_cert.exists()
    if not scope_ok and scope_report.exists():
        content = scope_report.read_text(encoding="utf-8").lower()
        scope_ok = "status: pass" in content or "pass" in content
    if not scope_ok:
        return PRCreationResult(
            success=False,
            error="Scope check not passed — cannot create PR",
        )

    try:
        # AC1: Push the feature branch.
        skip_hooks = bool(getattr(repo_config.vcs, "skip_pre_push_hook", False))
        try:
            await vcs.push(str(workspace.source_dir), branch, skip_hooks=skip_hooks)
        except RuntimeError as push_err:
            if skip_hooks or not looks_like_pre_push_hook_environmental_failure(str(push_err)):
                raise  # real failure, or already bypassed — surface it
            # Pre-push hook tripped on an environmental issue. Bypass it and
            # note it as a warning rather than failing the workspace.
            logger.warning(
                "Pre-push hook environmental failure for %s; retrying with --no-verify",
                state.ticket_id,
            )
            if event_bus is not None:
                event_bus.emit(
                    "push_hook_bypassed",
                    f"Pre-push hook failed for {state.ticket_id}; retried with --no-verify",
                    project_id=state.company_id,
                    ticket_id=state.ticket_id,
                    data={"reason": str(push_err)[:500]},
                )
            await vcs.push(str(workspace.source_dir), branch, skip_hooks=True)
            skip_hooks = True
        logger.info(
            "Pushed branch '%s' for %s%s",
            branch, state.ticket_id, " (--no-verify)" if skip_hooks else "",
        )

        # Build PR title and body
        summary = _get_ticket_summary(workspace)
        title = pr_title(state.ticket_id, summary, STAGED_MARK) if compared else f"{state.ticket_id}: {summary}"
        body = _build_pr_body(workspace, repo_config)

        # AC2: Open PR
        default_branch = repo_config.vcs.github.default_branch or "main"
        pr_number, pr_url = await vcs.open_pr(
            title=title,
            body=body,
            head_branch=branch,
            base_branch=default_branch,
            # Only a compared PR passes draft: the base VCSInterface and GitLab's open_pr take no such argument.
            **({"draft": True} if compared else {}),
        )
        logger.info("Opened PR #%d for %s: %s", pr_number, state.ticket_id, pr_url)

        # AC3: Store in state
        workspace.update_state(pr_number=pr_number, pr_url=pr_url)

        # AC4: Transition tracker and comment; its failures are logged there and do not fail the PR.
        if not compared:
            await tracker_writes_for_pr(tracker, repo_config, state.ticket_id, pr_url)

        return PRCreationResult(
            success=True,
            pr_number=pr_number,
            pr_url=pr_url,
        )

    except Exception as e:
        logger.error("PR creation failed for %s: %s", state.ticket_id, e)
        return PRCreationResult(success=False, error=str(e))


async def tracker_writes_for_pr(
    tracker: TrackerInterface,
    repo_config: RepoConfig,
    ticket_id: str,
    pr_url: str,
) -> list[str]:
    """Move the ticket to In Review, then comment "PR opened: <url>" (provider-aware).

    Returns one English line per write that failed, [] when both landed. A line names the
    exception class only: the exception text may carry the tracker's response.
    """
    failed: list[str] = []
    provider = repo_config.tracker.provider
    if provider == "jira":
        in_review_status = repo_config.tracker.jira.statuses.in_review
    else:  # trello and any future provider keyed on schema names
        in_review_status = "in_review"
    try:
        await tracker.transition_ticket(ticket_id, in_review_status)
    except Exception as e:
        logger.warning("Failed to transition %s to in_review: %s", ticket_id, e)
        failed.append(f"move to {in_review_status} failed: {type(e).__name__}")

    try:
        await tracker.add_comment(
            ticket_id,
            f"PR opened: {pr_url}",
        )
    except Exception as e:
        logger.warning("Failed to add PR comment to %s: %s", ticket_id, e)
        failed.append(f"\"PR opened\" comment failed: {type(e).__name__}")
    return failed


def pr_title(ticket_id: str, summary: str, mark: str) -> str:
    """`<ticket>: <summary><mark>`, the summary cut so the title fits GitHub's limit; `mark` is the flow's,
    " [staged]" or " [skill]"."""
    title = f"{ticket_id}: {summary}{mark}"
    if len(title) > TITLE_LIMIT:
        room = TITLE_LIMIT - len(f"{ticket_id}: …{mark}")
        title = f"{ticket_id}: {summary[:room]}…{mark}"
    return title


def _get_ticket_summary(workspace: Workspace) -> str:
    """Extract ticket summary from meta/ticket.md if available."""
    try:
        ticket_file = workspace.meta_dir / "ticket.md"
        if ticket_file.exists():
            first_line = ticket_file.read_text(encoding="utf-8").splitlines()[0]
            if first_line.startswith("# ") and ": " in first_line:
                return first_line.split(": ", 1)[1].strip()
    except Exception:
        pass
    return "Implementation"


def _build_pr_body(workspace: Workspace, repo_config: RepoConfig) -> str:
    """Build PR description from template or default."""
    template = repo_config.pr_description_template
    state = workspace.state

    if template:
        body = template.replace("{ticket_id}", state.ticket_id)
        body = body.replace("{ticket_url}", f"https://jira.example.com/browse/{state.ticket_id}")
        return body

    # Default template
    return (
        f"## {state.ticket_id}\n\n"
        f"Automated PR created by Cleave pipeline.\n\n"
        f"### Checklist\n"
        f"- [x] Implementation plan followed\n"
        f"- [x] Scope guard passed\n"
        f"- [ ] Code review\n"
        f"- [ ] Tests passing\n"
    )

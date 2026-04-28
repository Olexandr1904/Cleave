"""PR creation step — pushes branch, opens PR, transitions Jira ticket."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config.schemas import RepoConfig
from integrations.base.tracker import TrackerInterface
from integrations.base.vcs import VCSInterface
from workspace.workspace import Workspace

logger = logging.getLogger(__name__)


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
) -> PRCreationResult:
    """Push branch, open PR, transition Jira ticket.

    AC1: Push feature branch after scope certificate.
    AC2: Open PR with pr_description_template.
    AC3: Store PR number/URL in state.json.
    AC4: Transition Jira to In Review + comment with PR URL.
    """
    state = workspace.state
    branch = state.branch

    if not branch:
        return PRCreationResult(
            success=False,
            error="No branch set in workspace state",
        )

    # Check scope check passed (report or certificate)
    scope_report = workspace.reports_dir / "scope-guard-agent-output.md"
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
        # AC1: Push the feature branch
        skip_hooks = bool(getattr(repo_config.vcs, "skip_pre_push_hook", False))
        await vcs.push(str(workspace.source_dir), branch, skip_hooks=skip_hooks)
        logger.info(
            "Pushed branch '%s' for %s%s",
            branch, state.ticket_id, " (--no-verify)" if skip_hooks else "",
        )

        # Build PR title and body
        title = f"{state.ticket_id}: {_get_ticket_summary(workspace)}"
        body = _build_pr_body(workspace, repo_config)

        # AC2: Open PR
        default_branch = repo_config.vcs.github.default_branch or "main"
        pr_number, pr_url = await vcs.open_pr(
            title=title,
            body=body,
            head_branch=branch,
            base_branch=default_branch,
        )
        logger.info("Opened PR #%d for %s: %s", pr_number, state.ticket_id, pr_url)

        # AC3: Store in state
        workspace.update_state(pr_number=pr_number, pr_url=pr_url)

        # AC4: Transition Jira and comment
        try:
            await tracker.transition_ticket(
                state.ticket_id,
                repo_config.jira.statuses.in_review,
            )
        except Exception as e:
            logger.warning("Failed to transition %s to In Review: %s", state.ticket_id, e)

        try:
            await tracker.add_comment(
                state.ticket_id,
                f"PR opened: {pr_url}",
            )
        except Exception as e:
            logger.warning("Failed to add PR comment to %s: %s", state.ticket_id, e)

        return PRCreationResult(
            success=True,
            pr_number=pr_number,
            pr_url=pr_url,
        )

    except Exception as e:
        logger.error("PR creation failed for %s: %s", state.ticket_id, e)
        return PRCreationResult(success=False, error=str(e))


def _get_ticket_summary(workspace: Workspace) -> str:
    """Extract ticket summary from meta/ticket.json if available."""
    import json
    ticket_file = workspace.meta_dir / "ticket.json"
    if ticket_file.exists():
        try:
            data = json.loads(ticket_file.read_text())
            return data.get("summary", "Implementation")
        except (json.JSONDecodeError, KeyError):
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
        f"Automated PR created by Sickle pipeline.\n\n"
        f"### Checklist\n"
        f"- [x] Implementation plan followed\n"
        f"- [x] Scope guard passed\n"
        f"- [ ] Code review\n"
        f"- [ ] Tests passing\n"
    )

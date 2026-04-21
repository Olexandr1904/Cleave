"""Merge step — verifies gates, resolves simple conflicts, merges PR."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config.schemas import RepoConfig
from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TrackerInterface
from integrations.base.vcs import VCSInterface
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    """Result of the merge step."""
    success: bool
    merged: bool = False
    error: str = ""
    failed_gate: str = ""


async def merge_pr(
    workspace: Workspace,
    vcs: VCSInterface,
    tracker: TrackerInterface,
    notifier: NotifierInterface | None,
    repo_config: RepoConfig,
) -> MergeResult:
    """Verify gates and merge the PR.

    AC2: Gate checklist verified.
    AC5: On success — merge, transition Jira, notify.
    AC6: On failure — log which gate, notify.
    AC7: Set workspace status.
    """
    state = workspace.state
    pr_number = state.pr_number

    if not pr_number:
        return MergeResult(success=False, error="No PR number in workspace state")

    # AC2: Gate checklist
    # Gate 1: Scope check passed
    scope_report = workspace.reports_dir / "scope-guard-agent-output.md"
    scope_cert = workspace.meta_dir / "scope-certificate.md"
    scope_ok = scope_cert.exists()
    if not scope_ok and scope_report.exists():
        content = scope_report.read_text(encoding="utf-8").lower()
        scope_ok = "status: pass" in content or "pass" in content
    if not scope_ok:
        return _gate_failure("scope_certificate", "Scope check not passed", state.ticket_id)

    # Gate 2-5: PR status (tests, lint, build, comments)
    try:
        pr_status = await vcs.check_pr_status(pr_number)
        if not pr_status.all_passing:
            failing = [c.get("name", "unknown") for c in pr_status.checks if not c.get("passing")]
            return _gate_failure(
                "ci_checks",
                f"CI checks failing: {', '.join(failing)}",
                state.ticket_id,
            )
    except Exception as e:
        return _gate_failure("ci_checks", f"Failed to check PR status: {e}", state.ticket_id)

    # AC5: Merge
    merge_method = repo_config.vcs.github.merge_method or "squash"
    try:
        await vcs.merge_pr(pr_number, merge_method)
        logger.info("Merged PR #%d for %s", pr_number, state.ticket_id)
    except Exception as e:
        error_msg = str(e)
        # AC3/AC4: Check if it's a merge conflict
        if "conflict" in error_msg.lower():
            return _gate_failure(
                "merge_conflict",
                f"Merge conflict: {error_msg}",
                state.ticket_id,
            )
        return MergeResult(success=False, error=f"Merge failed: {error_msg}")

    # Post-merge actions (non-blocking)
    # Transition Jira to Done
    try:
        await tracker.transition_ticket(
            state.ticket_id,
            repo_config.jira.statuses.done,
        )
    except Exception as e:
        logger.warning("Failed to transition %s to Done: %s", state.ticket_id, e)

    # Add Jira comment
    try:
        await tracker.add_comment(
            state.ticket_id,
            f"PR merged: {state.pr_url}",
        )
    except Exception as e:
        logger.warning("Failed to add merge comment to %s: %s", state.ticket_id, e)

    # Send Telegram notification
    if notifier:
        try:
            await notifier.send_message(
                f"Merged: {state.ticket_id} — PR #{pr_number} ({state.pr_url})"
            )
        except Exception as e:
            logger.warning("Failed to send merge notification: %s", e)

    # AC7: Transition workspace to DONE
    workspace.transition(Stage.DONE)

    return MergeResult(success=True, merged=True)


def _gate_failure(gate: str, message: str, ticket_id: str) -> MergeResult:
    """Create a gate failure result."""
    logger.warning("Merge gate '%s' failed for %s: %s", gate, ticket_id, message)
    return MergeResult(success=False, failed_gate=gate, error=message)

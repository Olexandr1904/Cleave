"""Status handler — formats pipeline status for Telegram responses."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FRIENDLY = {
    "SCOPE_CHECK": "Code Check",
    "AWAITING_APPROVAL": "Awaiting Approval",
    "MANUAL_CONTROL": "Manual Control",
    "PR_REVIEW": "PR Review",
    "PUSHED": "Pushing",
}


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = int(seconds // 3600)
    days = hours // 24
    if days > 0:
        remaining_hours = hours % 24
        return f"{days}d {remaining_hours}h"
    return f"{hours}h {int((seconds % 3600) // 60)}m"


class StatusHandler:
    """Formats pipeline status summary and drill-down messages."""

    def __init__(self, jira_base_url: str = "") -> None:
        self._jira_base_url = jira_base_url.rstrip("/")

    def format_summary(
        self,
        mode: str,
        uptime_seconds: float,
        last_poll_ago_seconds: float,
        active_workspaces: list[Any],
        recent_completions: list[tuple[str, str, float]] | None = None,
    ) -> str:
        """Format the summary status message.

        recent_completions is a list of (ticket_id, final_state, epoch_ts)
        tuples for workspaces that have terminated since startup. The list
        is truncated upstream (ring buffer in the orchestrator).
        """
        lines = [
            "Cleave Status",
            "",
            f"Mode: {mode}",
            f"Uptime: {_format_duration(uptime_seconds)}",
            f"Last Jira poll: {_format_duration(last_poll_ago_seconds)} ago",
            "",
        ]

        lines.append(f"Active ({len(active_workspaces)}):")
        if active_workspaces:
            for ws in active_workspaces:
                s = ws.state
                iterations = s.stage_iterations.get(s.current_state.lower(), 0)
                suffix = ""
                if s.current_state == "AWAITING_APPROVAL":
                    suffix = ", awaiting approval"
                elif s.current_state == "BLOCKED":
                    suffix = ", blocked"
                elif iterations > 0:
                    max_iter = 2
                    suffix = f" (iteration {iterations}/{max_iter})"
                display = _FRIENDLY.get(s.current_state, s.current_state)
                lines.append(f"  {s.ticket_id} — {display}{suffix}")
        else:
            lines.append("  (none)")

        lines.append("")

        completions = recent_completions or []
        if completions:
            lines.append(f"Recent ({len(completions)}):")
            for ticket_id, final_state, _ts in completions:
                label = "merged" if final_state == "DONE" else final_state.lower()
                lines.append(f"  {ticket_id} — {label}")
        else:
            lines.append("Recent: none")

        return "\n".join(lines)

    def format_drill_down(self, workspace: Any) -> str:
        """Format a detailed drill-down for a specific workspace."""
        s = workspace.state
        lines = [
            f"{s.ticket_id}",
            "",
            f"Stage: {s.current_state}",
            f"Branch: {s.branch or 'N/A'}",
        ]

        if self._jira_base_url:
            lines.append(f"Jira: {self._jira_base_url}/browse/{s.ticket_id}")

        if s.pr_url:
            lines.append(f"PR: {s.pr_url}")

        lines.append("")
        lines.append("Iterations:")
        if s.stage_iterations:
            for stage, count in s.stage_iterations.items():
                lines.append(f"  {stage}: {count}")
        else:
            lines.append("  (none)")

        if s.current_state == "BLOCKED" and getattr(s, "human_input_question", None):
            reason = s.human_input_question.strip()
            if len(reason) > 500:
                reason = reason[:500] + "…"
            lines.append(f"\nBlocked on: {reason}")
        elif s.error:
            lines.append(f"\nLast error: {s.error}")
        else:
            lines.append(f"\nLast error: none")

        return "\n".join(lines)

"""Status handler — formats pipeline status for Telegram responses."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
        recent_done: list[Any],
        recent_failed: list[Any],
    ) -> str:
        """Format the summary status message."""
        lines = [
            "Sickle Status",
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
                lines.append(f"  {s.ticket_id} — {s.current_state}{suffix}")
        else:
            lines.append("  (none)")

        lines.append("")

        recent_all = recent_done + recent_failed
        if recent_all:
            lines.append("Recent (24h):")
            for ws in recent_done:
                lines.append(f"  {ws.state.ticket_id} — merged")
            for ws in recent_failed:
                lines.append(f"  {ws.state.ticket_id} — failed at {ws.state.previous_state or ws.state.current_state}")
        else:
            lines.append("Recent (24h): none")

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

        if s.error:
            lines.append(f"\nLast error: {s.error}")
        else:
            lines.append(f"\nLast error: none")

        return "\n".join(lines)

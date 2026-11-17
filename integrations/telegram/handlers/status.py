"""Status handler — formats pipeline status for Telegram responses."""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.tg_i18n import N_, detail, model_text, tr

logger = logging.getLogger(__name__)

_FRIENDLY = {
    "SCOPE_CHECK": N_("Code Check"),
    "PLAN_REVIEW": N_("Plan Review"),
    "CODE_REVIEW": N_("Code Review"),
    "AWAITING_APPROVAL": N_("Awaiting Approval"),
    "MANUAL_CONTROL": N_("Manual Control"),
    "PR_REVIEW": N_("PR Review"),
    "PUSHED": N_("Pushing"),
}


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return tr("{s}s", s=int(seconds))
    if seconds < 3600:
        return tr("{m}m", m=int(seconds // 60))
    hours = int(seconds // 3600)
    days = hours // 24
    if days > 0:
        remaining_hours = hours % 24
        return tr("{d}d {h}h", d=days, h=remaining_hours)
    return tr("{h}h {m}m", h=hours, m=int((seconds % 3600) // 60))


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
            tr("Cleave Status"),
            "",
            tr("Mode: {mode}", mode=mode),
            tr("Uptime: {duration}", duration=_format_duration(uptime_seconds)),
            tr("Last Jira poll: {duration} ago", duration=_format_duration(last_poll_ago_seconds)),
            "",
        ]

        lines.append(tr("Active ({n}):", n=len(active_workspaces)))
        if active_workspaces:
            for ws in active_workspaces:
                s = ws.state
                iterations = s.stage_iterations.get(s.current_state.lower(), 0)
                suffix = ""
                if s.current_state == "AWAITING_APPROVAL":
                    suffix = tr(", awaiting approval")
                elif s.current_state == "BLOCKED":
                    suffix = tr(", blocked")
                elif iterations > 0:
                    max_iter = 2
                    suffix = tr(" (iteration {n}/{max})", n=iterations, max=max_iter)
                display = tr(_FRIENDLY[s.current_state]) if s.current_state in _FRIENDLY else s.current_state
                lines.append(f"  {s.ticket_id} — {display}{suffix}")
        else:
            lines.append(tr("  (none)"))

        lines.append("")

        completions = recent_completions or []
        if completions:
            lines.append(tr("Recent ({n}):", n=len(completions)))
            for ticket_id, final_state, _ts in completions:
                label = tr("merged") if final_state == "DONE" else final_state.lower()
                lines.append(f"  {ticket_id} — {label}")
        else:
            lines.append(tr("Recent: none"))

        return "\n".join(lines)

    def format_drill_down(self, workspace: Any) -> str:
        """Format a detailed drill-down for a specific workspace."""
        s = workspace.state
        lines = [
            f"{s.ticket_id}",
            "",
            tr("Stage: {stage}", stage=s.current_state),
            tr("Branch: {branch}", branch=s.branch or "N/A"),
        ]

        if self._jira_base_url:
            lines.append(f"Jira: {self._jira_base_url}/browse/{s.ticket_id}")

        if s.pr_url:
            lines.append(f"PR: {s.pr_url}")

        lines.append("")
        lines.append(tr("Iterations:"))
        if s.stage_iterations:
            for stage, count in s.stage_iterations.items():
                lines.append(f"  {stage}: {count}")
        else:
            lines.append(tr("  (none)"))

        if s.current_state == "BLOCKED" and getattr(s, "human_input_question", None):
            reason = s.human_input_question.strip()
            if len(reason) > 500:
                reason = reason[:500] + "…"
            # The question is what an agent wrote (or Cleave's own reason): translated where it is sent.
            lines.append("\n" + tr("Blocked on: {reason}", reason=model_text(reason)))
        elif s.error:
            lines.append("\n" + tr("Last error: {error}", error=detail(s.error)))
        else:
            lines.append("\n" + tr("Last error: none"))

        return "\n".join(lines)

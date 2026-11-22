"""Status handler — formats pipeline status for Telegram responses."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from integrations.base.notifier import Button
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


# The states a ticket waits in for the operator's Retry or Skip; each gets a row of buttons under the summary.
STOPPED_STATES = ("BLOCKED", "FAILED", "DEFERRED")
_REASON_CHARS = 160  # of the one line of why a ticket stopped
_INDENT = "     "


def _stage_name(state: str | None) -> str:
    return tr(_FRIENDLY[state]) if state in _FRIENDLY else (state or "?")


def _first_line(text: Any) -> str:
    """The first non-empty line of `text` (not a str: ""), cut to _REASON_CHARS with an ellipsis."""
    if not isinstance(text, str):
        return ""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return line[:_REASON_CHARS] + "…" if len(line) > _REASON_CHARS else line


def _retry_time(retry_at: Any) -> str:
    try:
        return datetime.fromisoformat(retry_at).astimezone(timezone.utc).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _stopped_lines(s: Any) -> tuple[str, list[str]] | None:
    """A ticket that stopped (or waits at a gate): what follows its key, and the lines under it — why, from its
    question (an agent's, so marked for translation) or its error, and what Cleave waits for. None for a ticket
    that is running."""
    stage = _stage_name(s.previous_state)
    question, error = _first_line(s.human_input_question), _first_line(s.error)
    why = model_text(question) if question else (detail(error) if error else "")
    if s.current_state == "BLOCKED":
        head = tr("blocked at {stage}", stage=stage)
        waits = tr("Waits for: your reply to its message, or a button below")
    elif s.current_state == "FAILED":
        head, why = tr("failed at {stage}", stage=stage), detail(error) if error else ""
        waits = tr("Waits for: a button below")
    elif s.current_state == "DEFERRED":
        when = _retry_time(s.retry_at)
        head, why = tr("deferred at {stage}", stage=stage), detail(error) if error else ""
        waits = tr("Retries by itself at {time} UTC", time=when) if when else ""
    elif s.current_state == "AWAITING_APPROVAL":
        head, why = tr("awaiting approval after {stage}", stage=stage), ""
        waits = tr("Waits for: Approve or Reject on its message")
    else:
        return None
    return head, [_INDENT + line for line in (why, waits) if line]


def status_buttons(workspaces: list[Any]) -> list[Button]:
    """A row of Retry · From stage… · Skip for each ticket that stopped (STOPPED_STATES), in the summary's order."""
    buttons: list[Button] = []
    for row, ws in enumerate((ws for ws in workspaces if ws.state.current_state in STOPPED_STATES), start=1):
        key = ws.state.ticket_id
        buttons += [
            Button(tr("Retry {key}", key=key), f"retry:{key}", row=row),
            Button(tr("From stage…"), f"retry_pick:{key}", row=row),
            Button(tr("Skip"), f"skip:{key}", row=row),
        ]
    return buttons


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
                if (stopped := _stopped_lines(s)) is not None:
                    head, below = stopped
                    lines += [f"  {s.ticket_id} — {head}", *below]
                    continue
                iterations = s.stage_iterations.get(s.current_state.lower(), 0)
                suffix = ""
                if iterations > 0:
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

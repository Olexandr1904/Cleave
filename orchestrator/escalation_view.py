"""Shared renderer for escalated PR comment TG messages.

Extracted so command_handler can re-send messages for the recall flow
without depending on Orchestrator internals. Accepts either a dict or
an attribute-style object (e.g., ClassifiedComment) for the comment.
"""
from __future__ import annotations

from typing import Any

from integrations.base.notifier import Button
from orchestrator import tg_format


def _g(o: Any, key: str, default: Any = None) -> Any:
    """Read key from dict-or-attribute object."""
    if isinstance(o, dict):
        return o.get(key, default)
    return getattr(o, key, default)


def build_escalated_comment_message(
    state: Any,
    cc: Any,
    pr_number: int,
    ticket_title: str = "",
    *,
    recall: bool = False,
) -> tuple[str, list[Button]]:
    """Build (text, buttons) for an escalated PR comment.

    cc may be a dict or an object with attributes. Required keys/attrs:
    comment_id, author, file, line, body, reason, verdict (optional).
    """
    sep = "─" * 30
    recall_prefix = "🔁 (still pending) " if recall else ""
    hdr = tg_format.tg_header(
        f"{recall_prefix}💬",
        state.company_id,
        state.ticket_id,
        ticket_title,
    )

    verdict = _g(cc, "verdict", "Unsure")
    reason = _g(cc, "reason", "") or ""
    if verdict in ("Valid", "Not valid"):
        assessment_line = f"  {verdict} — {reason}"
    else:
        assessment_line = f"  {reason}"

    body = _g(cc, "body", "") or ""
    text = (
        f"{hdr}\n"
        f"PR #{pr_number} — Comment by @{_g(cc, 'author', '?')} "
        f"on {_g(cc, 'file', '?')}:{_g(cc, 'line', '?')}\n"
        f"{sep}\n"
        f"Suggestion:\n  {body[:300]}\n\n"
        f"Agent assessment:\n{assessment_line}\n"
        f"{sep}\n"
        "Tap a button below, or reply to this message with:\n"
        "  - fix — re-engage dev-agent\n"
        "  - won't fix: <reason> — post the reason on GitHub and resolve\n"
        "  - free text — re-investigate with your hint\n"
    )

    comment_key = f"{state.ticket_id}:{_g(cc, 'comment_id')}"
    buttons = [
        Button(label="Fix", action=f"pr_fix:{comment_key}"),
        Button(label="Won't Fix", action=f"pr_wontfix:{comment_key}"),
    ]
    return text, buttons

"""Shared Telegram message formatting helpers.

Extracted so Orchestrator and CommandHandler produce consistent ticket
headers without depending on each other.
"""
from __future__ import annotations

import json
import re
from typing import Any

_SEP = "_" * 30


def tg_header(emoji: str, project_id: str, ticket_id: str, title: str = "") -> str:
    """Build the standard ticket message header.

    Format (with title):
        ❌ [project_id] ticket_id
        Ticket title here
        ______________________________

    Format (no title):
        ❌ [project_id] ticket_id
        ______________________________

    Emoji prefix is omitted (no leading space) when emoji is empty string.
    """
    first = f"{emoji} [{project_id}] {ticket_id}" if emoji else f"[{project_id}] {ticket_id}"
    parts = [first]
    if title:
        parts.append(title)
    parts.append(_SEP)
    return "\n".join(parts)


def read_ticket_title(workspace: Any) -> str:
    """Read ticket summary from meta/ticket.json. Returns empty string on any failure."""
    try:
        ticket_file = workspace.meta_dir / "ticket.json"
        if ticket_file.exists():
            data = json.loads(ticket_file.read_text(encoding="utf-8"))
            return data.get("summary", "")
    except Exception:
        pass
    return ""


def strip_markdown(text: str) -> str:
    """Remove markdown syntax that renders as raw characters in Telegram.

    Handles: **bold**, `inline code`, ```blocks```, | tables |,
    |---|---| separator rows, ## headings.
    Does not touch Unicode (bullets •, box-drawing ─, emojis, arrows →).
    """
    if not text:
        return text

    # Triple-backtick code blocks (with optional language tag)
    text = re.sub(r"```[^\n]*\n?", "", text)

    # Inline backticks
    text = re.sub(r"`([^`\n]*)`", r"\1", text)

    # Bold: **text**
    text = re.sub(r"\*\*([^*\n]*)\*\*", r"\1", text)

    # Italic: *text* (single asterisk, not touching **)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)

    # Headings: ## Title → Title
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)

    # Table separator rows: |---|---| → remove line entirely
    text = re.sub(r"(?m)^\|[-| :]+\|\s*$\n?", "", text)

    # Table data rows: | A | B | → A — B
    def _table_row(m: re.Match) -> str:
        cells = [c.strip() for c in m.group(0).split("|") if c.strip()]
        return " — ".join(cells)

    text = re.sub(r"(?m)^\|.+\|.*$", _table_row, text)

    # Collapse 3+ blank lines left by removed blocks
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()

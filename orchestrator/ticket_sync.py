"""Keep ticket metadata files (ticket.md, comments.md, history.md, attachments)
in sync with the tracker.

Provider-neutral: uses the TrackerInterface public API only. No
Jira/Trello-specific code lives here.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from integrations.base.tracker import TicketData, TrackerInterface
from workspace.workspace import Workspace

logger = logging.getLogger(__name__)

# Attachment ingestion limits. The agent runtime caps each context file at
# 5 KB, so most of a 1 MB log won't reach the model — but we still keep the
# raw file on disk for tools to grep.
MAX_ATTACHMENT_BYTES = 1_000_000

# Extensions we treat as text even when MIME is missing or generic.
_TEXT_ATTACHMENT_EXTS = {
    ".txt", ".log", ".json", ".xml", ".yaml", ".yml", ".csv", ".tsv",
    ".md", ".html", ".htm", ".css",
    ".kt", ".kts", ".java", ".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp",
    ".py", ".rb", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx",
    ".gradle", ".properties", ".toml", ".ini", ".conf", ".sh",
    ".stacktrace", ".trace", ".diff", ".patch",
}


def attachment_is_keepable(filename: str, mime: str) -> bool:
    """Decide whether to download an attachment for analysis.

    Keep: text/*, image/*, application/json|xml|yaml, and files whose
    extension matches a known textual format (covers Jira's habit of
    serving crash logs as application/octet-stream).
    Skip: video/*, audio/* — too large and not analyzable by the LLM.
    """
    mime = (mime or "").lower()
    if mime.startswith(("video/", "audio/")):
        return False
    if mime.startswith(("text/", "image/")):
        return True
    if mime in {
        "application/json", "application/xml",
        "application/yaml", "application/x-yaml",
        "application/javascript", "application/x-shellscript",
    }:
        return True
    ext = ""
    dot = filename.rfind(".")
    if dot >= 0:
        ext = filename[dot:].lower()
    return ext in _TEXT_ATTACHMENT_EXTS


def ticket_to_markdown(ticket: TicketData) -> str:
    """Convert TicketData to a markdown document."""
    lines = [
        f"# {ticket.id}: {ticket.summary}",
        "",
        f"**URL:** {ticket.url}",
        f"**Priority:** {ticket.priority}",
        f"**Reporter:** {ticket.reporter}",
    ]
    if ticket.assignee:
        lines.append(f"**Assignee:** {ticket.assignee}")
    if ticket.sprint:
        lines.append(f"**Sprint:** {ticket.sprint}")
    if ticket.labels:
        lines.append(f"**Labels:** {', '.join(ticket.labels)}")

    lines.extend(["", "## Description", "", ticket.description])

    if ticket.acceptance_criteria:
        lines.extend(["", "## Acceptance Criteria", "", ticket.acceptance_criteria])

    if ticket.linked_issues:
        lines.extend(["", "## Linked Issues", ""])
        for link in ticket.linked_issues:
            lines.append(f"- {link.get('type', 'related')}: {link.get('key', '')}")

    if ticket.attachments:
        lines.extend(["", "## Attachments", ""])
        for att in ticket.attachments:
            fname = att.get("filename", "?")
            mime = att.get("mime_type", "") or "?"
            note = "" if attachment_is_keepable(fname, mime) else " — skipped (binary/media)"
            lines.append(f"- `{fname}` ({mime}){note}")

    return "\n".join(lines)


async def refetch_ticket_data(
    workspace: Workspace, tracker: TrackerInterface | None,
) -> None:
    """Write or append ticket meta files (ticket.md, comments.md, history.md).

    First run (file absent): writes fresh content.
    Rerun (file exists): appends a timestamped refresh block so agents can
    see what changed between runs.
    """
    ticket_id = workspace.state.ticket_id

    ticket_obj = None  # will be set if tracker fetches successfully
    if tracker:
        try:
            ticket_obj = await tracker.get_ticket(ticket_id)
            ticket_md = ticket_to_markdown(ticket_obj)
            ticket_file = workspace.meta_dir / "ticket.md"
            if ticket_file.exists():
                refresh_date = date.today().isoformat()
                ticket_file.write_text(
                    ticket_file.read_text(encoding="utf-8")
                    + f"\n\n## Refresh {refresh_date}\n\n{ticket_md}",
                    encoding="utf-8",
                )
            else:
                ticket_file.write_text(ticket_md, encoding="utf-8")
        except Exception as e:
            logger.warning(
                "Failed to refetch ticket description for %s: %s", ticket_id, e
            )

    if tracker:
        try:
            comments = await tracker.get_comments(ticket_id)
            if comments:
                comments_file = workspace.meta_dir / "comments.md"
                existing_ids: set[str] = set()
                if comments_file.exists():
                    existing_ids = set(
                        re.findall(
                            r"<!-- comment:(\S+) -->",
                            comments_file.read_text(encoding="utf-8"),
                        )
                    )
                new_lines: list[str] = []
                for c in comments:
                    if c.id and c.id in existing_ids:
                        continue
                    marker = f"<!-- comment:{c.id} -->\n" if c.id else ""
                    new_lines.append(
                        f"{marker}## {c.author} ({c.created})\n\n{c.body}\n"
                    )
                if new_lines:
                    block = "\n".join(new_lines)
                    if comments_file.exists():
                        comments_file.write_text(
                            comments_file.read_text(encoding="utf-8")
                            + "\n" + block,
                            encoding="utf-8",
                        )
                    else:
                        comments_file.write_text(
                            "# Ticket Comments\n\n" + block, encoding="utf-8",
                        )

            history = await tracker.get_status_history(ticket_id)
            history_file = workspace.meta_dir / "history.md"
            existing_history = (
                history_file.read_text(encoding="utf-8")
                if history_file.exists() else ""
            )
            new_changes: list[str] = []
            for h in history:
                line = (
                    f"- {h.created}: {h.from_status} → "
                    f"{h.to_status} by {h.author}"
                )
                if line not in existing_history:
                    new_changes.append(line)
            if new_changes:
                new_block = "\n".join(new_changes) + "\n"
                if history_file.exists():
                    history_file.write_text(
                        existing_history.rstrip() + "\n" + new_block,
                        encoding="utf-8",
                    )
                else:
                    history_file.write_text(
                        "# Status History\n\n" + new_block, encoding="utf-8",
                    )

        except Exception as e:
            logger.warning(
                "Failed to refetch comments/history for %s: %s", ticket_id, e,
            )

    # Download attachments — skip files already present.
    # Keep text-like content (crash logs, JSON, source) and images.
    # Skip video/audio (too large + AI can't analyze) and oversize binaries.
    if tracker and ticket_obj is not None and ticket_obj.attachments:
        attachments_dir = workspace.meta_dir / "attachments"
        attachments_dir.mkdir(exist_ok=True)
        for att in ticket_obj.attachments:
            filename = att.get("filename", "attachment")
            mime = att.get("mime_type", "")
            if not attachment_is_keepable(filename, mime):
                continue
            if (attachments_dir / filename).exists():
                continue  # already downloaded, skip
            try:
                content = await tracker.download_attachment(att["url"])
                if len(content) > MAX_ATTACHMENT_BYTES:
                    logger.info(
                        "Skipping oversized attachment %s (%d bytes > %d)",
                        filename, len(content), MAX_ATTACHMENT_BYTES,
                    )
                    continue
                (attachments_dir / filename).write_bytes(content)
                logger.info("Downloaded attachment %s for %s", filename, ticket_id)
            except Exception as e:
                logger.warning(
                    "Failed to download attachment %s: %s", filename, e,
                )

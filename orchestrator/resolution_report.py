"""Resolution report — single source of truth for PR comment decisions.

Each PR comment gets one permanent entry. Decisions persist across review
cycles. The PR review action reads this before classifying to skip
already-decided comments.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def read_entries(report_path: Path) -> dict[int, dict[str, str]]:
    """Parse the resolution report. Returns {comment_id: {field: value}}."""
    if not report_path.exists():
        return {}

    content = report_path.read_text(encoding="utf-8")
    entries: dict[int, dict[str, str]] = {}
    current_id: int | None = None
    current_fields: dict[str, str] = {}

    for line in content.splitlines():
        m = re.match(r"^## Comment #(\d+)", line)
        if m:
            if current_id is not None:
                entries[current_id] = current_fields
            current_id = int(m.group(1))
            current_fields = {}
            continue

        m = re.match(r"^- (\w[\w\s]*?):\s*(.+)$", line)
        if m and current_id is not None:
            key = m.group(1).strip().lower().replace(" ", "_")
            current_fields[key] = m.group(2).strip()

    if current_id is not None:
        entries[current_id] = current_fields

    return entries


def add_entry(
    report_path: Path,
    ticket_id: str,
    pr_number: int,
    comment_id: int,
    fields: dict[str, str],
) -> None:
    """Add a new comment entry to the resolution report."""
    if not report_path.exists():
        report_path.write_text(
            f"# PR Review Resolution — {ticket_id}\nPR: #{pr_number}\n\n",
            encoding="utf-8",
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## Comment #{comment_id}\n"]
    if "decided_at" not in fields:
        fields["decided_at"] = timestamp
    for key, value in fields.items():
        display_key = key.replace("_", " ").title()
        lines.append(f"- {display_key}: {value}\n")

    with open(report_path, "a", encoding="utf-8") as f:
        f.writelines(lines)


def update_entry(
    report_path: Path,
    comment_id: int,
    updates: dict[str, str],
) -> None:
    """Update fields on an existing comment entry in place."""
    if not report_path.exists():
        return

    content = report_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_target = False
    updated_keys: set[str] = set()

    for line in lines:
        m = re.match(r"^## Comment #(\d+)", line)
        if m:
            if in_target:
                for key, value in updates.items():
                    if key not in updated_keys:
                        display_key = key.replace("_", " ").title()
                        result.append(f"- {display_key}: {value}\n")
            in_target = int(m.group(1)) == comment_id
            updated_keys = set()
            result.append(line)
            continue

        if in_target:
            fm = re.match(r"^- (\w[\w\s]*?):\s*(.+)$", line)
            if fm:
                key = fm.group(1).strip().lower().replace(" ", "_")
                if key in updates:
                    display_key = key.replace("_", " ").title()
                    result.append(f"- {display_key}: {updates[key]}\n")
                    updated_keys.add(key)
                    continue
        result.append(line)

    if in_target:
        for key, value in updates.items():
            if key not in updated_keys:
                display_key = key.replace("_", " ").title()
                result.append(f"- {display_key}: {value}\n")

    report_path.write_text("".join(result), encoding="utf-8")

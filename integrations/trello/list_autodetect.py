"""Fuzzy match Trello list names to Cleave status keys.

Pure function used by the wizard (mirrored client-side in JS at
dashboard/static/js/trello-autodetect.js) and by Atlas as a fallback when
the config supplies an empty mapping.
"""

from __future__ import annotations

import re

_PATTERNS: dict[str, tuple[str, ...]] = {
    "todo":        ("to do", "todo", "backlog", "ready", "inbox", "queue"),
    "in_progress": ("in progress", "doing", "wip", "dev", "development"),
    "in_review":   ("in review", "review", "code review", "pr", "qa"),
    "done":        ("done", "shipped", "complete", "completed", "closed", "merged", "released"),
}


def _normalize(name: str) -> str:
    """Lowercase, replace separators with spaces, collapse whitespace."""
    n = name.lower().strip()
    n = re.sub(r"[-_/]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    return n


def _word_in(pattern: str, text: str) -> bool:
    """True if pattern appears as whole words in text."""
    return bool(re.search(r"(?<!\w)" + re.escape(pattern) + r"(?!\w)", text))


def autodetect_status_mapping(lists: list[dict]) -> dict[str, str]:
    """Return {status_key: list_id} for any of the four Cleave statuses that match.

    Match priority: exact pattern match wins. Substring (pattern in name) only if
    no exact match was found for that status. On tie within a tier, the leftmost
    list (lowest `pos`) wins. Statuses with no match are omitted.
    """
    candidates: dict[str, list[tuple[int, float, str]]] = {k: [] for k in _PATTERNS}
    for lst in lists:
        name = lst.get("name")
        if not name:
            continue
        list_id = lst.get("id", "")
        pos = lst.get("pos", 0)
        norm = _normalize(name)
        for status_key, patterns in _PATTERNS.items():
            best_tier = None
            for pat in patterns:
                if norm == pat:
                    best_tier = 0
                    break
                if _word_in(pat, norm) and best_tier is None:
                    best_tier = 1
            if best_tier is not None:
                candidates[status_key].append((best_tier, pos, list_id))

    result: dict[str, str] = {}
    for status_key, hits in candidates.items():
        if not hits:
            continue
        hits.sort()
        result[status_key] = hits[0][2]
    return result

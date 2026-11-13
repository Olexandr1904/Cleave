"""The reviewer's Telegram lines, in English."""
from __future__ import annotations

from orchestrator.reviewer.board import TurnChange
from orchestrator.reviewer.models import BoardRow


def turn_line(scope: str, row: BoardRow, change: TurnChange) -> str:
    if change.action == "answer_threads":
        return (
            f"[{scope}] #{row.number}: replies in {len(change.threads)} thread(s)"
            f" — handle them locally · {row.url}"
        )
    return f"[{scope}] #{row.number}: your turn — {change.action} · {row.url}"


def large_line(scope: str, row: BoardRow) -> str:
    return (
        f"[{scope}] #{row.number} is large ({row.changed_files} files)"
        f" — no automatic review · {row.url}"
    )


def readiness_failed_line(scope: str, reason: str, retry_minutes: int) -> str:
    return (
        f"[{scope}] readiness command failed: {reason}. Keeping the last board;"
        f" next try in {retry_minutes} min."
    )


def poll_failed_line(scope: str, reason: str, minutes: int) -> str:
    return (
        f"[{scope}] poll failed: {reason}. Keeping the last board;"
        f" next try in {minutes} min."
    )


def doc_missing_line(scope: str, doc: str, base: str) -> str:
    return f"[{scope}] reviews are off: {doc} is not on {base}."

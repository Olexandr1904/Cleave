"""The reviewer's Telegram lines, in English."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

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


def job_failed_line(scope: str, number: int, kind: str, reason: str) -> str:
    return f"[{scope}] #{number}: {kind} failed — {reason}"


def job_deferred_line(scope: str, number: int, kind: str, retry_at: datetime) -> str:
    return (
        f"[{scope}] #{number}: {kind} deferred — usage limit reached;"
        f" next try at {retry_at:%H:%M} UTC"
    )


def login_line() -> str:
    return (
        "[reviewer] claude is not logged in on the host. Log in from a GUI Terminal"
        " (run claude, then /login); reviews wait until then."
    )


def drift_line(scope: str, doc: str, section: str) -> str:
    return (
        f'[{scope}] The "{section}" section of {doc} changed on the base branch:'
        " check that Cleave's Prepare still matches it."
    )


def worktree_left_line(scope: str, number: int, path: Path, reason: str) -> str:
    return f"[{scope}] #{number}: worktree {path} was not removed — {reason}"

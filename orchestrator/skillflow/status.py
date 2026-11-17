"""The skill's status line, and what the end of a session turn is: a result, a stop or a wait."""
from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"`{3,}[^`\s]*")
# The model may wrap the line, or a field of it, in markdown: inline code, bold, italics, strikethrough.
_WRAPPING = re.compile(r"^[\s`*_~]+|[\s`*_~]+$")
_NEEDS_USER = re.compile(r"needs\s+user\s*:[\s`*_~]*(yes|no)", re.IGNORECASE)
# Any markup left around the word (`**stopped:** x` keeps its inner `**`, `_stopped_: x` its closing `_`) still
# makes it a stop. `_` is a word character, so it is named as markup on both sides: `stoppedX` is no stop.
_STOP = re.compile(r"[\W_]*stopped(?![^\W_])", re.IGNORECASE)


@dataclass(frozen=True)
class StatusLine:
    key: str
    outcome: str        # the second field, stripped: "fixed", "done", "no app fix", "stopped: low-confidence"
    needs_user: bool
    line: str           # the line as cleaned


@dataclass(frozen=True)
class TurnEnd:
    kind: str           # "waiting" | "stop" | "result"
    status: StatusLine | None = None


def parse_status_line(text: str, key: str) -> StatusLine | None:
    """The status line when it is the first line of `text`; None for anything else."""
    fields = [_WRAPPING.sub("", f) for f in _first_line(text).split("|")]
    # A result with no outcome would take the run to its commit with nothing to show.
    if len(fields) < 3 or fields[0].casefold() != key.casefold() or not fields[1]:
        return None
    needs_user = _NEEDS_USER.fullmatch(fields[-1])
    if needs_user is None:
        return None
    # Rebuilt from the clean fields: the operator reads it, and markup inside a field would show.
    return StatusLine(key=key, outcome=fields[1], needs_user=needs_user[1].casefold() == "yes",
                      line=" | ".join(fields))


def read_turn_end(text: str, key: str) -> TurnEnd:
    status = parse_status_line(text, key)
    if status is None:
        return TurnEnd("waiting")
    kind = "stop" if _STOP.match(status.outcome) else "result"
    return TurnEnd(kind, status)


def _first_line(text: str) -> str:
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not _FENCE.fullmatch(stripped):
            return stripped
    return ""

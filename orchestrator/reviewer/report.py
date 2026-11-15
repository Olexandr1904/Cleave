"""Parsers for what the review model writes: the report, the re-check replies plan and tg_lines.json.

The text is untrusted model output: it is scanned line by line and never evaluated.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass

from orchestrator.reviewer.markdown import fenced, split_lines

KIND_EMOJI = {"red": "🔴", "orange": "🟠", "yellow": "🟡", "question": "❓", "preexisting": "🟣"}
CONFIRMED = ("red", "orange", "yellow")

_MAX_REPORT = 1_000_000  # characters
_MAX_DIGITS = 9  # of an item number, a line, a dropped number or the candidates total
_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
_KIND_BY_EMOJI = {emoji: kind for kind, emoji in KIND_EMOJI.items()}
_DIGITS = re.compile(r"[0-9]+")
_ITEM = re.compile(r"^### \[([ xX])\] (\d+)\. (.+)$", re.ASCII)
# Linear: the trailing " (<lens>)" is cut off by _strip_lens, not by backtracking.
_DROPPED = re.compile(r"^- ([0-9]+)\. (.+?) [-—] (.+)$")
_DROPPED_START = re.compile(r"- [0-9]+\.")
_EVENT = re.compile(r"^(\S+) [-—] (.+)$")
_BACKTICKED = re.compile(r"`([^`]*)`")
_PATH_LINE = re.compile(r"^(.+):(\d+)$", re.ASCII)
_MOVES = re.compile(r"\(anchor moves to line (\d+)\)", re.ASCII)


class ReportError(Exception):
    pass


@dataclass(frozen=True)
class ReportItem:
    number: int
    kind: str
    title: str
    path: str
    line: int | None
    note: str  # "moves to <L>", "outside the diff" or ""
    body: str


@dataclass(frozen=True)
class DroppedItem:
    number: int
    text: str
    why: str


@dataclass(frozen=True)
class Report:
    title: str
    head_sha: str
    candidates: str
    candidates_total: int | None
    event: str
    event_reason: str
    items: tuple[ReportItem, ...]
    dropped: tuple[DroppedItem, ...]
    looks_good: str
    summary: str

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return {**d, "items": list(d["items"]), "dropped": list(d["dropped"])}

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        return cls(**{
            **d,
            "items": tuple(ReportItem(**i) for i in d["items"]),
            "dropped": tuple(DroppedItem(**x) for x in d["dropped"]),
        })


def parse_report(text: str, head_sha: str, drafted: frozenset[int] = frozenset()) -> Report:
    """A number in `drafted` is a dropped one the operator had drafted: it may appear once as a ❓ item and once
    as a dropped bullet. Every other repeat of a number raises."""
    if len(text) > _MAX_REPORT:
        raise ReportError("report too large")
    sections = _sections(split_lines(text))
    header = sections[0][1]
    title = next((line[2:].strip() for line in header if line.startswith("# ")), "")
    sha = _field(header, "**Head SHA:**").replace("`", "").strip()
    if len(sha) < 7 or not head_sha.startswith(sha):
        raise ReportError(f"report is for head {sha[:40]}, not {head_sha[:9]}")
    candidates = _field(header, "**Candidates:**")
    total = _DIGITS.match(candidates)
    event = _EVENT.match(_field(header, "**Suggested event:**"))
    if not event or event[1] not in _EVENTS:
        raise ReportError("no suggested event")

    claimed: dict[int, str] = {}  # each number's first claim: its item's kind or "dropped"; "both" after a draft's

    def claim(digits: str, by: str) -> int:
        n = _number(digits)
        if n in claimed:
            if n not in drafted or {claimed[n], by} != {"question", "dropped"}:
                raise ReportError(f"number {n} is used twice")
            by = "both"
        claimed[n] = by
        return n

    items: list[ReportItem] = []
    dropped: list[DroppedItem] = []
    looks_good = ""
    summary = None
    for name, lines in sections:
        kind = _KIND_BY_EMOJI.get(name[:1])
        if kind:
            items.extend(_items(kind, lines, claim))
            continue
        outside_fences = [line for line, in_fence in zip(lines, fenced(lines)) if not in_fence]
        for line in outside_fences:
            if m := _ITEM.match(line):
                raise ReportError(f"item {_number(m[2])} is outside a findings section")
        if name.startswith("Dropped"):
            for line in outside_fences:
                if m := _DROPPED.match(line):
                    dropped.append(DroppedItem(claim(m[1], "dropped"), m[2], _strip_lens(m[3])))
                elif _DROPPED_START.match(line):
                    raise ReportError(f"dropped line not understood: {line[:60]}")
        elif name.startswith("✅"):
            looks_good = "\n".join(lines).strip()
        elif name.startswith("Summary"):
            summary = "\n".join(lines).strip()
    if summary is None:
        raise ReportError("no ## Summary")
    return Report(
        title=title,
        head_sha=sha,
        candidates=candidates,
        candidates_total=_number(total[0]) if total else None,
        event=event[1],
        event_reason=event[2].strip(),
        items=tuple(items),
        dropped=tuple(dropped),
        looks_good=looks_good,
        summary=summary,
    )


def parse_replies_plan(text: str) -> list[dict]:
    try:
        plan = json.loads(text)
    except (ValueError, RecursionError):
        plan = None
    if not isinstance(plan, list) or not all(_is_reply(entry) for entry in plan):
        raise ReportError("replies plan unreadable")
    if len({entry["n"] for entry in plan}) != len(plan):  # replies are named by n: each names one
        raise ReportError("replies plan unreadable")
    return plan


def parse_tg_lines(text: str) -> dict[int, str]:
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(raw, dict):
        return {}
    lines: dict[int, str] = {}
    for key, value in raw.items():
        if not (isinstance(value, str) and _DIGITS.fullmatch(key)):
            continue
        try:
            n = int(key)
        except ValueError:  # more digits than int() converts
            continue
        lines[n] = " ".join(value.split())[:200]
    return lines


def _sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """The header as ("", lines), then one (heading text after "## ", lines) per section. A line in a fenced
    code block never starts a section or an item."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line, in_fence in zip(lines, fenced(lines)):
        if line.startswith("## ") and not in_fence:
            sections.append((line[3:], []))
        else:
            sections[-1][1].append(line)
    return sections


def _field(header: list[str], label: str) -> str:
    return next((line[len(label):].strip() for line in header if line.startswith(label)), "")


def _items(kind: str, lines: list[str], claim) -> list[ReportItem]:
    items: list[ReportItem] = []
    current: tuple[int, str, list[str]] | None = None
    # A section starts outside any fence, so its own lines tell which of them are fenced.
    for line, in_fence in zip(lines, fenced(lines)):
        if in_fence:
            if current:
                current[2].append(line)
            continue
        m = _ITEM.match(line)
        if not m and line.startswith("### ["):
            raise ReportError(f"item heading not understood: {line[:60]}")
        if line.startswith("### ") and line[4:5] in _KIND_BY_EMOJI:
            raise ReportError(f"kind heading at level 3: {line[:60]}")
        if m or line.startswith("### ") or line.strip() == "---":
            if current:
                items.append(_item(kind, *current))
            current = (claim(m[2], kind), m[3].strip(), []) if m else None
        elif current:
            current[2].append(line)
    if current:
        items.append(_item(kind, *current))
    return items


def _item(kind: str, number: int, title: str, lines: list[str]) -> ReportItem:
    file_line = next((line for line in lines if line.startswith("**File:**")), "")
    ref = m[1] if (m := _BACKTICKED.search(file_line)) else ""
    if m := _PATH_LINE.match(ref):
        path, line = m[1], _number(m[2])
    else:
        path, line = ref, None
    if m := _MOVES.search(file_line):
        note = f"moves to {m[1]}"
    elif "(outside the diff" in file_line:
        note = "outside the diff"
    else:
        note = ""
    return ReportItem(number, kind, title, path, line, note, "\n".join(lines).strip())


def _strip_lens(rest: str) -> str:
    """`rest` without one trailing " (<lens>)": what `(.+?)(?: \\([^)]*\\))?$` leaves, in linear time."""
    if rest.endswith(")"):
        # The lens holds no ")" of its own, so it opens after the last inner one; the earliest
        # " (" there wins (shortest reason), and the reason keeps at least one character.
        after_inner = rest.rfind(")", 0, len(rest) - 1) + 1
        start = rest.find(" (", max(after_inner, 1), len(rest) - 1)
        if start != -1:
            return rest[:start]
    return rest


def _number(digits: str) -> int:
    if len(digits) > _MAX_DIGITS:
        raise ReportError("number too long")
    return int(digits)


def _is_reply(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and type(entry.get("n")) is int  # not a bool
        and isinstance(entry.get("body"), str)
    )

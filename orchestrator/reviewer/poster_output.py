"""Parsers for the poster tool's output: the review dry run and confirm run, the replies steps and results.

Posting trusts these to say exactly what the tool is about to post, or did post. Every line, every JSON key
and the order of both must be what the parser knows: anything else raises PosterOutputError, which stops the
post (operator, 2026-11-14).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_MAX_DRY_RUN = 2_000_000  # characters
_MAX_DIGITS = 9  # of a number read from a line
_LABEL = re.compile(r"^(Report|Repo|PR|Event|Summary|Inline|HEAD|Anchors)\s*:\s*(.*)$")
_VALUES = {
    "Report": re.compile(r".+"),
    "Repo": re.compile(r"[^/\s]+/[^/\s]+"),
    "PR": re.compile(r"#[0-9]+"),
    "Event": re.compile(r"APPROVE|REQUEST_CHANGES|COMMENT"),
    "Summary": re.compile(r"yes|no"),
    "Inline": re.compile(r"[0-9]+ comment\(s\)"),
    "HEAD": re.compile(r"[0-9a-fA-F]{7,40}"),
    "Anchors": re.compile(r"[0-9]+ inline, [0-9]+ degraded to body"),
}
# `^\s+# ?0*([0-9]+)\s+(\S+)\s+-\s(.+)$` with the leading zeros dropped in code: `0*` and `[0-9]+` overlap,
# and backtracking between them is quadratic on a long run of zeros. The tool pads a number under 10 with a space.
_ITEM = re.compile(r"^\s+# ?([0-9]+)\s+(\S+)\s+-\s(.+)$")
_SKIPPING = re.compile(r"^Skipping ([0-9]+) finding\(s\) already posted by an earlier run$")
_POSTING = re.compile(r"^Posting ([0-9]+) comment\(s\) in ([0-9]+) review\(s\)$")
_NOTHING = re.compile(r"^Nothing left to post on this head\.$")
# The header's lines in the spec's order; only items repeat. Report, Repo, PR, Anchors, Skipping and Nothing may be
# absent.
_ORDER = ("Report", "Repo", "PR", "Event", "Summary", "Inline", "item", "HEAD", "Anchors", "Skipping", "Posting",
          "Nothing")
_REQUIRED = ("Event", "Summary", "Inline", "HEAD", "Posting")
_BATCH = re.compile(r"^--- DRY RUN batch ([0-9]+)/([0-9]+) ---$")
_POSTED = re.compile(r"^Posted batch ([0-9]+)/([0-9]+): (\S+)$")
_LOCATION = re.compile(r"^\s+\S+:[0-9]+$")
_DIGITS = re.compile(r"[0-9]+")
_PAYLOAD_KEYS = frozenset({"commit_id", "event", "comments", "body"})
_COMMENT_KEYS = frozenset({"path", "line", "side", "body"})
_STEP_KEYS = frozenset({"step", "n", "target", "body"})
_STEPS = ("reply", "comment", "resolve")


class PosterOutputError(Exception):
    pass


@dataclass(frozen=True)
class DryItem:
    number: int
    path: str
    line: int | None
    title: str


@dataclass(frozen=True)
class ReviewDryRun:
    event: str
    summary: bool
    items: tuple[DryItem, ...]
    head: str
    skipped: int
    comments: int
    reviews: int
    payloads: tuple[dict, ...]


def parse_review_dry_run(text: str) -> ReviewDryRun:
    if len(text) > _MAX_DRY_RUN:
        raise PosterOutputError("dry run output too large")
    lines = text.split("\n")
    first_batch = next((k for k, line in enumerate(lines) if _BATCH.match(line)), len(lines))
    labels: dict[str, str] = {}
    items: list[DryItem] = []
    skipped = 0
    posting = (0, 0)
    seen: set[str] = set()
    last = 0  # the rank in _ORDER of the line before
    for line in lines[:first_batch]:
        if not line.strip():
            continue
        kind, m = _header_line(line)
        if kind != "item" and kind in seen:
            raise PosterOutputError(f"{kind} line twice")
        rank = _ORDER.index(kind)
        if rank < last:
            raise PosterOutputError(f"line out of order: {line[:60]}")
        seen.add(kind)
        last = rank
        if kind == "item":
            items.append(DryItem(_number(m[1].lstrip("0") or "0"), *_location(m[2]), m[3]))
        elif kind == "Skipping":
            skipped = _number(m[1])
        elif kind == "Posting":
            posting = (_number(m[1]), _number(m[2]))
        elif kind != "Nothing":
            value = m[2].strip()
            if not _VALUES[kind].fullmatch(value):
                raise PosterOutputError(f"{kind} is not understood: {value[:60]}")
            labels[kind] = value
    for kind in _REQUIRED:
        if kind not in seen:
            raise PosterOutputError(f"no {kind} line")
    return ReviewDryRun(
        event=labels["Event"],
        summary=labels["Summary"] == "yes",
        items=tuple(items),
        head=labels["HEAD"],
        skipped=skipped,
        comments=posting[0],
        reviews=posting[1],
        payloads=_batches(lines[first_batch:]),
    )


def parse_review_confirm(text: str) -> list[tuple[int, int, str]]:
    """`(i, M, url)` per posted batch: 1..k in order with one total M, k <= M (a failed batch stops early). Before
    the first batch, the tool prints the dry run's header lines."""
    batches: list[tuple[int, int, str]] = []
    for line in text.split("\n"):
        if m := _POSTED.match(line):
            i, of = _number(m[1]), _number(m[2])
            if i != len(batches) + 1 or i > of or (batches and of != batches[0][1]):
                raise PosterOutputError(f"Posted batch {i}/{of} out of order")
            batches.append((i, of, m[3]))
        elif _LOCATION.match(line):
            if not batches:
                raise PosterOutputError(f"a comment's location before any batch: {line[:60]}")
        elif not line.strip():
            continue
        elif batches:
            raise PosterOutputError(f"unknown line: {line[:60]}")
        else:
            _header_line(line)
    return batches


def parse_replies_steps(text: str) -> list[dict]:
    steps = _json_list(text, "the replies dry run")
    for k, step in enumerate(steps, 1):
        if not _is_step(step):
            raise PosterOutputError(f"step {k} of the replies dry run is not understood")
    return steps


def parse_replies_results(text: str) -> list[dict]:
    results = _json_list(text, "the replies result")
    for k, result in enumerate(results, 1):
        if not _is_result(result):
            raise PosterOutputError(f"result {k} of the replies run is not understood")
    return results


def _header_line(line: str) -> tuple[str, re.Match]:
    if m := _LABEL.match(line):
        return m[1], m
    for kind, pattern in (("item", _ITEM), ("Skipping", _SKIPPING), ("Posting", _POSTING), ("Nothing", _NOTHING)):
        if m := pattern.match(line):
            return kind, m
    raise PosterOutputError(f"unknown line: {line[:60]}")


def _location(location: str) -> tuple[str, int | None]:
    """`path:line` split at its last ":" when digits follow; otherwise the whole of it is the path."""
    path, colon, tail = location.rpartition(":")
    if colon and _DIGITS.fullmatch(tail):
        return path, _number(tail)
    return location, None


def _batches(lines: list[str]) -> tuple[dict, ...]:
    """Each `--- DRY RUN batch i/M ---` line and the one JSON object after it, up to the next marker or the
    end; the batches are numbered 1..M with total M."""
    marks = [k for k, line in enumerate(lines) if _BATCH.match(line)]
    payloads: list[dict] = []
    total = None
    for index, start in enumerate(marks):
        m = _BATCH.match(lines[start])
        i, of = _number(m[1]), _number(m[2])
        total = of if total is None else total
        where = f"batch {i}/{of}"
        if i != index + 1 or of != total or i > of:
            raise PosterOutputError(f"{where} out of order")
        end = marks[index + 1] if index + 1 < len(marks) else len(lines)
        payloads.append(_payload(_loads("\n".join(lines[start + 1:end]), where), where))
    if total is not None and len(marks) != total:
        raise PosterOutputError(f"batch {len(marks) + 1}/{total} is missing")
    return tuple(payloads)


def _payload(payload: object, where: str) -> dict:
    if not isinstance(payload, dict):
        raise PosterOutputError(f"{where} is not one JSON object")
    if unknown := sorted(payload.keys() - _PAYLOAD_KEYS):
        raise PosterOutputError(f"{where} has the unknown key {unknown[0][:60]}")
    for key in ("commit_id", "event"):
        if not isinstance(payload.get(key), str):
            raise PosterOutputError(f"{where} has no str {key}")
    comments = payload.get("comments")
    if not isinstance(comments, list):
        raise PosterOutputError(f"{where} has no comments list")
    if not all(_is_comment(comment) for comment in comments):
        raise PosterOutputError(
            f"{where}: a comment needs exactly a str path, a line of 1 or more, side RIGHT and a str body"
        )
    if not isinstance(payload.get("body", ""), str):
        raise PosterOutputError(f"{where} has a body that is not a str")
    return payload


def _is_comment(comment: object) -> bool:
    return (
        isinstance(comment, dict)
        and comment.keys() == _COMMENT_KEYS
        and isinstance(comment["path"], str)
        and type(comment["line"]) is int  # not a bool
        and comment["line"] >= 1
        and comment["side"] == "RIGHT"
        and isinstance(comment["body"], str)
    )


def _json_list(text: str, what: str) -> list:
    """The whole text, stripped, as one JSON list: anything before or after it is not understood."""
    value = _loads(text.strip(), what)
    if not isinstance(value, list):
        raise PosterOutputError(f"{what} is not one JSON list")
    return value


class StrictJSONError(ValueError):
    """Raised by strict_loads' hooks; json.loads passes it through unchanged."""


def strict_loads(text: str) -> object:
    """`text` as one JSON value, for output that is not trusted: a key given twice (json.loads would keep the last
    one, not what was meant), NaN or Infinity raises StrictJSONError; anything else that is not JSON raises
    ValueError (or RecursionError)."""
    return json.loads(text, object_pairs_hook=_no_key_twice, parse_constant=_no_constant)


def _loads(text: str, what: str) -> object:
    """`text` as one JSON value, or None when it is not JSON. A key twice, NaN or Infinity is refused."""
    try:
        return strict_loads(text)
    except StrictJSONError as e:
        raise PosterOutputError(f"{what} has {e}") from None
    except (ValueError, RecursionError):
        return None


def _no_key_twice(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJSONError(f"the key {key[:60]} twice")
        obj[key] = value
    return obj


def _no_constant(name: str) -> object:
    raise StrictJSONError(name)


def _is_step(step: object) -> bool:
    if not (
        isinstance(step, dict)
        and step.keys() <= _STEP_KEYS
        and step.get("step") in _STEPS
        and type(step.get("n")) is int  # not a bool
        and isinstance(step.get("target"), str)
    ):
        return False
    if step["step"] == "resolve":
        return isinstance(step.get("body", ""), str)
    return isinstance(step.get("body"), str)


def _is_result(result: object) -> bool:
    """`{"n": int}` and exactly one of `replied` (str), `commented` (str) or `resolved` (true)."""
    if not (isinstance(result, dict) and type(result.get("n")) is int):  # not a bool
        return False
    rest = result.keys() - {"n"}
    if len(rest) != 1:
        return False
    (key,) = rest
    if key == "resolved":
        return result[key] is True
    return key in ("replied", "commented") and isinstance(result[key], str)


def _number(digits: str) -> int:
    if len(digits) > _MAX_DIGITS:
        raise PosterOutputError("number too long")
    return int(digits)

"""The reviewer's Telegram messages, in English: the review message and the re-check message."""
from __future__ import annotations

import posixpath

from orchestrator.reviewer.report import CONFIRMED, KIND_EMOJI, Report, ReportItem

_REVIEW_HINT = (
    'Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"'
)
# Characters of any one text from the report, the replies plan, tg_lines or the PR that a line prints:
# a hostile report could otherwise turn one line into many Telegram parts.
_MAX_TEXT = 200


def review_message(
    scope: str,
    number: int,
    title: str,
    ticket_key: str,
    url: str,
    files: int,
    report: Report,
    tg_lines: dict[int, str],
    posted_at_head: int = 0,
    summary_posted: bool = False,
) -> str:
    kinds = [item.kind for item in report.items]
    total = report.candidates_total
    if total is None:
        total = len(report.items) + len(report.dropped)
    confirmed = sum(kind in CONFIRMED for kind in kinds)
    ticket = f" · {ticket_key}" if ticket_key else ""
    counts = (
        f"head {report.head_sha[:9]} · {_count(files, 'file')} · {_count(total, 'candidate')}"
        f" → {confirmed} confirmed · {_count(kinds.count('question'), 'question')}"
        f" · {kinds.count('preexisting')} pre-existing · {len(report.dropped)} dropped"
    )
    lines = [
        f'[{scope}] Review of PR #{number} "{_cut(title)}"{ticket}',
        counts,
        f"Suggested: {report.event} — {_cut(report.event_reason)}",
    ]
    if posted_at_head or summary_posted:
        summary = " and the Summary" if summary_posted else ""
        lines.append(f"Already posted at this head: {_count(posted_at_head, 'comment')}{summary}")
    lines.append("")
    lines += [_item_line(item, tg_lines) for item in report.items]
    if report.dropped:
        lines.append("Dropped: " + "; ".join(
            f"{d.number} — {_cut(tg_lines.get(d.number) or d.why)}" for d in report.dropped
        ))
    if not report.items and not report.dropped:
        lines.append("No findings.")
    lines += ["", _REVIEW_HINT, url]
    return "\n".join(lines)


def recheck_message(
    scope: str,
    number: int,
    title: str,
    url: str,
    head_sha: str,
    replies: list[dict],
    tg_lines: dict[int, str],
) -> str:
    k = len(replies)
    lines = [
        f'[{scope}] Re-check of PR #{number} "{_cut(title)}"',
        f"head {head_sha[:9]} · {k} drafted repl{'y' if k == 1 else 'ies'}",
        "",
    ]
    if replies:
        for entry in replies:
            gist = tg_lines.get(entry["n"]) or entry["body"].strip().partition("\n")[0]
            lines.append(f"{entry['n']}. {_cut(gist)}")
        numbers = " ".join(str(entry["n"]) for entry in replies)
        lines += ["", f'Reply to this message: "post replies {numbers}" · "text {replies[0]["n"]}"']
    else:
        lines += ["No replies drafted.", ""]
    lines.append(url)
    return "\n".join(lines)


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Parts of at most `limit` UTF-16 code units (what Telegram counts: an emoji outside the BMP is 2),
    split at line ends (the newline at a split is dropped); a line longer than `limit` is cut between
    code points, as late as fits. No part is empty."""
    parts: list[str] = []
    part: str | None = None
    size = 0
    for line in text.split("\n"):
        units = _units(line)
        if part is not None and size + 1 + units <= limit:
            part += "\n" + line
            size += 1 + units
            continue
        if part:
            parts.append(part)
        while units > limit:
            head = _fitting(line, limit)
            parts.append(line[:head])
            units -= _units(line[:head])
            line = line[head:]
        part, size = line, units
    if part:
        parts.append(part)
    return parts


# The limit of a part the reviewer sends: split_message counts UTF-16 code units, as Telegram does (its limit is
# 4096), and the rest is headroom.
MESSAGE_LIMIT = 3900


def message_parts(text: str, limit: int, max_parts: int = 10) -> list[str]:
    """split_message's parts, at most max_parts. When more would be needed, the last part ends with a line
    saying how many lines are left out: the text is cut at a line end, or between the pieces split_message
    cuts a line too long for one part into (that line then counts as left out). A hostile report would
    otherwise make hundreds of parts; the dashboard has it all."""
    room = max_parts * (limit + 1)  # no character past this many fits in max_parts parts
    if len(text) <= room and len(parts := split_message(text, limit)) <= max_parts:
        return parts
    total = text.count("\n") + 1
    pieces = [(i, piece) for i, line in enumerate(text[:room].split("\n"))
              for piece in split_message(line, limit) or [""]]

    def cut(n: int) -> list[str]:
        """The first n pieces, then the closing line."""
        left_out = total - pieces[n][0]
        closing = f"… {_count(left_out, 'more line')} — the full report is on the dashboard"
        return split_message("\n".join([*(piece for _, piece in pieces[:n]), closing]), limit)

    lo, hi = 0, len(pieces) - 1  # the most pieces that fit: cut(0) does, all of them never do
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(cut(mid)) <= max_parts:
            lo = mid
        else:
            hi = mid - 1
    return cut(lo)


def _units(text: str) -> int:
    # surrogatepass: a lone surrogate (JSON can carry one) counts 1 instead of raising.
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _fitting(line: str, limit: int) -> int:
    """How many code points from the start of `line` fit in `limit` UTF-16 units (at least one)."""
    units = 0
    for i, char in enumerate(line):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return max(i, 1)
    return len(line)


def _item_line(item: ReportItem, tg_lines: dict[int, str]) -> str:
    where = ""
    if item.path:
        line = "" if item.line is None else f":{item.line}"
        where = f"{_cut(posixpath.basename(item.path))}{line} — "
    return f"{KIND_EMOJI[item.kind]} {item.number}. {where}{_cut(tg_lines.get(item.number) or item.title)}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _cut(text: str) -> str:
    return text[:_MAX_TEXT]

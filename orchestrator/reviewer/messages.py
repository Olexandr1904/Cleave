"""The reviewer's Telegram messages, in the current Telegram language: the review message, the re-check message, the
address message and the findings message. What the review model wrote (a finding's title or Telegram line, the event's
reason, why a candidate was dropped, a drafted reply, a verdict and why) is marked for translation
(tg_i18n.model_text)."""
from __future__ import annotations

import posixpath

from integrations.base.notifier import Button
from orchestrator.reviewer.fix import fix_summary
from orchestrator.reviewer.report import CONFIRMED, KIND_EMOJI, Report, ReportItem
from orchestrator.tg_i18n import N_, count, model_text, tr

_REVIEW_HINT = N_(
    'Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"'
)
# A self-review of the operator's own PR with a confirmed finding, in a repo with address_doc: "fix" fixes them.
_FIXABLE_REVIEW_HINT = N_(
    'Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again" · "fix"'
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
    fixable: bool = False,
) -> str:
    """`fixable`: the run is a self-review of the operator's own PR in a repo with address_doc (JobRun.findings_fixable);
    with a confirmed finding its reply line offers "fix"."""
    kinds = [item.kind for item in report.items]
    total = report.candidates_total
    if total is None:
        total = len(report.items) + len(report.dropped)
    confirmed = sum(kind in CONFIRMED for kind in kinds)
    ticket = f" · {ticket_key}" if ticket_key else ""
    counts = tr(
        "head {head} · {files} · {candidates} → {confirmed} confirmed · {questions} · {preexisting} pre-existing"
        " · {dropped} dropped",
        head=report.head_sha[:9], files=count(files, "file"), candidates=count(total, "candidate"),
        confirmed=confirmed, questions=count(kinds.count("question"), "question"),
        preexisting=kinds.count("preexisting"), dropped=len(report.dropped),
    )
    lines = [
        tr('[{scope}] Review of PR #{number} "{title}"{ticket}', scope=scope, number=number, title=_cut(title),
           ticket=ticket),
        counts,
        tr("Suggested: {event} — {reason}", event=report.event, reason=model_text(_cut(report.event_reason))),
    ]
    if posted_at_head or summary_posted:
        comments = count(posted_at_head, "comment")
        if summary_posted:
            lines.append(tr("Already posted at this head: {comments} and the Summary", comments=comments))
        else:
            lines.append(tr("Already posted at this head: {comments}", comments=comments))
    lines.append("")
    lines += [_item_line(item, tg_lines) for item in report.items]
    if report.dropped:
        lines.append(tr("Dropped: {dropped}", dropped="; ".join(
            f"{d.number} — {model_text(_cut(tg_lines.get(d.number) or d.why))}" for d in report.dropped
        )))
    if not report.items and not report.dropped:
        lines.append(tr("No findings."))
    lines += ["", tr(_FIXABLE_REVIEW_HINT if fixable and confirmed else _REVIEW_HINT), url]
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
    lines = [
        tr('[{scope}] Re-check of PR #{number} "{title}"', scope=scope, number=number, title=_cut(title)),
        tr("head {head} · {replies}", head=head_sha[:9],
           replies=count(len(replies), "drafted reply", "drafted replies")),
        "",
    ]
    if replies:
        for entry in replies:
            gist = tg_lines.get(entry["n"]) or entry["body"].strip().partition("\n")[0]
            lines.append(f"{entry['n']}. {model_text(_cut(gist))}")
        numbers = " ".join(str(entry["n"]) for entry in replies)
        lines += ["", tr('Reply to this message: "post replies {numbers}" · "text {first}"', numbers=numbers,
                         first=replies[0]["n"])]
    else:
        lines += [tr("No replies drafted."), ""]
    lines.append(url)
    return "\n".join(lines)


def _change(fix: dict) -> str:
    """A fix job's patch in a few words: its size, or that the session changed no code."""
    if not fix:
        return tr("no code change")
    files, added, deleted = fix_summary(fix)
    return tr("fix: {files}, +{added} −{deleted} — not built or tested here", files=count(files, "file"),
              added=added, deleted=deleted)


def _protected_line(fix: dict) -> list[str]:
    if not fix.get("protected"):
        return []
    return [tr('Protected files in the fix ({paths}): Cleave pushes it only on "push anyway".',
               paths=_cut(", ".join(fix["protected"])))]


def _push_command(fix: dict) -> list[str]:
    """The reply that pushes a fix: "push anyway" for one with protected files; none without a fix."""
    if not fix:
        return []
    return ['"push anyway"'] if fix.get("protected") else ['"push"']


def fix_buttons(kind: str, job_id: str, replies: list[dict], fix: dict, *,
                pushed: bool = False, unposted_replies: bool = True) -> list[Button]:
    """The buttons of a job's message: on a threads (address) message, a findings message and a re-check message,
    only the actions that can run (labels in the current Telegram language). A fix with replies to post gets one
    push button, which posts them too: pushing and replying is one move ("push" typed still pushes alone). A fix
    with protected files gets "Push anyway…", which asks for a confirmation first. Once the fix is `pushed`, no Push
    button is offered; "Reply all" only while some reply is `unposted_replies`; Diff stays while there is a fix."""
    if kind not in ("address", "findings", "recheck"):
        return []
    buttons: list[Button] = []
    with_replies = kind != "findings" and bool(replies) and unposted_replies
    if fix and kind != "recheck":
        if pushed:
            if with_replies:
                buttons.append(Button(tr("Reply all"), f"rv_post:{job_id}"))
        elif fix.get("protected"):
            label = N_("Push anyway + reply all…") if with_replies else N_("Push anyway…")
            buttons.append(Button(tr(label), f"rv_pushx:{job_id}"))
        elif with_replies:
            buttons.append(Button(tr("Push + reply all"), f"rv_pushpost:{job_id}"))
        else:
            buttons.append(Button(tr("Push"), f"rv_push:{job_id}"))
        buttons.append(Button(tr("Diff"), f"rv_diff:{job_id}"))
    elif with_replies:
        buttons.append(Button(tr("Reply all"), f"rv_post:{job_id}"))
    return buttons


def address_message(
    scope: str,
    number: int,
    title: str,
    url: str,
    head_sha: str,
    replies: list[dict],
    tg_lines: dict[int, str],
    fix: dict,
) -> str:
    change = _change(fix)
    lines = [
        tr('[{scope}] Threads on your PR #{number} "{title}"', scope=scope, number=number, title=_cut(title)),
        tr("head {head} · {replies} · {change}", head=head_sha[:9],
           replies=count(len(replies), "drafted reply", "drafted replies"), change=change),
        *_protected_line(fix),
        "",
    ]
    commands = _push_command(fix)
    if replies:
        for entry in replies:
            gist = tg_lines.get(entry["n"]) or entry["body"].strip().partition("\n")[0]
            lines.append(f"{entry['n']}. {model_text(_cut(gist))}")
        numbers = " ".join(str(entry["n"]) for entry in replies)
        commands += [f'"post replies {numbers}"', f'"text {replies[0]["n"]}"']
    else:
        lines.append(tr("No replies drafted."))
    commands += ['"diff"'] if fix else []
    lines += ["", tr("Reply to this message: {commands}", commands=" · ".join([*commands, '"again: …"'])), url]
    return "\n".join(lines)


def findings_message(
    scope: str,
    number: int,
    title: str,
    url: str,
    head_sha: str,
    verdicts: list[dict],
    tg_lines: dict[int, str],
    fix: dict,
) -> str:
    """A findings job's message: a line per finding of the self-review, under its number there, with its verdict."""
    lines = [
        tr('[{scope}] Self-review findings on your PR #{number} "{title}"', scope=scope, number=number,
           title=_cut(title)),
        tr("head {head} · {findings} · {change}", head=head_sha[:9], findings=count(len(verdicts), "finding"),
           change=_change(fix)),
        *_protected_line(fix),
        "",
    ]
    for entry in verdicts:
        gist = tg_lines.get(entry["n"]) or f"{entry['verdict']} · {entry['why']}"
        lines.append(f"{entry['n']}. {model_text(_cut(gist))}")
    commands = [*_push_command(fix), *(['"diff"'] if fix else []), '"again: …"']
    lines += ["", tr("Reply to this message: {commands}", commands=" · ".join(commands)), url]
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
        closing = tr("… {lines} — the full report is on the dashboard", lines=count(left_out, "more line"))
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
    return f"{KIND_EMOJI[item.kind]} {item.number}. {where}{model_text(_cut(tg_lines.get(item.number) or item.title))}"


def _cut(text: str) -> str:
    """At most _MAX_TEXT characters, then "…" when the text was longer: a cut line never reads as whole."""
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"

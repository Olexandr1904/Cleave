"""The deterministic grammar of replies to reviewer messages, and the check of a command.

A reply is acted on at once only when the grammar knows every one of its tokens; anything else
parses to None and goes to the LLM fallback. Russian and Ukrainian words are accepted input
synonyms; the answers are in the Telegram language (tg_i18n), and the commands they quote stay
in the grammar's English words.
"""
from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from orchestrator.reviewer.board import FIX_KINDS
from orchestrator.reviewer.lines import _spaced
from orchestrator.reviewer.report import CONFIRMED, Report
from orchestrator.tg_i18n import tr

# Matched on the raw text, so the instruction keeps the operator's own wording.
_EDIT = re.compile(
    r"^(edit|измени|исправь|поменяй|зміни|виправ)\s+#?([0-9]{1,9})\s*:\s*(\S.*)$",
    re.DOTALL | re.IGNORECASE,
)
_AGAIN = re.compile(r"^(again|redo|заново|переделай|наново)\s*(?::\s*(\S.*))?$", re.DOTALL | re.IGNORECASE)
_NUMBER = re.compile(r"#?([0-9]{1,9})")

# Phrases in normalized form (casefolded, "ё" as "е", "'" for the apostrophe), by their token.
_PHRASES = {
    "post": (
        "post", "пости", "постни", "запости", "публикуй", "опубликуй", "публікуй", "опублікуй",
    ),
    "APPROVE": (
        "approve", "апрув", "аппрув", "ставь апрув", "поставь апрув", "став апрув", "постав апрув",
        "ставь approve",
    ),
    "REQUEST_CHANGES": ("request changes", "request-changes", "реквест ченджес", "реквест ченджиз"),
    "all_confirmed": (
        "all confirmed", "все подтвержденные", "всі підтверджені", "усі підтверджені",
    ),
    "no_summary": ("no summary", "without summary", "без summary", "без саммари", "без самари"),
    "anyway": ("anyway", "все равно", "все одно", "однаково"),
    "replies": ("replies", "reply", "answers", "ответы", "ответ", "відповіді", "відповідь"),
    "text": ("text", "текст"),
    "draft": ("draft", "черновик", "чернетка"),
    "review_again": (
        "review again", "re-review", "rereview", "ревью заново", "заново ревью", "ревью ще раз",
        "рев'ю заново",
    ),
    "skip": ("skip", "пропусти", "пропусть"),
    # An address message's: each only as the whole reply.
    "push": ("push", "commit and push", "пуш", "пушь", "запушь", "закоммить и запушь", "запуш"),
    "diff": ("diff", "patch", "дифф", "диф", "патч"),
    # A self-review message's: fix its confirmed findings; only as the whole reply.
    "fix": ("fix", "fix findings", "пофикси", "почини", "фикс", "пофікси", "полагодь"),
    "filler": (
        "comments", "comment", "коментарии", "комментарии", "коменты", "комменты", "коментарі",
        "коменти", "questions", "question", "вопросы", "вопрос", "питання",
    ),
    # The fillers an event-only post takes: "approve comments" is not a bare approve.
    "and": ("and", "и", "і", "й", "та", "also", "+", "&"),
}
_TOKENS = {
    tuple(phrase.split()): token for token, phrases in _PHRASES.items() for phrase in phrases
}
_LONGEST = max(len(words) for words in _TOKENS)
_EVENTS = ("APPROVE", "REQUEST_CHANGES")
_POST_TOKENS = {
    "post", "number", "filler", "and", *_EVENTS, "all_confirmed", "no_summary", "anyway",
}
_REPLIES_TOKENS = {"post", "replies", "number", "filler", "and"}
_EVENT_ONLY_TOKENS = {*_EVENTS, "no_summary", "anyway", "and"}
_RETRY_EVENT = {"APPROVE": "approve", "REQUEST_CHANGES": "request changes"}


@dataclass(frozen=True)
class ReplyCommand:
    action: str  # post, post_replies, text, edit, draft, review_again, skip, push, diff, again, fix
    numbers: tuple[int, ...] = ()
    event: str = "COMMENT"
    all_confirmed: bool = False
    no_summary: bool = False
    anyway: bool = False
    instruction: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "numbers", tuple(sorted(set(self.numbers))))

    def to_dict(self) -> dict:
        return {**dataclasses.asdict(self), "numbers": list(self.numbers)}

    @classmethod
    def from_dict(cls, d: dict) -> ReplyCommand:
        return cls(**d)


def parse_reply(text: str) -> ReplyCommand | None:
    text = text.strip()
    if m := _EDIT.match(text):
        return ReplyCommand("edit", (int(m[2]),), instruction=m[3])
    if m := _AGAIN.match(text):
        return ReplyCommand("again", instruction=m[2] or "")
    tokens = _tokens(text)
    if not tokens:
        return None
    kinds = [kind for kind, _ in tokens]
    count = Counter(kinds)
    used = set(kinds)
    numbers = tuple(n for kind, n in tokens if kind == "number")
    if kinds in (["review_again"], ["skip"], ["push"], ["diff"], ["fix"]):
        return ReplyCommand(kinds[0])
    if used == {"text", "number"} and count["text"] == 1:
        return ReplyCommand("text", numbers)
    if count == Counter(draft=1, number=1):
        return ReplyCommand("draft", numbers)
    if count["post"] == 1 and "replies" in used:
        if used <= _REPLIES_TOKENS and numbers:
            return ReplyCommand("post_replies", numbers)
        return None
    events = [kind for kind in kinds if kind in _EVENTS]
    if len(events) > 1:
        return None
    event = events[0] if events else "COMMENT"
    flags = {"no_summary": "no_summary" in used, "anyway": "anyway" in used}
    if count["post"] == 1 and used <= _POST_TOKENS:
        all_confirmed = "all_confirmed" in used
        if numbers or all_confirmed or events:
            return ReplyCommand("post", numbers, event, all_confirmed=all_confirmed, **flags)
        return None
    if events and used <= _EVENT_ONLY_TOKENS:
        return ReplyCommand("post", (), event, **flags)
    return None


def check_command(
    cmd: ReplyCommand,
    kind: str,
    report: Report | None,
    replies: list[dict],
    posted: set[int],
    drafted: set[int],
) -> ReplyCommand | str:
    """The command with "all confirmed" expanded into numbers, or why nothing is done. An address message takes a
    re-check's commands on its replies plan, and push, diff and again; a findings message takes push, diff and again
    only; "fix" works on a review message (the service decides whether it is a fixable self-review)."""
    if cmd.action == "fix":
        if kind != "review":
            return tr('"fix" works on the message of a self-review of your own PR. Nothing was done.')
        return cmd
    if kind == "findings" and cmd.action not in ("push", "diff", "again", "review_again", "skip"):
        return tr('This is a findings message: reply "push", "diff" or "again: …". Nothing was done.')
    if kind == "address" and cmd.action in ("post", "edit", "draft"):
        return tr('This is a threads message: reply "push", "post replies 1 3", "text 2", "diff" or "again: …". '
                  "Nothing was done.")
    if kind not in FIX_KINDS and cmd.action in ("push", "diff", "again"):
        return tr('"{action}" works on a fix message of your own PR (threads or self-review findings). Nothing was '
                  "done.", action=cmd.action)
    recheck = kind in ("recheck", "address")
    if not recheck and cmd.action == "post_replies":
        return tr('This is a review message: "post replies" works on a re-check message. Nothing was done.')
    if recheck and cmd.action in ("post", "edit", "draft"):
        return tr('This is a re-check message: reply "post replies 1 3". Nothing was done.')
    # Shapes only the LLM can produce: the grammar never gives these counts of numbers.
    if cmd.action == "edit" and (len(cmd.numbers) != 1 or not cmd.instruction.strip()):
        return tr('Edit one number at a time: "edit 2: …". Nothing was done.')
    if cmd.action == "text" and not cmd.numbers:
        return tr('Name a number: "text 2". Nothing was done.')
    if cmd.action == "draft" and len(cmd.numbers) != 1:
        return tr('Draft one number at a time: "draft 7". Nothing was done.')
    if cmd.action == "post_replies" and not cmd.numbers:
        return tr('Name the replies: "post replies 1 3". Nothing was done.')
    items = report.items if report else ()
    dropped = {d.number for d in report.dropped} if report else set()
    if cmd.all_confirmed:  # the confirmed numbers not posted yet; a named one is checked as named
        confirmed = [item.number for item in items if item.kind in CONFIRMED]
        left = tuple(n for n in confirmed if n not in posted)
        if confirmed and not left and not cmd.numbers:
            return tr("Nothing left to post: every confirmed finding is already posted at this head. "
                      "Nothing was done.")
        cmd = dataclasses.replace(cmd, numbers=cmd.numbers + left, all_confirmed=False)
    known = {item.number for item in items} | dropped
    if recheck:
        known |= {entry["n"] for entry in replies}
    if unknown := [n for n in cmd.numbers if n not in known]:
        if len(unknown) == 1:
            return tr("Unknown number: {numbers}. Nothing was done.", numbers=_spaced(unknown))
        return tr("Unknown numbers: {numbers}. Nothing was done.", numbers=_spaced(unknown))
    undrafted = [n for n in cmd.numbers if n in dropped and n not in drafted]
    if cmd.action in ("post", "text", "edit") and undrafted:
        return tr('#{n} was dropped: reply "draft {n}" first. Nothing was done.', n=undrafted[0])
    already = [n for n in cmd.numbers if n in posted]
    if cmd.action in ("post", "post_replies") and already:
        return tr("#{n} is already posted at this head. Nothing was done.", n=already[0])
    if cmd.action == "draft":
        for n in cmd.numbers:
            if n not in dropped:
                return tr("#{n} is not a dropped number. Nothing was done.", n=n)
            if n in drafted:
                return tr('#{n} is drafted already: reply "text {n}". Nothing was done.', n=n)
    if cmd.action == "post" and not cmd.numbers and (
        cmd.event == "COMMENT" or (cmd.event == "REQUEST_CHANGES" and cmd.no_summary)
    ):
        return tr("Nothing to post: name numbers or keep the Summary. Nothing was done.")
    return cmd


def describe(cmd: ReplyCommand, head: str) -> str:
    """One line naming a checked command (its "all confirmed" already expanded); the command's own words stay the
    grammar's."""
    numbers = _spaced(cmd.numbers)
    if cmd.action == "post":
        if cmd.numbers:
            text = tr("post {numbers} + {event} at {head}", numbers=numbers, event=cmd.event, head=head[:9])
            text += tr(", no summary") if cmd.no_summary else ""
        elif cmd.no_summary:
            text = tr("{event} without a body at {head}", event=cmd.event, head=head[:9])
        else:
            text = tr("{event} with the Summary at {head}", event=cmd.event, head=head[:9])
        return text + (tr(", anyway") if cmd.anyway else "")
    if cmd.action == "post_replies":
        return f"post replies {numbers}"
    if cmd.action == "edit":
        return f'edit {numbers}: "{cmd.instruction}"'
    if cmd.action == "review_again":
        return "review again"
    if cmd.action in ("push", "diff", "fix"):
        return cmd.action
    if cmd.action == "again":
        return f'again: "{cmd.instruction}"' if cmd.instruction else "again"
    if cmd.action == "skip":
        return tr("skip this head")
    return f"{cmd.action} {numbers}"  # text, draft


def retry_text(numbers: Iterable[int], event: str, *, no_summary: bool = False, anyway: bool = False) -> str:
    """The reply that posts `numbers` with `event` and the flags, in the grammar's own words."""
    words = ["post", *map(str, numbers)]
    if event in _RETRY_EVENT:
        words.append(_RETRY_EVENT[event])
    words += ["no summary"] * no_summary + ["anyway"] * anyway
    return " ".join(words)


def _tokens(text: str) -> list[tuple[str, int]] | None:
    """The reply as (token, number) pairs, longest phrase first; None when a word is not known."""
    if text[-1:] in (".", "!"):  # one full stop only: "..." or "!!" is left for the LLM
        text = text[:-1]
    text = text.casefold().replace("ё", "е").replace("’", "'").replace("ʼ", "'")
    words = text.replace(",", " ").split()
    tokens: list[tuple[str, int]] = []
    i = 0
    while i < len(words):
        for size in range(min(_LONGEST, len(words) - i), 0, -1):
            if token := _TOKENS.get(tuple(words[i:i + size])):
                tokens.append((token, 0))
                i += size
                break
        else:
            if not (m := _NUMBER.fullmatch(words[i])):
                return None
            tokens.append(("number", int(m[1])))
            i += 1
    return tokens

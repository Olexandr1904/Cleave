"""SkillReplies: a reply to a skill run's message, or a tap on its Approve / Drop buttons, made one compare-and-set on
the run.

Approve (the button, or a reply that is an OK word) and Drop count only on the run's newest message that went out with
buttons, and not while a newer one waits in its outbox: an approval binds to what was shown. Any other reply is new
information for the session, taken on any message of the run. The runner alone saves run objects and sends outboxes: a
change here is one SkillStore.claim, so a second tap finds nothing to take, and every answer is one SkillChat.answer.
An answer about no run is a dead end: a reply to it is answered here, never left to the intent parser. Answers are in
the language of the run's project (language_of), an answer about no run in the chat's.

A comparison's messages are anchored like a run's, to its id. A reply to one is kept as the verdict's note; a tap on
Staged, Skill or Neither (or Retry on a stuck verdict) counts only on its newest message, and its claim expects that
message to still stand, so a tap that raced a newer message changes nothing; the tap is what lets the keeper make the
verdict's forge and tracker writes, so it counts only in the comparison's own chat. A run whose comparison has its
verdict takes no reply and no tap.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from orchestrator.reviewer.service import _where, utcnow
from orchestrator.skillflow.chat import SkillChat
from orchestrator.skillflow.compare_messages import _verdict_name
from orchestrator.skillflow.compare_models import ID_PREFIX, Comparison
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.models import DROPPABLE_STATES, FINAL_STATES, PARKED_STATES, REPLY_STATES, SkillRun
from orchestrator.skillflow.store import SkillStore
from orchestrator.tg_i18n import ENGLISH, N_, telegram_language, tr

logger = logging.getLogger(__name__)

OK_WORDS = frozenset({"ok", "okay", "yes", "go", "approve", "ок", "окей", "так", "да", "добре"})

# TelegramAdapter.start_polling() keeps pending updates: a reply older than this (one sent while the daemon was down,
# say) changes nothing.
_REPLY_MAX_AGE = timedelta(minutes=15)
_TAPS = {"sk_ok": "approve", "sk_drop": "drop"}
_TAKES = {"approve": PARKED_STATES, "text": REPLY_STATES, "drop": DROPPABLE_STATES}  # the states each input moves on
_NOT_KNOWN = N_("This skill run is not known; nothing was done.")
_UNREADABLE = N_("This skill run cannot be read; nothing was done.")
_NOT_LATEST = N_("That is not this run's latest message; nothing was done. Use the newest one.")
_UNSENT = N_("This run's newest message has not gone out yet; nothing was done. Wait for it.")
_DEAD_END = N_("Reply to a skill run's own message to act on it; nothing was done.")
_UNKNOWN_ACTION = N_("Unknown action; nothing was done.")
# The command handler's answer too, when the daemon has no skill flow at all.
SKILL_OFF = N_("The skill flow is off on this daemon; nothing was done.")

_VERDICTS = {"sk_win_staged": "staged", "sk_win_skill": "skill", "sk_win_none": "neither"}
_RETRY = "sk_retry"
_NOTE_KEPT = N_("Kept as the comparison's note.")
_CMP_NOT_KNOWN = N_("This comparison is not known; nothing was done.")
_CMP_DECIDED = N_("The comparison has its verdict ({verdict}); nothing was done.")
_CMP_DECIDING = N_("The verdict is being carried out; nothing was done.")
_CMP_STUCK = N_("The verdict ({verdict}) is stuck; nothing was done. The Retry button on its newest message continues"
                " it.")
_CMP_UNSENT = N_("The comparison's newest message has not gone out yet; nothing was done. Wait for it.")
_CMP_NOT_NEWEST = N_("That is not the comparison's newest message; nothing was done. Use the newest one.")
_NO_PR = N_("That side has no PR; nothing was done.")
_RUN_DECIDED = N_("This run's comparison has its verdict; nothing was done.")


class SkillReplies:
    def __init__(self, store: SkillStore, chat: SkillChat, wake: Callable[[], None],
                 now: Callable[[], datetime] = utcnow, *, comparisons: ComparisonStore | None = None,
                 wake_keeper: Callable[[], None] = lambda: None,
                 language_of: Callable[[str, str], str] = lambda scope, chat_id: ENGLISH) -> None:
        self._store = store
        self._chat = chat
        self._wake = wake  # the runner's: a run queued here starts without waiting out the runner's idle wait
        self._now = now
        self._comparisons = comparisons
        self._wake_keeper = wake_keeper  # the keeper's: a verdict claimed here is carried out without its idle wait
        self._language_of = language_of  # (scope, chat_id): scope "" when the answer is about nothing known

    def is_anchor(self, chat_id: str, message_id: int) -> bool:
        """Whether the message is a skill run's or a comparison's; a store error propagates (the caller answers
        it)."""
        return self._store.anchor(chat_id, message_id) is not None

    async def handle_reply(self, chat_id: str, message_id: int, text: str, sent_at: datetime | None = None) -> None:
        """Act on a reply to a run's message, or answer why nothing is done. `sent_at` is the reply's Telegram date:
        a reply to a run's message older than _REPLY_MAX_AGE changes nothing; a comparison's note is kept at any
        age. Never raises (cancellation aside)."""
        try:
            # Called for anchors only (is_anchor): an anchor gone since, or one whose run cannot be read, is
            # answered here all the same, never left to the intent parser.
            run_id = self._store.anchor(chat_id, message_id)
            if run_id and run_id.startswith(ID_PREFIX):
                await self._note(run_id, chat_id, message_id, text)
                return
            run = None if not run_id else self._store.run(run_id)
            now = self._now()
            with telegram_language(self._language_of("" if run is None else run.scope, chat_id)):
                if run_id == "":  # a dead end: an answer about no run
                    await self._chat.answer(None, chat_id, message_id, tr(_DEAD_END))
                elif run is None:
                    await self._chat.answer(None, chat_id, message_id, tr(_UNREADABLE))
                elif sent_at is not None and now - sent_at > _REPLY_MAX_AGE:
                    await self._answer(run, chat_id, message_id, _too_old_line(sent_at, now))
                elif _is_ok(text):
                    await self._act(run, "approve", chat_id, message_id)
                else:
                    await self._act(run, "text", chat_id, message_id, text)
        except Exception as e:
            await self._internal_error(chat_id, message_id, e)

    async def handle_callback(self, action: str, target: str, chat_id: str, message_id: int) -> None:
        """A tap on Approve (sk_ok) or Drop (sk_drop) of a run's message `message_id`, or on Staged, Skill, Neither
        (sk_win_*) or Retry (sk_retry) of a comparison's; `target` is the run's or the comparison's id. A tap
        carries no date of its own: no age rule. Never raises (cancellation aside)."""
        try:
            if action in _VERDICTS or action == _RETRY:
                await self._decide(action, target, chat_id, message_id)
                return
            what = _TAPS.get(action)
            run = None if what is None else self._store.run(target)
            with telegram_language(self._language_of("" if run is None else run.scope, chat_id)):
                if what is None:
                    logger.warning("Skill-run button %r not known: nothing was done", action)
                    await self._chat.answer(None, chat_id, message_id, tr(_UNKNOWN_ACTION))
                elif run is None:
                    await self._chat.answer(None, chat_id, message_id, tr(_NOT_KNOWN))
                else:
                    await self._act(run, what, chat_id, message_id)
        except Exception as e:
            await self._internal_error(chat_id, message_id, e)

    async def answer(self, chat_id: str, reply_to: int, text: str) -> None:
        """An answer about no run that the command handler sends itself ("Reply not handled"): a dead end like this
        module's own, so a reply to it is the skill flow's too. Never raises."""
        await self._chat.answer(None, chat_id, reply_to, text)

    async def _act(self, run: SkillRun, what: str, chat_id: str, message_id: int, text: str = "") -> None:
        """`what` (approve, text or drop) on the run as read, or the answer why nothing is done; in the current
        language, the run's."""
        # Correct only because no await falls between this read and the claim below: the daemon's one event loop
        # runs no tap or keeper cycle in between. Keep it that way, or the verdict can be claimed meanwhile.
        c = None if self._comparisons is None else self._comparisons.for_run(run.id)
        if c is not None and c.state != "open":
            # The verdict ends the run: a reply or tap now would start work the keeper is ending.
            await self._answer(run, chat_id, message_id, tr(_RUN_DECIDED))
            return
        if run.state not in _TAKES[what]:
            await self._answer(run, chat_id, message_id, _state_line(run.state))
            return
        if what != "text" and any(entry.get("buttons") for entry in run.outbox):
            # The run's newest question or stop is still to go out, and ask_message_ids name the one before it.
            await self._answer(run, chat_id, message_id, tr(_UNSENT))
            return
        if what != "text" and message_id not in run.ask_message_ids:
            await self._answer(run, chat_id, message_id, tr(_NOT_LATEST))
            return
        state, changes, done = _move(run, what, text)
        # From the state read, not any of _TAKES: the changes were chosen for that state alone.
        taken = self._store.claim(run.id, (run.state,), self._now().isoformat(), state, **changes)
        if taken is None:  # another reply, a tap or the runner moved it since the read
            current = self._store.run(run.id)
            await self._answer(run, chat_id, message_id,
                               tr(_UNREADABLE) if current is None else _state_line(current.state))
            return
        self._wake()
        await self._answer(taken, chat_id, message_id, done)

    async def _note(self, cid: str, chat_id: str, message_id: int, text: str) -> None:
        """A reply to a comparison's message, kept as the verdict's note: in any state, on any of its messages and
        at any age, an OK word too, since a note changes nothing else."""
        c = None if self._comparisons is None else self._comparisons.add_note(cid, text, self._now().isoformat())
        with telegram_language(self._language_of("" if c is None else c.scope, chat_id)):
            if c is None:
                await self._chat.answer(None, chat_id, message_id, tr(_CMP_NOT_KNOWN))
            else:
                await self._answer(c, chat_id, message_id, tr(_NOTE_KEPT))

    async def _decide(self, action: str, cid: str, chat_id: str, message_id: int) -> None:
        """A verdict tap, or Retry, on the comparison as read: one claim, or the answer why nothing is done."""
        c = None if self._comparisons is None else self._comparisons.get(cid)
        if c is not None and c.chat_id != chat_id:
            # The tap authorises forge and tracker writes: from another chat, the comparison is not known there.
            c = None
        with telegram_language(self._language_of("" if c is None else c.scope, chat_id)):
            if c is None:
                await self._chat.answer(None, chat_id, message_id, tr(_CMP_NOT_KNOWN))
                return
            verdict = _VERDICTS.get(action, "")   # "" for Retry
            refusal = _refusal(c, verdict, message_id)
            if refusal:
                await self._answer(c, chat_id, message_id, refusal)
                return
            now = self._now().isoformat()
            # What was read must still hold: the keeper counts one more message for each new one, and clears the pair
            # when a side moves on under the standing message; either way this tap was on a message that no longer
            # stands.
            if verdict:
                taken = self._comparisons.claim(
                    c.id, ("open",), now, "deciding", expect={"messages": c.messages, "fingerprint": c.fingerprint},
                    verdict=verdict, decided_at=now, steps_done=[], reason="")
            else:
                taken = self._comparisons.claim(c.id, ("stuck",), now, "deciding", expect={"messages": c.messages})
            if taken is None:
                current = self._comparisons.get(c.id)
                line = tr(_CMP_NOT_KNOWN) if current is None else (
                    _refusal(current, verdict, message_id) or tr(_CMP_NOT_NEWEST))
                await self._answer(c, chat_id, message_id, line)
                return
            self._wake_keeper()
            done = _decided_line(taken) if verdict else tr("Retrying the verdict from the step that failed.")
            await self._answer(taken, chat_id, message_id, done)

    async def _answer(self, target: SkillRun | Comparison, chat_id: str, message_id: int, text: str) -> None:
        await self._chat.answer(target, chat_id, message_id, f"{target.ticket_id} · {text}")

    async def _internal_error(self, chat_id: str, message_id: int, e: Exception) -> None:
        # Never the error's text: the class and where it was raised in the log, the class alone in the chat.
        logger.warning("Skill-run reply or tap not handled: %s", _where(e))
        with telegram_language(self._language_of("", chat_id)):
            text = tr("Internal error ({name}); nothing was done.", name=type(e).__name__)
        await self._chat.answer(None, chat_id, message_id, text)


class SkillOff:
    """The skill flow on a daemon that serves no repo: a reply to an old skill-run or comparison message, or a tap on
    one of its buttons, is answered "The skill flow is off on this daemon; nothing was done." in the chat's language
    and never reaches the intent parser."""

    def __init__(self, store: SkillStore, chat: SkillChat,
                 language_of: Callable[[str], str] = lambda chat_id: ENGLISH) -> None:
        self._store = store
        self._chat = chat
        self._language_of = language_of  # (chat_id)

    def is_anchor(self, chat_id: str, message_id: int) -> bool:
        """Whether the message is a skill run's or a comparison's; a store error propagates (the caller answers
        it)."""
        return self._store.anchor(chat_id, message_id) is not None

    async def handle_reply(self, chat_id: str, message_id: int, text: str, sent_at: datetime | None = None) -> None:
        await self._off(chat_id, message_id)

    async def handle_callback(self, action: str, target: str, chat_id: str, message_id: int) -> None:
        await self._off(chat_id, message_id)

    async def answer(self, chat_id: str, reply_to: int, text: str) -> None:
        """An answer the command handler sends itself: a dead end, so a reply to it is answered here too."""
        await self._chat.answer(None, chat_id, reply_to, text)

    async def _off(self, chat_id: str, message_id: int) -> None:
        with telegram_language(self._language_of(chat_id)):
            await self._chat.answer(None, chat_id, message_id, tr(SKILL_OFF))


def _refusal(c: Comparison, verdict: str, message_id: int) -> str:
    """Why a tap on `message_id` takes nothing from the comparison as read, "" when it may: `verdict` is the side
    tapped, "" for Retry. In the current language."""
    if c.state != ("open" if verdict else "stuck"):
        if c.state == "closed":
            return tr(_CMP_DECIDED, verdict=_verdict_name(c.verdict))
        if c.state == "open":   # a Retry tapped on a message from before the comparison's newest
            return tr(_CMP_NOT_NEWEST)
        if c.state == "stuck":  # a verdict tap: only Retry goes on from a stuck verdict
            return tr(_CMP_STUCK, verdict=_verdict_name(c.verdict))
        return tr(_CMP_DECIDING)
    if any(entry.get("buttons") for entry in c.outbox):
        # The newest message is still to go out, and ask_message_ids name the one before it.
        return tr(_CMP_UNSENT)
    if message_id not in c.ask_message_ids:
        return tr(_CMP_NOT_NEWEST)
    if verdict in ("staged", "skill") and not c.sides.get(verdict, {}).get("pr_number"):
        return tr(_NO_PR)
    return ""


def _decided_line(c: Comparison) -> str:
    """The answer to a verdict tap that took the comparison, in the current language."""
    if c.verdict == "neither":
        return tr("Verdict: neither. Both PRs stay drafts; both sides end.")
    other = c.sides.get("skill" if c.verdict == "staged" else "staged", {}).get("pr_number", 0)
    return tr("Verdict: {verdict}. Cleave marks PR #{number} ready and makes the tracker writes for it{closing}.",
              verdict=_verdict_name(c.verdict), number=c.sides[c.verdict]["pr_number"],
              closing=tr(", and closes PR #{number}", number=other) if other else "")


def _move(run: SkillRun, what: str, text: str) -> tuple[str, dict, str]:
    """The run's new state, the changes that go with it and the answer (in the current language), for `what` on
    the run in its state."""
    if what == "drop":
        # By what happened, not the phase: a reply sends a run stopped after its push back to the work step.
        if run.pushed:
            done = tr("Dropped. The branch {branch} is on the forge; Cleave pushes nothing more.", branch=run.branch)
        elif run.push_branch:   # a push that raised, or was cut by a restart
            done = tr("Dropped. A push of {branch} was started and may have reached the forge; Cleave pushes nothing"
                      " more.", branch=run.push_branch)
        else:
            done = tr("Dropped. Nothing was pushed.")
        return "dropped", {"reason": "dropped by the operator"}, done
    # A fresh deferral count, on a reply as on Approve: a run stopped after its third deferral in a row would stop
    # again at the first new limit.
    if what == "text":
        # Back to the work step: the publish steps never read `pending`, so a reply kept for a later step would be
        # lost; and the reply, not an earlier approval, is what the next turn says.
        return ("queued", {"pending": text, "approved": False, "phase": "work", "deferrals": 0},
                tr("Got it — the session continues with your reply."))
    if run.state == "waiting":
        return "queued", {"approved": True, "pending": ""}, tr("Approved — the session goes on.")
    return "queued", {"deferrals": 0}, tr("Continuing from the {phase} step.", phase=run.phase)


def _state_line(state: str) -> str:
    if state in FINAL_STATES:
        return tr("The run is finished ({state}); nothing was done.", state=state)
    return tr("The run is working ({state}); nothing was done. Reply again when it stops.", state=state)


def _is_ok(text: str) -> bool:
    """Whether the reply is one OK word, its case and one closing "." or "!" aside: "ok, but skip the UI tests" is
    new information."""
    word = text.strip().casefold()
    return (word[:-1] if word.endswith((".", "!")) else word) in OK_WORDS


def _too_old_line(sent_at: datetime, now: datetime) -> str:
    """The answer to a reply too old to act on: its time in UTC, its day in front when that is not today."""
    when = sent_at.astimezone(timezone.utc)
    day = "" if when.date() == now.astimezone(timezone.utc).date() else f"{when.day} {when:%b} "
    return tr("Your reply from {when} UTC is more than 15 minutes old; nothing was done. Reply again if it still"
              " holds.", when=f"{day}{when:%H:%M}")

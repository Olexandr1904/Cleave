"""SkillReplies: a reply to a skill run's message, or a tap on its Approve / Drop buttons, made one compare-and-set on
the run.

Approve (the button, or a reply that is an OK word) and Drop count only on the run's newest message that went out with
buttons, and not while a newer one waits in its outbox: an approval binds to what was shown. Any other reply is new
information for the session, taken on any message of the run. The runner alone saves run objects and sends outboxes: a
change here is one SkillStore.claim, so a second tap finds nothing to take, and every answer is one SkillChat.answer.
An answer about no run is a dead end: a reply to it is answered here, never left to the intent parser.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from orchestrator.reviewer.service import _where, utcnow
from orchestrator.skillflow.chat import SkillChat
from orchestrator.skillflow.models import DROPPABLE_STATES, FINAL_STATES, PARKED_STATES, REPLY_STATES, SkillRun
from orchestrator.skillflow.store import SkillStore

logger = logging.getLogger(__name__)

OK_WORDS = frozenset({"ok", "okay", "yes", "go", "approve", "ок", "окей", "так", "да", "добре"})

# TelegramAdapter.start_polling() keeps pending updates: a reply older than this (one sent while the daemon was down,
# say) changes nothing.
_REPLY_MAX_AGE = timedelta(minutes=15)
_TAPS = {"sk_ok": "approve", "sk_drop": "drop"}
_TAKES = {"approve": PARKED_STATES, "text": REPLY_STATES, "drop": DROPPABLE_STATES}  # the states each input moves on
_NOT_KNOWN = "This skill run is not known; nothing was done."
_UNREADABLE = "This skill run cannot be read; nothing was done."
_NOT_LATEST = "That is not this run's latest message; nothing was done. Use the newest one."
_UNSENT = "This run's newest message has not gone out yet; nothing was done. Wait for it."
_DEAD_END = "Reply to a skill run's own message to act on it; nothing was done."
_UNKNOWN_ACTION = "Unknown action; nothing was done."


class SkillReplies:
    def __init__(self, store: SkillStore, chat: SkillChat, wake: Callable[[], None],
                 now: Callable[[], datetime] = utcnow) -> None:
        self._store = store
        self._chat = chat
        self._wake = wake  # the runner's: a run queued here starts without waiting out the runner's idle wait
        self._now = now

    def is_anchor(self, chat_id: str, message_id: int) -> bool:
        """Whether the message is a skill run's; a store error propagates (the caller answers it)."""
        return self._store.anchor(chat_id, message_id) is not None

    async def handle_reply(self, chat_id: str, message_id: int, text: str, sent_at: datetime | None = None) -> None:
        """Act on a reply to a run's message, or answer why nothing is done. `sent_at` is the reply's Telegram date:
        a reply older than _REPLY_MAX_AGE changes nothing. Never raises (cancellation aside)."""
        try:
            # Called for anchors only (is_anchor): an anchor gone since, or one whose run cannot be read, is
            # answered here all the same, never left to the intent parser.
            run_id = self._store.anchor(chat_id, message_id)
            run = None if not run_id else self._store.run(run_id)
            now = self._now()
            if run_id == "":  # a dead end: an answer about no run
                await self._chat.answer(None, chat_id, message_id, _DEAD_END)
            elif run is None:
                await self._chat.answer(None, chat_id, message_id, _UNREADABLE)
            elif sent_at is not None and now - sent_at > _REPLY_MAX_AGE:
                await self._answer(run, chat_id, message_id, _too_old_line(sent_at, now))
            elif _is_ok(text):
                await self._act(run, "approve", chat_id, message_id)
            else:
                await self._act(run, "text", chat_id, message_id, text)
        except Exception as e:
            await self._internal_error(chat_id, message_id, e)

    async def handle_callback(self, action: str, run_id: str, chat_id: str, message_id: int) -> None:
        """A tap on Approve (sk_ok) or Drop (sk_drop) of the message `message_id`. A tap carries no date of its own:
        no age rule. Never raises (cancellation aside)."""
        try:
            what = _TAPS.get(action)
            if what is None:
                logger.warning("Skill-run button %r not known: nothing was done", action)
                await self._chat.answer(None, chat_id, message_id, _UNKNOWN_ACTION)
                return
            run = self._store.run(run_id)
            if run is None:
                await self._chat.answer(None, chat_id, message_id, _NOT_KNOWN)
            else:
                await self._act(run, what, chat_id, message_id)
        except Exception as e:
            await self._internal_error(chat_id, message_id, e)

    async def answer(self, chat_id: str, reply_to: int, text: str) -> None:
        """An answer about no run that the command handler sends itself ("Reply not handled"): a dead end like this
        module's own, so a reply to it is the skill flow's too. Never raises."""
        await self._chat.answer(None, chat_id, reply_to, text)

    async def _act(self, run: SkillRun, what: str, chat_id: str, message_id: int, text: str = "") -> None:
        """`what` (approve, text or drop) on the run as read, or the answer why nothing is done."""
        if run.state not in _TAKES[what]:
            await self._answer(run, chat_id, message_id, _state_line(run.state))
            return
        if what != "text" and any(entry.get("buttons") for entry in run.outbox):
            # The run's newest question or stop is still to go out, and ask_message_ids name the one before it.
            await self._answer(run, chat_id, message_id, _UNSENT)
            return
        if what != "text" and message_id not in run.ask_message_ids:
            await self._answer(run, chat_id, message_id, _NOT_LATEST)
            return
        state, changes, done = _move(run, what, text)
        # From the state read, not any of _TAKES: the changes were chosen for that state alone.
        taken = self._store.claim(run.id, (run.state,), self._now().isoformat(), state, **changes)
        if taken is None:  # another reply, a tap or the runner moved it since the read
            current = self._store.run(run.id)
            await self._answer(run, chat_id, message_id, _UNREADABLE if current is None else _state_line(current.state))
            return
        self._wake()
        await self._answer(taken, chat_id, message_id, done)

    async def _answer(self, run: SkillRun, chat_id: str, message_id: int, text: str) -> None:
        await self._chat.answer(run, chat_id, message_id, f"{run.ticket_id} · {text}")

    async def _internal_error(self, chat_id: str, message_id: int, e: Exception) -> None:
        # Never the error's text: the class and where it was raised in the log, the class alone in the chat.
        logger.warning("Skill-run reply or tap not handled: %s", _where(e))
        await self._chat.answer(None, chat_id, message_id, f"Internal error ({type(e).__name__}); nothing was done.")


def _move(run: SkillRun, what: str, text: str) -> tuple[str, dict, str]:
    """The run's new state, the changes that go with it and the answer, for `what` on the run in its state."""
    if what == "drop":
        # By what happened, not the phase: a reply sends a run stopped after its push back to the work step.
        if run.pushed:
            done = f"Dropped. The branch {run.branch} is on the forge; Cleave pushes nothing more."
        elif run.push_branch:   # a push that raised, or was cut by a restart
            done = (f"Dropped. A push of {run.push_branch} was started and may have reached the forge; Cleave "
                    "pushes nothing more.")
        else:
            done = "Dropped. Nothing was pushed."
        return "dropped", {"reason": "dropped by the operator"}, done
    # A fresh deferral count, on a reply as on Approve: a run stopped after its third deferral in a row would stop
    # again at the first new limit.
    if what == "text":
        # Back to the work step: the publish steps never read `pending`, so a reply kept for a later step would be
        # lost; and the reply, not an earlier approval, is what the next turn says.
        return ("queued", {"pending": text, "approved": False, "phase": "work", "deferrals": 0},
                "Got it — the session continues with your reply.")
    if run.state == "waiting":
        return "queued", {"approved": True, "pending": ""}, "Approved — the session goes on."
    return "queued", {"deferrals": 0}, f"Continuing from the {run.phase} step."


def _state_line(state: str) -> str:
    if state in FINAL_STATES:
        return f"The run is finished ({state}); nothing was done."
    return f"The run is working ({state}); nothing was done. Reply again when it stops."


def _is_ok(text: str) -> bool:
    """Whether the reply is one OK word, its case and one closing "." or "!" aside: "ok, but skip the UI tests" is
    new information."""
    word = text.strip().casefold()
    return (word[:-1] if word.endswith((".", "!")) else word) in OK_WORDS


def _too_old_line(sent_at: datetime, now: datetime) -> str:
    """The answer to a reply too old to act on: its time in UTC, its day in front when that is not today."""
    when = sent_at.astimezone(timezone.utc)
    day = "" if when.date() == now.astimezone(timezone.utc).date() else f"{when.day} {when:%b} "
    return (f"Your reply from {day}{when:%H:%M} UTC is more than 15 minutes old; nothing was done. "
            "Reply again if it still holds.")

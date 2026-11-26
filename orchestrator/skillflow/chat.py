"""SkillChat: the skill flow's messages to Telegram, every part a reply anchor of its run."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from integrations.base.notifier import Button
from orchestrator.reviewer.messages import MESSAGE_LIMIT, split_message
from orchestrator.reviewer.service import utcnow
from orchestrator.skillflow.models import SkillRun
from orchestrator.skillflow.store import SkillStore
from orchestrator.tg_translate import Translator, localize

logger = logging.getLogger(__name__)

_ANCHOR_TRIES = 3
_ANCHOR_WAIT = 0.2  # seconds between two tries of an anchor write


class SendError(Exception):
    """A message did not go out, or went out without its anchor. The text names the exception's class only: a
    Telegram error text can hold the bot token. `ids`: the parts the failed send got out, anchored, before it
    stopped."""

    def __init__(self, message: str, ids: list[int] | None = None) -> None:
        super().__init__(message)
        self.ids = list(ids or [])


class SkillChat:
    def __init__(self, notifier: Any, store: SkillStore, now: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, translator: Translator | None = None):
        self._notifier = notifier  # send_message(chat_id, text, buttons=, reply_to_message_id=) -> the message id
        self._store = store
        self._now = now
        self._sleep = sleep
        self._translator = translator  # the skill's text's (None: it goes out as the skill wrote it)

    async def localize(self, text: str) -> str:
        """`text` with its model-written texts in the current Telegram language (as they are without a translator
        or when the translation fails), unmarked."""
        return await localize(text, self._translator)

    async def send(self, run: SkillRun, text: str, buttons: list[Button] | None = None, skip: int = 0) -> list[int]:
        """Send `text` to the run's chat in parts, its model-written texts in the current Telegram language, the
        buttons on the last, each part anchored to the run before the next goes out; return the message ids of the parts it sent, in order. The first `skip` parts are left
        out: an earlier send got them out. A failed send or anchor write raises SendError with the parts sent
        before it: a reply to an unanchored message would reach the intent parser, so a part that could not be
        anchored is deleted first (once; parts already anchored stay)."""
        # Whole, before the split: a mark is never cut in two.
        text = await self.localize(text)
        # Telegram rejects a blank text, and the runner would send it again on every cycle.
        parts = [part for part in split_message(text, MESSAGE_LIMIT) if part.strip()]
        ids: list[int] = []
        for n, part in enumerate(parts, 1):
            if n <= skip:
                continue
            try:
                message_id = await self._notifier.send_message(
                    run.chat_id, part, buttons=buttons if n == len(parts) else None)
            except Exception as e:
                raise SendError(f"not sent: {type(e).__name__}", ids) from None
            try:
                await self._anchor(run.chat_id, message_id, run.id)
            except SendError as e:
                await self._delete(run.chat_id, message_id, run.id)
                raise SendError(str(e), ids) from None
            ids.append(message_id)
        return ids

    async def answer(self, run: SkillRun | None, chat_id: str, reply_to: int, text: str) -> None:
        """Send `text` as one reply to `reply_to`, anchored to the run; an answer that could not be anchored is
        deleted, as in send. An answer about no run is a dead end: its anchor names no run, so a reply to it is the
        skill flow's, never the intent parser's. That anchor is best effort: the answer stays when it cannot be
        written, for the operator to read. Failures are logged, never raised: what the answer reports is already
        done."""
        try:
            message_id = await self._notifier.send_message(chat_id, text, reply_to_message_id=reply_to)
        except Exception as e:
            logger.warning("Skill-run answer to message %d in chat %s not sent: %s",
                           reply_to, chat_id, type(e).__name__)
            return
        try:
            await self._anchor(chat_id, message_id, "" if run is None else run.id)
        except SendError as e:
            if run is None:
                logger.warning("Skill-run answer in chat %s kept without its dead-end anchor: %s", chat_id, e)
                return
            # A warning: the answer is deleted next, and only a failed delete leaves a reply to reach the parser.
            logger.warning("Skill-run answer in chat %s (run %s): %s", chat_id, run.id, e)
            await self._delete(chat_id, message_id, run.id)

    async def _delete(self, chat_id: str, message_id: int, run_id: str) -> None:
        try:
            await self._notifier.delete_message(chat_id, message_id)
        except Exception as e:
            logger.error("Skill-run message %d in chat %s (run %s) is not anchored and not deleted: %s; a reply to "
                         "it is not the skill flow's and reaches the ticket pipeline's commands: delete the message "
                         "by hand", message_id, chat_id, run_id, type(e).__name__)

    async def _anchor(self, chat_id: str, message_id: int, run_id: str) -> None:
        for attempt in range(1, _ANCHOR_TRIES + 1):
            try:
                self._store.add_anchor(chat_id, message_id, run_id, self._now().isoformat())
                return
            except Exception as e:
                if attempt == _ANCHOR_TRIES:
                    raise SendError(f"message {message_id} not anchored: {type(e).__name__}") from None
                await self._sleep(_ANCHOR_WAIT)

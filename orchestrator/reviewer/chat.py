"""The reviewer's Telegram chat: where its lines and messages go, each one a reply anchor."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from integrations.base.notifier import Button
from orchestrator.reviewer.service import utcnow
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import current_language
from orchestrator.tg_translate import Translator, localize

logger = logging.getLogger(__name__)

_ANCHOR_TRIES = 3
_ANCHOR_WAIT = 0.2  # seconds between two tries of an anchor write


@dataclass
class ReviewerChat:
    notifier: Any  # send_message(chat_id, text, buttons=, reply_to_message_id=) -> the Telegram message id
    chat_id: str
    store: ReviewerStore | None = None
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    translator: Translator | None = None  # the model-written texts' (None: they stay as they are)

    async def translations(self, texts: list[str]) -> dict[str, str]:
        """The model-written `texts` in the current Telegram language, those that were translated; {} in English,
        without a translator, or when the translation fails."""
        if self.translator is None or not texts:
            return {}
        return await self.translator.translations(texts, current_language())

    async def localize(self, text: str) -> str:
        """`text` with its model-written texts in the current language: done before a text is split into parts, so
        a mark is never cut in two."""
        return await localize(text, self.translator)

    async def send(
        self,
        text: str,
        *,
        job_id: str = "",
        scope: str = "",
        number: int = 0,
        reply_to: int | None = None,
        buttons: list[Button] | None = None,
    ) -> int:
        """Send one plain-text message and return its Telegram message id; a failed send raises. With a store,
        the message becomes a reply anchor pointing at the job, else at the PR (scope and number) or the repo
        (scope). The anchor write is tried _ANCHOR_TRIES times, _ANCHOR_WAIT apart, and never raises: the message
        is out. When it still fails, a reply to the message is not recognised: that is logged at ERROR and noted
        in the job's run."""
        message_id = await self.notifier.send_message(
            self.chat_id, text, buttons=buttons, reply_to_message_id=reply_to)
        if self.store is not None:
            await self._anchor(message_id, job_id, scope, number)
        return message_id

    async def _anchor(self, message_id: int, job_id: str, scope: str, number: int) -> None:
        for attempt in range(1, _ANCHOR_TRIES + 1):
            try:
                self.store.add_anchor(self.chat_id, message_id, job_id, self.now().isoformat(),
                                      scope=scope, number=number)
                return
            except Exception as e:
                if attempt < _ANCHOR_TRIES:
                    await self.sleep(_ANCHOR_WAIT)
                    continue
                logger.error("Reviewer message %d in chat %s (job %s) not anchored after %d tries: %s",
                             message_id, self.chat_id, job_id or "none", _ANCHOR_TRIES, type(e).__name__)
        if job_id:
            try:
                run = self.store.run(job_id)
                if run is not None:
                    run.notes.append(f"message {message_id} is not registered; replies to it are not recognised")
                    self.store.save_run(run)
            except Exception as e:
                logger.warning("Reviewer note on message %d not written: %s", message_id, type(e).__name__)

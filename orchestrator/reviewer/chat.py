"""The reviewer's Telegram chat: where its lines and messages go."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ReviewerChat:
    notifier: Any  # send_message(chat_id, text) -> the Telegram message id
    chat_id: str

    async def send(self, text: str) -> int:
        """Send one plain-text message and return its Telegram message id; a failed send raises."""
        return await self.notifier.send_message(self.chat_id, text)

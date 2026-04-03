"""Telegram adapter implementing NotifierInterface."""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, filters

from integrations.base.notifier import NotifierInterface

logger = logging.getLogger(__name__)


class TelegramAdapter(NotifierInterface):
    """Telegram Bot API adapter using python-telegram-bot."""

    def __init__(self, bot_token: str) -> None:
        self._bot = Bot(token=bot_token)
        self._pending_replies: dict[int, asyncio.Future[str]] = {}
        self._app: Application | None = None

    async def send_message(self, chat_id: str, message: str) -> int:
        """Send a message and return the message ID."""
        msg = await self._bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
        logger.info("Sent Telegram message %d to chat %s", msg.message_id, chat_id)
        return msg.message_id

    async def wait_for_reply(
        self, chat_id: str, message_id: int, timeout_seconds: int = 0
    ) -> str | None:
        """Wait for a reply to a specific message.

        Uses a Future that gets resolved when a reply matching the
        message_id is received via the polling handler.
        """
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending_replies[message_id] = future

        try:
            if timeout_seconds > 0:
                return await asyncio.wait_for(future, timeout=timeout_seconds)
            else:
                return await future
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout waiting for reply to message %d in chat %s",
                message_id, chat_id,
            )
            return None
        finally:
            self._pending_replies.pop(message_id, None)

    async def _handle_reply(self, update: Update, context: object) -> None:
        """Handle incoming replies and route to waiting futures."""
        message = update.message
        if not message or not message.reply_to_message:
            return

        original_id = message.reply_to_message.message_id
        future = self._pending_replies.get(original_id)
        if future and not future.done():
            future.set_result(message.text or "")
            logger.info(
                "Received reply to message %d: %s",
                original_id, (message.text or "")[:50],
            )

    async def start_polling(self) -> None:
        """Start the bot's polling loop for receiving replies."""
        self._app = Application.builder().token(self._bot.token).build()
        self._app.add_handler(
            MessageHandler(filters.REPLY & filters.TEXT, self._handle_reply)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("Telegram bot polling started")

    async def stop_polling(self) -> None:
        """Stop the bot's polling loop."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot polling stopped")

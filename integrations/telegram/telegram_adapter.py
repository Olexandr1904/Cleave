"""Telegram adapter implementing NotifierInterface."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
        self._command_handler: Any | None = None

    def set_command_handler(self, handler: Any) -> None:
        """Register the CommandHandler for processing incoming messages."""
        self._command_handler = handler

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
        """Wait for a reply to a specific message."""
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

    async def _handle_incoming(self, update: Update, context: object) -> None:
        """Handle all incoming messages. Routes replies to futures, others to CommandHandler."""
        message = update.message
        if not message or not message.text:
            return

        # If it's a reply to a tracked message, route to the reply future
        if message.reply_to_message:
            original_id = message.reply_to_message.message_id
            future = self._pending_replies.get(original_id)
            if future and not future.done():
                future.set_result(message.text)
                logger.info(
                    "Received reply to message %d: %s",
                    original_id, message.text[:50],
                )
                return

        # Otherwise, route to command handler
        if self._command_handler:
            chat_id = str(message.chat.id)
            try:
                await self._command_handler.handle_message(message.text, chat_id)
            except Exception as e:
                logger.error("Command handler error: %s", e, exc_info=True)

    async def start_polling(self) -> None:
        """Start the bot's polling loop for receiving messages."""
        self._app = Application.builder().token(self._bot.token).build()
        self._app.add_handler(
            MessageHandler(filters.TEXT, self._handle_incoming)
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

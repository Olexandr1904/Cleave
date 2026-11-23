"""Telegram adapter implementing NotifierInterface."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, TypeHandler, filters
from telegram.request import HTTPXRequest

from integrations.base.notifier import Button, NotifierInterface
from orchestrator.tg_translate import Translator, localize

logger = logging.getLogger(__name__)


def _traffic(direction: str, kind: str, /, **fields: Any) -> None:
    """Log one exchange with Telegram as `TG <in|out> <kind> <JSON fields>`. The text is whole and
    JSON-escaped, so an exchange is one line whatever it holds; a field without a value is left out."""
    logger.info("TG %s %s %s", direction, kind, json.dumps(
        {name: value for name, value in fields.items() if value is not None}, ensure_ascii=False, default=str,
    ))


class TelegramAdapter(NotifierInterface):
    """Telegram Bot API adapter using python-telegram-bot."""

    def __init__(self, bot_token: str, event_bus: Any | None = None) -> None:
        # Default python-telegram-bot timeouts (5s) trip easily on slow links
        # or long messages — bump them so transient slowness doesn't bubble up
        # as TimedOut and crash a notification path.
        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=10.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=10.0,
        )
        self._bot = Bot(token=bot_token, request=request)
        self._pending_replies: dict[int, asyncio.Future[str]] = {}
        self._app: Application | None = None
        self._command_handler: Any | None = None
        self._events = event_bus
        # Translates the model-written text a message marks into the current Telegram language (None: kept as is).
        self._translator: Translator | None = None

    def set_translator(self, translator: Translator | None) -> None:
        """Register the translator of the model-written text messages mark (orchestrator/tg_translate.py)."""
        self._translator = translator

    def set_command_handler(self, handler: Any) -> None:
        """Register the CommandHandler for processing incoming messages."""
        self._command_handler = handler

    async def send_message(
        self,
        chat_id: str,
        message: str,
        buttons: list[Button] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Send a message and return the message ID.

        Sends as plain text (no parse_mode) so that user-facing content like
        branch names, BA/QA report excerpts, and LLM-generated replies can
        contain characters such as _, *, and backticks without triggering
        Telegram's Markdown parser and returning 400 Bad Request. Model-written
        text the message marks is translated into the current Telegram language
        first (orchestrator/tg_i18n.py), and the marks removed.
        """
        message = await localize(message, self._translator)
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message,
        }
        if buttons:
            kwargs["reply_markup"] = self._markup(buttons)
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id

        msg = await self._call(
            "send", self._bot.send_message(**kwargs), chat_id=chat_id, reply_to=reply_to_message_id,
            buttons={b.label: b.action for b in buttons} if buttons else None, text=message,
        )
        if self._events:
            self._events.emit("tg_message_sent", f"Sent message to chat {chat_id}: {message[:80]}", data={"chat_id": chat_id, "text_preview": message[:200]})
        return msg.message_id

    async def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        """Edit an existing message (its marked text translated as in send_message)."""
        text = await localize(text, self._translator)
        await self._call(
            "edit",
            self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            ),
            chat_id=chat_id, message_id=message_id, text=text,
        )

    async def set_buttons(self, chat_id: str, message_id: int, buttons: list[Button] | None) -> None:
        """Replace the inline keyboard of a sent message; None or no buttons remove it. A keyboard Telegram finds
        unchanged ("message is not modified") is not an error."""
        try:
            await self._call(
                "buttons",
                self._bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=self._markup(buttons)),
                chat_id=chat_id, message_id=message_id,
                buttons={b.label: b.action for b in buttons} if buttons else None,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
            logger.debug("Buttons of message %s not changed: already as they are", message_id)

    @staticmethod
    def _markup(buttons: list[Button] | None) -> InlineKeyboardMarkup | None:
        """The inline keyboard of `buttons`, one row per Button.row in order of first appearance; None for none."""
        if not buttons:
            return None
        rows: dict[int, list[InlineKeyboardButton]] = {}
        for b in buttons:
            rows.setdefault(b.row, []).append(InlineKeyboardButton(b.label, callback_data=b.action))
        return InlineKeyboardMarkup(list(rows.values()))

    async def send_typing(self, chat_id: str) -> None:
        """Send 'typing...' chat action (visible for ~5 seconds)."""
        await self._bot.send_chat_action(chat_id=chat_id, action="typing")

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        """Delete a message."""
        await self._call(
            "delete", self._bot.delete_message(chat_id=chat_id, message_id=message_id),
            chat_id=chat_id, message_id=message_id,
        )

    async def _call(self, kind: str, api_call: Awaitable[Any], **fields: Any) -> Any:
        """Await a Bot API call and log it as a `TG out` line, with the error when it failed (and raise it)."""
        try:
            result = await api_call
        except Exception as e:
            _traffic("out", kind, **fields, error=f"{type(e).__name__}: {e}")
            raise
        # The id of a sent message is in Telegram's answer; an edit and a delete name theirs.
        _traffic("out", kind, **{"message_id": getattr(result, "message_id", None), **fields})
        return result

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

    async def _log_update(self, update: Update, context: object) -> None:
        """Log an update as a `TG in` line: a text message and a button press by their fields, anything
        else (a photo, a voice message, …) as Telegram sent it. Polling passes every update here before
        it is routed."""
        message, query, user = update.effective_message, update.callback_query, update.effective_user
        if not message or not (query or message.text):
            _traffic("in", "update", **update.to_dict())
            return
        fields = {
            "message_id": message.message_id,
            "chat_id": str(message.chat.id),
            "user": (user.username or user.id) if user else None,
        }
        if query:
            _traffic("in", "callback", **fields, data=query.data)
        else:
            _traffic(
                "in", "edited_message" if update.edited_message else "message", **fields,
                reply_to=message.reply_to_message.message_id if message.reply_to_message else None,
                sent=message.date.isoformat(), text=message.text,
            )

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

            # Check if it's a reply to an escalation message
            if self._command_handler and hasattr(self._command_handler, "handle_reply"):
                chat_id = str(message.chat.id)
                handled = await self._command_handler.handle_reply(
                    original_id, message.text, chat_id, sent_at=message.date,
                )
                if handled:
                    return

        # Otherwise, route to command handler
        if self._command_handler:
            chat_id = str(message.chat.id)
            if self._events:
                self._events.emit("tg_message_received", f"Received from chat {chat_id}: {message.text[:80]}", data={"chat_id": chat_id, "text_preview": message.text[:200]})
            try:
                await self._command_handler.handle_message(message.text, chat_id)
            except Exception as e:
                logger.error("Command handler error: %s", e, exc_info=True)

    async def _handle_callback(self, update: Update, context: object) -> None:
        """Handle inline keyboard button presses."""
        query = update.callback_query
        if not query or not query.data:
            return

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            await query.answer(text="Invalid action")
            return

        action, ticket_id = parts
        chat_id = str(query.message.chat.id)
        message_id = query.message.message_id

        # The answer only stops the spinner on the button. Telegram refuses one that comes late ("Query is too
        # old"); the press itself still stands.
        try:
            await query.answer()
        except TelegramError as e:
            logger.warning("Button press %s not answered, acting on it: %s", query.data, e)

        if self._command_handler and hasattr(self._command_handler, "handle_callback"):
            try:
                await self._command_handler.handle_callback(action, ticket_id, chat_id, message_id)
            except Exception as e:
                logger.error("Callback handler error: %s", e, exc_info=True)

    async def start_polling(self) -> None:
        """Start the bot's polling loop for receiving messages."""
        self._app = Application.builder().token(self._bot.token).build()
        # Group -1 runs before the routing handlers of group 0, and for every update, whatever its type.
        self._app.add_handler(TypeHandler(Update, self._log_update), group=-1)
        self._app.add_handler(
            MessageHandler(filters.TEXT, self._handle_incoming)
        )
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
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

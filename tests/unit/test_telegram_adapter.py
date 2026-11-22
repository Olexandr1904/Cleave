"""Tests for integrations/telegram/telegram_adapter.py — command handler integration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, ExtBot, Updater

from integrations.base.notifier import Button
from integrations.telegram.telegram_adapter import TelegramAdapter


class TestCommandHandlerIntegration:
    def test_set_command_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            adapter.set_command_handler(handler)
            assert adapter._command_handler is handler

    async def test_incoming_non_reply_routes_to_command_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            handler.handle_message = AsyncMock()
            adapter.set_command_handler(handler)

            update = MagicMock()
            update.message.reply_to_message = None
            update.message.text = "what's going on"
            update.message.chat.id = 12345

            await adapter._handle_incoming(update, None)
            handler.handle_message.assert_called_once_with("what's going on", "12345")

    async def test_incoming_reply_routes_to_reply_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            adapter.set_command_handler(handler)

            update = MagicMock()
            update.message.reply_to_message = MagicMock()
            update.message.reply_to_message.message_id = 42
            update.message.text = "yes proceed"
            update.message.chat.id = 12345

            import asyncio
            future = asyncio.get_event_loop().create_future()
            adapter._pending_replies[42] = future

            await adapter._handle_incoming(update, None)
            assert future.result() == "yes proceed"
            handler.handle_message.assert_not_called()


class TestSendMessageWithButtons:
    async def test_send_message_passes_reply_markup(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 99
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            buttons = [
                Button(label="Approve", action="approve:T-1"),
                Button(label="Reject", action="reject:T-1"),
            ]
            msg_id = await adapter.send_message("123", "Gate summary", buttons=buttons)

            assert msg_id == 99
            call_kwargs = bot_instance.send_message.call_args
            markup = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get("reply_markup")
            assert markup is not None
            assert len(markup.inline_keyboard[0]) == 2
            assert markup.inline_keyboard[0][0].text == "Approve"
            assert markup.inline_keyboard[0][0].callback_data == "approve:T-1"

    async def test_buttons_with_rows_make_one_keyboard_row_per_row_in_order(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 98
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            buttons = [
                Button(label="Retry ACME-2", action="retry:ACME-2", row=2),
                Button(label="Skip ACME-2", action="skip:ACME-2", row=2),
                Button(label="Retry ACME-1", action="retry:ACME-1", row=1),
            ]
            await adapter.send_message("123", "Status", buttons=buttons)

            markup = bot_instance.send_message.call_args.kwargs["reply_markup"]
            rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
            assert rows == [["retry:ACME-2", "skip:ACME-2"], ["retry:ACME-1"]]

    async def test_send_message_passes_reply_to_message_id(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 100
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            msg_id = await adapter.send_message("123", "Confirmed", reply_to_message_id=42)

            assert msg_id == 100
            call_kwargs = bot_instance.send_message.call_args
            reply_to = call_kwargs.kwargs.get("reply_to_message_id") or call_kwargs[1].get("reply_to_message_id")
            assert reply_to == 42

    async def test_send_message_without_buttons_sends_no_markup(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 101
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            await adapter.send_message("123", "Plain text")

            call_kwargs = bot_instance.send_message.call_args
            markup = call_kwargs.kwargs.get("reply_markup")
            assert markup is None


class TestCallbackQueryRouting:
    async def test_callback_routes_to_command_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            handler.handle_callback = AsyncMock()
            adapter.set_command_handler(handler)

            update = MagicMock()
            update.callback_query.data = "approve:T-1"
            update.callback_query.message.chat.id = 12345
            update.callback_query.message.message_id = 42
            update.callback_query.answer = AsyncMock()

            await adapter._handle_callback(update, None)

            handler.handle_callback.assert_called_once_with("approve", "T-1", "12345", 42)
            update.callback_query.answer.assert_called_once()

    async def test_a_press_telegram_no_longer_takes_an_answer_for_is_still_acted_on(self, caplog):
        """The operator pressed "Retry now" on a deferred ticket twice in half an hour and nothing happened.
        Answering a press only stops the spinner on the button. Telegram refused the answer as too late, the
        refusal was raised out of the handler, and the press was dropped before the command handler saw it."""
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            handler.handle_callback = AsyncMock()
            adapter.set_command_handler(handler)

            update = MagicMock()
            update.callback_query.data = "retry:ACME-1"
            update.callback_query.message.chat.id = 12345
            update.callback_query.message.message_id = 42
            update.callback_query.answer = AsyncMock(side_effect=BadRequest(
                "Query is too old and response timeout expired or query id is invalid"))

            with caplog.at_level(logging.WARNING, logger="integrations.telegram.telegram_adapter"):
                await adapter._handle_callback(update, None)

            handler.handle_callback.assert_called_once_with("retry", "ACME-1", "12345", 42)
            assert "Query is too old" in caplog.text


class _UkrainianTranslator:
    """Translates every marked text by prefixing it; records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[list[str], str]] = []

    async def translations(self, texts, language):
        self.asked.append((list(texts), language))
        return {t: f"УК {t}" for t in texts}


class TestLocalizedSend:
    async def test_marked_text_is_translated_before_it_is_sent(self):
        from orchestrator.tg_i18n import model_text, telegram_language

        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            bot_instance.send_message = AsyncMock(return_value=MagicMock(message_id=7))
            MockBot.return_value = bot_instance
            events = MagicMock()
            adapter = TelegramAdapter(bot_token="fake-token", event_bus=events)
            translator = _UkrainianTranslator()
            adapter.set_translator(translator)

            with telegram_language("uk"):
                await adapter.send_message("123", f"Причина: {model_text('disk full')}")
                await adapter.edit_message("123", 7, f"Причина: {model_text('timeout')}")

            assert bot_instance.send_message.call_args.kwargs["text"] == "Причина: УК disk full"
            assert bot_instance.edit_message_text.call_args.kwargs["text"] == "Причина: УК timeout"
            assert translator.asked == [(["disk full"], "uk"), (["timeout"], "uk")]
            assert "УК disk full" in events.emit.call_args.kwargs["data"]["text_preview"]

    async def test_plain_text_needs_no_translator(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            bot_instance.send_message = AsyncMock(return_value=MagicMock(message_id=8))
            MockBot.return_value = bot_instance
            adapter = TelegramAdapter(bot_token="fake-token")
            translator = _UkrainianTranslator()
            adapter.set_translator(translator)

            await adapter.send_message("123", "Plain text")

            assert bot_instance.send_message.call_args.kwargs["text"] == "Plain text"
            assert translator.asked == []


_LOGGER = "integrations.telegram.telegram_adapter"
_SENT = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
_CHAT = {"id": 12345, "type": "private"}
_OCTO = {"id": 777, "is_bot": False, "first_name": "Octo", "username": "octo"}


def _traffic(caplog) -> list[tuple[str, str, dict]]:
    """The traffic lines logged, each as (direction, kind, fields)."""
    lines = []
    for record in caplog.records:
        line = record.getMessage()
        if line.startswith("TG "):
            assert "\n" not in line  # one exchange, one line
            _, direction, kind, fields = line.split(" ", 3)
            lines.append((direction, kind, json.loads(fields)))
    return lines


def _message(message_id: int, **fields) -> dict:
    return {"message_id": message_id, "date": int(_SENT.timestamp()), "chat": _CHAT, "from": _OCTO, **fields}


def _adapter(bot: AsyncMock) -> TelegramAdapter:
    with patch("integrations.telegram.telegram_adapter.Bot", return_value=bot):
        return TelegramAdapter(bot_token="fake-token")


class TestOutgoingTrafficLog:
    async def test_a_sent_message_is_logged_whole_on_one_line(self, caplog):
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        text = "[acme/app] #12: review ready\n" + "знахідка " * 60

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(bot).send_message(
                "123", text, buttons=[Button(label="Approve", action="approve:ACME-1")], reply_to_message_id=42,
            )

        assert _traffic(caplog) == [("out", "send", {
            "chat_id": "123", "message_id": 99, "reply_to": 42, "buttons": {"Approve": "approve:ACME-1"},
            "text": text,
        })]

    async def test_a_plain_send_logs_no_empty_fields(self, caplog):
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=101))

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(bot).send_message("123", "Plain text")

        assert _traffic(caplog) == [("out", "send", {"chat_id": "123", "message_id": 101, "text": "Plain text"})]

    async def test_the_logged_text_is_the_translated_one_telegram_got(self, caplog):
        from orchestrator.tg_i18n import model_text, telegram_language

        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
        adapter = _adapter(bot)
        adapter.set_translator(_UkrainianTranslator())

        with caplog.at_level(logging.INFO, logger=_LOGGER), telegram_language("uk"):
            await adapter.send_message("123", f"Причина: {model_text('disk full')}")

        assert _traffic(caplog)[0][2]["text"] == "Причина: УК disk full"

    async def test_a_send_telegram_refused_is_logged_with_its_error_and_raised(self, caplog):
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=BadRequest("Message is too long"))

        with caplog.at_level(logging.INFO, logger=_LOGGER), pytest.raises(BadRequest):
            await _adapter(bot).send_message("123", "Gate summary")

        assert _traffic(caplog) == [("out", "send", {
            "chat_id": "123", "text": "Gate summary", "error": "BadRequest: Message is too long",
        })]

    async def test_an_edit_is_logged_with_its_new_text(self, caplog):
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(AsyncMock()).edit_message("123", 7, "Done.")

        assert _traffic(caplog) == [("out", "edit", {"chat_id": "123", "message_id": 7, "text": "Done."})]

    async def test_a_delete_is_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(AsyncMock()).delete_message("123", 7)

        assert _traffic(caplog) == [("out", "delete", {"chat_id": "123", "message_id": 7})]


class TestIncomingTrafficLog:
    async def test_a_reply_is_logged_whole_with_the_message_it_answers(self, caplog):
        text = "again: leave 3 as it is\n" + "бо так треба " * 30
        update = Update.de_json({"update_id": 1, "message": _message(
            501, text=text, reply_to_message=_message(99, text="[acme/app] #12: review ready"),
        )})

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(AsyncMock())._log_update(update, None)

        assert _traffic(caplog) == [("in", "message", {
            "chat_id": "12345", "message_id": 501, "user": "octo", "reply_to": 99, "sent": _SENT.isoformat(),
            "text": text,
        })]

    async def test_an_edited_message_is_logged_as_edited(self, caplog):
        update = Update.de_json({"update_id": 1, "edited_message": _message(501, text="status")})

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(AsyncMock())._log_update(update, None)

        assert [(direction, kind, fields["text"]) for direction, kind, fields in _traffic(caplog)] == [
            ("in", "edited_message", "status"),
        ]

    async def test_a_button_press_is_logged_with_its_action_and_message(self, caplog):
        update = Update.de_json({"update_id": 1, "callback_query": {
            "id": "1", "from": _OCTO, "chat_instance": "1", "data": "approve:ACME-1",
            "message": _message(99, text="Gate summary"),
        }})

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(AsyncMock())._log_update(update, None)

        assert _traffic(caplog) == [("in", "callback", {
            "chat_id": "12345", "message_id": 99, "user": "octo", "data": "approve:ACME-1",
        })]

    async def test_an_update_without_text_is_logged_as_telegram_sent_it(self, caplog):
        photo = {"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}
        update = Update.de_json({"update_id": 1, "message": _message(502, photo=[photo], caption="see this")})

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _adapter(AsyncMock())._log_update(update, None)

        [(direction, kind, fields)] = _traffic(caplog)
        assert (direction, kind) == ("in", "update")
        assert fields["message"]["caption"] == "see this"
        assert fields["message"]["photo"][0]["file_id"] == "f"

    async def test_polling_logs_an_update_and_still_routes_it(self, caplog):
        adapter = TelegramAdapter(bot_token="123:fake")
        handler = AsyncMock()
        adapter.set_command_handler(handler)
        # Offline: nothing asks Telegram who the bot is, and nothing fetches updates.
        with patch.object(ExtBot, "get_me", AsyncMock()), patch.object(Application, "start", AsyncMock()), \
                patch.object(Updater, "start_polling", AsyncMock()):
            await adapter.start_polling()
        try:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                await adapter._app.process_update(
                    Update.de_json({"update_id": 1, "message": _message(501, text="status")}),
                )
        finally:
            await adapter._app.shutdown()

        assert [(direction, kind) for direction, kind, _ in _traffic(caplog)] == [("in", "message")]
        handler.handle_message.assert_called_once_with("status", "12345")

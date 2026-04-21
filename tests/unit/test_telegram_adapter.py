"""Tests for integrations/telegram/telegram_adapter.py — command handler integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

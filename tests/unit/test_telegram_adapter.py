"""Tests for integrations/telegram/telegram_adapter.py — command handler integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

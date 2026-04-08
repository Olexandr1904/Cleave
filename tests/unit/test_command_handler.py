"""Tests for integrations/telegram/command_handler.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import ParsedIntent


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = None
    ws_state.pr_number = None
    ws_state.started_at = "2026-04-08T00:00:00Z"
    ws_state.last_updated_at = "2026-04-08T01:00:00Z"
    ws_state.stage_iterations = {}
    ws_state.error = None
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestCommandHandler:
    @pytest.fixture
    def mock_intent_parser(self):
        parser = AsyncMock()
        parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="status", params={}, reply="Here is the status",
        ))
        return parser

    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        return notifier

    @pytest.fixture
    def mock_mode_handler(self):
        handler = MagicMock()
        handler.get_mode.return_value = "auto"
        return handler

    @pytest.fixture
    def command_handler(self, mock_intent_parser, mock_notifier, mock_mode_handler):
        return CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            jira_base_url="https://faria.atlassian.net",
            started_at="2026-04-08T00:00:00Z",
        )

    async def test_handle_status_sends_message(self, command_handler, mock_notifier):
        await command_handler.handle_message("what's going on", "12345")
        mock_notifier.send_message.assert_called_once()
        call_args = mock_notifier.send_message.call_args
        assert call_args[0][0] == "12345"

    async def test_handle_set_mode(self, command_handler, mock_intent_parser, mock_notifier, mock_mode_handler):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="set_mode", params={"mode": "manual"}, reply="Switched to manual",
        ))
        await command_handler.handle_message("switch to manual", "12345")
        mock_mode_handler.set_mode.assert_called_once_with("manual")

    async def test_handle_unknown_sends_help(self, command_handler, mock_intent_parser, mock_notifier):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="unknown", params={}, reply="I didn't understand that.",
        ))
        await command_handler.handle_message("gibberish", "12345")
        mock_notifier.send_message.assert_called_once()

    async def test_handle_error_sends_error_message(self, command_handler, mock_intent_parser, mock_notifier):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="error", params={}, reply="I'm having trouble.",
        ))
        await command_handler.handle_message("hello", "12345")
        mock_notifier.send_message.assert_called_once()
        call_text = mock_notifier.send_message.call_args[0][1]
        assert "trouble" in call_text.lower()

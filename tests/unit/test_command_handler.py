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

    async def test_unauthorized_chat_id_is_dropped(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            allowed_chat_ids={"99999"},
        )
        await handler.handle_message("status", "12345")
        mock_intent_parser.parse.assert_not_called()
        mock_notifier.send_message.assert_not_called()

    async def test_authorized_chat_id_is_accepted(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            allowed_chat_ids={"12345"},
        )
        await handler.handle_message("status", "12345")
        mock_intent_parser.parse.assert_called_once()

    async def test_reject_disambiguates_multiple_awaiting(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        ws_a = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        ws_b = _make_workspace("T-2", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="reject", params={}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws_a, ws_b],
        )
        await handler.handle_message("reject it", "12345")
        # Should ask operator to disambiguate, not mark any as FAILED
        ws_a.transition.assert_not_called()
        ws_b.transition.assert_not_called()
        msg = mock_notifier.send_message.call_args[0][1]
        assert "T-1" in msg and "T-2" in msg

    async def test_analyze_dispatches_callback_and_reports(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="analyze", params={"ticket_ids": ["T-1", "T-2"]}, reply="",
        ))
        callback = AsyncMock(return_value={"valid": ["T-1"], "invalid": ["T-2: not found"]})
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            analyze_callback=callback,
        )
        await handler.handle_message("analyze T-1 and T-2", "12345")
        callback.assert_awaited_once_with(["T-1", "T-2"])
        msg = mock_notifier.send_message.call_args[0][1]
        assert "T-1" in msg
        assert "T-2: not found" in msg

    async def test_analyze_without_callback_sends_error(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="analyze", params={"ticket_ids": ["T-1"]}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
        )
        await handler.handle_message("analyze T-1", "12345")
        msg = mock_notifier.send_message.call_args[0][1]
        assert "not available" in msg.lower()

    async def test_status_includes_recent_completions(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            recent_completions_fn=lambda: [("T-DONE", "DONE", 1_700_000_000.0)],
        )
        await handler.handle_message("status", "12345")
        msg = mock_notifier.send_message.call_args[0][1]
        assert "T-DONE" in msg

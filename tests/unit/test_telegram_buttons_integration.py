"""Integration test: button press → adapter → command handler → workspace transition."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from integrations.base.notifier import Button
from integrations.telegram.command_handler import CommandHandler


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.pr_url = None
    ws_state.error = None
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestButtonIntegrationFlow:
    """Traces: user taps button → adapter parses callback → handler transitions workspace."""

    @pytest.fixture
    def notifier(self):
        n = AsyncMock()
        n.send_message = AsyncMock(return_value=1)
        n.edit_message = AsyncMock()
        return n

    async def test_approve_button_flow(self, notifier):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=notifier,
            mode_handler=MagicMock(get_mode=MagicMock(return_value="manual")),
            active_workspaces_fn=lambda: [ws],
        )

        # Simulate what the adapter does when it receives a CallbackQuery
        action, ticket_id = "approve:T-1".split(":", 1)
        await handler.handle_callback(action, ticket_id, "12345", 42)

        ws.transition.assert_called_once_with("DEV")
        # Confirmation sent as reply to button message
        call_kwargs = notifier.send_message.call_args
        assert "Approved" in call_kwargs[0][1]
        assert call_kwargs.kwargs["reply_to_message_id"] == 42

    async def test_reject_button_flow(self, notifier):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="QA")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=notifier,
            mode_handler=MagicMock(get_mode=MagicMock(return_value="manual")),
            active_workspaces_fn=lambda: [ws],
        )

        action, ticket_id = "reject:T-1".split(":", 1)
        await handler.handle_callback(action, ticket_id, "12345", 42)

        ws.transition.assert_called_once_with("FAILED")
        call_kwargs = notifier.send_message.call_args
        assert "Rejected" in call_kwargs[0][1]
        assert call_kwargs.kwargs["reply_to_message_id"] == 42

    async def test_stale_button_press_on_already_approved(self, notifier):
        """Button pressed after workspace already moved past AWAITING_APPROVAL."""
        ws = _make_workspace("T-1", "DEV", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=notifier,
            mode_handler=MagicMock(get_mode=MagicMock(return_value="manual")),
            active_workspaces_fn=lambda: [ws],
        )

        await handler.handle_callback("approve", "T-1", "12345", 42)

        # Should not transition — workspace is no longer in AWAITING_APPROVAL
        ws.transition.assert_not_called()
        call_args = notifier.send_message.call_args[0]
        assert "No workspace" in call_args[1]

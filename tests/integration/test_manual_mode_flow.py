"""Integration test — manual mode flow from mode switch through status request."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.intent_parser import ParsedIntent


class TestManualModeFlow:
    """Verify the full flow: set mode -> status request."""

    @pytest.fixture
    def mode_handler(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        state_path.write_text(
            json.dumps({"mode": "auto", "started_at": "2026-04-08T00:00:00Z"})
        )
        return ModeHandler(state_file_path=str(state_path))

    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        return notifier

    async def test_switch_to_manual_then_status(self, mode_handler, mock_notifier):
        """Full manual mode lifecycle: switch to manual, then request status."""
        intent_sequence = iter(
            [
                ParsedIntent(
                    intent="set_mode",
                    params={"mode": "manual"},
                    reply="Switched to manual.",
                ),
                ParsedIntent(intent="status", params={}, reply="Status"),
            ]
        )
        mock_parser = AsyncMock()
        mock_parser.parse = AsyncMock(side_effect=lambda *a, **k: next(intent_sequence))

        handler = CommandHandler(
            intent_parser=mock_parser,
            notifier=mock_notifier,
            mode_handler=mode_handler,
            active_workspaces_fn=lambda: [],
            jira_base_url="https://faria.atlassian.net",
            started_at="2026-04-08T00:00:00Z",
        )

        # Switch to manual
        await handler.handle_message("switch to manual", "12345")
        assert mode_handler.get_mode() == "manual"

        # Status check
        await handler.handle_message("what's going on", "12345")

        # Both messages should have produced outgoing notifications
        assert mock_notifier.send_message.call_count == 2

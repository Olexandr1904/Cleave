"""Tests for integrations/telegram/handlers/analyze.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.analyze import AnalyzeHandler


class TestAnalyzeHandler:
    @pytest.fixture
    def mock_tracker(self):
        tracker = AsyncMock()
        ticket = MagicMock()
        ticket.id = "MBMOB-123"
        ticket.summary = "Implement user search"
        ticket.url = "https://faria.atlassian.net/browse/MBMOB-123"
        tracker.get_ticket = AsyncMock(return_value=ticket)
        return tracker

    @pytest.fixture
    def handler(self, mock_tracker):
        return AnalyzeHandler(tracker=mock_tracker)

    async def test_validate_tickets_success(self, handler):
        result = await handler.validate_tickets(["MBMOB-123"])
        assert len(result.valid) == 1
        assert result.valid[0].id == "MBMOB-123"
        assert result.invalid == []

    async def test_validate_tickets_not_found(self, handler, mock_tracker):
        mock_tracker.get_ticket = AsyncMock(side_effect=Exception("Not found"))
        result = await handler.validate_tickets(["MBMOB-999"])
        assert result.valid == []
        assert len(result.invalid) == 1
        assert "MBMOB-999" in result.invalid[0]

    async def test_validate_multiple_tickets(self, handler, mock_tracker):
        ticket2 = MagicMock()
        ticket2.id = "MBMOB-456"
        ticket2.summary = "Fix login bug"
        mock_tracker.get_ticket = AsyncMock(side_effect=[
            MagicMock(id="MBMOB-123", summary="Search"),
            Exception("Not found"),
        ])
        result = await handler.validate_tickets(["MBMOB-123", "MBMOB-456"])
        assert len(result.valid) == 1
        assert len(result.invalid) == 1

    def test_is_already_active(self, handler):
        ws = MagicMock()
        ws_state = MagicMock()
        ws_state.ticket_id = "MBMOB-123"
        type(ws).state = PropertyMock(return_value=ws_state)
        active = [ws]
        assert handler.is_already_active("MBMOB-123", active) is True
        assert handler.is_already_active("MBMOB-999", active) is False

"""Tests for integrations/telegram/handlers/approval.py."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.approval import ApprovalHandler


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state == "AWAITING_APPROVAL"
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestApprovalHandler:
    @pytest.fixture
    def handler(self):
        return ApprovalHandler()

    def test_find_awaiting_workspaces(self, handler):
        workspaces = [
            _make_workspace("T-1", "DEV"),
            _make_workspace("T-2", "AWAITING_APPROVAL", "ANALYSIS"),
            _make_workspace("T-3", "QA"),
        ]
        result = handler.find_awaiting(workspaces)
        assert len(result) == 1
        assert result[0].state.ticket_id == "T-2"

    def test_find_awaiting_by_ticket_id(self, handler):
        workspaces = [
            _make_workspace("T-1", "AWAITING_APPROVAL", "ANALYSIS"),
            _make_workspace("T-2", "AWAITING_APPROVAL", "QA"),
        ]
        result = handler.find_awaiting(workspaces, ticket_id="T-2")
        assert len(result) == 1
        assert result[0].state.ticket_id == "T-2"

    def test_resolve_next_state_post_analysis(self, handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", "ANALYSIS")
        next_state = handler.resolve_next_state(ws)
        assert next_state == "DEV"

    def test_resolve_next_state_post_qa(self, handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", "QA")
        next_state = handler.resolve_next_state(ws)
        assert next_state == "PUSHED"

    def test_resolve_next_state_post_pr_review(self, handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", "PR_REVIEW")
        next_state = handler.resolve_next_state(ws)
        assert next_state == "DONE"

    def test_no_awaiting_returns_empty(self, handler):
        workspaces = [_make_workspace("T-1", "DEV")]
        result = handler.find_awaiting(workspaces)
        assert result == []

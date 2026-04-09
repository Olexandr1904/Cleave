"""Tests for integrations/telegram/handlers/status.py."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.status import StatusHandler


def _make_workspace(ticket_id, state, started_at=None, pr_url=None, company_id="test", repo_id="repo"):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.company_id = company_id
    ws_state.repo_id = repo_id
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = pr_url
    ws_state.pr_number = 42 if pr_url else None
    ws_state.started_at = started_at or datetime.now(timezone.utc).isoformat()
    ws_state.last_updated_at = datetime.now(timezone.utc).isoformat()
    ws_state.stage_iterations = {"analysis": 1}
    ws_state.error = None
    ws_state.previous_state = None
    ws_state.human_input_pending = False
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestStatusHandler:
    @pytest.fixture
    def handler(self):
        return StatusHandler(jira_base_url="https://acme.atlassian.net")

    def test_summary_with_active_workspaces(self, handler):
        workspaces = [
            _make_workspace("ACME-123", "DEV"),
            _make_workspace("ACME-456", "QA"),
        ]
        result = handler.format_summary(
            mode="auto",
            uptime_seconds=3600,
            last_poll_ago_seconds=120,
            active_workspaces=workspaces,
            recent_completions=[],
        )
        assert "ACME-123" in result
        assert "ACME-456" in result
        assert "auto" in result.lower()

    def test_summary_with_no_workspaces(self, handler):
        result = handler.format_summary(
            mode="manual",
            uptime_seconds=0,
            last_poll_ago_seconds=0,
            active_workspaces=[],
            recent_completions=[],
        )
        assert "no active" in result.lower() or "Active (0)" in result

    def test_summary_renders_recent_completions(self, handler):
        result = handler.format_summary(
            mode="auto",
            uptime_seconds=0,
            last_poll_ago_seconds=0,
            active_workspaces=[],
            recent_completions=[
                ("ACME-1", "DONE", 1_700_000_000.0),
                ("ACME-2", "FAILED", 1_700_000_100.0),
            ],
        )
        assert "ACME-1" in result
        assert "merged" in result
        assert "ACME-2" in result
        assert "failed" in result

    def test_drill_down_includes_jira_url(self, handler):
        ws = _make_workspace("ACME-123", "DEV")
        result = handler.format_drill_down(ws)
        assert "https://acme.atlassian.net/browse/ACME-123" in result

    def test_drill_down_includes_pr_url_when_present(self, handler):
        ws = _make_workspace("ACME-123", "PR_REVIEW", pr_url="https://github.com/org/repo/pull/42")
        result = handler.format_drill_down(ws)
        assert "https://github.com/org/repo/pull/42" in result

    def test_drill_down_no_pr_url_when_absent(self, handler):
        ws = _make_workspace("ACME-123", "DEV")
        result = handler.format_drill_down(ws)
        assert "PR:" not in result

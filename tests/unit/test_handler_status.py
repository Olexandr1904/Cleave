"""Tests for integrations/telegram/handlers/status.py."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.status import StatusHandler, status_buttons
from workspace.workspace import Stage


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
    ws_state.human_input_question = None
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

    def test_drill_down_blocked_shows_blocked_on(self, handler):
        ws = _make_workspace("ACME-1", "BLOCKED")
        ws.state.human_input_question = (
            "## Questions for Human Review\n\n"
            "1. [AC2] What error types must be handled?\n"
        )
        ws.state.error = None

        result = handler.format_drill_down(ws)

        assert "Blocked on:" in result
        assert "[AC2]" in result
        assert "Last error: none" not in result

    def test_drill_down_blocked_truncates_long_reason(self, handler):
        ws = _make_workspace("ACME-1", "BLOCKED")
        ws.state.human_input_question = "x" * 2000
        ws.state.error = None

        result = handler.format_drill_down(ws)

        assert "Blocked on:" in result
        # 500 chars of x + ellipsis marker
        assert "…" in result

    def test_drill_down_blocked_without_question_falls_back_to_error(self, handler):
        ws = _make_workspace("ACME-1", "BLOCKED")
        ws.state.human_input_question = None
        ws.state.error = "some error"

        result = handler.format_drill_down(ws)

        assert "Last error: some error" in result
        assert "Blocked on:" not in result

    def test_drill_down_non_blocked_with_error_still_shows_last_error(self, handler):
        ws = _make_workspace("ACME-1", "FAILED")
        ws.state.human_input_question = "should not appear"
        ws.state.error = "timed out"

        result = handler.format_drill_down(ws)

        assert "Last error: timed out" in result
        assert "Blocked on:" not in result


class TestStatusPanel:
    """The summary says per ticket where it stopped, why, and what Cleave waits for; it showed "BLOCKED, blocked"."""

    @pytest.fixture
    def handler(self):
        return StatusHandler()

    def _summary(self, handler, *workspaces):
        return handler.format_summary(mode="manual", uptime_seconds=0, last_poll_ago_seconds=0,
                                      active_workspaces=list(workspaces), recent_completions=[])

    def _ws(self, ticket_id, state, previous_state=None, **fields):
        ws = _make_workspace(ticket_id, state)
        ws.state.previous_state = previous_state
        ws.state.retry_at = None
        for name, value in fields.items():
            setattr(ws.state, name, value)
        return ws

    def test_a_blocked_ticket_names_its_stage_its_question_and_the_reply_it_waits_for(self, handler):
        ws = self._ws("ACME-1", "BLOCKED", "SCOPE_CHECK",
                      human_input_question="\nThe plan forbids config/baseline.xml.\nSecond line.")
        lines = self._summary(handler, ws).splitlines()
        i = lines.index("  ACME-1 — blocked at Code Check")
        assert lines[i + 1:i + 3] == ["     The plan forbids config/baseline.xml.",
                                      "     Waits for: your reply to its message, or a button below"]

    def test_a_blocked_ticket_without_a_question_shows_its_error(self, handler):
        ws = self._ws("ACME-1", "BLOCKED", "PUSHED", error="push refused\nmore")
        assert "     push refused" in self._summary(handler, ws).splitlines()

    def test_a_long_reason_is_cut_with_an_ellipsis(self, handler):
        ws = self._ws("ACME-1", "FAILED", "DEV", error="x" * 300)
        line = next(line for line in self._summary(handler, ws).splitlines() if line.startswith("     x"))
        assert line == "     " + "x" * 160 + "…"

    def test_a_failed_ticket(self, handler):
        ws = self._ws("ACME-1", "FAILED", "QA", error="gate could not run")
        lines = self._summary(handler, ws).splitlines()
        i = lines.index("  ACME-1 — failed at QA")
        assert lines[i + 1:i + 3] == ["     gate could not run", "     Waits for: a button below"]

    def test_a_deferred_ticket_says_when_it_retries_by_itself(self, handler):
        ws = self._ws("ACME-1", "DEFERRED", "QA", retry_at="2026-11-20T13:10:00+00:00")
        lines = self._summary(handler, ws).splitlines()
        i = lines.index("  ACME-1 — deferred at QA")
        assert lines[i + 1] == "     Retries by itself at 13:10 UTC"

    def test_a_ticket_at_a_gate_waits_for_the_buttons_on_its_message(self, handler):
        ws = self._ws("ACME-1", "AWAITING_APPROVAL", "PLAN_REVIEW")
        lines = self._summary(handler, ws).splitlines()
        i = lines.index("  ACME-1 — awaiting approval after Plan Review")
        assert lines[i + 1] == "     Waits for: Approve or Reject on its message"

    def test_a_running_ticket_takes_one_line(self, handler):
        lines = self._summary(handler, self._ws("ACME-1", "DEV")).splitlines()
        i = next(n for n, line in enumerate(lines) if line.startswith("  ACME-1"))
        assert not lines[i + 1].startswith("     ")

    def test_buttons_give_each_stopped_ticket_its_own_row(self, handler):
        workspaces = [self._ws("ACME-1", "BLOCKED", "DEV"), self._ws("ACME-2", "DEV"),
                      self._ws("ACME-3", "DEFERRED", "QA"), self._ws("ACME-4", "FAILED", "QA"),
                      self._ws("ACME-5", "AWAITING_APPROVAL", "PLAN_REVIEW")]
        buttons = status_buttons(workspaces)
        rows: dict[int, list[str]] = {}
        for b in buttons:
            rows.setdefault(b.row, []).append(b.action)
        assert list(rows.values()) == [
            ["retry:ACME-1", "retry_pick:ACME-1", "skip:ACME-1"],
            ["retry:ACME-3", "retry_pick:ACME-3", "skip:ACME-3"],
            ["retry:ACME-4", "retry_pick:ACME-4", "skip:ACME-4"],
        ]
        assert [b.label for b in buttons[:3]] == ["Retry ACME-1", "From stage…", "Skip"]

    def test_no_stopped_ticket_no_buttons(self, handler):
        assert status_buttons([self._ws("ACME-2", "DEV")]) == []

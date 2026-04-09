"""Tests for orchestrator mode-aware behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.stage_iterations = {}
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.error = None
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = None
    ws_state.pr_number = None
    type(ws).state = PropertyMock(return_value=ws_state)
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock(return_value=1)
    ws.reports_dir = MagicMock()
    return ws


def _make_orchestrator(mode="auto"):
    global_config = MagicMock()
    global_config.telegram.default_chat_id = "12345"
    global_config.defaults.poll_interval_seconds = 300
    global_config.workspaces.max_age_days = 14
    global_config.pipeline.mode = mode

    workflow = MagicMock()
    workspace_manager = MagicMock()
    workspace_manager.discover_workspaces.return_value = []
    workspace_manager.cleanup_old_workspaces.return_value = []

    orch = Orchestrator(
        global_config=global_config,
        projects={},
        registry=MagicMock(),
        workflow=workflow,
        workspace_manager=workspace_manager,
        agent_runtime=MagicMock(),
        tracker=AsyncMock(),
        notifier=AsyncMock(),
    )
    return orch


class TestModeAwarePollCycle:
    async def test_manual_mode_skips_jira_polling(self):
        orch = _make_orchestrator(mode="manual")
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        orch._tracker = AsyncMock()

        await orch.poll_cycle()
        orch._tracker.poll_tickets.assert_not_called()

    async def test_auto_mode_polls_jira(self):
        orch = _make_orchestrator(mode="auto")
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "auto"
        orch._projects = {"test": MagicMock()}
        orch._projects["test"].config.jira.url = "https://jira.example.com"
        orch._tracker.poll_tickets.return_value = []

        await orch.poll_cycle()
        # Tracker is called because mode is auto
        assert orch._tracker.poll_tickets.called


class TestApprovalGates:
    def _manual(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        return orch

    def _auto(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "auto"
        return orch

    # Legacy single-arg form: gate fires for every gate state in manual mode.
    def test_should_gate_returns_true_for_analysis_in_manual(self):
        assert self._manual()._should_approval_gate("ANALYSIS") is True

    def test_should_gate_returns_true_for_qa_in_manual(self):
        assert self._manual()._should_approval_gate("QA") is True

    def test_should_gate_returns_true_for_pr_review_in_manual(self):
        assert self._manual()._should_approval_gate("PR_REVIEW") is True

    def test_should_gate_returns_false_in_auto(self):
        assert self._auto()._should_approval_gate("ANALYSIS") is False

    def test_should_gate_returns_false_for_dev(self):
        assert self._manual()._should_approval_gate("DEV") is False

    # Two-arg form: gate only fires on happy-path transitions.
    def test_gate_analysis_to_dev_happy_path_fires(self):
        assert self._manual()._should_approval_gate("ANALYSIS", "dev") is True

    def test_gate_analysis_to_escalate_bypasses(self):
        """Unclear analysis escalates — gate must NOT fire."""
        assert self._manual()._should_approval_gate("ANALYSIS", "escalate") is False

    def test_gate_qa_to_push_happy_path_fires(self):
        assert self._manual()._should_approval_gate("QA", "push") is True

    def test_gate_qa_to_dev_failure_loop_bypasses(self):
        """QA fail loops back to dev — gate must NOT fire."""
        assert self._manual()._should_approval_gate("QA", "dev") is False

    def test_gate_pr_review_to_done_happy_path_fires(self):
        assert self._manual()._should_approval_gate("PR_REVIEW", "done") is True

    def test_gate_pr_review_to_dev_bypasses(self):
        """PR review fix-required goes back to dev — gate must NOT fire."""
        assert self._manual()._should_approval_gate("PR_REVIEW", "dev") is False

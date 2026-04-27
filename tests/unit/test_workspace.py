"""Tests for workspace/workspace.py — Workspace object and state management."""

from __future__ import annotations

import json

import pytest

from workspace.workspace import (
    InvalidTransitionError,
    Stage,
    Workspace,
    WorkspaceState,
    VALID_STATES,
    VALID_TRANSITIONS,
)


@pytest.fixture
def workspace_dir(tmp_path):
    """Create a temporary workspace directory structure."""
    ws_root = tmp_path / "test-workspace"
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()
    return ws_root


@pytest.fixture
def workspace(workspace_dir):
    """Create a Workspace with initial state."""
    state = WorkspaceState(
        ticket_id="TEST-123",
        company_id="test-company",
        repo_id="test-repo",
        workspace_root=str(workspace_dir),
    )
    ws = Workspace(str(workspace_dir), state)
    ws.save_state()
    return ws


class TestWorkspaceState:
    def test_defaults(self):
        state = WorkspaceState(
            ticket_id="T-1",
            company_id="c",
            repo_id="r",
            workspace_root="/tmp/ws",
        )
        assert state.current_state == Stage.NEW
        assert state.previous_state is None
        assert state.branch is None
        assert state.pr_number is None
        assert state.human_input_pending is False
        assert state.stage_iterations == {}
        assert state.started_at != ""
        assert state.last_updated_at != ""

    def test_auto_timestamps(self):
        state = WorkspaceState(
            ticket_id="T-1", company_id="c", repo_id="r", workspace_root="/tmp"
        )
        assert state.started_at == state.last_updated_at


class TestWorkspace:
    def test_directory_properties(self, workspace):
        assert workspace.source_dir.name == "source"
        assert workspace.meta_dir.name == "meta"
        assert workspace.reports_dir.name == "reports"
        assert workspace.logs_dir.name == "logs"

    def test_save_and_load_state(self, workspace):
        """Atomic write via temp file + rename."""
        workspace.update_state(current_state=Stage.ANALYSIS)

        # Reload from disk
        ws2 = Workspace(str(workspace.root))
        assert ws2.state.current_state == Stage.ANALYSIS
        assert ws2.state.ticket_id == "TEST-123"

    def test_state_json_format(self, workspace):
        """state.json has all required fields."""
        data = json.loads(workspace.state_path.read_text())
        required_fields = [
            "ticket_id", "company_id", "repo_id", "workspace_root",
            "branch", "pr_number", "current_state", "previous_state",
            "stage_iterations", "human_input_pending",
            "started_at", "last_updated_at",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_update_state_updates_timestamp(self, workspace):
        old_ts = workspace.state.last_updated_at
        workspace.update_state(current_state=Stage.ANALYSIS)
        assert workspace.state.last_updated_at >= old_ts

    def test_update_unknown_field_raises(self, workspace):
        with pytest.raises(ValueError, match="Unknown state field"):
            workspace.update_state(nonexistent_field="value")


class TestStateTransitions:
    def test_new_to_analysis(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        assert workspace.state.current_state == Stage.ANALYSIS

    def test_analysis_to_dev(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV

    def test_dev_to_scope_check(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        assert workspace.state.current_state == Stage.SCOPE_CHECK

    def test_scope_check_pass_to_qa(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        assert workspace.state.current_state == Stage.QA

    def test_scope_check_fail_to_dev(self, workspace):
        """Scope violations send back to DEV."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV

    def test_qa_pass_to_pushed(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        assert workspace.state.current_state == Stage.PUSHED

    def test_qa_fail_to_dev(self, workspace):
        """Test failures send back to DEV."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV

    def test_pushed_to_pr_review(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        assert workspace.state.current_state == Stage.PR_REVIEW

    def test_pr_review_fix_to_dev(self, workspace):
        """PR comments requiring fixes send back to DEV."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV

    def test_pr_review_to_done(self, workspace):
        """Happy path completion."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        workspace.transition(Stage.DONE)
        assert workspace.state.current_state == Stage.DONE

    def test_done_to_archived(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        workspace.transition(Stage.DONE)
        workspace.transition(Stage.ARCHIVED)
        assert workspace.state.current_state == Stage.ARCHIVED

    def test_any_stage_to_failed(self, workspace):
        """Every non-terminal state can transition to FAILED."""
        for state in [Stage.NEW, Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.BLOCKED]:
            ws_root = workspace.root
            s = WorkspaceState(
                ticket_id="T-1", company_id="c", repo_id="r",
                workspace_root=str(ws_root), current_state=state,
            )
            ws = Workspace(str(ws_root), s)
            ws.save_state()
            ws.transition(Stage.FAILED)
            assert ws.state.current_state == Stage.FAILED

    def test_invalid_transition_raises(self, workspace):
        """Terminal states cannot transition."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        workspace.transition(Stage.DONE)
        workspace.transition(Stage.ARCHIVED)
        with pytest.raises(InvalidTransitionError, match="Cannot transition"):
            workspace.transition(Stage.NEW)

    def test_new_to_done_invalid(self, workspace):
        with pytest.raises(InvalidTransitionError):
            workspace.transition(Stage.DONE)

    def test_unknown_state_raises(self, workspace):
        with pytest.raises(InvalidTransitionError, match="Unknown state"):
            workspace.transition("bogus")

    def test_all_valid_transitions(self, workspace_dir):
        """Verify every defined transition works."""
        for from_state, to_states in VALID_TRANSITIONS.items():
            for to_state in to_states:
                state = WorkspaceState(
                    ticket_id="T-1",
                    company_id="c",
                    repo_id="r",
                    workspace_root=str(workspace_dir),
                    current_state=from_state,
                )
                ws = Workspace(str(workspace_dir), state)
                ws.save_state()
                ws.transition(to_state)
                assert ws.state.current_state == to_state


class TestBlockedResume:
    def test_blocked_stores_previous_state(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.BLOCKED)
        assert workspace.state.current_state == Stage.BLOCKED
        assert workspace.state.previous_state == Stage.DEV
        assert workspace.state.human_input_pending is True

    def test_resume_from_blocked(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.BLOCKED)
        # Human replies, resume to SCOPE_CHECK (next step after DEV)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV
        assert workspace.state.previous_state is None
        assert workspace.state.human_input_pending is False

    def test_blocked_from_multiple_stages(self, workspace_dir):
        """BLOCKED can be entered from any non-terminal state (except NEW)."""
        blockable = ["ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW"]
        for state in blockable:
            s = WorkspaceState(
                ticket_id="T-1", company_id="c", repo_id="r",
                workspace_root=str(workspace_dir), current_state=state,
            )
            ws = Workspace(str(workspace_dir), s)
            ws.save_state()
            ws.transition(Stage.BLOCKED)
            assert ws.state.current_state == Stage.BLOCKED
            assert ws.state.previous_state == state


class TestAwaitingApproval:
    def test_awaiting_approval_in_valid_states(self):
        assert "AWAITING_APPROVAL" in VALID_STATES

    def test_analysis_to_awaiting_approval(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.AWAITING_APPROVAL)
        assert workspace.state.current_state == Stage.AWAITING_APPROVAL
        assert workspace.state.previous_state == Stage.ANALYSIS

    def test_qa_to_awaiting_approval(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.AWAITING_APPROVAL)
        assert workspace.state.current_state == Stage.AWAITING_APPROVAL

    def test_pr_review_to_awaiting_approval(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        workspace.transition(Stage.AWAITING_APPROVAL)
        assert workspace.state.current_state == Stage.AWAITING_APPROVAL

    def test_awaiting_approval_to_dev(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.AWAITING_APPROVAL)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV
        assert workspace.state.previous_state is None

    def test_awaiting_approval_to_pushed(self, workspace):
        """Post-QA approval resumes to PUSHED."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.AWAITING_APPROVAL)
        workspace.transition(Stage.PUSHED)
        assert workspace.state.current_state == Stage.PUSHED

    def test_awaiting_approval_to_done(self, workspace):
        """Post-PR_REVIEW approval finalizes."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.PUSHED)
        workspace.transition(Stage.PR_REVIEW)
        workspace.transition(Stage.AWAITING_APPROVAL)
        workspace.transition(Stage.DONE)
        assert workspace.state.current_state == Stage.DONE

    def test_awaiting_approval_to_failed(self, workspace):
        """Rejection moves to FAILED."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.AWAITING_APPROVAL)
        workspace.transition(Stage.FAILED)
        assert workspace.state.current_state == Stage.FAILED

    def test_awaiting_approval_stores_previous_state(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.AWAITING_APPROVAL)
        assert workspace.state.previous_state == Stage.ANALYSIS
        assert workspace.state.human_input_pending is True

    def test_resume_from_awaiting_approval_clears_pending(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.AWAITING_APPROVAL)
        workspace.transition(Stage.DEV)
        assert workspace.state.human_input_pending is False
        assert workspace.state.previous_state is None

    def test_new_to_awaiting_approval_invalid(self, workspace):
        with pytest.raises(InvalidTransitionError):
            workspace.transition(Stage.AWAITING_APPROVAL)


class TestManualControlState:
    def test_manual_control_in_valid_states(self):
        from workspace.workspace import VALID_STATES
        assert "MANUAL_CONTROL" in VALID_STATES

    def test_transition_dev_to_manual_control(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.MANUAL_CONTROL)
        assert workspace.state.current_state == Stage.MANUAL_CONTROL
        assert workspace.state.previous_state == Stage.DEV

    def test_transition_manual_control_to_analysis(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.MANUAL_CONTROL)
        workspace.transition(Stage.ANALYSIS)
        assert workspace.state.current_state == Stage.ANALYSIS

    def test_manual_control_cannot_go_to_done(self, workspace):
        from workspace.workspace import InvalidTransitionError
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.MANUAL_CONTROL)
        with pytest.raises(InvalidTransitionError):
            workspace.transition(Stage.DONE)

    def test_manual_control_fields_default_none(self, workspace):
        assert workspace.state.manual_control_started_at is None
        assert workspace.state.manual_control_comment is None

    def test_all_active_states_can_reach_manual_control(self):
        from workspace.workspace import VALID_TRANSITIONS
        active_states = {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED",
                         "PR_REVIEW", "BLOCKED", "AWAITING_APPROVAL"}
        for state in active_states:
            assert "MANUAL_CONTROL" in VALID_TRANSITIONS[state], (
                f"{state} cannot transition to MANUAL_CONTROL"
            )

    def test_manual_control_stores_previous_state_and_pending(self, workspace):
        """Entering MANUAL_CONTROL preserves previous_state and sets human_input_pending."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.MANUAL_CONTROL)
        assert workspace.state.previous_state == Stage.ANALYSIS
        assert workspace.state.human_input_pending is True

    def test_exit_manual_control_clears_pending_and_previous(self, workspace):
        """Leaving MANUAL_CONTROL clears human_input_pending and previous_state."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.MANUAL_CONTROL)
        workspace.transition(Stage.ANALYSIS)
        assert workspace.state.human_input_pending is False
        assert workspace.state.previous_state is None

    def test_blocked_to_manual_control(self, workspace):
        """Can take control from BLOCKED state."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.BLOCKED)
        workspace.transition(Stage.MANUAL_CONTROL)
        assert workspace.state.current_state == Stage.MANUAL_CONTROL
        assert workspace.state.previous_state == Stage.BLOCKED

    def test_awaiting_approval_to_manual_control(self, workspace):
        """Can take control from AWAITING_APPROVAL state."""
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.AWAITING_APPROVAL)
        workspace.transition(Stage.MANUAL_CONTROL)
        assert workspace.state.current_state == Stage.MANUAL_CONTROL
        assert workspace.state.previous_state == Stage.AWAITING_APPROVAL


class TestDeferred:
    def test_deferred_in_valid_states(self):
        assert "DEFERRED" in VALID_STATES

    def test_retry_at_field_defaults_none(self):
        state = WorkspaceState(
            ticket_id="T-1", company_id="c", repo_id="r", workspace_root="/tmp"
        )
        assert state.retry_at is None

    def test_dev_to_deferred(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.DEFERRED, retry_at="2026-04-14T20:00:00+00:00")
        assert workspace.state.current_state == Stage.DEFERRED
        assert workspace.state.previous_state == Stage.DEV
        assert workspace.state.retry_at == "2026-04-14T20:00:00+00:00"

    def test_qa_to_deferred(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.SCOPE_CHECK)
        workspace.transition(Stage.QA)
        workspace.transition(Stage.DEFERRED, retry_at="2026-04-14T20:00:00+00:00")
        assert workspace.state.current_state == Stage.DEFERRED
        assert workspace.state.previous_state == Stage.QA

    def test_resume_from_deferred_clears_retry_at(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.DEFERRED, retry_at="2026-04-14T20:00:00+00:00")
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV
        assert workspace.state.previous_state is None
        assert workspace.state.retry_at is None

    def test_deferred_to_failed_allowed(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.DEFERRED, retry_at="2026-04-14T20:00:00+00:00")
        workspace.transition(Stage.FAILED)
        assert workspace.state.current_state == Stage.FAILED
        assert workspace.state.retry_at is None


class TestFailedRecoverable:
    def test_failed_records_previous_state(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.FAILED)
        assert workspace.state.previous_state == Stage.DEV

    def test_retry_from_failed_to_dev(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.FAILED)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV
        assert workspace.state.previous_state is None

    def test_failed_to_archived(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.FAILED)
        workspace.transition(Stage.ARCHIVED)
        assert workspace.state.current_state == Stage.ARCHIVED

    def test_failed_to_done_rejected(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.FAILED)
        with pytest.raises(InvalidTransitionError):
            workspace.transition(Stage.DONE)

    def test_retry_from_failed_clears_human_input_pending(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.FAILED)
        assert workspace.state.human_input_pending is True
        workspace.transition(Stage.DEV)
        assert workspace.state.human_input_pending is False


class TestIterations:
    def test_increment_iteration(self, workspace):
        count = workspace.increment_iteration("SCOPE_CHECK")
        assert count == 1
        count = workspace.increment_iteration("SCOPE_CHECK")
        assert count == 2

        # Persisted to disk
        ws2 = Workspace(str(workspace.root))
        assert ws2.state.stage_iterations["SCOPE_CHECK"] == 2

    def test_independent_counters(self, workspace):
        workspace.increment_iteration("SCOPE_CHECK")
        workspace.increment_iteration("SCOPE_CHECK")
        workspace.increment_iteration("QA")
        assert workspace.state.stage_iterations["SCOPE_CHECK"] == 2
        assert workspace.state.stage_iterations["QA"] == 1


class TestPaused:
    def test_paused_in_valid_states(self):
        assert "PAUSED" in VALID_STATES

    def test_dev_to_paused(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        assert workspace.state.current_state == Stage.PAUSED
        assert workspace.state.previous_state == Stage.DEV
        assert workspace.state.human_input_pending is True

    def test_paused_from_each_active_stage(self, workspace_dir):
        active = ["ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW"]
        for state in active:
            s = WorkspaceState(
                ticket_id="T-1", company_id="c", repo_id="r",
                workspace_root=str(workspace_dir), current_state=state,
            )
            ws = Workspace(str(workspace_dir), s)
            ws.save_state()
            ws.transition(Stage.PAUSED)
            assert ws.state.current_state == Stage.PAUSED
            assert ws.state.previous_state == state

    def test_paused_rejected_from_invalid_states(self, workspace_dir):
        for state in ["NEW", "BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL",
                      "DEFERRED", "DONE", "FAILED", "ARCHIVED"]:
            s = WorkspaceState(
                ticket_id="T-1", company_id="c", repo_id="r",
                workspace_root=str(workspace_dir), current_state=state,
            )
            ws = Workspace(str(workspace_dir), s)
            ws.save_state()
            with pytest.raises(InvalidTransitionError):
                ws.transition(Stage.PAUSED)

    def test_unpause_returns_to_previous_active_stage(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV
        assert workspace.state.previous_state is None
        assert workspace.state.human_input_pending is False

    def test_paused_to_failed_allowed(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        workspace.transition(Stage.FAILED)
        assert workspace.state.current_state == Stage.FAILED

    def test_paused_to_manual_control_allowed(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        workspace.transition(Stage.MANUAL_CONTROL)
        assert workspace.state.current_state == Stage.MANUAL_CONTROL

    def test_paused_to_done_rejected(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        with pytest.raises(InvalidTransitionError):
            workspace.transition(Stage.DONE)

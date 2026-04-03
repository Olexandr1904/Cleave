"""Tests for workspace/workspace.py — Workspace object and state management."""

from __future__ import annotations

import json

import pytest

from workspace.workspace import (
    InvalidTransitionError,
    Workspace,
    WorkspaceState,
    VALID_TRANSITIONS,
)


@pytest.fixture
def workspace_dir(tmp_path):
    """Create a temporary workspace directory structure."""
    ws_root = tmp_path / "test-workspace"
    ws_root.mkdir()
    (ws_root / "context").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "repo").mkdir()
    return ws_root


@pytest.fixture
def workspace(workspace_dir):
    """Create a Workspace with initial state."""
    state = WorkspaceState(
        ticket_id="TEST-123",
        project_id="test-project",
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
            project_id="p",
            repo_id="r",
            workspace_root="/tmp/ws",
        )
        assert state.status == "pending"
        assert state.current_stage == "pending"
        assert state.branch is None
        assert state.pr_number is None
        assert state.human_input_pending is False
        assert state.stage_iterations == {}
        assert state.started_at != ""
        assert state.last_updated_at != ""

    def test_auto_timestamps(self):
        state = WorkspaceState(
            ticket_id="T-1", project_id="p", repo_id="r", workspace_root="/tmp"
        )
        assert state.started_at == state.last_updated_at


class TestWorkspace:
    def test_directory_properties(self, workspace):
        assert workspace.repo_dir.name == "repo"
        assert workspace.context_dir.name == "context"
        assert workspace.logs_dir.name == "logs"

    def test_save_and_load_state(self, workspace):
        """AC4 (2.2): Atomic write via temp file + rename."""
        workspace.update_state(current_stage="ba_agent", status="running")

        # Reload from disk
        ws2 = Workspace(str(workspace.root))
        assert ws2.state.current_stage == "ba_agent"
        assert ws2.state.status == "running"
        assert ws2.state.ticket_id == "TEST-123"

    def test_state_json_format(self, workspace):
        """AC1 (2.2): state.json has all required fields."""
        data = json.loads(workspace.state_path.read_text())
        required_fields = [
            "ticket_id", "project_id", "repo_id", "workspace_root",
            "branch", "pr_number", "current_stage", "stage_iterations",
            "human_input_pending", "started_at", "last_updated_at", "status",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_update_state_updates_timestamp(self, workspace):
        old_ts = workspace.state.last_updated_at
        workspace.update_state(current_stage="dev_agent")
        # Timestamp should be updated (or same if very fast)
        assert workspace.state.last_updated_at >= old_ts

    def test_update_unknown_field_raises(self, workspace):
        with pytest.raises(ValueError, match="Unknown state field"):
            workspace.update_state(nonexistent_field="value")


class TestStateTransitions:
    def test_pending_to_running(self, workspace):
        """AC2 (2.2): pending -> running is valid."""
        workspace.transition_status("running")
        assert workspace.state.status == "running"

    def test_running_to_waiting(self, workspace):
        workspace.transition_status("running")
        workspace.transition_status("waiting_for_human")
        assert workspace.state.status == "waiting_for_human"

    def test_waiting_to_running(self, workspace):
        workspace.transition_status("running")
        workspace.transition_status("waiting_for_human")
        workspace.transition_status("running")
        assert workspace.state.status == "running"

    def test_running_to_completed(self, workspace):
        workspace.transition_status("running")
        workspace.transition_status("completed")
        assert workspace.state.status == "completed"

    def test_running_to_failed(self, workspace):
        workspace.transition_status("running")
        workspace.transition_status("failed")
        assert workspace.state.status == "failed"

    def test_invalid_transition_raises(self, workspace):
        """AC3 (2.2): Invalid transitions raise error."""
        workspace.transition_status("running")
        workspace.transition_status("completed")
        with pytest.raises(InvalidTransitionError, match="Cannot transition"):
            workspace.transition_status("running")

    def test_pending_to_completed_invalid(self, workspace):
        with pytest.raises(InvalidTransitionError):
            workspace.transition_status("completed")

    def test_unknown_status_raises(self, workspace):
        with pytest.raises(InvalidTransitionError, match="Unknown status"):
            workspace.transition_status("bogus")

    def test_all_valid_transitions(self, workspace_dir):
        """Verify all defined transitions work."""
        for from_status, to_statuses in VALID_TRANSITIONS.items():
            for to_status in to_statuses:
                state = WorkspaceState(
                    ticket_id="T-1",
                    project_id="p",
                    repo_id="r",
                    workspace_root=str(workspace_dir),
                    status=from_status,
                )
                ws = Workspace(str(workspace_dir), state)
                ws.save_state()
                ws.transition_status(to_status)
                assert ws.state.status == to_status


class TestIterations:
    def test_increment_iteration(self, workspace):
        """AC5 (2.2): stage_iterations tracks per-agent counts."""
        count = workspace.increment_iteration("scope_guard")
        assert count == 1
        count = workspace.increment_iteration("scope_guard")
        assert count == 2

        # Persisted to disk
        ws2 = Workspace(str(workspace.root))
        assert ws2.state.stage_iterations["scope_guard"] == 2

    def test_independent_counters(self, workspace):
        workspace.increment_iteration("scope_guard")
        workspace.increment_iteration("scope_guard")
        workspace.increment_iteration("fix_agent")
        assert workspace.state.stage_iterations["scope_guard"] == 2
        assert workspace.state.stage_iterations["fix_agent"] == 1

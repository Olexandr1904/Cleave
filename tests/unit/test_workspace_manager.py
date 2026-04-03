"""Tests for workspace/workspace_manager.py — workspace creation, discovery, cleanup."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from workspace.workspace import Workspace, WorkspaceState
from workspace.workspace_manager import WorkspaceManager, WorkspaceError


@pytest.fixture
def base_dir(tmp_path):
    return tmp_path / "workspaces"


@pytest.fixture
def manager(base_dir):
    return WorkspaceManager(str(base_dir))


def _create_fake_workspace(
    base_dir: Path,
    project_id: str = "proj",
    repo_id: str = "repo",
    ticket_id: str = "T-1",
    status: str = "running",
    days_ago: int = 0,
) -> Path:
    """Create a fake workspace directory with state.json."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    ws_name = f"{ticket_id}_{ts.strftime('%Y%m%d_%H%M%S')}"
    ws_root = base_dir / project_id / repo_id / ws_name
    ws_root.mkdir(parents=True)
    (ws_root / "context").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "repo").mkdir()

    state = {
        "ticket_id": ticket_id,
        "project_id": project_id,
        "repo_id": repo_id,
        "workspace_root": str(ws_root),
        "branch": None,
        "pr_number": None,
        "pr_url": None,
        "current_stage": "dev_agent",
        "stage_iterations": {},
        "human_input_pending": False,
        "human_input_question": None,
        "human_input_reply": None,
        "started_at": ts.isoformat(),
        "last_updated_at": ts.isoformat(),
        "status": status,
        "error": None,
    }
    (ws_root / "state.json").write_text(json.dumps(state, indent=2))
    return ws_root


class TestWorkspaceCreation:
    @patch("workspace.workspace_manager.subprocess.run")
    def test_successful_creation(self, mock_run, manager):
        """AC1+AC2+AC3: Workspace created with correct structure."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        ws = manager.create("proj", "repo", "T-100", "git@github.com:org/repo.git")

        assert ws.root.exists()
        assert ws.context_dir.exists()
        assert ws.logs_dir.exists()
        assert ws.state_path.exists()
        assert ws.state.ticket_id == "T-100"
        assert ws.state.project_id == "proj"
        assert ws.state.repo_id == "repo"
        assert ws.state.status == "pending"

        # Verify directory naming: {ticket}_{timestamp}
        assert "T-100_" in ws.root.name

    @patch("workspace.workspace_manager.subprocess.run")
    def test_shallow_clone(self, mock_run, manager):
        """AC4: Shallow clone depth is configurable."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        manager.create("proj", "repo", "T-1", "url", clone_depth=1)

        cmd = mock_run.call_args[0][0]
        assert "--depth" in cmd
        assert "1" in cmd

    @patch("workspace.workspace_manager.subprocess.run")
    def test_full_clone_no_depth_flag(self, mock_run, manager):
        """AC4: depth=0 means full clone (no --depth flag)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        manager.create("proj", "repo", "T-1", "url", clone_depth=0)

        cmd = mock_run.call_args[0][0]
        assert "--depth" not in cmd

    @patch("workspace.workspace_manager.subprocess.run")
    def test_clone_failure_cleanup(self, mock_run, manager, base_dir):
        """AC5: Clone failure raises error and cleans up workspace."""
        mock_run.return_value = MagicMock(
            returncode=128, stderr="fatal: repository not found"
        )

        with pytest.raises(WorkspaceError, match="Git clone failed"):
            manager.create("proj", "repo", "T-1", "bad-url")

        # Workspace should be cleaned up
        workspace_dirs = list(base_dir.rglob("state.json"))
        assert len(workspace_dirs) == 0


class TestWorkspaceDiscovery:
    def test_discovers_running(self, manager, base_dir):
        """AC1+AC2 (2.3): Running workspaces are discovered."""
        _create_fake_workspace(base_dir, status="running", ticket_id="T-1")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 1
        assert workspaces[0].state.ticket_id == "T-1"

    def test_discovers_waiting(self, manager, base_dir):
        """AC2 (2.3): waiting_for_human workspaces are discovered."""
        _create_fake_workspace(base_dir, status="waiting_for_human")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 1

    def test_skips_completed(self, manager, base_dir):
        """AC3 (2.3): Completed workspaces are skipped."""
        _create_fake_workspace(base_dir, status="completed")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 0

    def test_skips_failed(self, manager, base_dir):
        """AC3 (2.3): Failed workspaces are skipped."""
        _create_fake_workspace(base_dir, status="failed")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 0

    def test_discovers_multiple(self, manager, base_dir):
        """AC5 (2.3): Multiple workspaces discovered."""
        _create_fake_workspace(base_dir, status="running", ticket_id="T-1")
        _create_fake_workspace(base_dir, status="waiting_for_human", ticket_id="T-2")
        _create_fake_workspace(base_dir, status="completed", ticket_id="T-3")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 2
        ids = {ws.state.ticket_id for ws in workspaces}
        assert ids == {"T-1", "T-2"}

    def test_empty_base_dir(self, manager):
        """No workspaces returns empty list."""
        workspaces = manager.discover_workspaces()
        assert workspaces == []


class TestWorkspaceCleanup:
    def test_deletes_old_completed(self, manager, base_dir):
        """AC2 (2.4): Old completed workspaces are deleted."""
        ws_root = _create_fake_workspace(
            base_dir, status="completed", ticket_id="T-OLD", days_ago=10
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 1
        assert not ws_root.exists()

    def test_keeps_young_workspace(self, manager, base_dir):
        """AC2 (2.4): Young completed workspace is kept."""
        ws_root = _create_fake_workspace(
            base_dir, status="completed", ticket_id="T-NEW", days_ago=1
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 0
        assert ws_root.exists()

    def test_never_deletes_running(self, manager, base_dir):
        """AC3 (2.4): Running workspaces are never deleted."""
        ws_root = _create_fake_workspace(
            base_dir, status="running", ticket_id="T-RUN", days_ago=30
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 0
        assert ws_root.exists()

    def test_never_deletes_waiting(self, manager, base_dir):
        """AC3 (2.4): waiting_for_human workspaces are never deleted."""
        ws_root = _create_fake_workspace(
            base_dir, status="waiting_for_human", ticket_id="T-WAIT", days_ago=30
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 0
        assert ws_root.exists()

    def test_deletes_old_failed(self, manager, base_dir):
        """Failed workspaces older than max_age are deleted."""
        _create_fake_workspace(
            base_dir, status="failed", ticket_id="T-FAIL", days_ago=10
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 1

    def test_empty_base_dir(self, manager):
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert deleted == []

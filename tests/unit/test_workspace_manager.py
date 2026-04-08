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
    return tmp_path / "data"


@pytest.fixture
def manager(base_dir):
    return WorkspaceManager(str(base_dir))


def _create_fake_workspace(
    base_dir: Path,
    company_id: str = "faria",
    repo_id: str = "managebac",
    ticket_id: str = "T-1",
    current_state: str = "DEV",
    days_ago: int = 0,
) -> Path:
    """Create a fake workspace directory with state.json (v2 structure)."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    ws_root = base_dir / company_id / repo_id / "tickets" / ticket_id
    ws_root.mkdir(parents=True, exist_ok=True)
    (ws_root / "meta").mkdir(exist_ok=True)
    (ws_root / "reports").mkdir(exist_ok=True)
    (ws_root / "logs").mkdir(exist_ok=True)
    (ws_root / "source").mkdir(exist_ok=True)

    state = {
        "ticket_id": ticket_id,
        "company_id": company_id,
        "repo_id": repo_id,
        "workspace_root": str(ws_root),
        "branch": None,
        "pr_number": None,
        "pr_url": None,
        "current_state": current_state,
        "previous_state": None,
        "stage_iterations": {},
        "human_input_pending": False,
        "human_input_question": None,
        "human_input_reply": None,
        "started_at": ts.isoformat(),
        "last_updated_at": ts.isoformat(),
        "error": None,
    }
    (ws_root / "state.json").write_text(json.dumps(state, indent=2))
    return ws_root


class TestWorkspaceCreation:
    @patch("workspace.workspace_manager.subprocess.run")
    def test_successful_creation(self, mock_run, manager):
        """Workspace created with correct v2 directory structure."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        ws = manager.create("faria", "managebac", "MBMOB-100", "git@github.com:org/repo.git")

        assert ws.root.exists()
        assert ws.meta_dir.exists()
        assert ws.reports_dir.exists()
        assert ws.logs_dir.exists()
        assert ws.state_path.exists()
        assert ws.state.ticket_id == "MBMOB-100"
        assert ws.state.company_id == "faria"
        assert ws.state.repo_id == "managebac"
        assert ws.state.current_state == "NEW"

        # Verify path structure: base/company/repo/tickets/ticket_id
        assert "faria" in str(ws.root)
        assert "managebac" in str(ws.root)
        assert "tickets" in str(ws.root)
        assert "MBMOB-100" in str(ws.root)

    @patch("workspace.workspace_manager.subprocess.run")
    def test_creates_feature_branch(self, mock_run, manager):
        """Feature branch created with correct naming."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        ws = manager.create("co", "repo", "T-1", "url", default_branch="develop", branch_prefix="feature")

        assert ws.state.branch is not None
        assert ws.state.branch.startswith("feature/T-1")

    @patch("workspace.workspace_manager.subprocess.run")
    def test_shallow_clone(self, mock_run, manager):
        """Shallow clone depth is configurable."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        manager.create("co", "repo", "T-1", "url", clone_depth=1)

        # First call is the clone
        cmd = mock_run.call_args_list[0][0][0]
        assert "--depth" in cmd
        assert "1" in cmd

    @patch("workspace.workspace_manager.subprocess.run")
    def test_full_clone_no_depth_flag(self, mock_run, manager):
        """depth=0 means full clone (no --depth flag)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        manager.create("co", "repo", "T-1", "url", clone_depth=0)

        cmd = mock_run.call_args_list[0][0][0]
        assert "--depth" not in cmd

    @patch("workspace.workspace_manager.subprocess.run")
    def test_clone_failure_cleanup(self, mock_run, manager, base_dir):
        """Clone failure raises error and cleans up workspace."""
        mock_run.return_value = MagicMock(
            returncode=128, stderr="fatal: repository not found"
        )

        with pytest.raises(WorkspaceError, match="Git clone failed"):
            manager.create("co", "repo", "T-1", "bad-url")

        # Workspace should be cleaned up
        workspace_dirs = list(base_dir.rglob("state.json"))
        assert len(workspace_dirs) == 0


class TestWorkspaceDiscovery:
    def test_discovers_active_states(self, manager, base_dir):
        """Active states (NEW, ANALYSIS, DEV, etc.) are discovered."""
        _create_fake_workspace(base_dir, current_state="DEV", ticket_id="T-1")
        _create_fake_workspace(base_dir, current_state="ANALYSIS", ticket_id="T-2")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 2

    def test_discovers_blocked(self, manager, base_dir):
        """BLOCKED workspaces are discovered (still active)."""
        _create_fake_workspace(base_dir, current_state="BLOCKED")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 1

    def test_skips_done(self, manager, base_dir):
        """DONE workspaces are skipped."""
        _create_fake_workspace(base_dir, current_state="DONE")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 0

    def test_skips_failed(self, manager, base_dir):
        """FAILED workspaces are skipped."""
        _create_fake_workspace(base_dir, current_state="FAILED")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 0

    def test_skips_archived(self, manager, base_dir):
        """ARCHIVED workspaces are skipped."""
        _create_fake_workspace(base_dir, current_state="ARCHIVED")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 0

    def test_discovers_multiple(self, manager, base_dir):
        """Multiple active workspaces discovered, terminal skipped."""
        _create_fake_workspace(base_dir, current_state="DEV", ticket_id="T-1")
        _create_fake_workspace(base_dir, current_state="BLOCKED", ticket_id="T-2")
        _create_fake_workspace(base_dir, current_state="DONE", ticket_id="T-3")
        _create_fake_workspace(base_dir, current_state="FAILED", ticket_id="T-4")
        workspaces = manager.discover_workspaces()
        assert len(workspaces) == 2
        ids = {ws.state.ticket_id for ws in workspaces}
        assert ids == {"T-1", "T-2"}

    def test_empty_base_dir(self, manager):
        workspaces = manager.discover_workspaces()
        assert workspaces == []


class TestSourceCleanup:
    def test_cleanup_source_removes_source_only(self, manager, base_dir):
        """Source-only cleanup preserves meta, reports, logs."""
        ws_root = _create_fake_workspace(base_dir, current_state="DONE")
        # Add a file to source to verify it gets deleted
        (ws_root / "source" / "file.txt").write_text("code")
        # Add report to verify it's preserved
        (ws_root / "reports" / "ba.md").write_text("report")

        ws = Workspace(str(ws_root))
        manager.cleanup_source(ws)

        assert not (ws_root / "source").exists()
        assert (ws_root / "meta").exists()
        assert (ws_root / "reports").exists()
        assert (ws_root / "reports" / "ba.md").exists()
        assert (ws_root / "logs").exists()
        assert (ws_root / "state.json").exists()


class TestFullCleanup:
    def test_deletes_old_archived(self, manager, base_dir):
        """Old ARCHIVED workspaces are fully deleted."""
        ws_root = _create_fake_workspace(
            base_dir, current_state="ARCHIVED", ticket_id="T-OLD", days_ago=10
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 1
        assert not ws_root.exists()

    def test_keeps_young_archived(self, manager, base_dir):
        """Young ARCHIVED workspace is kept."""
        ws_root = _create_fake_workspace(
            base_dir, current_state="ARCHIVED", ticket_id="T-NEW", days_ago=1
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 0
        assert ws_root.exists()

    def test_never_deletes_done(self, manager, base_dir):
        """DONE workspaces are never fully deleted (only source via cleanup_source)."""
        ws_root = _create_fake_workspace(
            base_dir, current_state="DONE", ticket_id="T-DONE", days_ago=30
        )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 0
        assert ws_root.exists()

    def test_never_deletes_active(self, manager, base_dir):
        """Active workspaces are never deleted."""
        for state in ["NEW", "ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "BLOCKED"]:
            _create_fake_workspace(
                base_dir, current_state=state, ticket_id=f"T-{state}", days_ago=30
            )
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert len(deleted) == 0

    def test_empty_base_dir(self, manager):
        deleted = manager.cleanup_old_workspaces(max_age_days=7)
        assert deleted == []

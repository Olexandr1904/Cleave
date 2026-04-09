"""Tests for admin workspace type."""

from __future__ import annotations

import pytest

from workspace.workspace import AdminWorkspace, AdminWorkspaceState


class TestAdminWorkspaceState:
    def test_defaults(self):
        state = AdminWorkspaceState(
            operation="add",
            workspace_root="/tmp/admin-ws",
        )
        assert state.operation == "add"
        assert state.status == "pending"
        assert state.started_at != ""
        assert state.error is None

    def test_valid_operations(self):
        for op in ("add", "list", "remove"):
            state = AdminWorkspaceState(operation=op, workspace_root="/tmp/ws")
            assert state.operation == op


class TestAdminWorkspace:
    @pytest.fixture
    def admin_dir(self, tmp_path):
        ws_root = tmp_path / "admin-ws"
        ws_root.mkdir()
        return ws_root

    def test_creates_directories(self, admin_dir):
        AdminWorkspace.create(str(admin_dir), operation="add")
        assert (admin_dir / "meta").exists()
        assert (admin_dir / "reports").exists()
        assert (admin_dir / "logs").exists()
        assert not (admin_dir / "source").exists()  # No source dir for admin

    def test_state_saved_and_loaded(self, admin_dir):
        AdminWorkspace.create(str(admin_dir), operation="add")

        loaded = AdminWorkspace(str(admin_dir))
        assert loaded.state.operation == "add"
        assert loaded.state.status == "pending"

    def test_no_source_dir_property(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="list")
        assert ws.source_dir is None

    def test_meta_and_reports_dirs(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        assert ws.meta_dir == admin_dir / "meta"
        assert ws.reports_dir == admin_dir / "reports"

    def test_update_status(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        ws.update_state(status="completed")

        loaded = AdminWorkspace(str(admin_dir))
        assert loaded.state.status == "completed"

    def test_update_unknown_field_raises(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        with pytest.raises(ValueError, match="Unknown state field"):
            ws.update_state(nonexistent_field="value")

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from workspace.workspace import Workspace, WorkspaceState


def _make_workspace(ticket_id: str, state: str, previous: str | None = None, error: str | None = None) -> MagicMock:
    ws = MagicMock(spec=Workspace)
    ws.state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="test-co",
        repo_id="test-repo",
        workspace_root="/tmp/test",
        current_state=state,
        previous_state=previous,
        error=error,
    )
    ws.source_dir = MagicMock()
    ws.source_dir.__str__ = lambda self: "/tmp/test/source"
    ws.reports_dir = MagicMock()
    ws.meta_dir = MagicMock()
    return ws


@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = EventStore(db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def orchestrator():
    orch = MagicMock()
    orch.get_active_workspaces = MagicMock(return_value=[])
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.get_running = MagicMock(return_value=None)
    orch._agent_runtime.cancel = MagicMock(return_value=False)
    return orch


@pytest.fixture
def mode_handler():
    mh = MagicMock()
    mh.get_mode = MagicMock(return_value="manual")
    mh.set_mode = MagicMock()
    return mh


@pytest.fixture
def client(bus, store, orchestrator, mode_handler):
    app = create_app(
        bus, store,
        orchestrator=orchestrator,
        mode_handler=mode_handler,
    )
    return TestClient(app)


class TestApproveEndpoint:
    def test_approve_awaiting_workspace(self, client, orchestrator):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous="ANALYSIS")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        ws.transition.assert_called()

    def test_approve_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/approve")
        assert resp.status_code == 400

    def test_approve_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/T-1/approve")
        assert resp.status_code == 404


class TestRejectEndpoint:
    def test_reject_awaiting_workspace(self, client, orchestrator):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous="ANALYSIS")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/reject")
        assert resp.status_code == 200
        ws.transition.assert_called()

    def test_reject_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/reject")
        assert resp.status_code == 400


class TestRetryEndpoint:
    def test_retry_blocked_workspace(self, client, orchestrator):
        ws = _make_workspace("T-1", "BLOCKED", previous="DEV", error="stuck")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/retry")
        assert resp.status_code == 200

    def test_retry_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/retry")
        assert resp.status_code == 400


class TestTakeControlEndpoint:
    def test_take_control_no_agent_running(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/take-control",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "command" in data
        # transition is now called with the timestamp atomically
        assert ws.transition.called
        args, kwargs = ws.transition.call_args
        assert args[0] == "MANUAL_CONTROL"
        assert "manual_control_started_at" in kwargs

    def test_take_control_agent_running_without_confirm(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._agent_runtime.get_running.return_value = {
            "agent_id": "dev-agent", "pid": 123, "started_at": 1000.0,
        }
        resp = client.post("/api/workspaces/T-1/take-control")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "agent_running"
        assert data["agent"] == "dev-agent"
        ws.transition.assert_not_called()

    def test_take_control_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-1", "DONE")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/take-control",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 400


class TestReleaseControlEndpoint:
    def test_release_control(self, client, orchestrator):
        ws = _make_workspace("T-1", "MANUAL_CONTROL", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/release-control",
                           content=json.dumps({"comment": "Fixed the bug"}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["new_state"] == "ANALYSIS"

    def test_release_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/release-control")
        assert resp.status_code == 400


class TestModeEndpoint:
    def test_switch_mode(self, client, mode_handler):
        resp = client.post("/api/daemon/mode",
                           content=json.dumps({"mode": "auto"}))
        assert resp.status_code == 200
        mode_handler.set_mode.assert_called_with("auto")

    def test_invalid_mode(self, client, mode_handler):
        mode_handler.set_mode.side_effect = ValueError("Invalid mode")
        resp = client.post("/api/daemon/mode",
                           content=json.dumps({"mode": "bad"}))
        assert resp.status_code == 400


class TestDaemonStatusEndpoint:
    def test_daemon_status(self, client, orchestrator, mode_handler):
        ws_active = _make_workspace("T-1", "DEV")
        ws_blocked = _make_workspace("T-2", "BLOCKED", previous="QA")
        ws_awaiting = _make_workspace("T-3", "AWAITING_APPROVAL", previous="ANALYSIS")
        orchestrator.get_active_workspaces.return_value = [ws_active, ws_blocked, ws_awaiting]
        resp = client.get("/api/daemon/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "manual"
        assert data["active"] == 3
        assert data["blocked"] == 1
        assert data["awaiting"] == 1


class TestRetryEndpointExtended:
    def test_retry_failed_workspace(self, client, orchestrator):
        """Retry should work for FAILED state too."""
        ws = _make_workspace("T-1", "FAILED", previous="DEV", error="crashed")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/retry")
        assert resp.status_code == 200

    def test_retry_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/T-1/retry")
        assert resp.status_code == 404

    def test_retry_clears_error_and_pending(self, client, orchestrator):
        ws = _make_workspace("T-1", "BLOCKED", previous="DEV", error="stuck")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/retry")
        assert resp.status_code == 200
        assert ws.state.error is None
        assert ws.state.human_input_pending is False


class TestTakeControlExtended:
    def test_take_control_confirm_with_agent_running(self, client, orchestrator):
        """confirm=True should kill agent and proceed."""
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._agent_runtime.get_running.return_value = {
            "agent_id": "dev-agent", "pid": 123, "started_at": 1000.0,
        }
        resp = client.post("/api/workspaces/T-1/take-control",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        orchestrator._agent_runtime.cancel.assert_called_with("T-1")
        # transition is now called with the timestamp atomically
        assert ws.transition.called
        args, kwargs = ws.transition.call_args
        assert args[0] == "MANUAL_CONTROL"
        assert "manual_control_started_at" in kwargs

    def test_take_control_manual_control_state_rejected(self, client, orchestrator):
        ws = _make_workspace("T-1", "MANUAL_CONTROL", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/take-control",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 400

    def test_take_control_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/T-1/take-control",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 404


class TestReleaseControlExtended:
    def test_release_with_empty_comment(self, client, orchestrator):
        ws = _make_workspace("T-1", "MANUAL_CONTROL", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/release-control",
                           content=json.dumps({"comment": ""}))
        assert resp.status_code == 200
        assert resp.json()["new_state"] == "ANALYSIS"

    def test_release_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/T-1/release-control")
        assert resp.status_code == 404

    def test_release_no_body(self, client, orchestrator):
        """Release without JSON body should still work (defaults to empty comment)."""
        ws = _make_workspace("T-1", "MANUAL_CONTROL", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/release-control")
        assert resp.status_code == 200


class TestDaemonStatusExtended:
    def test_daemon_status_with_manual_control(self, client, orchestrator, mode_handler):
        ws_manual = _make_workspace("T-1", "MANUAL_CONTROL", previous="DEV")
        ws_active = _make_workspace("T-2", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws_manual, ws_active]
        resp = client.get("/api/daemon/status")
        data = resp.json()
        assert data["manual_control"] == 1
        assert data["active"] == 2

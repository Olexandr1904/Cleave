from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from workspace.workspace import Stage, Workspace, WorkspaceState


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


class TestClearGradleAndRetryEndpoint:
    def test_clears_cache_and_retries_when_signature_matches(
        self, client, orchestrator, tmp_path, monkeypatch,
    ):
        # Fake Gradle home with a transforms tree to wipe
        fake_home = tmp_path / "g"
        transforms = fake_home / "caches" / "8.14.1" / "transforms" / "abc"
        transforms.mkdir(parents=True)
        (transforms / "binary").write_bytes(b"\x00" * 2048)
        monkeypatch.setenv("GRADLE_USER_HOME", str(fake_home))

        ws = _make_workspace(
            "T-1", "FAILED", previous="PUSHED",
            error="AAPT2 aapt2-8.6.1-linux Daemon #2: Daemon startup failed",
        )
        orchestrator.get_active_workspaces.return_value = [ws]

        resp = client.post("/api/workspaces/T-1/clear-gradle-and-retry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["bytes_freed"] >= 2048
        assert data["new_state"] == "PUSHED"
        assert not (fake_home / "caches" / "8.14.1" / "transforms").exists()
        ws.transition.assert_called_once_with("PUSHED")

    def test_refuses_when_error_does_not_match_signature(self, client, orchestrator):
        # Defensive: even if the operator hits the endpoint manually for an
        # unrelated failure, we don't wipe the cache.
        ws = _make_workspace(
            "T-1", "FAILED", previous="DEV",
            error="some unrelated build error with no AAPT2 signal",
        )
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/clear-gradle-and-retry")
        assert resp.status_code == 400
        ws.transition.assert_not_called()

    def test_refuses_when_state_not_failed(self, client, orchestrator):
        ws = _make_workspace("T-1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-1/clear-gradle-and-retry")
        assert resp.status_code == 400

    def test_returns_404_when_workspace_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/MISSING/clear-gradle-and-retry")
        assert resp.status_code == 404


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


class TestTakeControlOnFailed:
    def test_take_control_allowed_on_failed(self, client, orchestrator):
        ws = _make_workspace("T-F1", "FAILED", previous="DEV", error="boom")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-F1/take-control")
        assert resp.status_code == 200
        ws.transition.assert_called()

    def test_take_control_allowed_on_deferred(self, client, orchestrator):
        ws = _make_workspace("T-D1", "DEFERRED", previous="QA")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-D1/take-control")
        assert resp.status_code == 200
        ws.transition.assert_called()

    def test_take_control_still_blocked_on_done(self, client, orchestrator):
        ws = _make_workspace("T-DN", "DONE")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-DN/take-control")
        assert resp.status_code == 400


class TestResumeEndpoint:
    def test_resume_deferred_transitions_to_previous(self, client, orchestrator):
        ws = _make_workspace("T-R1", "DEFERRED", previous="QA")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-R1/resume")
        assert resp.status_code == 200
        ws.transition.assert_called_with("QA")

    def test_resume_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-R2", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-R2/resume")
        assert resp.status_code == 400

    def test_resume_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/missing/resume")
        assert resp.status_code == 404


class TestArchiveEndpoint:
    def test_archive_failed_transitions_to_archived(self, client, orchestrator):
        ws = _make_workspace("T-A1", "FAILED", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A1/archive")
        assert resp.status_code == 200
        ws.transition.assert_called_with("ARCHIVED")

    def test_archive_done_transitions_to_archived(self, client, orchestrator):
        ws = _make_workspace("T-A2", "DONE")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A2/archive")
        assert resp.status_code == 200
        ws.transition.assert_called_with("ARCHIVED")

    def test_archive_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-A3", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A3/archive")
        assert resp.status_code == 400

    def test_archive_deferred_hops_via_failed(self, client, orchestrator):
        ws = _make_workspace("T-A4", "DEFERRED", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A4/archive")
        assert resp.status_code == 200
        assert ws.transition.call_args_list == [call("FAILED"), call("ARCHIVED")]


class TestPauseEndpoint:
    def test_pause_active_workspace_transitions_to_paused(self, client, orchestrator):
        ws = _make_workspace("T-P1", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-P1/pause",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["new_state"] == "PAUSED"
        ws.transition.assert_called_with(Stage.PAUSED)

    def test_pause_no_agent_running_does_not_require_confirm(self, client, orchestrator):
        ws = _make_workspace("T-P2", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-P2/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        ws.transition.assert_called_with(Stage.PAUSED)

    def test_pause_agent_running_without_confirm_returns_agent_running(
        self, client, orchestrator
    ):
        ws = _make_workspace("T-P3", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._agent_runtime.get_running.return_value = {
            "agent_id": "dev-agent", "pid": 123, "started_at": 1000.0,
        }
        resp = client.post("/api/workspaces/T-P3/pause")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "agent_running"
        assert data["agent"] == "dev-agent"
        ws.transition.assert_not_called()

    def test_pause_agent_running_with_confirm_kills_and_pauses(self, client, orchestrator):
        ws = _make_workspace("T-P4", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        orchestrator._agent_runtime.get_running.return_value = {
            "agent_id": "dev-agent", "pid": 123, "started_at": 1000.0,
        }
        resp = client.post("/api/workspaces/T-P4/pause",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        orchestrator._agent_runtime.cancel.assert_called_with("T-P4")
        ws.transition.assert_called_with(Stage.PAUSED)

    @pytest.mark.parametrize("bad_state", [
        "NEW", "BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL",
        "DEFERRED", "PAUSED", "DONE", "FAILED", "ARCHIVED",
    ])
    def test_pause_from_invalid_state_returns_400(self, client, orchestrator, bad_state):
        ws = _make_workspace("T-PX", bad_state)
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-PX/pause",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 400
        ws.transition.assert_not_called()

    def test_pause_missing_workspace_returns_404(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/T-MISSING/pause",
                           content=json.dumps({"confirm": True}))
        assert resp.status_code == 404


class TestUnpauseEndpoint:
    def test_unpause_paused_returns_to_previous(self, client, orchestrator):
        ws = _make_workspace("T-U1", "PAUSED", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-U1/unpause")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["new_state"] == "DEV"
        ws.transition.assert_called_with("DEV")

    def test_unpause_falls_back_to_analysis_when_previous_null(self, client, orchestrator):
        ws = _make_workspace("T-U2", "PAUSED", previous=None)
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-U2/unpause")
        assert resp.status_code == 200
        assert resp.json()["new_state"] == "ANALYSIS"
        ws.transition.assert_called_with("ANALYSIS")

    @pytest.mark.parametrize("bad_state", [
        "NEW", "ANALYSIS", "DEV", "BLOCKED", "AWAITING_APPROVAL",
        "MANUAL_CONTROL", "DEFERRED", "DONE", "FAILED", "ARCHIVED",
    ])
    def test_unpause_from_non_paused_returns_400(self, client, orchestrator, bad_state):
        ws = _make_workspace("T-UX", bad_state)
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-UX/unpause")
        assert resp.status_code == 400
        ws.transition.assert_not_called()

    def test_unpause_missing_workspace_returns_404(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/T-MISSING/unpause")
        assert resp.status_code == 404


class TestPauseFallsBackToDiskScan:
    """When a workspace exists on disk but isn't in the active list,
    pause/unpause should still find it (and re-adopt it).
    """

    def _seed_disk_ws(self, tmp_path, ticket_id: str, state: str):
        from dataclasses import asdict
        ws_root = tmp_path / "co" / "repo" / "tickets" / ticket_id
        ws_root.mkdir(parents=True)
        (ws_root / "meta").mkdir()
        (ws_root / "reports").mkdir()
        (ws_root / "logs").mkdir()
        (ws_root / "source").mkdir()
        s = WorkspaceState(
            ticket_id=ticket_id, company_id="co", repo_id="repo",
            workspace_root=str(ws_root), current_state=state,
        )
        (ws_root / "state.json").write_text(json.dumps(asdict(s)))
        return ws_root

    @pytest.fixture
    def disk_client(self, bus, store, orchestrator, mode_handler, tmp_path):
        from types import SimpleNamespace
        global_config = SimpleNamespace(
            workspaces=SimpleNamespace(base_dir=str(tmp_path)),
        )
        orchestrator._active_workspaces = []
        app = create_app(
            bus, store,
            orchestrator=orchestrator,
            mode_handler=mode_handler,
            global_config=global_config,
        )
        return TestClient(app)

    def test_pause_finds_orphan_workspace_on_disk_and_readopts(
        self, disk_client, orchestrator, tmp_path
    ):
        self._seed_disk_ws(tmp_path, "ORPHAN-1", "DEV")
        orchestrator.get_active_workspaces.return_value = []  # not in active list
        resp = disk_client.post("/api/workspaces/ORPHAN-1/pause",
                                content=json.dumps({"confirm": True}))
        assert resp.status_code == 200
        # Re-adopted into the active list
        assert any(
            w.state.ticket_id == "ORPHAN-1"
            for w in orchestrator._active_workspaces
        )

    def test_pause_returns_404_when_not_in_active_and_not_on_disk(
        self, disk_client, orchestrator
    ):
        orchestrator.get_active_workspaces.return_value = []
        resp = disk_client.post("/api/workspaces/GHOST-1/pause",
                                content=json.dumps({"confirm": True}))
        assert resp.status_code == 404

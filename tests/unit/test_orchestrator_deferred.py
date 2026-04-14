"""Tests for orchestrator quota-deferral routing and notifications."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runtime import AgentResult
from workspace.workspace import Workspace, WorkspaceState


def _make_workspace(tmp_path, ticket_id: str, state: str = "DEV") -> Workspace:
    ws_root = tmp_path / ticket_id
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()
    ws_state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="acme",
        repo_id="acme-mobile",
        workspace_root=str(ws_root),
        current_state=state,
        stage_iterations={"dev": 1},
    )
    ws = Workspace(str(ws_root), ws_state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator_with_stubs(tmp_path, monkeypatch):
    """Build an Orchestrator with fakes for agent_runtime, notifier, tracker."""
    from orchestrator.orchestrator import Orchestrator
    from config.config_loader import GlobalConfig

    cfg = MagicMock(spec=GlobalConfig)
    cfg.defaults = MagicMock(poll_interval_seconds=900)
    cfg.workspaces = MagicMock(base_dir=str(tmp_path), max_age_days=7)
    cfg.telegram = MagicMock(default_chat_id="chat-1")

    orch = Orchestrator.__new__(Orchestrator)
    orch._global_config = cfg
    orch._projects = {}
    orch._active_workspaces = []
    orch._workspace_manager = MagicMock()
    orch._workspace_manager.cleanup_old_workspaces = MagicMock(return_value=[])
    orch._tracker = None
    orch._vcs = None
    orch._repo_vcs = {}
    orch._notifier = AsyncMock()
    orch._dry_run = False
    orch._mode_handler = MagicMock()
    orch._mode_handler.get_mode = MagicMock(return_value="auto")
    orch._shutdown_event = MagicMock()
    orch._recent_completions = []
    orch._quota_window_end = None

    from dashboard.events import EventBus
    orch._events = EventBus()

    orch._agent_runtime = MagicMock()
    return orch


class TestQuotaFailureRouting:
    async def test_quota_failure_transitions_to_deferred_with_retry_at(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="DEV")
        orch._active_workspaces.append(ws)

        retry_at = datetime.now(timezone.utc) + timedelta(hours=5)
        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota", retry_at=retry_at,
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)

        assert ws.state.current_state == "DEFERRED"
        assert ws.state.previous_state == "DEV"
        assert ws.state.retry_at == retry_at.isoformat()

    async def test_quota_failure_rolls_back_iteration(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-2", state="DEV")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota",
                retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)

        # Iteration was incremented to 2 inside _handle_agent_stage, then rolled back to 1.
        assert ws.state.stage_iterations.get("dev", 0) == 1

    async def test_quota_failure_uses_default_delay_when_retry_at_missing(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-3", state="DEV")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="rate limited", failure_kind="quota", retry_at=None,
            )
        )

        before = datetime.now(timezone.utc)
        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)
        after = datetime.now(timezone.utc)

        parsed = datetime.fromisoformat(ws.state.retry_at)
        assert before + timedelta(minutes=59) <= parsed <= after + timedelta(hours=1, minutes=1)

    async def test_permanent_failure_transitions_to_failed(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-4", state="QA")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="qa-agent", success=False, output="",
                error="disk full", failure_kind="permanent",
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "qa-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "qa", stage_def)

        assert ws.state.current_state == "FAILED"
        assert ws.state.previous_state == "QA"
        assert ws.state.error == "disk full"


class TestQuotaNotificationDebounce:
    async def test_first_quota_notification_sent(self, orchestrator_with_stubs, tmp_path):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="DEV")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota",
                retry_at=datetime.now(timezone.utc) + timedelta(hours=5),
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)

        assert orch._notifier.send_message.await_count == 1

    async def test_second_quota_notification_suppressed_within_window(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws1 = _make_workspace(tmp_path, "T-1", state="DEV")
        ws2 = _make_workspace(tmp_path, "T-2", state="DEV")
        orch._active_workspaces.extend([ws1, ws2])

        retry_at = datetime.now(timezone.utc) + timedelta(hours=5)
        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota", retry_at=retry_at,
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws1, "dev", stage_def)
        await orch._handle_agent_stage(ws2, "dev", stage_def)

        assert orch._notifier.send_message.await_count == 1
        assert ws1.state.current_state == "DEFERRED"
        assert ws2.state.current_state == "DEFERRED"

    async def test_permanent_failure_notification_sent(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="QA")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="qa-agent", success=False, output="",
                error="disk full", failure_kind="permanent",
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "qa-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "qa", stage_def)

        assert orch._notifier.send_message.await_count == 1

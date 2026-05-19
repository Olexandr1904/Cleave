"""Tests for agent_stage FAILED path: failure summarizer integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.agent_runtime import AgentResult
from workspace.workspace import Stage, Workspace, WorkspaceState


def _make_workspace(tmp_path, ticket_id: str, state: str = Stage.DEV) -> Workspace:
    ws_root = tmp_path / ticket_id
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()
    ws_state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="acme",
        repo_id="acme-app",
        workspace_root=str(ws_root),
        current_state=state,
        stage_iterations={"dev": 1},
    )
    ws = Workspace(str(ws_root), ws_state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator_with_stubs(tmp_path, monkeypatch):
    """Minimal Orchestrator with fakes — mirrors test_orchestrator_deferred.py."""
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
    orch._trackers = {}
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


@pytest.mark.asyncio
async def test_agent_failed_uses_summary_when_available(
    orchestrator_with_stubs, tmp_path
):
    """When summarizer returns a string, state.error is set to that string and
    state.raw_error preserves the original CLI error."""
    orch = orchestrator_with_stubs
    ws = _make_workspace(tmp_path, "T-10", state=Stage.DEV)
    orch._active_workspaces.append(ws)

    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="raw cli err", failure_kind="permanent",
        )
    )

    summary = "Reason: Kotlin daemon OOM.\nSuggested action: retry."
    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    with patch(
        "orchestrator.pipeline.agent_stage.summarize_agent_failure",
        new_callable=AsyncMock, return_value=summary,
    ) as mock_summarize:
        await orch._handle_agent_stage(ws, "dev", stage_def)

    mock_summarize.assert_awaited_once_with(
        ws, "dev", reason_kind="agent_failed", raw_error="raw cli err"
    )
    assert ws.state.current_state == Stage.FAILED
    assert ws.state.error == summary
    assert ws.state.raw_error == "raw cli err"


@pytest.mark.asyncio
async def test_agent_failed_falls_back_when_summarizer_returns_none(
    orchestrator_with_stubs, tmp_path
):
    """When summarizer returns None, state.error falls back to the raw CLI error."""
    orch = orchestrator_with_stubs
    ws = _make_workspace(tmp_path, "T-11", state=Stage.DEV)
    orch._active_workspaces.append(ws)

    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="raw cli err", failure_kind="permanent",
        )
    )

    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    with patch(
        "orchestrator.pipeline.agent_stage.summarize_agent_failure",
        new_callable=AsyncMock, return_value=None,
    ) as mock_summarize:
        await orch._handle_agent_stage(ws, "dev", stage_def)

    mock_summarize.assert_awaited_once_with(
        ws, "dev", reason_kind="agent_failed", raw_error="raw cli err"
    )
    assert ws.state.current_state == Stage.FAILED
    assert ws.state.error == "raw cli err"
    assert ws.state.raw_error == "raw cli err"

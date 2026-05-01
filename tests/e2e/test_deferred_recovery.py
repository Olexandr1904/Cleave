"""E2E test: DEFERRED workspace auto-resumes after retry_at passes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runtime import AgentResult
from workspace.workspace import Stage, Workspace, WorkspaceState


def _run(coro_fn):
    """Run an async body in a fresh event loop on a dedicated thread.

    A thread is required (rather than an in-process loop) because Playwright's
    sync API — used by other e2e tests — keeps an asyncio loop running in the
    main thread's greenlet context for the duration of the pytest session,
    which makes ``asyncio.run``/``loop.run_until_complete`` refuse to start.
    Running our coroutine on its own thread gives it an isolated event loop.
    """
    import threading

    result: dict = {}

    def target():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(coro_fn())
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _seed_ws(tmp_path, ticket_id: str, state: str = "DEV", retry_at: str | None = None):
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
        previous_state="DEV" if state == "DEFERRED" else None,
        retry_at=retry_at,
        stage_iterations={"dev": 1},
    )
    ws = Workspace(str(ws_root), ws_state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator(tmp_path):
    """Minimal Orchestrator wired to fakes, enough for poll_cycle + _handle_agent_stage."""
    from orchestrator.orchestrator import Orchestrator

    cfg = MagicMock()
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
    orch._mode_handler = MagicMock(get_mode=MagicMock(return_value="auto"))
    orch._shutdown_event = MagicMock()
    orch._recent_completions = []
    orch._quota_window_end = None
    orch._agent_runtime = MagicMock()

    from dashboard.events import EventBus
    orch._events = EventBus()
    return orch


def test_quota_hit_then_resume_after_window(orchestrator, tmp_path):
    """Full cycle: quota hit → DEFERRED → retry_at passes → sweep resumes → next call succeeds."""
    orch = orchestrator
    ws = _seed_ws(tmp_path, "T-1", state="DEV")
    orch._active_workspaces.append(ws)

    # Stage definition shim
    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    # First call: quota failure with retry_at in the past (simulating "window already passed by the time sweep runs")
    past_retry = datetime.now(timezone.utc) - timedelta(seconds=10)
    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="usage limit", failure_kind="quota", retry_at=past_retry,
        )
    )

    async def body():
        # Drive one failure
        await orch._handle_agent_stage(ws, "dev", stage_def)
        assert ws.state.current_state == "DEFERRED"
        assert ws.state.retry_at == past_retry.isoformat()
        assert ws.state.previous_state == "DEV"
        # Iteration rolled back
        assert ws.state.stage_iterations.get("dev", 0) == 1
        # One telegram notification sent
        assert orch._notifier.send_message.await_count == 1

        # Run the sweep — should resume to DEV (retry_at in past)
        await orch._sweep_deferred()
        assert ws.state.current_state == "DEV"
        assert ws.state.previous_state is None
        assert ws.state.retry_at is None

    _run(body)


def test_multiple_tickets_debounced_to_one_notification(orchestrator, tmp_path):
    orch = orchestrator
    ws1 = _seed_ws(tmp_path, "T-1", state="DEV")
    ws2 = _seed_ws(tmp_path, "T-2", state="DEV")
    ws3 = _seed_ws(tmp_path, "T-3", state="DEV")
    orch._active_workspaces.extend([ws1, ws2, ws3])

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

    async def body():
        for ws in (ws1, ws2, ws3):
            await orch._handle_agent_stage(ws, "dev", stage_def)

    _run(body)

    assert all(w.state.current_state == "DEFERRED" for w in (ws1, ws2, ws3))
    assert orch._notifier.send_message.await_count == 1


def test_restart_picks_up_deferred_from_disk(tmp_path):
    """A DEFERRED workspace persisted to disk is rediscovered on restart."""
    from workspace.workspace_manager import WorkspaceManager

    base = tmp_path / "cleave"
    ws_root = base / "acme" / "acme-app" / "tickets" / "T-R"
    ws_root.mkdir(parents=True)
    (ws_root / "meta").mkdir()
    (ws_root / "source").mkdir()

    retry_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    state = WorkspaceState(
        ticket_id="T-R", company_id="acme", repo_id="acme-app",
        workspace_root=str(ws_root), current_state="DEFERRED",
        previous_state="QA", retry_at=retry_at,
    )
    Workspace(str(ws_root), state).save_state()

    mgr = WorkspaceManager(str(base))
    discovered = mgr.discover_workspaces()
    assert len(discovered) == 1
    assert discovered[0].state.current_state == "DEFERRED"
    assert discovered[0].state.retry_at == retry_at

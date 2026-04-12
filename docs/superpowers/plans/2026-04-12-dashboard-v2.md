# Dashboard V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the Sickle dashboard into an operations dashboard with working navigation, action buttons (approve/reject/retry), and a "Take Control" feature that pauses the pipeline and opens a Claude Code session.

**Architecture:** Multi-file vanilla JS frontend (no framework, no build step) served by the existing Starlette backend. New REST action endpoints modify workspace state. A new `MANUAL_CONTROL` workspace state locks a ticket for human takeover. The orchestrator skips MANUAL_CONTROL workspaces and exposes agent cancellation.

**Tech Stack:** Starlette (Python), vanilla JS (ES modules), CSS, SQLite (existing event store)

---

### Task 1: Add MANUAL_CONTROL to workspace state machine

**Files:**
- Modify: `workspace/workspace.py:15-36` (VALID_STATES, VALID_TRANSITIONS)
- Modify: `workspace/workspace.py:39-59` (WorkspaceState dataclass)
- Test: `tests/unit/test_workspace.py`

- [ ] **Step 1: Write failing tests for MANUAL_CONTROL state**

Add to `tests/unit/test_workspace.py`:

```python
class TestManualControlState:
    def test_manual_control_in_valid_states(self):
        from workspace.workspace import VALID_STATES
        assert "MANUAL_CONTROL" in VALID_STATES

    def test_transition_dev_to_manual_control(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("MANUAL_CONTROL")
        assert workspace.state.current_state == "MANUAL_CONTROL"
        assert workspace.state.previous_state == "DEV"

    def test_transition_manual_control_to_analysis(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("MANUAL_CONTROL")
        workspace.transition("ANALYSIS")
        assert workspace.state.current_state == "ANALYSIS"

    def test_manual_control_cannot_go_to_done(self, workspace):
        from workspace.workspace import InvalidTransitionError
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("MANUAL_CONTROL")
        with pytest.raises(InvalidTransitionError):
            workspace.transition("DONE")

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/unit/test_workspace.py::TestManualControlState -v`
Expected: FAIL — `MANUAL_CONTROL` not in VALID_STATES

- [ ] **Step 3: Add MANUAL_CONTROL to state machine and WorkspaceState**

In `workspace/workspace.py`, update `VALID_STATES`:

```python
VALID_STATES = {
    "NEW", "ANALYSIS", "DEV", "SCOPE_CHECK", "QA",
    "PUSHED", "PR_REVIEW", "DONE",
    "BLOCKED", "FAILED", "ARCHIVED",
    "AWAITING_APPROVAL", "MANUAL_CONTROL",
}
```

Update `VALID_TRANSITIONS` — add `"MANUAL_CONTROL"` to every active state's set, and add the MANUAL_CONTROL entry:

```python
VALID_TRANSITIONS: dict[str, set[str]] = {
    "NEW":                {"ANALYSIS", "FAILED"},
    "ANALYSIS":           {"DEV", "BLOCKED", "FAILED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
    "DEV":                {"SCOPE_CHECK", "BLOCKED", "FAILED", "MANUAL_CONTROL"},
    "SCOPE_CHECK":        {"QA", "DEV", "BLOCKED", "FAILED", "MANUAL_CONTROL"},
    "QA":                 {"PUSHED", "DEV", "BLOCKED", "FAILED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
    "PUSHED":             {"PR_REVIEW", "BLOCKED", "FAILED", "MANUAL_CONTROL"},
    "PR_REVIEW":          {"DEV", "DONE", "BLOCKED", "FAILED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
    "DONE":               {"ARCHIVED"},
    "BLOCKED":            {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "FAILED", "MANUAL_CONTROL"},
    "FAILED":             set(),
    "ARCHIVED":           set(),
    "AWAITING_APPROVAL":  {"DEV", "PUSHED", "DONE", "FAILED", "MANUAL_CONTROL"},
    "MANUAL_CONTROL":     {"ANALYSIS"},
}
```

Add fields to `WorkspaceState` dataclass (after `escalation_chat_id`):

```python
    manual_control_started_at: str | None = None
    manual_control_comment: str | None = None
```

Update the `transition()` method — add MANUAL_CONTROL to the set that preserves `previous_state`:

```python
        if new_state in ("BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL"):
            updates["previous_state"] = current
            updates["human_input_pending"] = True
        elif current in ("BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL"):
            # Resuming from a paused state — clear pending flag
            updates["previous_state"] = None
            updates["human_input_pending"] = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/unit/test_workspace.py -v`
Expected: ALL PASS (both new and existing tests)

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat: add MANUAL_CONTROL state to workspace state machine"
```

---

### Task 2: Add agent cancellation to AgentRuntime

**Files:**
- Modify: `orchestrator/agent_runtime.py:41-57` (AgentRuntime.__init__ and new methods)
- Test: `tests/unit/test_agent_runtime_cancel.py` (new file)

- [ ] **Step 1: Write failing tests for agent cancellation**

Create `tests/unit/test_agent_runtime_cancel.py`:

```python
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runtime import AgentRuntime


@pytest.fixture
def runtime():
    registry = MagicMock()
    llm = MagicMock()
    return AgentRuntime(registry, llm)


class TestAgentCancel:
    def test_register_running_agent(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=12345)
        info = runtime.get_running("TICKET-1")
        assert info is not None
        assert info["agent_id"] == "dev-agent"
        assert info["pid"] == 12345

    def test_get_running_returns_none_when_not_running(self, runtime):
        assert runtime.get_running("TICKET-1") is None

    def test_unregister_running_agent(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=12345)
        runtime.unregister_running("TICKET-1")
        assert runtime.get_running("TICKET-1") is None

    def test_cancel_returns_false_when_not_running(self, runtime):
        result = runtime.cancel("TICKET-1")
        assert result is False

    def test_cancel_kills_process_and_unregisters(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        # Mock os.kill to avoid actually killing a process
        import os
        import signal
        original_kill = os.kill
        killed = []
        def mock_kill(pid, sig):
            killed.append((pid, sig))
        os.kill = mock_kill
        try:
            result = runtime.cancel("TICKET-1")
            assert result is True
            assert killed == [(99999, signal.SIGTERM)]
            assert runtime.get_running("TICKET-1") is None
        finally:
            os.kill = original_kill
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/unit/test_agent_runtime_cancel.py -v`
Expected: FAIL — `register_running` not found

- [ ] **Step 3: Implement agent tracking and cancellation**

Add to `AgentRuntime.__init__` (after `self._events = event_bus`):

```python
        self._running: dict[str, dict[str, Any]] = {}  # ticket_id -> {agent_id, pid, started_at}
```

Add new methods to `AgentRuntime`:

```python
    def register_running(self, ticket_id: str, agent_id: str, pid: int) -> None:
        """Register an agent as running for a workspace."""
        self._running[ticket_id] = {
            "agent_id": agent_id,
            "pid": pid,
            "started_at": time.time(),
        }

    def unregister_running(self, ticket_id: str) -> None:
        """Remove a workspace from the running tracker."""
        self._running.pop(ticket_id, None)

    def get_running(self, ticket_id: str) -> dict[str, Any] | None:
        """Get info about a running agent for a workspace, or None."""
        return self._running.get(ticket_id)

    def cancel(self, ticket_id: str) -> bool:
        """Kill a running agent for a workspace. Returns True if killed."""
        info = self._running.pop(ticket_id, None)
        if info is None:
            return False
        pid = info["pid"]
        try:
            import os
            import signal
            os.kill(pid, signal.SIGTERM)
            logger.info("Killed agent %s (pid %d) for %s", info["agent_id"], pid, ticket_id)
        except ProcessLookupError:
            logger.warning("Agent process %d already gone for %s", pid, ticket_id)
        return True
```

Now wire `register_running` / `unregister_running` into `_execute_cli`. In the `_execute_cli` method, the actual subprocess happens inside `adapter.execute_in_workspace()`. We need to hook into this. The simplest approach: register before calling `execute_in_workspace` and unregister after. We need the PID from the subprocess.

Update `_execute_cli` method — wrap the execute call:

```python
    async def _execute_cli(
        self,
        agent_id: str,
        prompt: str,
        model: str,
        workspace: Workspace,
        allowed_tools: list[str],
    ) -> AgentResult:
        """Execute agent via Claude Code CLI (subprocess)."""
        adapter: ClaudeCodeAdapter = self._llm  # type: ignore[assignment]

        ticket_id = workspace.state.ticket_id
        # Register as running (PID updated when subprocess starts)
        self.register_running(ticket_id, agent_id, pid=0)
        try:
            response = await adapter.execute_in_workspace(
                prompt=prompt,
                cwd=str(workspace.source_dir),
                allowed_tools=allowed_tools if allowed_tools else None,
                model=model,
            )
        finally:
            self.unregister_running(ticket_id)

        # Write output
        output_path = workspace.reports_dir / f"{agent_id}-output.md"
        output_path.write_text(response.content, encoding="utf-8")

        return AgentResult(
            agent_id=agent_id,
            success=True,
            output=response.content,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
```

Note: The PID is 0 because `execute_in_workspace` manages the subprocess internally. For proper PID tracking, the `ClaudeCodeAdapter` would need to expose it. For now, `cancel()` with PID=0 will attempt `os.kill(0, ...)` which sends to the process group — this is a limitation. A future improvement would be to have the adapter expose the subprocess PID. For the dashboard MVP, we can use a simpler approach: set a cancellation flag that the adapter checks.

Actually, let's simplify: instead of PID-based killing, use an `asyncio.Event` cancellation pattern. Update the approach:

Replace the cancel methods with:

```python
    def register_running(self, ticket_id: str, agent_id: str, pid: int = 0) -> None:
        """Register an agent as running for a workspace."""
        self._running[ticket_id] = {
            "agent_id": agent_id,
            "pid": pid,
            "started_at": time.time(),
        }

    def cancel(self, ticket_id: str) -> bool:
        """Kill a running agent for a workspace. Returns True if was running."""
        info = self._running.pop(ticket_id, None)
        if info is None:
            return False
        pid = info.get("pid", 0)
        if pid > 0:
            try:
                import os
                import signal
                os.kill(pid, signal.SIGTERM)
                logger.info("Killed agent %s (pid %d) for %s", info["agent_id"], pid, ticket_id)
            except ProcessLookupError:
                logger.warning("Agent process %d already gone for %s", pid, ticket_id)
        else:
            logger.info("Cancelled agent %s for %s (no PID, will stop on next check)", info["agent_id"], ticket_id)
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/unit/test_agent_runtime_cancel.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite to check no regressions**

Run: `source .venv/bin/activate && pytest tests/unit/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/agent_runtime.py tests/unit/test_agent_runtime_cancel.py
git commit -m "feat: add agent tracking and cancellation to AgentRuntime"
```

---

### Task 3: Skip MANUAL_CONTROL in orchestrator + expose cancel

**Files:**
- Modify: `orchestrator/orchestrator.py:423-456` (advance_workspace method)
- Test: `tests/unit/test_orchestrator.py` (add test)

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_orchestrator.py` (find the existing test class):

```python
class TestManualControlSkip:
    async def test_advance_skips_manual_control(self):
        """Orchestrator should skip workspaces in MANUAL_CONTROL state."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from workspace.workspace import Workspace, WorkspaceState

        ws_state = WorkspaceState(
            ticket_id="T-1", company_id="c", repo_id="r",
            workspace_root="/tmp/test",
            current_state="MANUAL_CONTROL",
            previous_state="DEV",
        )
        ws = MagicMock(spec=Workspace)
        ws.state = ws_state

        # Create minimal orchestrator
        from orchestrator.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch._workflow = MagicMock()
        orch._events = None
        orch._mode_handler = None
        orch._notifier = None
        orch._global_config = MagicMock()
        orch._agent_runtime = MagicMock()
        orch._dry_run = False

        # advance_workspace should return without doing anything
        await orch.advance_workspace(ws)
        # Verify no transition was called
        ws.transition.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/test_orchestrator.py::TestManualControlSkip -v`
Expected: FAIL or ERROR — MANUAL_CONTROL not handled in advance_workspace

- [ ] **Step 3: Add MANUAL_CONTROL skip to orchestrator**

In `orchestrator/orchestrator.py`, in `advance_workspace()`, after the BLOCKED check (around line 428), add:

```python
        if current == "MANUAL_CONTROL":
            return  # Under human control — skip entirely
```

So the block becomes:

```python
        if current == "BLOCKED":
            return  # Waiting for human reply

        if current == "MANUAL_CONTROL":
            return  # Under human control — skip entirely

        if current == "AWAITING_APPROVAL":
```

Also add `"MANUAL_CONTROL"` to the terminal set that cleans up workspaces? No — MANUAL_CONTROL is not terminal, the workspace should stay in `_active_workspaces`.

- [ ] **Step 4: Run tests**

Run: `source .venv/bin/activate && pytest tests/unit/test_orchestrator.py -v --tb=short`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: orchestrator skips MANUAL_CONTROL workspaces"
```

---

### Task 4: Add terminal_command to DashboardConfig

**Files:**
- Modify: `config/schemas.py:76-80` (DashboardConfig)
- Test: `tests/unit/test_config_loader.py` (verify default)

- [ ] **Step 1: Add field**

In `config/schemas.py`, update `DashboardConfig`:

```python
@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    db_path: str = "data/events.db"
    terminal_command: str = "gnome-terminal -- bash -c"
```

- [ ] **Step 2: Run existing config tests to verify no breakage**

Run: `source .venv/bin/activate && pytest tests/unit/test_config_loader.py -v --tb=short`
Expected: ALL PASS (new field has a default, so existing tests should still work)

- [ ] **Step 3: Commit**

```bash
git add config/schemas.py
git commit -m "feat: add terminal_command to DashboardConfig"
```

---

### Task 5: Create dashboard action endpoints (backend)

**Files:**
- Create: `dashboard/actions.py`
- Modify: `dashboard/web.py` (register action routes, pass dependencies)
- Modify: `main.py` (pass orchestrator/mode_handler to create_app)
- Test: `tests/unit/test_dashboard_actions.py` (new file)

- [ ] **Step 1: Write failing tests for action endpoints**

Create `tests/unit/test_dashboard_actions.py`:

```python
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
        ws.transition.assert_called_with("MANUAL_CONTROL")

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/unit/test_dashboard_actions.py -v`
Expected: FAIL — `create_app` doesn't accept `orchestrator`/`mode_handler` params yet

- [ ] **Step 3: Create `dashboard/actions.py`**

```python
"""Dashboard action endpoints — POST handlers for workspace actions."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"DONE", "FAILED", "ARCHIVED"}


def _find_workspace(orchestrator: Any, ticket_id: str) -> Any | None:
    """Find workspace by ticket_id in active workspaces."""
    for ws in orchestrator.get_active_workspaces():
        if ws.state.ticket_id == ticket_id:
            return ws
    return None


def _error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "error", "message": msg}, status_code=status)


def build_action_routes(
    orchestrator: Any,
    mode_handler: Any,
    event_bus: Any | None = None,
    global_config: Any | None = None,
) -> list:
    """Build Starlette Route objects for all action endpoints."""
    from starlette.routing import Route

    async def approve(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "AWAITING_APPROVAL":
            return _error(f"Cannot approve: state is {ws.state.current_state}")

        from integrations.telegram.handlers.approval import ApprovalHandler
        handler = ApprovalHandler()
        next_state = handler.resolve_next_state(ws)
        ws.transition(next_state)
        if event_bus:
            event_bus.emit(
                "dashboard_approve",
                f"Approved {ticket_id} via dashboard → {next_state}",
                ticket_id=ticket_id,
                data={"new_state": next_state},
            )
        return JSONResponse({"status": "ok", "new_state": next_state})

    async def reject(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "AWAITING_APPROVAL":
            return _error(f"Cannot reject: state is {ws.state.current_state}")

        previous = ws.state.previous_state or "ANALYSIS"
        ws.transition(previous)
        if event_bus:
            event_bus.emit(
                "dashboard_reject",
                f"Rejected {ticket_id} via dashboard → back to {previous}",
                ticket_id=ticket_id,
                data={"new_state": previous},
            )
        return JSONResponse({"status": "ok", "new_state": previous})

    async def retry(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state not in ("BLOCKED", "FAILED"):
            return _error(f"Cannot retry: state is {ws.state.current_state}")

        target = ws.state.previous_state or "ANALYSIS"
        ws.state.human_input_pending = False
        ws.state.error = None
        ws.transition(target)
        ws.save_state()
        if event_bus:
            event_bus.emit(
                "dashboard_retry",
                f"Retried {ticket_id} via dashboard → {target}",
                ticket_id=ticket_id,
                data={"new_state": target},
            )
        return JSONResponse({"status": "ok", "new_state": target})

    async def take_control(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state in TERMINAL_STATES | {"MANUAL_CONTROL"}:
            return _error(f"Cannot take control: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        confirm = body.get("confirm", False)

        # Check if agent is running
        agent_runtime = orchestrator._agent_runtime
        running = agent_runtime.get_running(ticket_id)
        if running and not confirm:
            elapsed = time.time() - running.get("started_at", time.time())
            return JSONResponse({
                "status": "agent_running",
                "agent": running["agent_id"],
                "started_ago": f"{int(elapsed)}s",
            })

        # Kill agent if running
        if running:
            agent_runtime.cancel(ticket_id)

        # Transition to MANUAL_CONTROL
        ws.transition("MANUAL_CONTROL")
        ws.update_state(manual_control_started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        # Build claude command
        command = _build_claude_command(ws)

        # Launch terminal
        terminal_cmd = "gnome-terminal -- bash -c"
        if global_config and hasattr(global_config, "dashboard"):
            terminal_cmd = getattr(global_config.dashboard, "terminal_command", terminal_cmd)
        try:
            full_cmd = f'{terminal_cmd} \'{command}; exec bash\''
            subprocess.Popen(full_cmd, shell=True)
        except Exception as e:
            logger.warning("Failed to launch terminal: %s", e)

        if event_bus:
            event_bus.emit(
                "manual_control_started",
                f"Manual control taken for {ticket_id}",
                ticket_id=ticket_id,
                data={"previous_state": ws.state.previous_state},
            )
        return JSONResponse({"status": "ok", "command": command})

    async def release_control(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "MANUAL_CONTROL":
            return _error(f"Cannot release: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        comment = body.get("comment", "")

        ws.update_state(manual_control_comment=comment)
        ws.transition("ANALYSIS")

        if event_bus:
            event_bus.emit(
                "manual_control_released",
                f"Manual control released for {ticket_id}" + (f": {comment}" if comment else ""),
                ticket_id=ticket_id,
                data={"comment": comment},
            )
        return JSONResponse({"status": "ok", "new_state": "ANALYSIS"})

    async def set_mode(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body")
        mode = body.get("mode", "")
        try:
            mode_handler.set_mode(mode)
        except ValueError as e:
            return _error(str(e))
        if event_bus:
            event_bus.emit("mode_changed", f"Mode set to {mode} via dashboard", data={"mode": mode})
        return JSONResponse({"status": "ok", "mode": mode})

    async def daemon_status(request: Request) -> JSONResponse:
        workspaces = orchestrator.get_active_workspaces()
        active = len(workspaces)
        blocked = sum(1 for ws in workspaces if ws.state.current_state == "BLOCKED")
        awaiting = sum(1 for ws in workspaces if ws.state.current_state == "AWAITING_APPROVAL")
        manual = sum(1 for ws in workspaces if ws.state.current_state == "MANUAL_CONTROL")

        mode = mode_handler.get_mode() if mode_handler else "auto"

        return JSONResponse({
            "mode": mode,
            "active": active,
            "blocked": blocked,
            "awaiting": awaiting,
            "manual_control": manual,
        })

    return [
        Route("/api/workspaces/{ticket_id:path}/approve", approve, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/reject", reject, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/retry", retry, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/take-control", take_control, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/release-control", release_control, methods=["POST"]),
        Route("/api/daemon/mode", set_mode, methods=["POST"]),
        Route("/api/daemon/status", daemon_status),
    ]


def _build_claude_command(ws: Any) -> str:
    """Build the claude CLI command with full workspace context."""
    state = ws.state
    source_dir = str(ws.source_dir)
    reports_dir = str(ws.reports_dir)
    meta_dir = str(ws.meta_dir)

    # List available reports
    reports = []
    try:
        for f in Path(reports_dir).iterdir():
            if f.is_file():
                reports.append(f"  {f.name}")
    except (OSError, FileNotFoundError):
        pass

    # List available meta files
    meta = []
    try:
        for f in Path(meta_dir).iterdir():
            if f.is_file():
                meta.append(f"  {f.name}")
    except (OSError, FileNotFoundError):
        pass

    parts = [
        f"You are resuming work on ticket {state.ticket_id}.",
        f"Previous state: {state.previous_state or 'unknown'} (iteration history: {state.stage_iterations})",
    ]
    if state.error:
        parts.append(f"Error/escalation: {state.error}")
    if reports:
        parts.append("Reports available in ../reports/:\n" + "\n".join(reports))
    if meta:
        parts.append("Meta files in ../meta/:\n" + "\n".join(meta))
    parts.append(
        "The operator has taken manual control. Ask them what they want to do."
    )

    prompt = "\n\n".join(parts)
    # Escape single quotes for shell
    prompt = prompt.replace("'", "'\\''")
    return f"cd {source_dir} && claude -p '{prompt}'"
```

- [ ] **Step 4: Update `dashboard/web.py` to accept orchestrator and register action routes**

In `dashboard/web.py`, update `create_app` signature and body:

```python
def create_app(
    bus: EventBus,
    store: EventStore,
    workspace_base_dir: str = "",
    orchestrator: Any | None = None,
    mode_handler: Any | None = None,
    global_config: Any | None = None,
) -> Starlette:
```

At the end of `create_app`, before `return Starlette(routes=routes)`, add:

```python
    # Action routes (only if orchestrator is available)
    if orchestrator is not None:
        from dashboard.actions import build_action_routes
        action_routes = build_action_routes(
            orchestrator=orchestrator,
            mode_handler=mode_handler,
            event_bus=bus,
            global_config=global_config,
        )
        routes.extend(action_routes)
```

- [ ] **Step 5: Update `main.py` to pass orchestrator and mode_handler to create_app**

In `main.py`, find the `create_app` call and update:

```python
            app = create_app(
                event_bus, event_store,
                workspace_base_dir=global_config.workspaces.base_dir,
                orchestrator=orchestrator,
                mode_handler=mode_handler,
                global_config=global_config,
            )
```

- [ ] **Step 6: Run action tests**

Run: `source .venv/bin/activate && pytest tests/unit/test_dashboard_actions.py -v`
Expected: ALL PASS

- [ ] **Step 7: Run full test suite**

Run: `source .venv/bin/activate && pytest tests/unit/ --tb=short`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add dashboard/actions.py dashboard/web.py main.py tests/unit/test_dashboard_actions.py
git commit -m "feat: add dashboard action endpoints (approve, reject, retry, take-control, release, mode)"
```

---

### Task 6: Extract CSS from index.html into style.css

**Files:**
- Create: `dashboard/static/style.css`
- Modify: `dashboard/static/index.html`

- [ ] **Step 1: Create `dashboard/static/style.css`**

Extract the entire `<style>` block from `dashboard/static/index.html` (lines 8-527) into a new file `dashboard/static/style.css`. The content is everything between `<style>` and `</style>` tags.

Add new styles for MANUAL_CONTROL state and action elements at the end:

```css
  /* ── MANUAL_CONTROL state ── */
  .state-MANUAL_CONTROL { background: #2d1a3d; color: #d2a8ff; border: 1px solid #8957e5; }

  @keyframes pulse-purple {
    0%, 100% { box-shadow: 0 0 0 0 rgba(210, 168, 255, 0.4); }
    50% { box-shadow: 0 0 0 3px rgba(210, 168, 255, 0.1); }
  }
  .card-manual { animation: pulse-purple 2s ease-in-out infinite; }

  @keyframes badge-pulse-purple {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }
  .badge-pulse-purple { animation: badge-pulse-purple 1.5s ease-in-out infinite; }

  /* ── Action bar ── */
  .action-bar {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .action-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    color: #8b949e;
    letter-spacing: .06em;
    margin-right: 8px;
  }

  .action-btn {
    border-radius: 5px;
    padding: 4px 12px;
    font-size: 11px;
    cursor: pointer;
    font-weight: 600;
    border: 1px solid;
    background: none;
    transition: opacity .15s;
  }
  .action-btn:hover { opacity: 0.85; }
  .action-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .btn-approve { background: #1a3d1a; color: #56d364; border-color: #2ea043; }
  .btn-reject { background: #3d1a1a; color: #f85149; border-color: #da3633; }
  .btn-retry { background: #21262d; color: #c9d1d9; border-color: #30363d; }
  .btn-take-control { background: #1a2d3d; color: #79c0ff; border-color: #1f6feb; font-weight: 700; }
  .btn-finished { background: #1f6feb; color: #fff; border-color: #1f6feb; font-weight: 700; }

  .action-links { margin-left: auto; font-size: 11px; }
  .action-links a { color: #58a6ff; text-decoration: none; }
  .action-links a:hover { text-decoration: underline; }

  /* ── Manual control banner ── */
  .manual-banner {
    background: #2d1a3d44;
    border: 1px solid #8957e5;
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 14px;
  }

  .manual-banner-header {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .manual-banner-finish {
    margin-top: 12px;
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }

  .manual-comment {
    flex: 1;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 12px;
    min-height: 36px;
    color: #c9d1d9;
    font-size: 12px;
    font-family: inherit;
    resize: vertical;
  }

  /* ── Confirmation dialog ── */
  .dialog-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .dialog {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 20px 24px;
    width: 380px;
    max-width: 90vw;
  }

  .dialog-title {
    font-size: 14px;
    font-weight: 700;
    color: #e6edf3;
    margin-bottom: 12px;
  }

  .dialog-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
  }

  /* ── Sidebar daemon status ── */
  .daemon-status {
    padding: 5px 12px;
    margin: 2px 8px;
    font-size: 11px;
  }

  .status-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-right: 5px;
  }
  .status-dot.online { background: #56d364; }
  .status-dot.offline { background: #f85149; }

  /* ── Summary stats ── */
  .toolbar-stats {
    font-size: 11px;
    color: #8b949e;
  }
  .toolbar-stats .stat-blocked { color: #f85149; }
  .toolbar-stats .stat-awaiting { color: #e3b341; }

  /* ── Hide-done toggle ── */
  .toggle-done {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    color: #6e7681;
    cursor: pointer;
    user-select: none;
  }
```

- [ ] **Step 2: Update index.html — remove inline style, add link**

Replace the entire `<style>...</style>` block with:

```html
<link rel="stylesheet" href="/static/style.css">
```

- [ ] **Step 3: Mount static files in web.py**

In `dashboard/web.py`, add static file serving. Add import and mount:

```python
from starlette.staticfiles import StaticFiles
```

In `create_app`, before `return Starlette(routes=routes)`, add:

```python
    static_dir = str(Path(__file__).parent / "static")
```

And change the return to:

```python
    app = Starlette(routes=routes)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app
```

- [ ] **Step 4: Verify dashboard loads in browser**

Run the daemon and open http://localhost:8080 — the page should render with styles loaded from `/static/style.css`.

- [ ] **Step 5: Commit**

```bash
git add dashboard/static/style.css dashboard/static/index.html dashboard/web.py
git commit -m "refactor: extract CSS to style.css, mount static files"
```

---

### Task 7: Split JavaScript into ES modules

**Files:**
- Create: `dashboard/static/js/helpers.js`
- Create: `dashboard/static/js/api.js`
- Create: `dashboard/static/js/board.js`
- Create: `dashboard/static/js/detail.js`
- Create: `dashboard/static/js/actions.js`
- Create: `dashboard/static/js/reports.js`
- Create: `dashboard/static/js/events.js`
- Create: `dashboard/static/js/app.js`
- Modify: `dashboard/static/index.html`

This is the largest task. The current `index.html` has ~490 lines of JS in a single `<script>` block. We split it into focused modules.

- [ ] **Step 1: Create `dashboard/static/js/helpers.js`**

```javascript
// helpers.js — utility functions shared across modules

export function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function fmtTs(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return esc(ts);
  return d.toLocaleString('en-GB', {
    day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).replace(',', '');
}

export function timeAgo(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  if (isNaN(d)) return '';
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

export function fmtIso(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  if (isNaN(d)) return esc(isoStr);
  return d.toLocaleString('en-GB', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).replace(',', '');
}

export const PIPELINE_STAGES = ['NEW', 'ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW', 'DONE'];
export const STAGE_ORDER = {};
PIPELINE_STAGES.forEach((s, i) => { STAGE_ORDER[s] = i; });

export const BADGE_CLASS = {
  agent_dispatched: 'badge-green', agent_completed: 'badge-green',
  workspace_created: 'badge-green', pr_created: 'badge-green',
  project_loaded: 'badge-green', workspace_resumed: 'badge-green',
  agent_failed: 'badge-red', escalation_sent: 'badge-red',
  stage_transition: 'badge-blue',
  tg_message_received: 'badge-yellow', tg_message_sent: 'badge-yellow', intent_parsed: 'badge-yellow',
  approval_requested: 'badge-purple', poll_cycle: 'badge-gray', daemon_started: 'badge-gray',
  dashboard_approve: 'badge-green', dashboard_reject: 'badge-red',
  dashboard_retry: 'badge-blue', manual_control_started: 'badge-purple',
  manual_control_released: 'badge-purple', mode_changed: 'badge-purple',
};

export function badgeClass(type) {
  return BADGE_CLASS[type] || 'badge-gray';
}

export function stateBadgeHtml(stateVal) {
  const cls = 'state-' + (stateVal || 'NEW').replace(/[^A-Z_]/g, '');
  let pulseClass = '';
  if (stateVal === 'BLOCKED') pulseClass = ' badge-pulse-red';
  if (stateVal === 'AWAITING_APPROVAL') pulseClass = ' badge-pulse-yellow';
  if (stateVal === 'MANUAL_CONTROL') pulseClass = ' badge-pulse-purple';
  return `<span class="state-badge ${cls}${pulseClass}">${esc(stateVal || 'NEW')}</span>`;
}
```

- [ ] **Step 2: Create `dashboard/static/js/api.js`**

```javascript
// api.js — all fetch calls to the backend

export async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function fetchText(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.text();
}

export async function postJSON(url, body = {}) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok && !data.status) {
    throw new Error(data.message || `HTTP ${resp.status}`);
  }
  return data;
}

export async function loadWorkspaces(projectId) {
  let url = '/api/workspaces';
  if (projectId) url += `?project_id=${encodeURIComponent(projectId)}`;
  const data = await fetchJSON(url);
  return data.workspaces || [];
}

export async function loadEvents(opts = {}) {
  let url = '/api/events?limit=' + (opts.limit || 200);
  if (opts.projectId) url += `&project_id=${encodeURIComponent(opts.projectId)}`;
  if (opts.ticketId) url += `&ticket_id=${encodeURIComponent(opts.ticketId)}`;
  const data = await fetchJSON(url);
  return data.events || [];
}

export async function loadDaemonStatus() {
  return fetchJSON('/api/daemon/status');
}

export async function loadReport(ticketId, filename, folder) {
  return fetchText(
    `/api/workspaces/${encodeURIComponent(ticketId)}/report/${encodeURIComponent(filename)}?folder=${encodeURIComponent(folder)}`
  );
}
```

- [ ] **Step 3: Create `dashboard/static/js/actions.js`**

```javascript
// actions.js — action button handlers and confirmation dialogs

import { postJSON } from './api.js';
import { esc } from './helpers.js';

export async function approveWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/approve`);
}

export async function rejectWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/reject`);
}

export async function retryWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/retry`);
}

export async function takeControl(ticketId, confirm = false) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/take-control`, { confirm });
}

export async function releaseControl(ticketId, comment = '') {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/release-control`, { comment });
}

export async function setMode(mode) {
  return postJSON('/api/daemon/mode', { mode });
}

export function showConfirmDialog(title, bodyHtml, confirmLabel, onConfirm) {
  const overlay = document.createElement('div');
  overlay.className = 'dialog-overlay';
  overlay.innerHTML = `<div class="dialog">
    <div class="dialog-title">${esc(title)}</div>
    <div>${bodyHtml}</div>
    <div class="dialog-actions">
      <button class="action-btn btn-retry" id="dlg-cancel">Cancel</button>
      <button class="action-btn btn-take-control" id="dlg-confirm">${esc(confirmLabel)}</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector('#dlg-cancel').onclick = () => overlay.remove();
  overlay.querySelector('#dlg-confirm').onclick = async () => {
    overlay.remove();
    await onConfirm();
  };
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
}
```

- [ ] **Step 4: Create `dashboard/static/js/reports.js`**

```javascript
// reports.js — report tab loading and display

import { loadReport } from './api.js';
import { esc } from './helpers.js';

export function renderReportTabs(ticketId, reports, meta) {
  const allFiles = [];
  (reports || []).forEach(f => allFiles.push({ name: f, folder: 'reports' }));
  (meta || []).forEach(f => allFiles.push({ name: f, folder: 'meta' }));

  if (allFiles.length === 0) return '';

  const tabBar = allFiles.map(r =>
    `<button class="tab-btn" data-ticket="${esc(ticketId)}" data-file="${esc(r.name)}" data-folder="${esc(r.folder)}"
      >${esc(r.name)}</button>`
  ).join('');

  return `<div class="detail-section">
    <div class="detail-section-title">Reports &amp; Files</div>
    <div class="tab-bar" id="report-tabs">${tabBar}</div>
    <div id="report-content-area"><div style="color:#6e7681;font-size:12px;">Select a file to view.</div></div>
  </div>`;
}

export function bindReportTabClicks() {
  const tabs = document.getElementById('report-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', async (e) => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;
    const ticketId = btn.dataset.ticket;
    const file = btn.dataset.file;
    const folder = btn.dataset.folder;

    // Update active tab
    tabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const area = document.getElementById('report-content-area');
    area.innerHTML = '<div style="color:#6e7681;font-size:12px;padding:8px 0;">Loading…</div>';

    try {
      const text = await loadReport(ticketId, file, folder);
      area.innerHTML = `<div class="report-content">${esc(text)}</div>`;
    } catch (err) {
      area.innerHTML = `<div style="color:#f85149;font-size:12px;">Error: ${esc(String(err))}</div>`;
    }
  });
}
```

- [ ] **Step 5: Create `dashboard/static/js/events.js`**

```javascript
// events.js — event log rendering

import { esc, fmtTs, badgeClass } from './helpers.js';

export function renderEventsHtml(events, compact) {
  if (!events || events.length === 0) {
    return '<div class="state-msg">No events found.</div>';
  }

  return events.map(ev => {
    const cls = badgeClass(ev.event_type);
    const parts = [];
    if (!compact && ev.project_id) parts.push(`project: ${esc(ev.project_id)}`);
    if (!compact && ev.ticket_id)  parts.push(`ticket: ${esc(ev.ticket_id)}`);
    if (ev.agent_id) parts.push(`agent: ${esc(ev.agent_id)}`);
    if (ev.data && ev.data.duration != null) parts.push(`${ev.data.duration.toFixed(1)}s`);
    if (ev.data && ev.data.input_tokens != null) parts.push(`${ev.data.input_tokens}/${ev.data.output_tokens} tok`);

    return `<div class="event-row">
      <span class="event-ts">${fmtTs(ev.timestamp)}</span>
      <span class="event-badge ${cls}">${esc(ev.event_type)}</span>
      <div class="event-body">
        <div class="event-msg">${esc(ev.message)}</div>
        ${parts.length ? `<div class="event-meta">${parts.map(p => `<span>${p}</span>`).join('')}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}
```

- [ ] **Step 6: Create `dashboard/static/js/board.js`**

```javascript
// board.js — board view rendering

import { loadWorkspaces } from './api.js';
import { esc, timeAgo, stateBadgeHtml } from './helpers.js';
import { approveWorkspace } from './actions.js';

export async function renderBoard(projectId, showDone = true) {
  const content = document.getElementById('content');
  try {
    const workspaces = await loadWorkspaces(projectId);

    if (workspaces.length === 0) {
      content.innerHTML = '<div class="state-msg">No workspaces found.</div>';
      return { workspaces };
    }

    let filtered = workspaces;
    if (!showDone) {
      filtered = workspaces.filter(ws => !['DONE', 'FAILED', 'ARCHIVED'].includes(ws.current_state));
    }

    // Group by project
    const byProject = {};
    filtered.forEach(ws => {
      const proj = ws.company_id || 'unknown';
      if (!byProject[proj]) byProject[proj] = [];
      byProject[proj].push(ws);
    });

    let html = '';
    for (const [proj, wsList] of Object.entries(byProject)) {
      html += `<div class="project-group">
        <div class="project-group-title">${esc(proj)}</div>
        <div class="cards-grid">`;
      for (const ws of wsList) {
        html += renderCard(ws);
      }
      html += `</div></div>`;
    }

    content.innerHTML = html;

    // Bind inline approve buttons
    content.querySelectorAll('[data-action="approve"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        try {
          await approveWorkspace(tid);
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Approve failed: ' + err.message);
        }
      });
    });

    return { workspaces };
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error loading workspaces: ${esc(String(e))}</div>`;
    return { workspaces: [] };
  }
}

function renderCard(ws) {
  const stateVal = ws.current_state || 'NEW';
  let cardClass = 'card';
  if (stateVal === 'BLOCKED') cardClass += ' card-blocked';
  if (stateVal === 'AWAITING_APPROVAL') cardClass += ' card-awaiting';
  if (stateVal === 'MANUAL_CONTROL') cardClass += ' card-manual';

  const dimmed = ['DONE', 'FAILED', 'ARCHIVED'].includes(stateVal);

  const prLink = ws.pr_url
    ? `<a class="card-pr-link" href="${esc(ws.pr_url)}" target="_blank" onclick="event.stopPropagation()">PR #${esc(String(ws.pr_number || ''))}</a>`
    : '';

  const errorHtml = ws.error
    ? `<div class="card-error" title="${esc(ws.error)}">${esc(ws.error)}</div>`
    : '';

  const approveBtn = stateVal === 'AWAITING_APPROVAL'
    ? `<button class="action-btn btn-approve" data-action="approve" data-ticket="${esc(ws.ticket_id)}" style="padding:1px 8px;font-size:10px;">Approve</button>`
    : '';

  const manualLabel = stateVal === 'MANUAL_CONTROL'
    ? `<div style="font-size:10px;color:#d2a8ff;">You have control</div>`
    : '';

  // Iteration info
  const iters = ws.stage_iterations || {};
  const totalIters = Object.values(iters).reduce((a, b) => a + b, 0);
  const iterLabel = totalIters > 0 ? `<span style="font-size:10px;color:#58a6ff;">iter ${totalIters}</span>` : '';

  return `<div class="${cardClass}" data-ticket="${esc(ws.ticket_id)}" style="${dimmed ? 'opacity:0.5;' : ''}">
    <div class="card-header">
      <span class="card-ticket">${esc(ws.ticket_id)}</span>
      ${stateBadgeHtml(stateVal)}
    </div>
    <div class="card-repo">${esc(ws.repo_id || '')}</div>
    ${errorHtml}
    ${manualLabel}
    <div class="card-footer">
      <span class="card-time">${esc(timeAgo(ws.started_at))}</span>
      ${iterLabel}
      ${approveBtn}
      ${prLink}
    </div>
  </div>`;
}
```

- [ ] **Step 7: Create `dashboard/static/js/detail.js`**

```javascript
// detail.js — ticket detail view

import { loadWorkspaces, loadEvents } from './api.js';
import { esc, timeAgo, fmtIso, stateBadgeHtml, PIPELINE_STAGES, STAGE_ORDER } from './helpers.js';
import { renderEventsHtml } from './events.js';
import { renderReportTabs, bindReportTabClicks } from './reports.js';
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, showConfirmDialog } from './actions.js';

export async function renderDetail(ticketId, onBack) {
  const content = document.getElementById('content');
  content.innerHTML = '<div class="state-msg">Loading…</div>';

  try {
    const workspaces = await loadWorkspaces();
    const ws = workspaces.find(w => w.ticket_id === ticketId);

    if (!ws) {
      content.innerHTML = `<div class="state-msg" style="color:#f85149;">Workspace not found: ${esc(ticketId)}</div>`;
      return;
    }

    let events = [];
    try {
      events = await loadEvents({ ticketId, limit: 200 });
    } catch (e) { /* ignore */ }

    const stateVal = ws.current_state || 'NEW';

    // Build HTML sections
    const headerHtml = buildHeader(ws, stateVal, onBack);
    const actionBarHtml = stateVal === 'MANUAL_CONTROL'
      ? buildManualBanner(ws)
      : buildActionBar(ws, stateVal);
    const pipelineHtml = buildPipeline(ws, stateVal);
    const infoHtml = buildInfoSection(ws);
    const reportsHtml = renderReportTabs(ws.ticket_id, ws.reports, ws.meta);

    // Events — show last 5, expandable
    const recentEvents = events.slice(0, 5);
    const eventsPreview = renderEventsHtml(recentEvents, true);
    const allEventsHtml = renderEventsHtml(events, true);
    const expandLabel = events.length > 5 ? `<div style="cursor:pointer;color:#58a6ff;font-size:11px;" id="expand-events">Show all (${events.length} events)</div>` : '';

    content.innerHTML = `<div id="detail-view">
      ${headerHtml}
      ${actionBarHtml}
      <div class="pipeline-bar">
        <div class="pipeline-bar-title">Pipeline</div>
        ${pipelineHtml}
      </div>
      ${infoHtml}
      ${reportsHtml}
      <div class="detail-section">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
          <div class="detail-section-title" style="margin-bottom:0;">Event Timeline</div>
          ${expandLabel}
        </div>
        <div class="event-list" id="events-list">${eventsPreview}</div>
      </div>
    </div>`;

    // Bind expand events
    const expandBtn = document.getElementById('expand-events');
    if (expandBtn) {
      expandBtn.addEventListener('click', () => {
        document.getElementById('events-list').innerHTML = allEventsHtml;
        expandBtn.style.display = 'none';
      });
    }

    // Bind report tabs
    bindReportTabClicks();

    // Bind action buttons
    bindActionButtons(ticketId, ws, stateVal, onBack);

  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
  }
}

function buildHeader(ws, stateVal, onBack) {
  return `<div class="detail-header">
    <button class="back-btn" id="back-btn">← Back</button>
    <span class="detail-ticket-id">${esc(ws.ticket_id)}</span>
    ${stateBadgeHtml(stateVal)}
    <span class="detail-time-info">Started ${esc(timeAgo(ws.started_at))} · Updated ${esc(timeAgo(ws.last_updated_at))}</span>
  </div>`;
}

function buildActionBar(ws, stateVal) {
  const isAwaiting = stateVal === 'AWAITING_APPROVAL';
  const isBlocked = stateVal === 'BLOCKED' || stateVal === 'FAILED';
  const canTakeControl = !['DONE', 'ARCHIVED', 'MANUAL_CONTROL'].includes(stateVal);

  let buttons = '<span class="action-label">Actions</span>';
  if (isAwaiting) {
    buttons += `<button class="action-btn btn-approve" id="act-approve">Approve</button>`;
    buttons += `<button class="action-btn btn-reject" id="act-reject">Reject</button>`;
  }
  if (isBlocked) {
    buttons += `<button class="action-btn btn-retry" id="act-retry">Retry</button>`;
  }
  if (canTakeControl) {
    buttons += `<span style="display:inline-block;width:1px;height:20px;background:#30363d;margin:0 4px;"></span>`;
    buttons += `<button class="action-btn btn-take-control" id="act-take-control">Take Control</button>`;
  }

  let links = '';
  if (ws.pr_url) {
    links += `<a href="${esc(ws.pr_url)}" target="_blank">PR #${esc(String(ws.pr_number || ''))}</a>`;
  }

  return `<div class="action-bar">
    ${buttons}
    <span class="action-links">${links}</span>
  </div>`;
}

function buildManualBanner(ws) {
  const since = timeAgo(ws.manual_control_started_at || ws.last_updated_at);
  const prev = ws.previous_state || '?';
  return `<div class="manual-banner">
    <div class="manual-banner-header">
      ${stateBadgeHtml('MANUAL_CONTROL')}
      <span style="font-size:12px;color:#d2a8ff;">You have control since ${esc(since)}</span>
      <span style="font-size:11px;color:#6e7681;">(was in ${esc(prev)})</span>
    </div>
    <div class="manual-banner-finish">
      <textarea class="manual-comment" id="manual-comment" placeholder="What did you do? (optional)"></textarea>
      <button class="action-btn btn-finished" id="act-finished">Finished</button>
    </div>
  </div>`;
}

function buildPipeline(ws, stateVal) {
  const stageIdx = STAGE_ORDER[stateVal] != null ? STAGE_ORDER[stateVal] : -1;
  const prevIdx = ws.previous_state ? (STAGE_ORDER[ws.previous_state] ?? -1) : -1;

  let html = '<div class="pipeline-stages">';
  PIPELINE_STAGES.forEach((stage, idx) => {
    let dotClass = 'stage-dot';
    let labelClass = 'stage-label';
    let symbol = (idx + 1).toString();

    if (stateVal === 'MANUAL_CONTROL' && idx === prevIdx) {
      dotClass += ' current';
      labelClass += ' current';
      dotClass = dotClass.replace(' current', '');
      // Purple for manual control position
      symbol = '⚡';
      html += `<div class="pipeline-stage"><div class="stage-node">
        <div class="stage-dot" style="background:#2d1a3d;border-color:#8957e5;color:#d2a8ff;box-shadow:0 0 6px #8957e566;">${symbol}</div>
        <div class="stage-label" style="color:#d2a8ff;">${esc(stage)}</div>
      </div>`;
    } else if (stateVal === 'FAILED' && idx === stageIdx) {
      dotClass += ' failed'; labelClass += ' failed'; symbol = '!';
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    } else if (stateVal === 'MANUAL_CONTROL' ? idx < prevIdx : idx < stageIdx) {
      dotClass += ' done'; labelClass += ' done'; symbol = '✓';
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    } else if (idx === stageIdx && stateVal !== 'MANUAL_CONTROL') {
      dotClass += ' current'; labelClass += ' current';
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    } else {
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    }

    if (idx < PIPELINE_STAGES.length - 1) {
      const active = stateVal === 'MANUAL_CONTROL' ? prevIdx : stageIdx;
      const connDone = idx < active ? ' done' : '';
      html += `</div><div class="stage-connector${connDone}"></div>`;
    } else {
      html += '</div>';
    }
  });
  html += '</div>';
  return html;
}

function buildInfoSection(ws) {
  const iters = ws.stage_iterations
    ? Object.entries(ws.stage_iterations).map(([k, v]) => `${esc(k)}: ${esc(String(v))}`).join(', ')
    : '';

  let grid = `<div class="info-grid">
    <span class="info-label">Branch</span><span class="info-value">${esc(ws.branch || '—')}</span>
    <span class="info-label">Repo</span><span class="info-value">${esc(ws.repo_id || '—')}</span>
    <span class="info-label">Project</span><span class="info-value">${esc(ws.company_id || '—')}</span>
    <span class="info-label">Started</span><span class="info-value">${esc(fmtIso(ws.started_at))}</span>
    <span class="info-label">Last updated</span><span class="info-value">${esc(fmtIso(ws.last_updated_at))}</span>`;
  if (ws.pr_url) {
    grid += `<span class="info-label">PR</span><span class="info-value"><a href="${esc(ws.pr_url)}" target="_blank">#${esc(String(ws.pr_number || ''))}</a></span>`;
  }
  if (iters) {
    grid += `<span class="info-label">Iterations</span><span class="info-value">${iters}</span>`;
  }
  grid += '</div>';

  let errorPanel = '';
  if (ws.error) {
    errorPanel = `<div style="background:#161b22;border:1px solid #da3633;border-radius:8px;padding:14px 16px;width:320px;">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;color:#f85149;letter-spacing:.06em;margin-bottom:10px;">Error / Escalation</div>
      <div style="font-size:12px;color:#f85149;line-height:1.5;">${esc(ws.error)}</div>
    </div>`;
  }

  return `<div style="display:flex;gap:14px;margin-bottom:14px;">
    <div class="detail-section" style="flex:1;min-width:0;margin-bottom:0;">
      <div class="detail-section-title">Info</div>${grid}
    </div>
    ${errorPanel}
  </div>`;
}

function bindActionButtons(ticketId, ws, stateVal, onBack) {
  // Back
  const backBtn = document.getElementById('back-btn');
  if (backBtn) backBtn.addEventListener('click', () => onBack(ws.company_id));

  // Approve
  const approveBtn = document.getElementById('act-approve');
  if (approveBtn) {
    approveBtn.addEventListener('click', async () => {
      try {
        await approveWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Approve failed: ' + e.message); }
    });
  }

  // Reject
  const rejectBtn = document.getElementById('act-reject');
  if (rejectBtn) {
    rejectBtn.addEventListener('click', async () => {
      try {
        await rejectWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Reject failed: ' + e.message); }
    });
  }

  // Retry
  const retryBtn = document.getElementById('act-retry');
  if (retryBtn) {
    retryBtn.addEventListener('click', async () => {
      try {
        await retryWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Retry failed: ' + e.message); }
    });
  }

  // Take Control
  const tcBtn = document.getElementById('act-take-control');
  if (tcBtn) {
    tcBtn.addEventListener('click', async () => {
      try {
        const result = await takeControl(ticketId, false);
        if (result.status === 'agent_running') {
          showConfirmDialog(
            `Take Control of ${ticketId}?`,
            `<div style="background:#3d1a1a22;border:1px solid #da363366;border-radius:6px;padding:10px 12px;margin-bottom:8px;">
              <div style="font-size:12px;color:#f85149;font-weight:600;">Agent is currently running</div>
              <div style="font-size:11px;color:#c9d1d9;">${esc(result.agent)} — started ${esc(result.started_ago)} ago</div>
              <div style="font-size:11px;color:#8b949e;margin-top:4px;">Taking control will stop this agent.</div>
            </div>`,
            'Stop Agent & Take Control',
            async () => {
              await takeControl(ticketId, true);
              await renderDetail(ticketId, onBack);
            }
          );
        } else {
          await renderDetail(ticketId, onBack);
        }
      } catch (e) { alert('Take control failed: ' + e.message); }
    });
  }

  // Finished (release control)
  const finBtn = document.getElementById('act-finished');
  if (finBtn) {
    finBtn.addEventListener('click', async () => {
      const comment = document.getElementById('manual-comment')?.value || '';
      try {
        await releaseControl(ticketId, comment);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Release failed: ' + e.message); }
    });
  }
}
```

- [ ] **Step 8: Create `dashboard/static/js/app.js`**

```javascript
// app.js — state management, routing, sidebar, auto-refresh

import { loadWorkspaces, loadEvents, loadDaemonStatus } from './api.js';
import { esc, stateBadgeHtml } from './helpers.js';
import { renderBoard } from './board.js';
import { renderDetail } from './detail.js';
import { renderEventsHtml } from './events.js';

const state = {
  view: 'board',
  projectId: null,
  ticketId: null,
  filterType: '',
  timer: null,
  showDone: true,
};

// ── Navigation ──
function showBoard(projectId) {
  state.view = 'board';
  state.projectId = projectId;
  state.ticketId = null;
  document.getElementById('view-title').textContent = projectId ? `Board: ${projectId}` : 'Board';
  document.getElementById('toolbar-eventlog-controls').style.display = 'none';
  updateActiveNav(projectId ? `nav-proj-${projectId}` : 'nav-board');
  scheduleAutoRefresh();
  doRenderBoard();
}

function showEventLog() {
  state.view = 'eventlog';
  state.ticketId = null;
  document.getElementById('view-title').textContent = 'Event Log';
  document.getElementById('toolbar-eventlog-controls').style.display = 'flex';
  updateActiveNav('nav-eventlog');
  scheduleAutoRefresh();
  doRenderEventLog();
}

function showDetail(ticketId) {
  state.view = 'detail';
  state.ticketId = ticketId;
  document.getElementById('toolbar-eventlog-controls').style.display = 'none';
  document.getElementById('view-title').textContent = `Ticket: ${ticketId}`;
  stopAutoRefresh();
  renderDetail(ticketId, (projectId) => showBoard(projectId));
}

function updateActiveNav(id) {
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
}

// ── Board ──
async function doRenderBoard() {
  const { workspaces } = await renderBoard(state.projectId, state.showDone);
  // Bind card clicks
  document.querySelectorAll('.card[data-ticket]').forEach(card => {
    card.addEventListener('click', () => showDetail(card.dataset.ticket));
  });
  // Update sidebar project list from workspace data
  updateProjectSidebar(workspaces || []);
  // Update toolbar stats
  updateToolbarStats(workspaces || []);
}

// ── Event Log ──
async function doRenderEventLog() {
  const content = document.getElementById('content');
  try {
    let events = await loadEvents({ projectId: state.projectId });
    if (state.filterType) {
      events = events.filter(e => e.event_type === state.filterType);
    }
    content.innerHTML = `<div class="event-list">${renderEventsHtml(events, false)}</div>`;
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
  }
}

// ── Sidebar project list (from workspace data, not events) ──
function updateProjectSidebar(workspaces) {
  const pl = document.getElementById('project-list');
  const projects = [...new Set(workspaces.map(ws => ws.company_id).filter(Boolean))];
  if (projects.length === 0) {
    pl.innerHTML = '<div style="padding:6px 16px;color:#6e7681;font-size:12px;">No projects yet.</div>';
    return;
  }
  pl.innerHTML = projects.map(p =>
    `<a class="nav-link" id="nav-proj-${esc(p)}">${esc(p)}</a>`
  ).join('');
  // Bind clicks
  projects.forEach(p => {
    const el = document.getElementById(`nav-proj-${p}`);
    if (el) el.addEventListener('click', () => showBoard(p));
  });
}

// ── Toolbar stats ──
function updateToolbarStats(workspaces) {
  const stats = document.getElementById('toolbar-stats');
  if (!stats) return;
  const active = workspaces.filter(ws => !['DONE', 'FAILED', 'ARCHIVED'].includes(ws.current_state)).length;
  const blocked = workspaces.filter(ws => ws.current_state === 'BLOCKED').length;
  const awaiting = workspaces.filter(ws => ws.current_state === 'AWAITING_APPROVAL').length;
  const manual = workspaces.filter(ws => ws.current_state === 'MANUAL_CONTROL').length;

  let parts = [`${active} active`];
  if (blocked) parts.push(`<span class="stat-blocked">${blocked} blocked</span>`);
  if (awaiting) parts.push(`<span class="stat-awaiting">${awaiting} awaiting</span>`);
  if (manual) parts.push(`<span style="color:#d2a8ff;">${manual} manual</span>`);
  stats.innerHTML = parts.join(' · ');
}

// ── Daemon status in sidebar ──
async function updateDaemonStatus() {
  try {
    const data = await loadDaemonStatus();
    const el = document.getElementById('daemon-status');
    if (el) {
      el.innerHTML = `
        <div class="daemon-status"><span class="status-dot online"></span>Mode: <span style="color:#e3b341;">${esc(data.mode)}</span></div>
        <div class="daemon-status" style="color:#6e7681;">Active: ${data.active} · Blocked: ${data.blocked}</div>`;
    }
  } catch (e) {
    const el = document.getElementById('daemon-status');
    if (el) el.innerHTML = '<div class="daemon-status"><span class="status-dot offline"></span>Offline</div>';
  }
}

// ── Auto-refresh ──
function stopAutoRefresh() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

function scheduleAutoRefresh() {
  stopAutoRefresh();
  const cb = document.getElementById('auto-refresh-cb');
  if (cb && cb.checked) {
    state.timer = setInterval(() => {
      if (state.view === 'board') doRenderBoard();
      else if (state.view === 'eventlog') doRenderEventLog();
      updateDaemonStatus();
    }, 5000);
  }
}

// ── Init ──
async function init() {
  // Bind nav
  document.getElementById('nav-board').addEventListener('click', () => showBoard(null));
  document.getElementById('nav-eventlog').addEventListener('click', () => showEventLog());

  // Bind filter
  document.getElementById('filter-type').addEventListener('change', () => {
    state.filterType = document.getElementById('filter-type').value;
    doRenderEventLog();
  });

  // Bind refresh
  document.getElementById('toolbar-refresh-btn').addEventListener('click', () => {
    if (state.view === 'board') doRenderBoard();
    else if (state.view === 'eventlog') doRenderEventLog();
    else if (state.view === 'detail') renderDetail(state.ticketId, (pid) => showBoard(pid));
  });

  // Bind auto-refresh toggle
  document.getElementById('auto-refresh-cb').addEventListener('change', () => {
    if (state.view !== 'detail') scheduleAutoRefresh();
  });

  // Bind hide-done toggle
  const hideDone = document.getElementById('toggle-done');
  if (hideDone) {
    hideDone.addEventListener('change', () => {
      state.showDone = !hideDone.checked;
      doRenderBoard();
    });
  }

  // Initial load
  updateActiveNav('nav-board');
  await doRenderBoard();
  await updateDaemonStatus();
  scheduleAutoRefresh();
}

init();
```

- [ ] **Step 9: Rewrite `dashboard/static/index.html` as a shell**

Replace the entire file with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sickle Dashboard</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<nav id="sidebar">
  <div id="sidebar-logo">Sickle</div>
  <div class="sidebar-section">
    <a class="nav-link" id="nav-board">Board</a>
    <a class="nav-link" id="nav-eventlog">Event Log</a>
  </div>
  <div class="sidebar-section">
    <h2>Projects</h2>
    <div id="project-list">
      <div class="state-msg" style="padding:6px 16px;text-align:left;font-size:12px;">Loading…</div>
    </div>
  </div>
  <div class="sidebar-section">
    <h2>Daemon</h2>
    <div id="daemon-status">
      <div class="daemon-status" style="color:#6e7681;">Loading…</div>
    </div>
  </div>
</nav>

<div id="main">
  <div id="toolbar">
    <h1 id="view-title">Board</h1>
    <span id="toolbar-stats" class="toolbar-stats"></span>
    <span id="toolbar-eventlog-controls" style="display:none; align-items:center; gap:10px;">
      <select id="filter-type">
        <option value="">All event types</option>
        <option value="agent_dispatched">agent_dispatched</option>
        <option value="agent_completed">agent_completed</option>
        <option value="agent_failed">agent_failed</option>
        <option value="stage_transition">stage_transition</option>
        <option value="workspace_created">workspace_created</option>
        <option value="pr_created">pr_created</option>
        <option value="dashboard_approve">dashboard_approve</option>
        <option value="dashboard_reject">dashboard_reject</option>
        <option value="dashboard_retry">dashboard_retry</option>
        <option value="manual_control_started">manual_control_started</option>
        <option value="manual_control_released">manual_control_released</option>
        <option value="tg_message_received">tg_message_received</option>
        <option value="tg_message_sent">tg_message_sent</option>
        <option value="poll_cycle">poll_cycle</option>
        <option value="daemon_started">daemon_started</option>
      </select>
    </span>
    <label class="toggle-done">
      <input type="checkbox" id="toggle-done"> Hide done
    </label>
    <button id="toolbar-refresh-btn">Refresh</button>
    <label class="auto-refresh">
      <input type="checkbox" id="auto-refresh-cb" checked>
      Auto (5s)
    </label>
  </div>

  <div id="content">
    <div class="state-msg">Loading…</div>
  </div>
</div>

<script type="module" src="/static/js/app.js"></script>
</body>
</html>
```

- [ ] **Step 10: Verify dashboard loads and works in browser**

Restart the daemon and open http://localhost:8080:
- Board view shows ticket cards grouped by project
- Clicking a project in sidebar filters the board
- Clicking a ticket card opens the detail view
- Detail view shows pipeline, info, reports, events
- Action buttons are visible and contextual
- Back button returns to board

- [ ] **Step 11: Commit**

```bash
git add dashboard/static/
git commit -m "feat: rewrite dashboard frontend as modular vanilla JS with actions and take-control"
```

---

### Task 8: Update feature docs

**Files:**
- Modify: `docs/features/dashboard.md`

- [ ] **Step 1: Update dashboard feature doc**

Add to the requirements section:

```markdown
- FR9: Action buttons: approve, reject, retry from the dashboard
- FR10: Take Control feature: pause pipeline, open Claude Code session, release back
- FR11: Daemon status in sidebar (mode, active/blocked counts)
- FR12: Frontend split into ES modules (no build step)
```

Add to acceptance criteria:

```markdown
- [x] Action endpoints work (approve, reject, retry, take-control, release)
- [x] MANUAL_CONTROL state added to workspace state machine
- [x] Orchestrator skips MANUAL_CONTROL workspaces
- [x] Agent cancellation works when taking control
- [x] Frontend modularized into separate JS files
- [x] Sidebar project list derived from workspaces (not events)
- [x] Take Control launches terminal with Claude Code command
- [x] Release Control transitions back to ANALYSIS
```

Add to changelog:

```markdown
| 2026-04-12 | V2: Operations dashboard with actions, take-control, modular frontend |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/dashboard.md
git commit -m "docs: update dashboard feature doc for v2"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Section 1 (Layout): Task 7 (app.js sidebar, toolbar stats, daemon status)
- ✅ Section 2 (Board): Task 7 (board.js with cards, grouping, state-specific behavior)
- ✅ Section 3 (Detail): Task 7 (detail.js with all subsections)
- ✅ Section 4 (Event Log): Task 7 (events.js, app.js routing)
- ✅ Section 5 (Take Control): Tasks 1 (state machine), 2 (cancel), 3 (orchestrator skip), 5 (endpoints), 7 (frontend)
- ✅ Section 6 (Endpoints): Task 5 (all endpoints)
- ✅ Section 7 (File structure): Tasks 6+7 (CSS + JS split)
- ✅ Section 8 (Backend changes): Tasks 1-5
- ✅ Section 9 (What stays same): verified — EventBus, EventStore, polling all unchanged

**Placeholder scan:** No TBDs, TODOs, or vague steps found. All code blocks are complete.

**Type consistency:** `_find_workspace`, `_build_claude_command`, `build_action_routes` — consistent signatures across tasks. `WorkspaceState` fields match between Task 1 (backend) and Task 7 (frontend reads). `get_running`/`cancel`/`register_running` — consistent between Task 2 (implementation) and Task 5 (usage in actions.py).

# Dashboard Pause Tickets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `PAUSED` ticket state with operator-driven pause/unpause actions in the dashboard, so a ticket can be indefinitely frozen without taking manual control or archiving.

**Architecture:** New `Stage.PAUSED` distinct from `DEFERRED` (auto-resume on quota recovery) and `MANUAL_CONTROL` (operator hands-on). Backend exposes `/pause` and `/unpause` action endpoints (mirroring existing `/take-control` and `/resume`). Orchestrator skips PAUSED workspaces in its poll loop (no auto-resume). Frontend adds inline buttons on board cards plus action-bar buttons on the detail page; both reuse the existing agent-running confirm flow.

**Tech Stack:** Python 3 (Starlette, pytest, asyncio), JS (vanilla ES modules), CSS, Playwright for E2E.

**Spec:** [docs/superpowers/specs/2026-04-27-dashboard-pause-tickets-design.md](docs/superpowers/specs/2026-04-27-dashboard-pause-tickets-design.md)

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| [workspace/workspace.py](workspace/workspace.py) | Modify | Add `Stage.PAUSED`, transitions, paused-state handling. |
| [orchestrator/orchestrator.py](orchestrator/orchestrator.py) | Modify | Add PAUSED to `_SKIP` set in poll cycle. |
| [dashboard/actions.py](dashboard/actions.py) | Modify | New `pause` and `unpause` handlers + routes. |
| [dashboard/static/js/actions.js](dashboard/static/js/actions.js) | Modify | API wrappers `pauseWorkspace` / `unpauseWorkspace`. |
| [dashboard/static/js/helpers.js](dashboard/static/js/helpers.js) | Modify | Pulse class for PAUSED badge. |
| [dashboard/static/js/board.js](dashboard/static/js/board.js) | Modify | Sort order, `card-paused` class, inline pause/unpause buttons. |
| [dashboard/static/js/detail.js](dashboard/static/js/detail.js) | Modify | Pause/Unpause buttons in action bar. |
| [dashboard/static/style.css](dashboard/static/style.css) | Modify | `.state-PAUSED` and `.card-paused` styles. |
| [tests/unit/test_workspace.py](tests/unit/test_workspace.py) | Modify | TestPaused class. |
| [tests/unit/test_orchestrator_deferred.py](tests/unit/test_orchestrator_deferred.py) | Modify | Sweep ignores PAUSED, `_SKIP` includes PAUSED. |
| [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py) | Modify | TestPauseEndpoint / TestUnpauseEndpoint. |
| [tests/e2e/test_actions.py](tests/e2e/test_actions.py) | Modify | E2E happy-path for pause + unpause. |

---

## Task 1: Add `PAUSED` stage, transitions, and paused-state handling

**Files:**
- Modify: [workspace/workspace.py](workspace/workspace.py)
- Modify: [tests/unit/test_workspace.py](tests/unit/test_workspace.py)

- [ ] **Step 1: Write the failing tests**

Append at the end of [tests/unit/test_workspace.py](tests/unit/test_workspace.py):

```python
class TestPaused:
    def test_paused_in_valid_states(self):
        assert "PAUSED" in VALID_STATES

    def test_dev_to_paused(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        assert workspace.state.current_state == Stage.PAUSED
        assert workspace.state.previous_state == Stage.DEV
        assert workspace.state.human_input_pending is True

    def test_paused_from_each_active_stage(self, workspace_dir):
        active = ["ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW"]
        for state in active:
            s = WorkspaceState(
                ticket_id="T-1", company_id="c", repo_id="r",
                workspace_root=str(workspace_dir), current_state=state,
            )
            ws = Workspace(str(workspace_dir), s)
            ws.save_state()
            ws.transition(Stage.PAUSED)
            assert ws.state.current_state == Stage.PAUSED
            assert ws.state.previous_state == state

    def test_paused_rejected_from_invalid_states(self, workspace_dir):
        for state in ["NEW", "BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL",
                      "DEFERRED", "DONE", "FAILED", "ARCHIVED"]:
            s = WorkspaceState(
                ticket_id="T-1", company_id="c", repo_id="r",
                workspace_root=str(workspace_dir), current_state=state,
            )
            ws = Workspace(str(workspace_dir), s)
            ws.save_state()
            with pytest.raises(InvalidTransitionError):
                ws.transition(Stage.PAUSED)

    def test_unpause_returns_to_previous_active_stage(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        workspace.transition(Stage.DEV)
        assert workspace.state.current_state == Stage.DEV
        assert workspace.state.previous_state is None
        assert workspace.state.human_input_pending is False

    def test_paused_to_failed_allowed(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        workspace.transition(Stage.FAILED)
        assert workspace.state.current_state == Stage.FAILED

    def test_paused_to_manual_control_allowed(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        workspace.transition(Stage.MANUAL_CONTROL)
        assert workspace.state.current_state == Stage.MANUAL_CONTROL

    def test_paused_to_done_rejected(self, workspace):
        workspace.transition(Stage.ANALYSIS)
        workspace.transition(Stage.DEV)
        workspace.transition(Stage.PAUSED)
        with pytest.raises(InvalidTransitionError):
            workspace.transition(Stage.DONE)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_workspace.py::TestPaused -v`

Expected: every test FAILs with `KeyError`/`AttributeError`/`InvalidTransitionError` because `Stage.PAUSED` does not exist yet.

- [ ] **Step 3: Add `PAUSED` to the `Stage` enum**

In [workspace/workspace.py](workspace/workspace.py), add a new line at the end of the `Stage` class (line ~30, after `DEFERRED`):

```python
class Stage(StrEnum):
    """Pipeline stages (architecture-v2 §3.3)."""
    NEW = "NEW"
    ANALYSIS = "ANALYSIS"
    DEV = "DEV"
    SCOPE_CHECK = "SCOPE_CHECK"
    QA = "QA"
    PUSHED = "PUSHED"
    PR_REVIEW = "PR_REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    MANUAL_CONTROL = "MANUAL_CONTROL"
    DEFERRED = "DEFERRED"
    PAUSED = "PAUSED"
```

- [ ] **Step 4: Add `PAUSED` to `VALID_TRANSITIONS`**

In [workspace/workspace.py](workspace/workspace.py), update the `VALID_TRANSITIONS` dict so each of the 6 active stages can transition to `PAUSED`, and `PAUSED` can transition out to active stages plus `FAILED` and `MANUAL_CONTROL`. Replace the block at lines 36-51 with:

```python
VALID_TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.NEW:                {Stage.ANALYSIS, Stage.FAILED},
    Stage.ANALYSIS:           {Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.DEV:                {Stage.SCOPE_CHECK, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.SCOPE_CHECK:        {Stage.QA, Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.QA:                 {Stage.PUSHED, Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.PUSHED:             {Stage.PR_REVIEW, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.PR_REVIEW:          {Stage.DEV, Stage.DONE, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.PAUSED},
    Stage.DONE:               {Stage.ARCHIVED},
    Stage.BLOCKED:            {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.FAILED, Stage.MANUAL_CONTROL},
    Stage.FAILED:             {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.MANUAL_CONTROL, Stage.ARCHIVED},
    Stage.ARCHIVED:           set(),
    Stage.AWAITING_APPROVAL:  {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.DONE, Stage.FAILED, Stage.MANUAL_CONTROL},
    Stage.MANUAL_CONTROL:     {Stage.ANALYSIS},
    Stage.DEFERRED:           {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.FAILED, Stage.MANUAL_CONTROL},
    Stage.PAUSED:             {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.FAILED, Stage.MANUAL_CONTROL},
}
```

- [ ] **Step 5: Add `PAUSED` to the `paused_states` set in `transition()`**

In [workspace/workspace.py](workspace/workspace.py), update line 197 (inside the `transition()` method) to include `Stage.PAUSED`. This makes entry-into-PAUSED set `previous_state` and `human_input_pending=True`, and exit-from-PAUSED clear them:

```python
        paused_states = {Stage.BLOCKED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.DEFERRED, Stage.FAILED, Stage.PAUSED}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_workspace.py -v`

Expected: all `TestPaused` tests pass; existing `test_all_valid_transitions` still passes (it iterates `VALID_TRANSITIONS` so the new PAUSED entries will be exercised).

- [ ] **Step 7: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat: add PAUSED stage with active-stage entry/exit transitions"
```

---

## Task 2: Skip `PAUSED` workspaces in the orchestrator poll cycle

**Files:**
- Modify: [orchestrator/orchestrator.py](orchestrator/orchestrator.py)
- Modify: [tests/unit/test_orchestrator_deferred.py](tests/unit/test_orchestrator_deferred.py)

- [ ] **Step 1: Write the failing tests**

Append at the end of [tests/unit/test_orchestrator_deferred.py](tests/unit/test_orchestrator_deferred.py):

```python
class TestPausedSkipped:
    async def test_sweep_deferred_does_not_touch_paused(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-PAUSED-1", state=Stage.DEV)
        ws.transition(Stage.PAUSED)
        orch._active_workspaces.append(ws)

        await orch._sweep_deferred()

        assert ws.state.current_state == Stage.PAUSED
        assert ws.state.previous_state == Stage.DEV

    async def test_paused_in_skip_set(self):
        """The poll cycle's _SKIP set must include PAUSED so the
        orchestrator never advances paused workspaces. We verify by
        inspecting the source — the constant lives inline inside
        _poll_cycle, so we read the file."""
        from pathlib import Path
        src = Path("orchestrator/orchestrator.py").read_text()
        # The _SKIP set definition should mention Stage.PAUSED
        assert "Stage.PAUSED" in src, "Stage.PAUSED must appear in orchestrator.py"
        # And specifically inside an _SKIP set literal
        import re
        m = re.search(r"_SKIP\s*=\s*\{([^}]*)\}", src)
        assert m, "Expected an _SKIP = {...} set literal"
        assert "Stage.PAUSED" in m.group(1), (
            f"Stage.PAUSED missing from _SKIP set: {m.group(1)}"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_orchestrator_deferred.py::TestPausedSkipped -v`

Expected: `test_paused_in_skip_set` FAILs because PAUSED is not yet in the `_SKIP` set. `test_sweep_deferred_does_not_touch_paused` PASSes already (sweep only acts on DEFERRED) — that's fine, it's a regression guard.

- [ ] **Step 3: Add `Stage.PAUSED` to the `_SKIP` set in `_poll_cycle`**

In [orchestrator/orchestrator.py](orchestrator/orchestrator.py), update lines 423-424:

```python
        # Skip workspaces in terminal or clearly waiting states
        _SKIP = {Stage.DONE, Stage.ARCHIVED, Stage.BLOCKED,
                 Stage.MANUAL_CONTROL, Stage.DEFERRED, Stage.FAILED, Stage.PAUSED}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_deferred.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_deferred.py
git commit -m "feat: skip PAUSED workspaces in orchestrator poll cycle"
```

---

## Task 3: Backend `pause` endpoint

**Files:**
- Modify: [dashboard/actions.py](dashboard/actions.py)
- Modify: [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py)

- [ ] **Step 1: Write the failing tests**

Append at the end of [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py):

```python
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
        # No body → no confirm; should still work because no agent is running.
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_dashboard_actions.py::TestPauseEndpoint -v`

Expected: all tests FAIL — `pause` route doesn't exist yet, so requests return 404 (or 405) for the wrong reason.

- [ ] **Step 3: Add the `pause` handler and route**

In [dashboard/actions.py](dashboard/actions.py), inside `build_action_routes()`, add the handler **after** the existing `resume` handler (around line 234), and add the route in the returned routes list.

Add this handler (place between `resume` at lines 216-233 and `archive` at line 235):

```python
    async def pause(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)

        PAUSEABLE = {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK,
                     Stage.QA, Stage.PUSHED, Stage.PR_REVIEW}
        if ws.state.current_state not in PAUSEABLE:
            return _error(f"Cannot pause: state is {ws.state.current_state}")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        confirm = body.get("confirm", False)

        agent_runtime = orchestrator._agent_runtime
        running = agent_runtime.get_running(ticket_id)
        if running and not confirm:
            elapsed = time.time() - running.get("started_at", time.time())
            return JSONResponse({
                "status": "agent_running",
                "agent": running["agent_id"],
                "started_ago": f"{int(elapsed)}s",
            })

        if running:
            agent_runtime.cancel(ticket_id)

        previous = ws.state.current_state
        ws.transition(Stage.PAUSED)

        if event_bus:
            event_bus.emit(
                "workspace_paused",
                f"Paused {ticket_id} from {previous} via dashboard",
                ticket_id=ticket_id,
                data={"previous_state": previous},
            )
        return JSONResponse({"status": "ok", "new_state": Stage.PAUSED})
```

Then add the route in the returned `routes` list (around line 380, alongside the other action routes):

```python
        Route("/api/workspaces/{ticket_id:path}/pause", pause, methods=["POST"]),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_dashboard_actions.py::TestPauseEndpoint -v`

Expected: all 6 parametrised + standalone tests pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/actions.py tests/unit/test_dashboard_actions.py
git commit -m "feat: dashboard pause endpoint kills agent and transitions to PAUSED"
```

---

## Task 4: Backend `unpause` endpoint

**Files:**
- Modify: [dashboard/actions.py](dashboard/actions.py)
- Modify: [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py)

- [ ] **Step 1: Write the failing tests**

Append at the end of [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_dashboard_actions.py::TestUnpauseEndpoint -v`

Expected: all tests FAIL — `unpause` route doesn't exist.

- [ ] **Step 3: Add the `unpause` handler and route**

In [dashboard/actions.py](dashboard/actions.py), add this handler immediately after `pause` (and before `archive`):

```python
    async def unpause(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != Stage.PAUSED:
            return _error(f"Cannot unpause: state is {ws.state.current_state}")

        target = ws.state.previous_state or Stage.ANALYSIS
        ws.transition(target)

        if event_bus:
            event_bus.emit(
                "workspace_unpaused",
                f"Unpaused {ticket_id} via dashboard → {target}",
                ticket_id=ticket_id,
                data={"new_state": target},
            )
        return JSONResponse({"status": "ok", "new_state": target})
```

Add the route in the returned `routes` list (next to `pause`):

```python
        Route("/api/workspaces/{ticket_id:path}/unpause", unpause, methods=["POST"]),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_dashboard_actions.py::TestUnpauseEndpoint -v`

Expected: all tests pass.

- [ ] **Step 5: Run the full action-tests suite to catch regressions**

Run: `pytest tests/unit/test_dashboard_actions.py -v`

Expected: all tests pass (existing + new).

- [ ] **Step 6: Commit**

```bash
git add dashboard/actions.py tests/unit/test_dashboard_actions.py
git commit -m "feat: dashboard unpause endpoint resumes to previous_state"
```

---

## Task 5: Frontend API wrappers

**Files:**
- Modify: [dashboard/static/js/actions.js](dashboard/static/js/actions.js)

This is a tiny, mechanical addition — no test (the existing JS has no unit tests for these wrappers; behaviour is covered by unit tests for the backend and the E2E test in Task 9).

- [ ] **Step 1: Add `pauseWorkspace` and `unpauseWorkspace`**

In [dashboard/static/js/actions.js](dashboard/static/js/actions.js), insert these exports immediately after `archiveWorkspace` (around line 32):

```javascript
export async function pauseWorkspace(ticketId, confirm = false) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/pause`, { confirm });
}

export async function unpauseWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/unpause`);
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/static/js/actions.js
git commit -m "feat: add pauseWorkspace/unpauseWorkspace API wrappers"
```

---

## Task 6: Badge styling and pulse class for PAUSED

**Files:**
- Modify: [dashboard/static/js/helpers.js](dashboard/static/js/helpers.js)
- Modify: [dashboard/static/style.css](dashboard/static/style.css)

- [ ] **Step 1: Add the PAUSED pulse mapping in `stateBadgeHtml`**

In [dashboard/static/js/helpers.js](dashboard/static/js/helpers.js), update `stateBadgeHtml` (lines 78-86). Add the new pulse line so PAUSED gets a distinct (blue) pulse class:

```javascript
export function stateBadgeHtml(stateVal) {
  const cls = 'state-' + (stateVal || 'NEW').replace(/[^A-Z_]/g, '');
  let pulseClass = '';
  if (stateVal === 'BLOCKED') pulseClass = ' badge-pulse-red';
  if (stateVal === 'AWAITING_APPROVAL') pulseClass = ' badge-pulse-yellow';
  if (stateVal === 'MANUAL_CONTROL') pulseClass = ' badge-pulse-purple';
  if (stateVal === 'PAUSED') pulseClass = ' badge-pulse-blue';
  const label = STATE_LABELS[stateVal] || stateVal || 'NEW';
  return `<span class="state-badge ${cls}${pulseClass}">${esc(label)}</span>`;
}
```

- [ ] **Step 2: Add CSS for `.state-PAUSED`, `.card-paused`, and `.badge-pulse-blue`**

In [dashboard/static/style.css](dashboard/static/style.css), insert these rules immediately after the `.state-AWAITING_APPROVAL` line (after line 267):

```css
  .state-PAUSED    { background: #1a2d3d; color: #79c0ff; border: 1px solid #388bfd; }
```

And insert these immediately after the existing `.card-awaiting` rule (after line 270):

```css
  .card-paused { box-shadow: inset 3px 0 0 #79c0ff; opacity: 0.85; }
  .badge-pulse-blue { color: #79c0ff; font-weight: 700; }
```

- [ ] **Step 3: Manually verify the badge renders**

Start the dashboard server (or run an existing E2E test to confirm assets load). The PAUSED badge will be exercised by the E2E test in Task 9.

Quick smoke test: `pytest tests/unit/test_dashboard_actions.py -v` (asserts no test broke from helper / CSS changes — they shouldn't, since these files are not imported in unit tests).

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/js/helpers.js dashboard/static/style.css
git commit -m "feat: add PAUSED badge and card styling"
```

---

## Task 7: Board view — sort, card class, inline pause/unpause buttons

**Files:**
- Modify: [dashboard/static/js/board.js](dashboard/static/js/board.js)

- [ ] **Step 1: Insert `PAUSED` into the board sort order**

In [dashboard/static/js/board.js](dashboard/static/js/board.js), update the `stateOrder` map (lines 33-37). Place `PAUSED` between `MANUAL_CONTROL` and the active stages — operator-stopped tickets group with other "stopped" cards:

```javascript
    const stateOrder = {
      BLOCKED: 0, AWAITING_APPROVAL: 1, DEFERRED: 2, MANUAL_CONTROL: 3, PAUSED: 4,
      DEV: 5, ANALYSIS: 6, SCOPE_CHECK: 7, QA: 8, PR_REVIEW: 9, PUSHED: 10,
      NEW: 11, DONE: 12, SETUP_DONE: 12, FAILED: 13, ARCHIVED: 14,
    };
```

- [ ] **Step 2: Add `card-paused` CSS class in `renderCard`**

In [dashboard/static/js/board.js](dashboard/static/js/board.js), update `renderCard` (lines 186-244). After the `if (stateVal === 'MANUAL_CONTROL') cardClass += ' card-manual';` line (line 193), add:

```javascript
  if (stateVal === 'PAUSED') cardClass += ' card-paused';
```

- [ ] **Step 3: Render the pause/unpause inline button**

In [dashboard/static/js/board.js](dashboard/static/js/board.js), `renderCard`. After the `approveBtn` block (lines 204-206), add a `pauseBtn` block:

```javascript
  const PAUSEABLE_STATES = ['ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW'];
  let pauseBtn = '';
  if (PAUSEABLE_STATES.includes(stateVal)) {
    pauseBtn = `<button class="action-btn btn-pause" data-action="pause" data-ticket="${esc(ws.ticket_id)}" onclick="event.stopPropagation()" title="Pause this ticket — agent (if running) will be stopped">Pause</button>`;
  } else if (stateVal === 'PAUSED') {
    pauseBtn = `<button class="action-btn btn-pause" data-action="unpause" data-ticket="${esc(ws.ticket_id)}" onclick="event.stopPropagation()" title="Resume work on this ticket">Unpause</button>`;
  }
```

Then include it in the `card-actions` span. Replace the existing `card-actions` block (lines 236-241) with:

```html
      <span class="card-actions">
        ${prLink}
        ${approveBtn}
        ${pauseBtn}
        ${cleanBtn}
        ${deleteBtn}
      </span>
```

- [ ] **Step 4: Update the imports and bind the new buttons**

In [dashboard/static/js/board.js](dashboard/static/js/board.js), update the import (line 5):

```javascript
import { approveWorkspace, pauseWorkspace, unpauseWorkspace } from './actions.js';
```

Then add binding logic alongside the other inline buttons. Insert this block immediately after the existing approve binding (line 75, after the `approve` `forEach` block ends with the closing `});`):

```javascript
    // Bind inline pause buttons
    content.querySelectorAll('[data-action="pause"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        try {
          const result = await pauseWorkspace(tid, false);
          if (result && result.status === 'agent_running') {
            const ok = confirm(`Agent ${result.agent} is currently running for ${tid} (started ${result.started_ago} ago). Pausing will stop the agent. Continue?`);
            if (!ok) return;
            await pauseWorkspace(tid, true);
          }
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Pause failed: ' + err.message);
        }
      });
    });

    // Bind inline unpause buttons
    content.querySelectorAll('[data-action="unpause"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        try {
          await unpauseWorkspace(tid);
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Unpause failed: ' + err.message);
        }
      });
    });
```

- [ ] **Step 5: Manual smoke test** *(no automated test for board.js — covered by E2E in Task 9)*

Run the dashboard locally if convenient. The full path is exercised in Task 9.

- [ ] **Step 6: Commit**

```bash
git add dashboard/static/js/board.js
git commit -m "feat: inline pause/unpause buttons on board cards"
```

---

## Task 8: Detail view — Pause/Unpause buttons in action bar

**Files:**
- Modify: [dashboard/static/js/detail.js](dashboard/static/js/detail.js)

- [ ] **Step 1: Update the import**

In [dashboard/static/js/detail.js](dashboard/static/js/detail.js), update line 7:

```javascript
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, resumeWorkspace, archiveWorkspace, pauseWorkspace, unpauseWorkspace, showConfirmDialog } from './actions.js';
```

- [ ] **Step 2: Render pause/unpause buttons in `buildActionBar`**

In [dashboard/static/js/detail.js](dashboard/static/js/detail.js), update `buildActionBar` (lines 96-131). Insert these blocks immediately after the existing `isDeferred` block (after line 113, before `canArchive`):

```javascript
  const PAUSEABLE_STATES = ['ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW'];
  const canPause = PAUSEABLE_STATES.includes(stateVal);
  const isPaused = stateVal === 'PAUSED';
  if (canPause) {
    buttons += `<button class="action-btn btn-pause" id="act-pause">Pause</button>`;
  }
  if (isPaused) {
    buttons += `<button class="action-btn btn-pause" id="act-unpause">Unpause</button>`;
  }
```

- [ ] **Step 3: Bind the buttons in `bindActionButtons`**

In [dashboard/static/js/detail.js](dashboard/static/js/detail.js), update `bindActionButtons` (lines 272-377). Insert this block immediately after the existing `Take Control` binding (after line 364, before `// Finished (release control)`):

```javascript
  // Pause
  const pauseBtn = document.getElementById('act-pause');
  if (pauseBtn) {
    pauseBtn.addEventListener('click', async () => {
      try {
        const result = await pauseWorkspace(ticketId, false);
        if (result && result.status === 'agent_running') {
          showConfirmDialog(
            `Pause ${ticketId}?`,
            `<div style="background:#3d1a1a22;border:1px solid #da363366;border-radius:6px;padding:10px 12px;margin-bottom:8px;">
              <div style="font-size:12px;color:#f85149;font-weight:600;">Agent is currently running</div>
              <div style="font-size:11px;color:#c9d1d9;">${esc(result.agent)} &mdash; started ${esc(result.started_ago)} ago</div>
              <div style="font-size:11px;color:#8b949e;margin-top:4px;">Pausing will stop this agent.</div>
            </div>`,
            'Stop Agent & Pause',
            async () => {
              await pauseWorkspace(ticketId, true);
              await renderDetail(ticketId, onBack);
            }
          );
        } else {
          await renderDetail(ticketId, onBack);
        }
      } catch (e) { alert('Pause failed: ' + e.message); }
    });
  }

  // Unpause
  const unpauseBtn = document.getElementById('act-unpause');
  if (unpauseBtn) {
    unpauseBtn.addEventListener('click', async () => {
      try {
        await unpauseWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Unpause failed: ' + e.message); }
    });
  }
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/js/detail.js
git commit -m "feat: pause/unpause buttons in detail action bar"
```

---

## Task 9: E2E happy-path test

**Files:**
- Modify: [tests/e2e/test_actions.py](tests/e2e/test_actions.py)

- [ ] **Step 1: Write the E2E test**

Append at the end of [tests/e2e/test_actions.py](tests/e2e/test_actions.py):

```python
class TestPause:
    def test_pause_button_visible_for_active_state(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()  # DEV
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-pause")).to_be_visible()

    def test_pause_button_not_visible_for_blocked(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-3"]').click()  # BLOCKED
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-pause")).to_have_count(0)

    def test_pause_transitions_state_on_disk(
        self, page: Page, dashboard_server: dict
    ):
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-1")
        before = json.loads(sp.read_text())
        assert before["current_state"] == "DEV"

        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#act-pause", timeout=3000)
        page.locator("#act-pause").click()

        after = wait_for_state_change(sp, "DEV")
        assert after["current_state"] == "PAUSED"
        assert after["previous_state"] == "DEV"

    def test_unpause_returns_to_previous_state(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("P-1", "PAUSED", previous_state="DEV")
        ctx = dashboard_server_custom()
        sp = state_path(base, "P-1")

        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="P-1"]').click()
        page.wait_for_selector("#act-unpause", timeout=3000)
        page.locator("#act-unpause").click()

        after = wait_for_state_change(sp, "PAUSED")
        assert after["current_state"] == "DEV"
        assert after["previous_state"] is None
```

- [ ] **Step 2: Run the E2E suite**

Run: `pytest tests/e2e/test_actions.py::TestPause -v`

Expected: all four tests pass. The seeded `SPIKE-1` ticket starts in DEV (per the existing seed), so it's a valid pause target.

If the dashboard server fixture seed doesn't include a DEV-state ticket exactly named SPIKE-1, look at [tests/e2e/conftest.py](tests/e2e/conftest.py) (the `dashboard_server` fixture body, around line 100-180) to confirm the seed and adjust the ticket id if needed.

- [ ] **Step 3: Run the full test suite once to catch regressions**

Run: `pytest tests/unit tests/e2e -v`

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_actions.py
git commit -m "test: e2e coverage for pause/unpause flow"
```

---

## Final Verification

- [ ] **Run full test suite**

Run: `pytest tests/ -v`

Expected: all tests pass.

- [ ] **Manual smoke test (recommended)**

Start the dashboard, open the board, click Pause on an active ticket card, confirm the badge changes to PAUSED. Click Unpause to confirm it returns to the prior stage.

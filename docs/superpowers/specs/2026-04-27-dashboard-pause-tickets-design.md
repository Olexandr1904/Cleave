# Dashboard: Pause Tickets

**Status:** Design approved · 2026-04-27

## Goal

Let an operator manually pause a ticket from the dashboard so the orchestrator stops working on it indefinitely, with explicit operator-driven resume. Distinct from `DEFERRED` (auto-resume on quota recovery) and `BLOCKED` (system-detected, requires retry).

## Motivation

Operators currently have no way to say "don't touch this ticket right now" without taking manual control of it (which implies they're going to work on it themselves) or archiving it (terminal). Pause fills that gap: a temporary, operator-driven freeze with a clean resume path back to the prior stage.

## Decisions

| # | Question | Choice |
|---|----------|--------|
| 1 | Pause semantics | **A** — Indefinite manual pause. No timer. |
| 2 | Behavior when an agent is running | **A** — Kill the agent immediately on pause, but warn the user first. |
| 3 | State modeling | **A** — New `PAUSED` state, distinct from `DEFERRED`. |
| 4 | Where pause is allowed from | **A** — Active working stages only: `ANALYSIS`, `DEV`, `SCOPE_CHECK`, `QA`, `PUSHED`, `PR_REVIEW`. |
| 5 | UI placement | **B** — Detail page action bar **and** inline button on board card. |

Resume target: ticket returns to its `previous_state` (mirrors how `DEFERRED → resume` works).

## State Model

### New stage

Add `PAUSED = "PAUSED"` to `Stage` in [workspace/workspace.py](workspace/workspace.py).

### Transitions

Add to `VALID_TRANSITIONS`:

```python
Stage.ANALYSIS:    {..., Stage.PAUSED}
Stage.DEV:         {..., Stage.PAUSED}
Stage.SCOPE_CHECK: {..., Stage.PAUSED}
Stage.QA:          {..., Stage.PAUSED}
Stage.PUSHED:      {..., Stage.PAUSED}
Stage.PR_REVIEW:   {..., Stage.PAUSED}

Stage.PAUSED: {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK,
               Stage.QA, Stage.PUSHED, Stage.PR_REVIEW,
               Stage.FAILED, Stage.MANUAL_CONTROL}
```

Pause is **not** allowed from `BLOCKED`, `AWAITING_APPROVAL`, `MANUAL_CONTROL`, `DEFERRED`, `NEW`, `DONE`, `ARCHIVED`, `FAILED`, or `PAUSED` itself. Those states need different actions (retry, approve/reject, release-control, resume, archive).

### Orchestrator behavior

- Add `Stage.PAUSED` to the `_SKIP` set in [orchestrator/orchestrator.py:423-424](orchestrator/orchestrator.py#L423-L424). Polling loop will not call `advance_workspace` on paused tickets.
- `_sweep_deferred` is **not** modified — it only touches `DEFERRED`, leaving `PAUSED` alone. No auto-resume sweep for `PAUSED`.

### Telemetry

Emit `paused` and `unpaused` events through the existing `EventBus` in [dashboard/events.py](dashboard/events.py), following the same shape as `manual_control_taken`/`manual_control_released` and `deferred_resumed`. The `paused` event message includes the source stage; `unpaused` includes the target stage.

## Backend

### Endpoints

Add to [dashboard/actions.py](dashboard/actions.py) and register in [dashboard/web.py](dashboard/web.py):

```
POST /api/workspaces/{ticket_id}/pause     body: { confirm?: bool }
POST /api/workspaces/{ticket_id}/unpause   body: {}
```

### `pause` handler

1. `_find_workspace(ticket_id)` → 404 if not found.
2. Reject if `current_state` not in `{ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW}` → 409 with explanatory message.
3. If an agent process is currently running for this workspace and `confirm` is not `true`, return 409 with body `{ agent_running: true, message: "An agent is currently running. Pausing will terminate it." }`. The frontend uses this to show a confirm dialog.
4. With `confirm=true` (or no agent running): kill the agent process if any (reuse the same kill helper Take Control uses).
5. `ws.transition(Stage.PAUSED)` — `previous_state` is captured automatically by `transition()`.
6. Emit `paused` event: `f"Paused {ticket_id} from {previous_state}"`.
7. Return `{ ok: true, state: "PAUSED" }`.

### `unpause` handler

1. `_find_workspace(ticket_id)` → 404 if not found.
2. Reject if `current_state != PAUSED` → 409.
3. Resolve target = `state.previous_state`. If null, fall back to `Stage.ANALYSIS` (same defensive default `resume` already uses).
4. `ws.transition(target)`.
5. Emit `unpaused` event: `f"Unpaused {ticket_id} to {target}"`.
6. Return `{ ok: true, state: target }`.

### Reused infrastructure

`_find_workspace`, `Workspace.transition()`, `EventBus._emit`, agent-kill helper. No new helpers, no new abstractions.

## Frontend

### [dashboard/static/js/actions.js](dashboard/static/js/actions.js)

Add `pauseWorkspace(id, { confirm } = {})` and `unpauseWorkspace(id)` — thin POST wrappers in the same style as `takeControl`/`resumeNow`. `pauseWorkspace` returns the parsed JSON so callers can detect `agent_running` and re-call with `confirm: true`.

### [dashboard/static/js/helpers.js](dashboard/static/js/helpers.js)

- Map `PAUSED` to its own badge class (e.g., `state-PAUSED`) so it's visually distinct.
- Use `PAUSED` as the badge label (no entry needed in `STATE_LABELS` if the default uppercase string is fine).

### [dashboard/static/js/board.js](dashboard/static/js/board.js)

- **Sort:** place `PAUSED` adjacent to `DEFERRED` / `MANUAL_CONTROL` in the state-priority list — stopped cards group together, ahead of active stages.
- **Card style:** add `card-paused` class (dimmed/muted, similar to `card-dimmed`).
- **Inline buttons:**
  - Show **Pause** button on cards whose state is in `{ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW}`.
  - Show **Unpause** button on cards whose state is `PAUSED`.
  - Pause flow: call `pauseWorkspace(id)`. If response indicates `agent_running`, show `confirm("An agent is currently running and will be terminated. Continue?")` and retry with `confirm: true`.

### [dashboard/static/js/detail.js](dashboard/static/js/detail.js)

`buildActionBar()` adds Pause/Unpause buttons under the same visibility rules as the inline board buttons. Same agent-running confirm flow.

No deferred-style countdown banner — pause is indefinite; the state badge alone communicates the state.

### [dashboard/static/style.css](dashboard/static/style.css)

- `.state-PAUSED` badge — muted grey/blue, distinct from BLOCKED (red), AWAITING (yellow), MANUAL (purple), DEFERRED (orange).
- `.card-paused` card class — dimmed/desaturated treatment.

## Tests

### Unit — [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py)

`TestPauseEndpoint`:
- `test_pause_from_active_stage_transitions_to_paused` — from DEV, asserts PAUSED and `previous_state == DEV`.
- `test_pause_from_invalid_stage_returns_409` — parametrised over DONE, BLOCKED, AWAITING_APPROVAL, MANUAL_CONTROL, DEFERRED, NEW, ARCHIVED, FAILED.
- `test_pause_with_running_agent_without_confirm_returns_409` — body indicates `agent_running: true`.
- `test_pause_with_running_agent_and_confirm_kills_agent_and_pauses` — kill helper called, transition succeeds.
- `test_pause_missing_workspace_returns_404`.

`TestUnpauseEndpoint`:
- `test_unpause_returns_to_previous_state` — PAUSED with `previous_state=DEV` → DEV.
- `test_unpause_from_non_paused_returns_409`.
- `test_unpause_falls_back_to_analysis_when_previous_state_null`.

### Unit — workspace state machine

[tests/unit/test_workspace.py](tests/unit/test_workspace.py):
- PAUSED reachable from each of the 6 active stages.
- PAUSED rejected from all other stages.
- PAUSED's exit set matches the design.

### Unit — orchestrator

[tests/unit/test_orchestrator_deferred.py](tests/unit/test_orchestrator_deferred.py) (or a sibling test file in `tests/unit/test_orchestrator_*.py`):
- `_SKIP` includes `PAUSED`; `advance_workspace` is not called for paused workspaces in a poll cycle.
- `_sweep_deferred` does not touch PAUSED workspaces.

### E2E — [tests/e2e/test_actions.py](tests/e2e/test_actions.py)

One happy-path test: open detail of a DEV ticket, click Pause, assert badge becomes PAUSED and `state.json` reflects it; click Unpause, assert ticket returns to DEV.

Inline-board-button click and the agent-running confirm dialog are covered by unit-level tests; we skip browser coverage for them.

## Out of scope

- Time-based pause (auto-resume after N hours). Considered and rejected — operators want indefinite pause; if time-based comes up later, it can layer on top by reusing the `retry_at` field.
- Pause from non-active stages (BLOCKED, AWAITING_APPROVAL, etc.). Rejected — those states already have appropriate operator actions.
- Bulk pause across multiple tickets. Not requested.
- Deferred-style countdown banner. Not relevant for indefinite pause.

## Files changed

**Backend:**
- [workspace/workspace.py](workspace/workspace.py) — Stage enum + VALID_TRANSITIONS.
- [dashboard/actions.py](dashboard/actions.py) — pause/unpause handlers.
- [dashboard/web.py](dashboard/web.py) — route registration.
- [orchestrator/orchestrator.py](orchestrator/orchestrator.py) — `_SKIP` set.

**Frontend:**
- [dashboard/static/js/actions.js](dashboard/static/js/actions.js) — API wrappers.
- [dashboard/static/js/helpers.js](dashboard/static/js/helpers.js) — badge mapping.
- [dashboard/static/js/board.js](dashboard/static/js/board.js) — sort, card style, inline buttons.
- [dashboard/static/js/detail.js](dashboard/static/js/detail.js) — action bar buttons.
- [dashboard/static/style.css](dashboard/static/style.css) — badge + card styles.

**Tests:**
- [tests/unit/test_dashboard_actions.py](tests/unit/test_dashboard_actions.py)
- [tests/unit/test_workspace.py](tests/unit/test_workspace.py)
- [tests/unit/test_orchestrator_deferred.py](tests/unit/test_orchestrator_deferred.py) (or a sibling `test_orchestrator_*.py`)
- [tests/e2e/test_actions.py](tests/e2e/test_actions.py)

# Quota Deferral & Recoverable FAILED — Design

**Status:** Draft
**Date:** 2026-04-14
**Author:** Oleksandr Brazhenko (with Claude)

## Problem

When the Claude Code CLI hits a Claude Max usage limit mid-ticket, Sickle currently transitions the workspace to `FAILED` with the generic error `"Claude Code CLI exited with code 1: "`. `FAILED` is terminal: the workspace is pruned from the active list, never re-discovered on restart, has no outbound transitions, and `previous_state` is never recorded on the transition. The dashboard's Retry button, the Telegram `retry` command, and the Take Control button are all broken on FAILED tickets, either by backend state-machine rejection, by 404 from an in-memory active-list lookup, or by a frontend/backend gating mismatch.

Beyond the quota case, the same brittleness affects every failure: any agent exception or CLI crash permanently bricks a ticket that would often be fine on a second attempt. The only current recovery path is hand-editing `state.json` and restarting the daemon.

**Observed incident:** `ACME-14595` completed BA → Dev → Scope Guard → QA successfully (code fix committed on feature branch), QA bounced back to Dev, and the second Dev run hit a Claude usage limit. The workspace is stuck in `FAILED` with all recovery paths non-functional.

## Goals

1. Classify Claude CLI quota/usage-limit errors distinctly from unclassified failures.
2. Park quota-hit tickets in a new `DEFERRED` state with a `retry_at` timestamp and auto-resume them when the window passes (AUTO mode) or via manual action (MANUAL mode). Note: auto-resume runs in both modes since DEFERRED is not a decision point; manual mode's approval gates still apply to the stages the ticket resumes into.
3. Make `FAILED` recoverable: retain the ticket in the active list, record `previous_state` on entry, allow outbound transitions, and fix the dashboard/Telegram recovery surfaces.
4. Notify the user once per situation (per-ticket for FAILED, debounced across all tickets for quota DEFERRED), avoiding duplicate Telegram messages when many tickets hit the same global quota.

## Non-goals

- Global process-wide "quota-blocked" dispatch lock. Each ticket discovers the quota independently; the cost of a few failed CLI calls per poll cycle (at most N = `max_parallel_tickets`) is acceptable given Sickle's scale (2–5 parallel tickets, 15-minute poll interval).
- Auto-retry for unclassified failures. `FAILED` is human-driven on recovery; no automatic retry path.
- Retry caps / escalation loops for `DEFERRED`. Tickets may defer indefinitely through repeated quota windows without the daemon intervening. The event log makes repeats visible.
- TTL-based auto-archival of `FAILED` workspaces. Manual archive only.

## Design

### 1. State machine

**New state:** `DEFERRED`. Added to `VALID_STATES` in `workspace/workspace.py`.

**New `WorkspaceState` field:** `retry_at: str | None = None` — ISO 8601 UTC timestamp. Set when transitioning into `DEFERRED`, cleared when transitioning out. Default `None`; no migration needed because Python dataclass defaults handle missing fields in existing `state.json` files.

**Transition table changes** in `VALID_TRANSITIONS`:

```python
"ANALYSIS":    {"DEV", "BLOCKED", "FAILED", "DEFERRED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
"DEV":         {"SCOPE_CHECK", "BLOCKED", "FAILED", "DEFERRED", "MANUAL_CONTROL"},
"SCOPE_CHECK": {"QA", "DEV", "BLOCKED", "FAILED", "DEFERRED", "MANUAL_CONTROL"},
"QA":          {"PUSHED", "DEV", "BLOCKED", "FAILED", "DEFERRED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
"PUSHED":      {"PR_REVIEW", "BLOCKED", "FAILED", "DEFERRED", "MANUAL_CONTROL"},
"PR_REVIEW":   {"DEV", "DONE", "BLOCKED", "FAILED", "DEFERRED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},

"DEFERRED":    {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW",
                "FAILED", "MANUAL_CONTROL"},

"FAILED":      {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW",
                "MANUAL_CONTROL", "ARCHIVED"},
```

**`previous_state` recording in `Workspace.transition()`** — extend the paused-states set at `workspace/workspace.py:169-175` to include `DEFERRED` and `FAILED` alongside `BLOCKED`, `AWAITING_APPROVAL`, and `MANUAL_CONTROL`. The existing clause that clears `previous_state` on resume from a paused state already handles the exit path correctly.

**Terminal set narrows** to `{DONE, ARCHIVED}` in three places: `workspace/workspace_manager.py:165` (`discover_workspaces`), `orchestrator/orchestrator.py:284` (poll-cycle pruning), and `dashboard/actions.py:18` (`TERMINAL_STATES`). `FAILED` and `DEFERRED` workspaces now stay in `_active_workspaces` and are re-discovered on daemon restart.

### 2. Quota detection — adapter layer

All classification lives in `integrations/llm/claude_code_adapter.py`. The orchestrator only sees a typed exception.

**New exception:**

```python
class QuotaExhaustedError(RuntimeError):
    """Claude CLI hit a usage/rate limit. Carries the reset time if known."""
    def __init__(self, message: str, retry_at: datetime | None = None):
        super().__init__(message)
        self.retry_at = retry_at
```

**Classifier helper** `_classify_cli_error(stdout: str, stderr: str) -> QuotaExhaustedError | None`:

1. **Structured parse first.** `json.loads(stdout)`. If the result is a dict with `is_error=true`, read the `result` or `content` field. Look for the marker `Claude AI usage limit reached|<epoch_ms>` via regex. On match, parse the epoch and return `QuotaExhaustedError(msg, retry_at=datetime.fromtimestamp(epoch/1000, tz=timezone.utc))`.
2. **Substring fallback.** If JSON parsing fails, search the combined `stdout + stderr` (case-insensitive) for `"usage limit reached"`, `"rate_limit"`, `"overloaded_error"`, `"quota"`. On any match, return `QuotaExhaustedError(msg, retry_at=None)`.
3. **Otherwise** return `None`.

**Invocation points.** Two places in `_run_cli()` call the classifier:
- At `claude_code_adapter.py:245` (non-zero `returncode` branch): try classifier, raise `QuotaExhaustedError` on hit, else raise generic `RuntimeError`.
- At `claude_code_adapter.py:278` (JSON `is_error=true` branch with `rc=0`): try classifier, raise `QuotaExhaustedError` on hit, else raise generic `RuntimeError`.

**Fallback delay** when `retry_at is None`: adapter leaves it as `None`. Orchestrator applies a default of **1 hour** via a module-level constant `DEFAULT_QUOTA_RETRY_DELAY = timedelta(hours=1)` in `orchestrator.py`.

### 3. Orchestrator changes

Three edits to `orchestrator/orchestrator.py`.

#### 3a. Failure routing

**`AgentResult`** (in `orchestrator/agent_runtime.py`) gains two fields:

```python
failure_kind: str | None = None   # "quota" | "permanent" | None
retry_at: datetime | None = None  # populated when failure_kind == "quota"
```

**`AgentRuntime.execute()`** except block at `agent_runtime.py:225-232`:

```python
except QuotaExhaustedError as e:
    logger.warning("Agent '%s' deferred on quota: %s", agent_id, e)
    return AgentResult(
        agent_id=agent_id, success=False, output="",
        error=str(e), failure_kind="quota", retry_at=e.retry_at,
    )
except Exception as e:
    logger.error("Agent '%s' failed: %s", agent_id, e)
    return AgentResult(
        agent_id=agent_id, success=False, output="",
        error=str(e), failure_kind="permanent",
    )
```

**Orchestrator's failure branch** at `orchestrator.py:512-516` becomes:

```python
if not result.success:
    self._emit("agent_failed", ...)
    if result.failure_kind == "quota":
        self._rollback_iteration(workspace, stage_id)
        retry_at = result.retry_at or (datetime.now(timezone.utc) + DEFAULT_QUOTA_RETRY_DELAY)
        workspace.transition("DEFERRED", retry_at=retry_at.isoformat())
        await self._notify_deferred(workspace, retry_at)
    else:
        workspace.transition("FAILED")
        workspace.update_state(error=result.error)
        await self._notify_failed(workspace, result.error)
    return
```

**`_rollback_iteration(workspace, stage_id)`** — new helper that decrements `workspace.state.stage_iterations[stage_id]` if > 0, then `save_state()`. Rationale: the quota-aborted run produced no output, so it should not count against `max_iterations` for that stage.

#### 3b. Deferred sweep

Inserted at the top of `poll_cycle()` at `orchestrator.py:257`, before ticket polling and workspace advancement:

```python
now = datetime.now(timezone.utc)
for ws in list(self._active_workspaces):
    if ws.state.current_state != "DEFERRED":
        continue
    retry_at_str = ws.state.retry_at
    if not retry_at_str:
        continue
    if datetime.fromisoformat(retry_at_str) <= now:
        target = ws.state.previous_state or "ANALYSIS"
        ws.transition(target, retry_at=None)
        self._emit("deferred_resumed", f"Resumed {ws.state.ticket_id} from DEFERRED to {target}",
                   project_id=ws.state.company_id, ticket_id=ws.state.ticket_id)
```

The new `retry_at=None` extra on `transition()` is applied in the same atomic save as the state change (the existing `transition()` already forwards `**extra` to `update_state`).

Worst-case resume lag: one poll interval (15 minutes with default config) past `retry_at`. Acceptable given quota windows are ~5 hours.

#### 3c. Take Control gate fix

`dashboard/actions.py:109`:

```python
BLOCKS_TAKE_CONTROL = {"DONE", "ARCHIVED", "MANUAL_CONTROL"}
if ws.state.current_state in BLOCKS_TAKE_CONTROL:
    return _error(f"Cannot take control: state is {ws.state.current_state}")
```

`FAILED` and `DEFERRED` both permit Take Control, matching the frontend's existing `canTakeControl` computation in `detail.js:94`.

#### 3d. Quota notification debounce

New orchestrator field: `self._quota_window_end: datetime | None = None`. Stores the `retry_at` of the first notification in the current window; any quota hit whose `now < _quota_window_end` is suppressed.

```python
async def _notify_deferred(self, workspace, retry_at):
    now = datetime.now(timezone.utc)
    if self._quota_window_end is not None and now < self._quota_window_end:
        return  # still inside the already-announced quota window
    self._quota_window_end = retry_at
    # ...build message and send via self._notifier...
```

When the sweep at 3b resumes tickets past `retry_at`, it also clears `self._quota_window_end` if `now >= self._quota_window_end`, so the next fresh quota hit notifies again.

In-memory only. On daemon restart the field resets and the next quota hit notifies fresh — intentional, so the user becomes aware again after downtime.

### 4. Dashboard

**Badge and pipeline highlight** (`dashboard/static/js/detail.js`, `board.js`):
- `DEFERRED` added to the state badge styling (same pattern as `BLOCKED`/`FAILED`/`MANUAL_CONTROL`).
- `DEFERRED` added to the `OFF_PIPELINE` list at `detail.js:139` so the pipeline visualization highlights `previous_state` as "stuck/waiting."
- New `activeMode = 'deferred'` branch in `buildPipeline`.

**Deferred banner** on the detail page when `stateVal === 'DEFERRED'`:

```
⏱ Deferred — quota exhausted. Will resume at {retry_at_local} (in {relative}) from {previous_state}.
[Resume now]
```

"Resume now" button calls a new endpoint.

**New endpoint** `POST /api/workspaces/{ticket_id}/resume`:
- Find workspace; reject if not `DEFERRED`.
- Clear `retry_at`, transition to `previous_state or "ANALYSIS"`.
- Emit `deferred_resumed` event.

Registered in `build_action_routes` in `dashboard/actions.py` alongside the existing retry/take_control routes.

**Archive button** on `FAILED` / `DONE` / `DEFERRED` tickets:
- New endpoint `POST /api/workspaces/{ticket_id}/archive` that transitions to `ARCHIVED`.
- Frontend: button appears on the detail page's action bar next to Retry.

**Retry button on FAILED** — no frontend change. Already rendered at `detail.js:101-103`. The backend retry endpoint at `dashboard/actions.py:82` already handles `FAILED`; it works automatically once:
- `FAILED` gains outbound transitions (Section 1),
- `previous_state` is recorded on the transition into `FAILED` (Section 1),
- `FAILED` workspaces stay in the active list (Section 1).

**Board sort** (`dashboard/static/js/board.js`): `DEFERRED` slots in immediately after `BLOCKED` / `AWAITING_APPROVAL` in the existing sort order.

### 5. Telegram

**Notifications**, both one-shot per entry into their state:

- **DEFERRED (first ticket in a quota window):**
  `⏱ [{company}/{repo}] Quota exhausted. {ticket_id} (at {previous_state}) deferred, will retry at {retry_at_utc} UTC. Other tickets hitting the same quota will defer silently until then.`
- **DEFERRED (subsequent tickets in the same window):** nothing sent (debounced by Section 3d).
- **FAILED:**
  `❌ [{company}/{repo}] {ticket_id} FAILED at {previous_state}. Error: {first_line_of_error}. Reply 'retry {id}' or use the dashboard.`

Dispatched from `_notify_deferred` / `_notify_failed`, using `_get_chat_id(workspace)` for routing.

**Retry command.** Existing `_handle_retry` at `integrations/telegram/command_handler.py:249-290` starts working automatically for `FAILED` once Section 1 lands. One small edit: extend the condition at `command_handler.py:277` to treat `DEFERRED` the same as `BLOCKED`/`FAILED` (use `previous_state` as the retry target and clear `retry_at`).

**Intent parser prompt** at `integrations/telegram/intent_parser.py:29` gains a `deferred_workspaces` context variable so free-text messages like "resume 14595" or "retry 14595" classify as `retry` intents with the ticket id.

### 6. Persistence & restart safety

- `retry_at` is a regular field in `state.json`, persisted via the existing atomic write.
- `discover_workspaces()` picks up `DEFERRED` and `FAILED` workspaces on daemon start.
- A ticket that was `DEFERRED` during a downtime window whose `retry_at` has already passed gets picked up on the first poll cycle's sweep and resumes immediately.
- The quota notification debounce is in-memory only — the post-restart state intentionally "forgets" that a notification was already sent, so the user is re-alerted if they rebooted the daemon and didn't see the previous message.

## Testing strategy

Four new test modules.

### `tests/unit/test_claude_code_adapter_quota.py`

Unit tests for `_classify_cli_error`:
- Structured JSON, `is_error=true`, `result` contains `"Claude AI usage limit reached|1744650000000"` → returns `QuotaExhaustedError` with `retry_at == datetime(2026-04-14 19:00:00 UTC)`.
- Structured JSON, `is_error=true`, no marker → returns `None`.
- Non-JSON stdout containing `"rate_limit"` → returns `QuotaExhaustedError(retry_at=None)`.
- Non-JSON stdout with unrelated error ("file not found") → returns `None`.
- Empty stdout + empty stderr → returns `None`.
- Case-insensitive substring match (`"USAGE LIMIT REACHED"`) → returns `QuotaExhaustedError`.

### `tests/unit/test_workspace_transitions.py` (extend existing)

- Enter `DEFERRED` from each active stage → `previous_state` recorded, `retry_at` stored.
- Resume from `DEFERRED` to any valid stage → `previous_state` cleared, `retry_at` cleared.
- Enter `FAILED` from active stages → `previous_state` recorded.
- Retry from `FAILED` → `previous_state` cleared.
- `FAILED` → `ARCHIVED` permitted.
- `FAILED` → `DONE` rejected (must go through a happy-path stage or `ARCHIVED`).
- `DEFERRED` → `FAILED` permitted (for the escalation case where a user manually fails out of DEFERRED).

### `tests/unit/test_orchestrator_deferred.py`

Using a fake `AgentRuntime` that injects `QuotaExhaustedError` with a controlled `retry_at`:
- Single ticket hits quota → transitions to `DEFERRED` with the correct `retry_at`, iteration counter not incremented for the aborted run, Telegram notification sent once.
- Three tickets hit quota in the same poll cycle → three `DEFERRED` transitions, one Telegram notification (debounced).
- Poll cycle with a DEFERRED ticket whose `retry_at <= now` → sweep transitions to `previous_state`, clears `retry_at`, emits `deferred_resumed`.
- Poll cycle with `retry_at > now` → workspace stays `DEFERRED`.
- Daemon restart mid-defer (simulated by reconstructing orchestrator with an existing `state.json` on disk) → `discover_workspaces` picks up DEFERRED; first poll cycle resumes if window elapsed, else continues to wait.
- Notification debounce resets after `retry_at` passes → the next quota hit after resume notifies again.

### `tests/e2e/test_deferred_recovery.py`

Full flow, extending the existing e2e harness in `tests/e2e/conftest.py`:
- Seed a workspace at `DEV` with a stubbed adapter that raises `QuotaExhaustedError` on the first call and returns normally on the second.
- Run one poll cycle → ticket in `DEFERRED` with `retry_at` in the future.
- Freeze time forward past `retry_at`, run another poll cycle → ticket transitions back to `DEV`, agent runs, pipeline continues.
- Verify no state corruption, iteration counter is accurate, and the Telegram notifier received exactly one DEFERRED message.

## Files touched

**Modified:**
- `workspace/workspace.py` — add `DEFERRED` to `VALID_STATES`, update `VALID_TRANSITIONS`, extend paused-states set in `transition()`, add `retry_at` field on `WorkspaceState`.
- `workspace/workspace_manager.py` — narrow `terminal_states` in `discover_workspaces` to `{DONE, ARCHIVED}`.
- `integrations/llm/claude_code_adapter.py` — add `QuotaExhaustedError`, `_classify_cli_error`, invoke at both non-zero-rc and `is_error=true` branches.
- `orchestrator/agent_runtime.py` — `AgentResult.failure_kind` and `retry_at` fields, `QuotaExhaustedError` handling in `execute()`.
- `orchestrator/orchestrator.py` — deferred sweep at top of `poll_cycle`, failure routing split in `_handle_agent_stage`, `_rollback_iteration` helper, `_notify_deferred` / `_notify_failed` helpers, `_last_quota_notify_at` field, narrow terminal set, `DEFAULT_QUOTA_RETRY_DELAY` constant.
- `dashboard/actions.py` — `BLOCKS_TAKE_CONTROL` set, new `resume` and `archive` endpoints, update `TERMINAL_STATES`.
- `dashboard/static/js/detail.js` — DEFERRED badge, banner, `OFF_PIPELINE` entry, Resume-now button handler, Archive button handler.
- `dashboard/static/js/board.js` — DEFERRED badge and sort order.
- `dashboard/static/js/actions.js` — `resumeWorkspace`, `archiveWorkspace` client functions.
- `integrations/telegram/command_handler.py` — extend `_handle_retry` to treat `DEFERRED` alongside `BLOCKED`/`FAILED`.
- `integrations/telegram/intent_parser.py` — add `deferred_workspaces` to the intent context.

**New:**
- `tests/unit/test_claude_code_adapter_quota.py`
- `tests/unit/test_orchestrator_deferred.py`
- `tests/e2e/test_deferred_recovery.py`

## Migration & rollback

**Migration:** None. `retry_at` defaults to `None` on existing state files via dataclass default. Existing DEFERRED-less `state.json` files load unchanged.

**In-flight `FAILED` tickets when the change deploys** (e.g. `ACME-14595` itself): on daemon restart, `discover_workspaces` picks them up. They have `previous_state=None`, so the Retry button will fall back to `ANALYSIS`. Manual remediation for these few tickets: edit `previous_state` in their `state.json` to the stage they were actually in when they failed, then retry from the dashboard.

**Rollback:** Revert the commit. `DEFERRED` workspaces on disk will fail to load on the old code because `DEFERRED` isn't in `VALID_STATES`; manual fix is to edit their `state.json` to set `current_state` to the value in `previous_state` and delete `retry_at`. Accept this as a one-time cost if rolling back.

## Open questions

None remaining from brainstorming.

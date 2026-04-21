# Design: Action-Stage Verification (Stage Verifier Hardening)

**Status:** Design
**Created:** 2026-04-17
**Author:** Oleksandr Brazhenko

## Problem

Feature 1 (project health + stage verification, merged 2026-04-15) wired `stage_verifier.verify()` into `_handle_agent_stage` only. The `push` and `pr_review` stages are action stages (non-agent), handled by `_handle_action_stage`, which never calls the verifier. The original spec ([2026-04-15-project-health-and-stage-verification-design.md](2026-04-15-project-health-and-stage-verification-design.md)) explicitly lists both as mechanically verified stages.

**Concrete failure:** ACME-14595 in the `acme/acme-app` workspace. After QA approval, the workspace transitioned to `PUSHED` state. The push action ran `create_pr` against the VCS adapter, but `git ls-remote origin` confirms the feature branch was never pushed. No `stage_verification_failed` event fired because the verifier was never invoked. The workspace sat in `PUSHED` with `pr_number: null` indefinitely.

The verifier functions themselves (`_verify_push`, `_verify_pr_review` in `orchestrator/stage_verifier.py`) are fully implemented and tested. They are simply never called.

## Goals

- Wire stage verification into the action-stage execution path so that `push` and `pr_review` actions are mechanically verified before the workspace advances.
- Unify the action-stage flow with the agent-stage flow: capture → execute → verify → transition → emit.
- Preserve all existing behavior for agent stages (no changes to `_handle_agent_stage`).

## Non-Goals

- Changes to dashboard approve/retry dispatch semantics (Spec B — orphan workspace handling).
- Orphan workspace detection or handling (Spec B).
- New verifiers beyond what the original spec defined.
- Changes to `_handle_agent_stage` (already correct).
- Changes to `stage_verifier.py` verifier functions (already correct).

## Architecture Overview

Today, the orchestrator has two dispatch paths:

```
_dispatch_stage
  ├── stage_def.agent?  → _handle_agent_stage  → capture → execute → VERIFY → transition
  └── stage_def.action? → _handle_action_stage  → execute (action transitions internally)
                                                   ^^^^^^ no capture, no verify, no events
```

After this change:

```
_dispatch_stage
  ├── stage_def.agent?  → _handle_agent_stage  → capture → execute → VERIFY → transition
  └── stage_def.action? → _handle_action_stage  → capture → execute → VERIFY → transition
                                                   ^^^^^^ mirrors agent path exactly
```

Action methods stop transitioning state themselves and instead return a structured `ActionResult`. The handler owns the full lifecycle.

## Component: `ActionResult` dataclass

**File:** `orchestrator/stage_verifier.py` (co-located with `VerifyResult`).

```python
@dataclass
class ActionResult:
    success: bool
    next_state: str          # target state on success (e.g. "PR_REVIEW", "DONE")
    error: str               # human-readable, empty on success
    metadata: dict[str, Any] # pr_url, pr_number, etc.
    skipped: bool = False    # action chose not to run (e.g. delay not met)
```

Design choices:

- `next_state` is set by the action, not the handler. The action knows its happy-path destination (`PR_REVIEW` for push, `DONE`/`DEV` for pr_review).
- `metadata` is a flat dict — the two current actions have different shapes. A typed union is not justified for two cases.
- `skipped` handles the `_action_fetch_pr_comments` delay-not-met case: the action returns early without doing anything, and the handler skips verification and transition entirely.

## Component: Refactored `_handle_action_stage`

**File:** `orchestrator/orchestrator.py`, replacing the current `_handle_action_stage` method.

**New flow:**

```
_handle_action_stage(workspace, stage_id, stage_def):
    1. stage_start_commit = capture_stage_start(workspace, stage_id)
    2. workspace.increment_iteration(stage_id)
    3. result = call action method → returns ActionResult
    4. if result.skipped → rollback_iteration, return (no transition, no events)
    5. if not result.success:
         → transition("FAILED"), set error, emit "action_failed", return
    6. verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
    7. if not verify_result.ok:
         → transition("BLOCKED"), set error with verifier reason, emit "stage_verification_failed", return
    8. workspace.update_state(**result.metadata)  # pr_url, pr_number, etc.
    9. if _should_approval_gate(current_state, next_stage):
         → transition("AWAITING_APPROVAL"), emit "approval_requested", return
   10. transition(result.next_state)
   11. emit "action_completed" with stage + metadata (replaces old "pr_created")
```

**Step 4 — skipped actions:** When `_action_fetch_pr_comments` returns `skipped=True` because the PR review delay has not elapsed, the handler rolls back the iteration counter (same as the quota-deferral pattern for agents) and returns silently. The workspace stays in `PR_REVIEW` for the next poll cycle.

**Step 8 — metadata application:** `result.metadata` is written to workspace state before transitioning. For `push`, this includes `pr_url` and `pr_number`. For `pr_review`, this may include `comments_count`. Consumers (dashboard detail view, `_action_fetch_pr_comments`) read these from `workspace.state` — this preserves the existing contract.

**Step 9 — approval gate:** The approval-gate check moves from inside the action methods to the handler, unifying it with the agent path. The handler calls `_should_approval_gate(current_state)` with one argument (no `next_stage`), matching the existing pattern in `_action_fetch_pr_comments`. This triggers the `next_stage is None` branch in `_should_approval_gate`, which fires if `current_state` is in the gate set (currently: `ANALYSIS`, `QA`, `PR_REVIEW`). The `PUSHED` state is not gated, so the push action's approval check is a no-op — correct, since approval happens before push (after QA), not after. The action's `next_state` is stored in `workspace.state.previous_state` by the `AWAITING_APPROVAL` transition, so `resolve_next_state` in `ApprovalHandler` still works correctly.

**Events:**

| Event | When | Data |
|---|---|---|
| `action_completed` | action + verifier both pass | `stage`, `metadata` (includes `pr_url`, `pr_number` when applicable) |
| `action_failed` | action returns `success=False` | `stage`, `error` |
| `stage_verification_failed` | action succeeded but verifier disagrees | `stage`, `reason` (reuses existing event type from agent path) |

The existing `pr_created` event is folded into `action_completed` metadata. No new event types are introduced.

## Component: Refactored action methods

### `_action_push_and_open_pr`

**Before:** Calls `create_pr(...)`, transitions to `PR_REVIEW` or `FAILED`, emits `pr_created`.
**After:** Returns `ActionResult`. No transitions, no events.

```python
async def _action_push_and_open_pr(self, workspace) -> ActionResult:
    vcs, repo_config = self._get_vcs_for_workspace(workspace)
    if not vcs or not repo_config:
        return ActionResult(
            success=False, next_state="", error="No VCS adapter configured",
            metadata={},
        )
    result = await create_pr(workspace, vcs, self._tracker, repo_config)
    if result.success:
        return ActionResult(
            success=True, next_state="PR_REVIEW", error="",
            metadata={"pr_url": result.pr_url, "pr_number": result.pr_number},
        )
    return ActionResult(
        success=False, next_state="", error=result.error, metadata={},
    )
```

### `_action_fetch_pr_comments`

**Before:** Has three outcomes — skip (delay not met), no comments (transition `DONE` or `AWAITING_APPROVAL`), has comments (write report, transition `DEV`).
**After:** Returns `ActionResult` with `skipped=True` for delay case.

- Delay not met → `ActionResult(skipped=True, ...)`
- No comments → `ActionResult(success=True, next_state="DONE", ...)`
- Has comments → writes report to disk, `ActionResult(success=True, next_state="DEV", metadata={"comments_count": len(comments)})`

The `_should_approval_gate("PR_REVIEW")` call that currently lives inside this method is removed. The handler applies the gate uniformly at step 9.

### `_action_finalize`

Trivial action stage with no verifier. Returns `ActionResult(success=True, next_state="DONE", metadata={})`. The verifier call at step 6 returns `ok=True` for unknown stage IDs (existing behavior at `stage_verifier.py:64`), so it passes through harmlessly.

### `notify_human` — special case, not refactored

The `notify_human` action delegates to `_handle_escalate`, which transitions the workspace to `BLOCKED` and sends a Telegram notification. This method is shared infrastructure used by both agent and action escalation paths. Refactoring it to return `ActionResult` would require changing the agent-path escalation too — that's scope creep.

`_handle_action_stage` keeps `notify_human` as a special-case dispatch before the ActionResult flow:

```python
if action == "notify_human":
    await self._handle_escalate(workspace)
    return
# ... ActionResult flow for all other actions ...
```

## Data Flow

```
Orchestrator poll loop
  └── _dispatch_stage(workspace)
        └── _handle_action_stage(workspace, "push", stage_def)
              ├── capture_stage_start(workspace, "push")  → saves HEAD sha
              ├── _action_push_and_open_pr(workspace)     → ActionResult
              ├── stage_verifier.verify("push", ...)      → VerifyResult
              │     └── _verify_push: git ls-remote origin refs/heads/<branch>
              │           match against local HEAD
              ├── on verify fail → BLOCKED + stage_verification_failed event
              └── on verify pass → write pr_url to state → PR_REVIEW + action_completed event
```

## Failure Modes

| Failure | Before (broken) | After (fixed) |
|---|---|---|
| `create_pr` succeeds but `git push` inside it silently fails (no remote ref) | State → `PR_REVIEW`, no verification | `_verify_push` catches mismatch → `BLOCKED` with "branch not pushed" |
| PR creation returns success but PR doesn't actually exist on VCS | State → `PR_REVIEW`, ticket advances | `_verify_pr_review` catches missing `pr_number` → `BLOCKED` |
| `create_pr` outright fails (network error, auth) | State → `FAILED` (unchanged) | Same — `ActionResult(success=False)` → `FAILED` |
| PR review delay not met | Silent return, no transition (unchanged) | `ActionResult(skipped=True)` → silent return (same behavior, explicit signal) |

## Testing

### Unit: `tests/unit/test_action_stage.py` (new)

Tests for `_handle_action_stage` with mocked action methods and verifiers:

1. **Happy path:** action succeeds, verifier passes → workspace transitions to `next_state`, metadata written, `action_completed` emitted.
2. **Action failure:** action returns `success=False` → `FAILED`, error set, `action_failed` emitted, verifier never called.
3. **ACME-14595 repro:** action returns success, verifier returns `ok=False` → `BLOCKED`, `stage_verification_failed` emitted, `next_state` not applied.
4. **Skipped action:** `skipped=True` → no transition, no events, no verifier, iteration rolled back.
5. **Approval gate:** action returns `next_state="DONE"`, manual mode on, gate fires → `AWAITING_APPROVAL`.

### Unit: existing action method tests

Update tests in `tests/unit/test_dashboard_actions.py` (or wherever `_action_push_and_open_pr` coverage lives):

- Assert return type is `ActionResult` with correct fields.
- Assert no `workspace.transition()` calls inside action methods.
- Assert `_action_fetch_pr_comments` returns `skipped=True` when delay not met.

### E2E: `tests/e2e/test_regression.py`

Add a regression test reproducing ACME-14595:

- Seed a workspace at `PUSHED` state with a feature branch.
- Stub the VCS adapter so `create_pr` "succeeds" but `git push` does not actually push (remote ref stays empty).
- Run the orchestrator dispatch loop.
- Assert: workspace is in `BLOCKED`, `state.error` contains "branch not pushed", `stage_verification_failed` event recorded.
- Negative case: same setup but push actually succeeds → workspace advances to `PR_REVIEW`.

## Rollout

No migration. The refactor changes internal method signatures only — `ActionResult` is not persisted or exposed via API. Existing workspaces in any state continue to work. The next time an action stage runs, verification kicks in.

## Acceptance Criteria

- [ ] `ActionResult` dataclass in `orchestrator/stage_verifier.py`.
- [ ] `_handle_action_stage` follows capture → execute → verify → transition → emit flow.
- [ ] `_action_push_and_open_pr` returns `ActionResult`, does not transition state.
- [ ] `_action_fetch_pr_comments` returns `ActionResult` (with `skipped` for delay case), does not transition state.
- [ ] Approval gate applied by handler, not by action methods.
- [ ] `pr_created` event folded into `action_completed` metadata.
- [ ] Unit tests: 5 handler scenarios + updated action method tests.
- [ ] E2E test: ACME-14595 regression (push fails silently → BLOCKED).
- [ ] Zero `stage_verification_failed` events in the e2e test for the success path.

# Pipeline Reliability Improvements — Design Spec
**Date:** 2026-05-04  
**Scope:** Three targeted fixes to reduce silent failures and unnecessary retry loops

---

## Background

Post-incident analysis of 7 blocked tickets identified three recurring failure patterns:

1. Dev-agent exits in <30s with no commit — pipeline emits generic "no new commit" error with no diagnostic, re-queues silently
2. Scope guard bounces dev back 4+ times before passing — no human escalation, no cap on consecutive failures
3. BA agent produces implementation plans without probing thread-safety or async edge cases — issues surface 3–5 PR review cycles later

---

## Change 1: Dev-agent fast exit → immediate escalation

### What changes

`_verify_dev()` in `orchestrator/stage_verifier.py` receives a new optional parameter `duration_seconds: float | None`. If the agent completed in under 60 seconds **and** HEAD did not move, the failure reason is:

> "dev-agent completed in Xs with no changes — likely could not map plan to code. Escalating for human review."

If HEAD did not move but duration ≥ 60s, the existing generic reason is kept ("no new commit on feature branch").

### Threading the duration

`AgentResult.duration_seconds` is already captured in `orchestrator/agent_runtime.py`. The orchestrator passes it to the verify call site in `orchestrator/orchestrator.py`. No new state fields needed.

### Outcome

Same as the existing no-commit path: workspace transitions to `BLOCKED`, human is notified via Telegram. The improvement is the diagnostic message so the operator knows immediately why dev failed rather than having to dig into logs.

### Threshold

60 seconds. Rationale: a dev-agent that reads even one file and runs one grep takes longer than 60s in practice. Sub-60s completion with no commit means the agent either crashed immediately or gave up without reading the code.

---

## Change 2: Scope guard consecutive bounce cap

### What changes

**`workflows/default-workflow.yaml`**: `scope_check.max_iterations: 3 → 2`

**`orchestrator/orchestrator.py`**: when scope_check transitions to QA (outcome = "pass"), clear `workspace.state.stage_iterations['scope_check']` before saving state.

### Why both parts are needed

`stage_iterations['scope_check']` is currently cumulative across the ticket's lifetime — it is never cleared when scope_check passes. Without clearing on pass, a ticket that goes through scope_check successfully early on would have `iterations=1` before any failure, meaning max_iterations=2 would fire after just one failure. Clearing on pass makes the counter track only *consecutive* failures.

### Outcome

- 1 scope_check failure → bounces to dev, counter = 1
- 2nd consecutive failure → counter = 2 = max_iterations → escalates to human with the standard max-iterations escalation message
- If scope_check passes at any point → counter cleared → any future scope_check entries start fresh

---

## Change 3: BA agent Android edge-case checklist

### What changes

`agents/ba-agent.md`: add a new subsection **"Android/Kotlin implementation checklist"** inside Step 4 (write the implementation plan). This section is conditional — it applies when the ticket targets a native Android repository (detected via `repo_id` containing `android` or `jira_repo_label: acme-mobile-android`).

### Checklist content

The plan must explicitly state how each of the following is handled, or mark it N/A with a reason:

| Concern | What to address |
|---|---|
| Shared mutable state | Does any new field need `@Volatile` or a lock? Which threads read/write it? |
| Thread of execution | Which thread does each operation run on? (OkHttp callback, main thread, coroutine dispatcher) |
| Lambda capture | Does any lambda capture `Activity`/`Fragment`? Null-check the reference inside the lambda, not before entering it. |
| Overlapping async ops | Can two async operations target the same resource concurrently? If yes, how are they distinguished (token, ID, cancel-on-new)? |
| Error path parity | If the success path sets or clears state, does the error path mirror it? |
| URL/redirect chains | Can the URL be rewritten mid-flight (e.g. `localNativeRedirect`)? If yes, track both original and rewritten values. |

### Scope

This checklist does not change the escalation threshold or the bug vs feature distinction. It is additive — the BA agent must fill it in as part of the plan, not as a separate gate.

---

## Files changed

| File | Change |
|---|---|
| `orchestrator/stage_verifier.py` | Add `duration_seconds` param to `_verify_dev()`; branch on <60s |
| `orchestrator/orchestrator.py` | Thread `duration_seconds` into verify call; clear `scope_check` iterations on pass transition |
| `workflows/default-workflow.yaml` | `scope_check.max_iterations: 3 → 2` |
| `agents/ba-agent.md` | Add Android/Kotlin edge-case checklist to Step 4 |

---

## What this does not change

- The PR review → DEV cycle (those iterations are already cleared correctly)
- The BA escalation threshold for bugs vs features
- QA or push stage logic
- Any workspace state schema (no new fields)

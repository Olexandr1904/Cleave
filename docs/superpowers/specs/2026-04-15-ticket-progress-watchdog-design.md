# Ticket Progress Watchdog — Design

**Date:** 2026-04-15
**Status:** Proposed
**Part of:** [Feature Plan](2026-04-15-feature-plan.md)

## Problem

A ticket agent can silently hang. Claude CLI stalls mid-response. A test loop spins forever. A git push waits on a credential prompt that will never come. The orchestrator currently has no mechanism that asks *"is this ticket still making progress?"* — `orchestrator/safeguards.py` only checks *what* files are written, not *whether anything is happening*. Stalled tickets occupy slots, block other work, and burn subscription time until a human notices.

## Goal

Detect per-ticket stalls automatically and transition stalled tickets to the existing `BLOCKED` state with a clear reason, without introducing false positives on legitimately slow stages (test runs, long builds).

## Non-goals

- Replacing `BLOCKED` or the Telegram escalation path — the watchdog **feeds** them
- Detecting daemon-level liveness (that's a separate concern)
- Retrying or auto-resuming stalled tickets — `DEFERRED` already handles quota-based retry
- Per-project configurability (YAGNI — one set of thresholds in `global.yaml`)

## Design — three separable layers

The watchdog is deliberately three independent layers. Each catches what the layer below missed. Separating them is not architectural decoration; it lets detection and action evolve independently and keeps the dangerous kill path small and rare.

### Layer 1 — Heartbeat Emitter

The active agent writes a timestamp file at a fixed interval while it's working.

- **Location:** `workspace/<company>/<repo>/tickets/<id>/.heartbeat`
- **Format:** single-line file containing epoch milliseconds
- **Cadence:** roughly every 10 seconds, but written opportunistically — between tool calls, before subprocess spawns, after subprocess returns
- **Cost:** one `write` per tick; negligible
- **Semantics:** "I'm alive" signal only. Does not mean "I'm making progress" — just "this process is still executing code that cares about the file."

The emitter lives in `orchestrator/agent_runtime.py` because that's where tool-call boundaries already run. No new long-lived thread.

### Layer 2 — Stall Detector

A pass in the orchestrator main loop reads every active ticket's heartbeat file and compares the mtime to the current time against a stage-specific threshold.

- **Thresholds (defaults, in `global.yaml`):**
  - `llm_call: 5m` — any stage currently inside a Claude CLI subprocess
  - `test_run: 15m` — QA stage running test suites
  - `default: 10m` — anything else
- **Output:** emits a dashboard event (`ticket.stalled`) and optionally raises a `low`-severity Telegram alert (see severity spec)
- **Side effect:** **none.** The detector does not kill. Its job is to notice.

The detector runs on the same tick as `safeguards.py` — no new scheduler. It shares the orchestrator's existing per-tick iteration over active tickets.

The threshold lookup needs to know *which stage* the ticket is currently in, which the state machine already tracks. No new metadata.

### Layer 3 — Timeout Enforcer

A harder check, also run on the orchestrator tick but with stricter conditions. Kills the ticket subprocess tree, cleans up, and transitions state.

- **Trigger conditions (OR):**
  - Ticket has been in the "stalled" set for more than `kill_after: 30m`
  - Ticket's total wall-clock exceeds `max_wall_clock: 2h` regardless of stalls
- **Actions (in order):**
  1. Send SIGTERM to the ticket's subprocess tree; wait 10s; SIGKILL if still alive
  2. Release any git locks in the ticket workspace (`.git/index.lock`, `.git/HEAD.lock`)
  3. Write a final entry to the ticket's event log explaining why it was killed
  4. Transition ticket to `BLOCKED` with reason `watchdog_timeout`
  5. Emit `critical`-severity Telegram alert via the severity routing spec
- **Invariant:** Layer 3 is the **only** component allowed to kill processes. Nothing else in Cleave may call `kill_tree()` on a ticket subprocess.

The enforcer lives in a new module `orchestrator/watchdog.py`. Keeping it isolated from the orchestrator's main file makes the kill path audit-able and testable in isolation.

## Why three layers, not one

A single `"if no heartbeat in 30 min, kill"` check would be simpler to write but wrong in three ways:

1. **A single threshold fits no stage.** Test runs legitimately take 20 minutes; LLM calls shouldn't take 2 minutes. One number accepts long stalls for fast stages and kills legitimate work for slow ones.
2. **Detection is useful without action.** During a long QA run we want the dashboard to show *"still alive, but quiet"* without killing anything. Two separate layers make this possible. A single function can't.
3. **Kill logic must run rarely and be audit-able.** Separating "notice" from "act" means the expensive, risky path — kill process tree, clean git locks, transition state, notify human — runs only when genuinely needed. A monolithic check runs the dangerous code on every tick.

## Configuration

New section in `global.yaml`:

```yaml
watchdog:
  heartbeat_interval: 10s
  stall_thresholds:
    llm_call: 5m
    test_run: 15m
    default: 10m
  kill_after: 30m
  max_wall_clock: 2h
```

All durations use the existing duration parser (if one doesn't exist, add it — small utility). No per-project overrides until proven necessary.

## State machine interaction

The watchdog does **not** add a new state. Stalled tickets transition to the existing `BLOCKED` state with a new reason code `watchdog_timeout`. The existing resume flow (Telegram reply → `BLOCKED` → re-entry) works unchanged. This is the key design choice that keeps the feature additive.

## Dashboard integration

Three new event types flow through `dashboard/events.py`:

- `ticket.heartbeat` — optional, disabled by default (high-volume)
- `ticket.stalled` — emitted by Layer 2; shown on the ticket row as a yellow indicator
- `ticket.killed` — emitted by Layer 3; shown as a red indicator with the reason

The dashboard's per-ticket timeline view gains a "last heartbeat" field derived from `.heartbeat` file mtime at render time.

## Failure modes and how each layer handles them

| Failure | Which layer notices | What happens |
|---------|--------------------|--------------|
| Agent crashes cleanly | Orchestrator already handles via subprocess exit code | Watchdog not involved |
| Agent hangs inside Claude CLI | Layer 2 detects stale heartbeat | Flagged → Layer 3 kills after `kill_after` |
| Agent deadlocks in git operation | Layer 2 detects stale heartbeat | Same as above; git lock cleanup happens in Layer 3 step 2 |
| Heartbeat emitter crashes but agent still runs | Layer 2 false-positive: flags a running ticket as stalled | Layer 3 will kill a still-running process. **Acceptable**: false positives are rare because the emitter is trivial, and killing a running-but-non-emitting process is the right call anyway (it shouldn't exist). |
| Clock skew between heartbeat write and orchestrator read | Negligible — same host, same filesystem | — |
| Ticket legitimately exceeds threshold (e.g., huge test suite) | Layer 2 flags it | Operator configures a higher `test_run` threshold or flags the specific repo as slow-test in `global.yaml`. **This is the main source of real-world tuning work.** |

## Testing strategy

- **Unit:** Layer 2 detection logic against synthetic heartbeat files with controlled mtimes
- **Unit:** Layer 3 kill sequence against a fake subprocess that sleeps; verify SIGTERM → SIGKILL → lock release → state transition order
- **Integration:** a test that spawns a real Python subprocess with `time.sleep(600)`, sets thresholds low, and asserts it gets killed and the ticket lands in `BLOCKED` with the right reason
- **Regression:** an existing ticket end-to-end test should *not* trip the watchdog — verifies thresholds don't accidentally kill healthy work

## Success criteria

- Zero tickets remain in a running state past their configured kill threshold
- Killed tickets land in `BLOCKED` with reason `watchdog_timeout` and a clear Telegram escalation
- No false positives during normal operation (measured over one week)
- Heartbeat emission adds no measurable overhead to agent runtime

## Open questions

- Start with default thresholds, or measure current ticket stage durations first and derive thresholds from data? Recommendation: ship defaults, tune after one week of real data.
- Should Layer 3 attempt a graceful "save progress" step before SIGTERM (e.g., write a final state file)? Recommendation: no — agents are stateless between stages, so there's nothing to save.

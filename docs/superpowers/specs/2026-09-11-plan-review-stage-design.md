# Plan-review gate + pipeline telemetry

**Date:** 2026-09-11
**Status:** Design — approved, awaiting implementation plan

## Context

These two items were selected from a larger proposal ("Cleave × kmp-planner
integration design") that also asked for ticket tiers, a triage agent,
shared-file locks and a decomposition agent. Those were rejected; the reasoning
is recorded under [Rejected](#rejected-and-why) so the next reader does not
re-derive it.

The two survivors are independent of each other and can ship in either order.

## Problem

**1. The implementation plan is the only unguarded input in the pipeline.**

`scope-guard-agent` derives its allowed-file list *from* the plan
([scope-guard-agent.md:67-70](../../../agents/scope-guard-agent.md)). `code-review-agent`
judges the diff, which the dev wrote against the plan. `qa-agent` judges coverage of
that same plan. So every downstream gate measures the work against `ba.md` — and
nothing measures `ba.md` itself. A wrong plan produces a PR where every gate is green
and the wrong feature ships.

**2. We cannot answer "do tickets need different pipelines?" because we lack the data.**

The rejected proposal wanted per-size pipelines. That is a real question, but it was
argued from intuition. Bounce history is in fact already recorded — `stage_transition`
events carry `from_state`/`to_state`/`ticket_id` and are persisted to SQLite with no
retention ([driver.py:62-69](../../../orchestrator/pipeline/driver.py),
[main.py:525](../../../main.py)). What is missing is cost attribution:
`agent_execution_detail` carries `model`, tokens, `tool_calls` and `duration` but is
emitted without `ticket_id` ([agent_runtime.py:416](../../../orchestrator/agent_runtime.py)),
so spend cannot be tied to a ticket.

## Goal

1. A cold-context stage that reviews `ba.md` before any code exists, bouncing back to
   `analysis` on defects.
2. Enough recorded data to decide the tiering question on evidence after ~20 tickets.

## Non-goals

- Ticket tiers, a triage agent, per-tier workflows. Deferred until part 2 produces data.
- Making `workflow` a per-ticket property. Today it is loaded once per daemon
  ([main.py:250-251](../../../main.py)); that refactor is a separate ticket and a
  prerequisite for tiers, not for this work.
- Shared-file locks, cross-ticket decomposition, child-ticket creation.
- Fixing pre-existing inconsistencies noticed along the way (see the last section).

---

# Part 1 — the `plan_review` stage

## Decisions taken

| Question | Decision |
|---|---|
| Which tickets does it run on? | **All of them**, unconditionally. No branching in the graph. |
| What does it check? | **Plan form *and* verification against the real repo.** |
| Where does the human approve in manual mode? | **After `plan_review`**, not after `analysis`. |
| How does the BA receive defects? | Through a file on disk, following the existing `dev ← scope-guard` pattern. No new mechanism. |

Running on all tickets means the agent will frequently be handed a two-line plan for a
bug, because `ba-agent`'s bar for bugs is deliberately low
([ba-agent.md, Step 1](../../../agents/ba-agent.md)). The agent prompt must treat that
as normal rather than as a defect; see [Agent](#agent).

## State machine

A new `Stage.PLAN_REVIEW`. Every edit to the transition matrix is **additive** —
nothing is removed, because Telegram can force arbitrary stage transitions
([command_handler.py:353](../../../integrations/telegram/command_handler.py)) and
narrowing the matrix would break those paths.

In [workspace.py:40-54](../../../workspace/workspace.py):

- add `Stage.PLAN_REVIEW` to the `Stage.ANALYSIS` target set;
- new row: `Stage.PLAN_REVIEW → {DEV, ANALYSIS, BLOCKED, FAILED, DEFERRED,
  AWAITING_APPROVAL, MANUAL_CONTROL, PAUSED}` (`AWAITING_APPROVAL` is required because
  the manual-mode gate now fires here);
- add `Stage.PLAN_REVIEW` to the target sets of `BLOCKED`, `FAILED`,
  `AWAITING_APPROVAL`, `DEFERRED` and `PAUSED`, so recovery and resume can re-enter it.

In [driver.py:29](../../../orchestrator/pipeline/driver.py): `"plan_review": Stage.PLAN_REVIEW`.

## Workflow graph

In [default-workflow.yaml](../../../workflows/default-workflow.yaml), `analysis.next`
becomes `plan_review`, and the new stage is inserted:

```yaml
  # The plan is what scope_check, code_review and qa all measure against, so a
  # defect here is invisible to every later gate. Cold context on purpose: a
  # judge that resumes the author's session inherits the author's justifications.
  - id: "plan_review"
    agent: "plan-review-agent"
    description: "Review the plan itself, before any code exists"
    on_pass: "dev"
    on_fail: "analysis"
    max_iterations: 2
    on_max_iterations: "escalate"
```

No `resume_from_previous`. `max_iterations: 2` and escalation match the other judging
stages; the BA runs cold on each bounce, so an uncapped loop could re-emit a similar
plan indefinitely.

## Agent

New `agents/plan-review-agent.md`, persona "Surveyor".

**Tools:** `read_file`, `list_directory`, `search_code`. No `write_file` — the agent
must describe defects, never rewrite the plan, and withholding the tool makes that
structural rather than a promise in a prompt. No `git_operation` — no diff exists yet.

**Inputs:** `ai_pipeline/{ticket_id}/ba.md`, `meta/ticket.md`, `meta/parent.md`,
`meta/comments.md`, `rules/arch-rules.md`, plus the repository itself.

**Output:** the runtime writes `plan-review-agent-output.md` into `reports_dir` after
every agent run ([agent_runtime.py:490](../../../orchestrator/agent_runtime.py)), which
is what outcome detection reads. The agent therefore needs no write access to produce
its report.

The report opens with a verdict line — `Status: PASS` or `Status: FAIL` — followed by
the defects; each defect names the plan section and a concrete reason. The verdict line
is a contract, not a suggestion: see [Outcome detection](#outcome-detection-and-verification).

**Two hard rules in the prompt**, both carried over from the proven
[code-review-agent](../../../agents/code-review-agent.md) contract:

1. If the plan is sound, say `PASS`. Never invent findings to look useful.
2. A short plan for a bug ticket is correct, not a defect. The BA's bar for bugs is
   deliberately low, and flagging that would make this gate noisy on the majority of
   tickets — a noisy gate gets ignored, which costs more than the gate is worth.

## Checklist

New `checklists/plan-review-checklist.md`, referenced from the agent's
`dependencies.checklists`. Stack-agnostic by design — Cleave's state machine and agents
carry no stack assumptions ([multi-stack-readiness.md](../../multi-stack-readiness.md)),
and a mobile-flavoured checklist would generate nonsense findings on a backend repo.

**Form**

- every acceptance criterion has a pass/fail condition;
- no unverifiable language ("works correctly", "is fast", "handles errors");
- the Files to Create / Files to Modify lists are concrete and complete — this is
  scope-guard's input, and an empty or vague list is an automatic FAIL;
- every task in the plan references an acceptance criterion;
- assumptions are labelled as assumptions rather than stated as fact;
- the plan does not contradict `rules/arch-rules.md`.

**Verification against the repository**

- every file under Files to Modify exists;
- every file under Files to Create does not already exist;
- symbols, modules and endpoints named in the plan are findable by search.

A plan built on an invented project structure is a defect — this is the class of error
the form checks alone cannot catch.

## BA feedback loop

`ba-agent` has no `resume_from_previous`, so on a bounce it starts cold and reads from
disk. Add `ai_pipeline/{ticket_id}/plan-review-agent-output.md` to its `inputs`
([ba-agent.md:24-28](../../../agents/ba-agent.md)) and one line to its Process: when
that file exists, correct the named defects in the existing plan rather than rewriting
it from scratch.

This mirrors how `dev-agent` already consumes `scope-guard.md`
([dev-agent.md:26-33](../../../agents/dev-agent.md)). No new plumbing.

## Approval gate

Inserting a stage between `analysis` and `dev` silently disables the existing
manual-mode gate: it fires only when the happy-path next stage is exactly `"dev"`
([approval_gate.py:17-22](../../../orchestrator/approval_gate.py)), which would no
longer be true of `ANALYSIS`. The gate moves rather than being patched in place, so the
operator reviews a plan the machine has already vetted:

- `APPROVAL_GATE_STATES = {Stage.PLAN_REVIEW, Stage.QA}`;
- `GATE_HAPPY_PATH_NEXT_STAGE = {Stage.PLAN_REVIEW: "dev", Stage.QA: "push"}`;
- `"PLAN_REVIEW": "DEV"` in `APPROVAL_NEXT_STATE`
  ([approval.py:10](../../../integrations/telegram/handlers/approval.py)).

`ANALYSIS` leaves the gate set. The number of manual-mode stops is unchanged.

## Outcome detection and verification

`plan_review` gets its **own** strict branch in `_parse_outcome`
([agent_stage.py:150](../../../orchestrator/pipeline/agent_stage.py)) rather than
joining the `("scope_check", "code_review", "qa")` tuple. The shared
`_looks_like_pass` ([agent_stage.py:81-90](../../../orchestrator/pipeline/agent_stage.py))
is a bag of substrings that includes `"advances to"` and treats `"verdict: hold"` as a
pass, and `_parse_outcome` checks pass **before** fail. A plan-review report containing
"the plan advances to dev only if…" would therefore be read as PASS — the exact
inversion this gate exists to prevent.

The strict branch instead matches `^status:\s*(pass|fail)` against the runtime output
and **fails closed**: anything else — a malformed report, a crashed agent, an empty
file — counts as `fail` and bounces to `analysis`. With `max_iterations: 2` a genuinely
broken agent escalates to a human rather than waving plans through. Fail-closed is the
right direction for a gate; the existing loose matcher is left untouched for the stages
that already depend on its quirks.
- [constants.py](../../../orchestrator/constants.py): add
  `RUNTIME_OUTPUT_PLAN_REVIEW = "plan-review-agent-output.md"` and register it in
  `STAGE_RUNTIME_OUTPUT`. Do **not** add an entry to `STAGE_REPORT_FILE` — that map is
  dead code (see the last section) and the agent cannot write a structured report
  anyway.
- [stage_verifier.py:112](../../../orchestrator/stage_verifier.py): verify the report
  exists, matching the `code_review` branch.

## Operator surface

`PLAN_REVIEW` must appear wherever the other stages are enumerated, or the dashboard
will render an unknown state and the pause controls will skip it:
[web.py:401](../../../dashboard/web.py),
[actions.py:28](../../../dashboard/actions.py) and
[actions.py:306](../../../dashboard/actions.py),
[board.js:74](../../../dashboard/static/js/board.js) and
[board.js:338](../../../dashboard/static/js/board.js),
[helpers.js:58](../../../dashboard/static/js/helpers.js) and
[helpers.js:81](../../../dashboard/static/js/helpers.js),
[detail.js:114](../../../dashboard/static/js/detail.js),
[status.py:12](../../../integrations/telegram/handlers/status.py),
[command_handler.py:353](../../../integrations/telegram/command_handler.py) and
[command_handler.py:630-631](../../../integrations/telegram/command_handler.py).

---

# Part 2 — pipeline telemetry

No new event types and no schema migration. The bounce history is already in SQLite;
only the cost attribution is missing.

**Instrumentation (one line).** In
[agent_runtime.py:416](../../../orchestrator/agent_runtime.py), pass
`ticket_id=workspace.state.ticket_id` and `project_id=workspace.state.company_id` to the
`agent_execution_detail` emit. `workspace` is already in scope at that point.

**Reporting.** New `scripts/pipeline_stats.py`, read-only against the existing events
database. Per ticket: entries into each stage, bounce edges (`SCOPE_CHECK→DEV`,
`CODE_REVIEW→DEV`, `QA→DEV`, `PLAN_REVIEW→ANALYSIS`), input/output tokens, wall-clock
duration, and terminal state. Output as a table.

This is what the tiering question gets decided on: if bounces and spend turn out to be
flat across tickets, tiers buy nothing and the idea dies cheaply.

---

## Verification

1. The workflow YAML loads and yields `analysis → plan_review → dev`, with
   `on_fail → analysis`.
2. The transition matrix permits `ANALYSIS → PLAN_REVIEW → DEV` and
   `PLAN_REVIEW → AWAITING_APPROVAL`.
3. `_parse_outcome("plan_review", …)` returns `pass` only for `Status: PASS`, and
   returns `fail` for `Status: FAIL`, for a report containing the phrase "advances to",
   and for an empty or missing report.
4. The approval gate fires on `PLAN_REVIEW → dev` and no longer fires on `ANALYSIS`.
5. `pipeline_stats.py` produces a table from the existing database.
6. A `--dry-run` pass over a live ticket, then a real run.

## Risks

- **A noisy gate on bug tickets.** The mitigation is rule 2 in the agent prompt, but it
  is a prompt, not a guarantee. First twenty tickets: check how often `plan_review`
  bounced a bug. If it is frequent, the honest fix is to make the stage conditional on
  the BA declaring a real plan — which is the scoping option rejected here, reopened on
  evidence.
- **BA ↔ plan_review ping-pong.** The BA runs cold each time, so it may re-emit a plan
  with the same defect. Capped at 2 iterations, then escalation.
- **Added latency and spend on every ticket** — one extra agent run before any code is
  written. Part 2 will measure exactly how much.

## Rejected (and why)

Recorded so the proposal does not return unexamined.

- **Tiers and a triage agent.** The sizing rubric was presented as mechanical and
  reproducible, but "does this change a user-visible state machine?" is exactly as
  non-deterministic as "is this big?" — and the proposal itself predicted mis-sizing.
  Also premised on workflow-by-label routing, which does not exist: repo and model route
  by label, the workflow does not. Revisit after Part 2 produces data.
- **Shared-file locks.** No evidence of the contention they solve at current
  concurrency. Also premised on `DEFERRED` being a general condition-wait; it is a
  time-based quota backoff ([runtime.py:141-170](../../../orchestrator/runtime.py)).
- **`decompose-agent` and child-ticket creation.** `TrackerInterface`
  ([tracker.py](../../../integrations/base/tracker.py)) has no create operation, so this
  means new methods across the Jira and Trello adapters. Better proven by hand a few
  times before being automated.
- **Dependency gating at poll time.** Already implemented —
  [`skip_blocked_tickets`](../../../orchestrator/ticket_prioritizer.py).

## Pre-existing issues noticed, not fixed

- `STAGE_REPORT_FILE` ([constants.py:24](../../../orchestrator/constants.py)) has no
  callers.
- `scope-guard-agent` declares `outputs: scope-guard.md` but holds no `write_file`
  grant, so that file is likely never produced; `pr_creation.py:62` reads it.
- The open P0 from
  [Cleave-agent-tooling-findings-2026-08-27.md](../../../Cleave-agent-tooling-findings-2026-08-27.md)
  §4.1 — agents still have six tools; `Task`, `Skill`, `WebSearch`/`WebFetch` and
  `TodoWrite` are unreachable. Unrelated to this spec, but it caps how good any agent
  here can be, `plan-review-agent` included.

# Cleanup & Bugfix Plan

Status: **PRs 1–4 shipped 2026-04-30.** Only postponed items remain (A6, A8). All other in-scope work is closed.

Threat model assumed: AI pipeline runs on a sandbox box owned by the operator.
Agents have host-level access by design. Items related to "open dashboard",
`shell=True` in tools, JS escape gaps, etc. are operator responsibility, not
bugs. What remains below is bugs regardless of threat model, plus structural debt.

Item IDs preserved from the original audit (gaps where items were closed).

---

## A — Correctness bugs (silent-failure class)

| #  | Status | Location | Bug | Note |
|----|--------|----------|-----|------|
| A1 | ✅ FIXED | `orchestrator/orchestrator.py` | Iteration cap reset zeros counter → escalation never fires | Commit `4a8cc06`. Reset removed. |
| A2 | ✅ FIXED | `integrations/github/github_adapter.py` | `self._token` referenced but never assigned in `__init__` | Commit `f40b345`. `__init__` now sets it; regression test added. |
| A3 | ✅ FIXED | `orchestrator/agent_runtime.py` | Hitting `max_tool_rounds` returns `success=True` with empty output | Commit `702a3ba`. Now `success=False, failure_kind="permanent"`. |
| A5 | ✅ FIXED | `integrations/telegram/command_handler.py` | Inconsistent `"BLOCKED"` string vs `Stage.BLOCKED` enum | Commit `03bf1a7`. |
| A6 | POSTPONED | `integrations/jira/jira_adapter.py:190` | `transition_ticket` smart-hop picks `transitions[0]` blindly | Needs Jira-workflow knowledge before fixing. |
| A8 | POSTPONED | `workspace/workspace.py:170-182` | Unknown JSON keys silently dropped on load → next save deletes data on field rename | Schema-version design needed; not a quick win. |

## B — Resource / lifecycle bugs

| #  | Status | Location | Bug |
|----|--------|----------|-----|
| B1 | ✅ FIXED | `integrations/llm/claude_code_adapter.py` | `proc.kill()` on timeout — commit `192c0b7`. `await proc.wait()` added in both call sites. |
| B2 | ✅ FIXED | `orchestrator/agent_runtime.py` | `cancel()` SIGTERMs and forgets — commit `f61d5d2`. Now async with SIGKILL escalation; dashboard callers updated. |
| B3 | ✅ FIXED | `orchestrator/orchestrator.py` | `asyncio.wait()` task leak — commit `add6ac7`. Pending tasks cancelled and reaped. |
| B4 | ✅ FIXED | `integrations/github/github_adapter.py` | Sync `subprocess.run` in async — commit `4a2add4`. `_run_git` now async via `create_subprocess_exec`. |
| B5 | ✅ FIXED | `orchestrator/agent_runtime.py` | No total context budget — commit `e3ee090` (bundled with parallel budget-config work). |
| B6 | ✅ FIXED | `orchestrator/orchestrator.py` | Auto-resume tail-recursion — commit `ef1a746`. Recursion depth capped at 5. |

## C — Architecture refactors

| #  | Status | Target | Note |
|----|--------|--------|------|
| C1 | LIVE | `orchestrator/tool_sandbox.py` rename / docstring | No rename. Current docstring claims "sandboxed tool execution... confined to workspace's source/ and reports/" — path-jail truth, not OS-sandbox truth. |
| C2 | PARTIAL | `orchestrator/orchestrator.py` god-object | Several extractions landed (`pr_creation`, `stage_verifier`, `gradle_remediation`, `escalation_view`, `ticket_prioritizer`, `comment_classifier`, `resolution_report`, `model_resolver`, `merge_step`, `workflow_router`, `safeguards`). Despite this, file grew 2296 → 2465 LOC, 59 methods. PR-review work added more than refactors removed. |
| C3 | LIVE | `orchestrator.py` `_tracker._` reach-throughs | 5 confirmed at lines 590, 640, 2020, 2030, 2044 (was 4 — one new added). |
| C4 | PARTIAL | `main.py` 527-line `main()` | `parse_args` extracted (lines 32-68). Body still 526 lines (lines 71-596). |
| C5 | PARTIAL | `command_handler.py` giant dispatchers | `handle_reply` 144 → 146 LOC. `handle_callback` 167 → 183 LOC. `handlers/` subpackage exists but holds new horizontal handlers, not extracted branches. |
| C6 | LIVE | `tool_sandbox.py:546` `get_tool_definitions` 233-line dict | 26 inline tool dicts. |

## D — Test thinness

| #  | Status | File | Note |
|----|--------|------|------|
| D1 | DROPPED | `tests/unit/test_orchestrator_modes.py:81` | On reread: `.called` IS the meaningful assertion (mode dispatches to tracker). Symmetric with `assert_not_called()` in the manual-mode sibling test. Audit was over-strict. |
| D2 | ✅ FIXED | `tests/unit/test_intent_parser.py` | Commit `6e91853`. Now asserts mode + ticket IDs appear in the system prompt passed to the adapter. |
| D6 | DROPPED | `tests/unit/test_dashboard_actions.py:187, :297` | On reread: both lines are followed by `args[0] == "MANUAL_CONTROL"` and kwargs checks. The `assert ws.transition.called` is redundant prefix, not a thin test. Audit was over-strict. |
| D8 | ✅ FIXED | `dashboard/atlas_runner.py` | Commit `6e91853`. 4 smoke tests added: happy path, failure, rollback-raise resilience, on_complete invariant. |

## E — Cleanup

| #  | Status | Item | Note |
|----|--------|------|------|
| E2 | ✅ FIXED | `start.md` (865 LOC) | Commit `89e6076`. Moved to `docs/legacy/start.md`; references in project-brief, prd, architecture updated. |
| E3 | ✅ FIXED | `environment.template` vs `deploy/environment.template` | Commit `89e6076`. `deploy/environment.template` now uses `CLAUDE_API_KEY` to match the rest of the codebase. |

---

## Effort legend

- **XS** — under 30 minutes
- **S** — under 2 hours
- **M** — half a day
- **L** — multi-day, likely multi-PR

## Status legend

- **LIVE** — bug confirmed present
- **PARTIAL** — some progress, work remains
- **FIX** / **REFACTOR** / **POSTPONE** / **DROP** — triage decision

## Workflow

1. ~~Recheck pass.~~ ✅ Done.
2. ~~Remove STALE / invalid items.~~ ✅ Done.
3. ~~PR 1 (section A correctness).~~ ✅ Shipped: A1, A2, A3, A5. A6 + A8 postponed.
4. ~~PR 2 (section B lifecycle).~~ ✅ Shipped: B1, B2, B3, B4, B5, B6.
5. ~~PR 3 (section D test rewrites).~~ ✅ Shipped: D2, D8. D1 + D6 dropped after re-read.
6. ~~PR 4 (section E cleanup).~~ ✅ Shipped: E2, E3.
7. **Open work:** A6 (jira transitions[0]) and A8 (workspace state schema drift) — both postponed pending design decisions.
8. Delete this file when A6 and A8 are decided (or this file becomes the home of those decisions).

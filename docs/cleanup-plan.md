# Cleanup & Bugfix Plan

Status: **PR 1 (section A) shipped 2026-04-30.** 18 actionable items remain.

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
| B1 | LIVE | `integrations/llm/claude_code_adapter.py:234, :313` | `proc.kill()` on timeout, no `await proc.wait()` → zombies + leaked pipes |
| B2 | LIVE | `orchestrator/agent_runtime.py:107` | `cancel()` SIGTERMs and forgets — no SIGKILL fallback, no wait |
| B3 | LIVE | `orchestrator/orchestrator.py:392-401` | `asyncio.wait()` leaves loser task uncancelled every poll |
| B4 | LIVE | `integrations/github/github_adapter.py:83` | Synchronous `subprocess.run` for git inside `async` methods → blocks loop ≤300s |
| B5 | LIVE | `orchestrator/agent_runtime.py:168` | Per-file 5KB cap, no total budget cap → silent context-window overflow |
| B6 | LIVE | `orchestrator/orchestrator.py:731` | Auto-resume tail-recurses → cascading gates can stack-overflow |

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
| D1 | LIVE | `tests/unit/test_orchestrator_modes.py:81` | Body still `assert orch._tracker.poll_tickets.called`. |
| D2 | LIVE | `tests/unit/test_intent_parser.py:79` | Body still `assert mock_adapter.quick_query.called`. |
| D6 | LIVE | `tests/unit/test_dashboard_actions.py:187, :297` | Both still `assert ws.transition.called`. |
| D8 | LIVE | `dashboard/atlas_runner.py` | File present, no test references. Decide: smoke test or delete. |

## E — Cleanup

| #  | Status | Item | Note |
|----|--------|------|------|
| E2 | LIVE | `start.md` (865 LOC) | Present at repo root. Referenced from docs/, not from code. |
| E3 | LIVE | `environment.template` vs `deploy/environment.template` | Both present. Mismatch confirmed: root uses `CLAUDE_API_KEY`, `deploy/` uses `ANTHROPIC_API_KEY`. |

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
4. **PR 2 (section B lifecycle)** ← next
5. PR 3 (section D test rewrites)
6. PR 4 (section E cleanup)
7. Delete this file when all items closed.

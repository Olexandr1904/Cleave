# Feature: Agent System (BMAD-style)

**Status:** Implemented
**Created:** 2026-04-07
**Updated:** 2026-05-12
**Author:** Oleksandr Brazhenko

## Description

Pluggable AI agent system following the BMAD pattern. Each agent is a standalone markdown prompt file containing persona, role, core principles, tasks, templates, checklists, and activation instructions. Adding a new agent requires only dropping a file into `agents/` — zero code changes. Agents are stateless: they receive context, execute via Claude API, write output to workspace context files, and exit.

## Requirements

- FR1: Each agent defined as a standalone prompt file (`agents/{agent-id}.md`) with persona, role, core principles, tasks, templates, checklists, and activation instructions
- FR2: Adding a new agent requires only dropping a file into `agents/` — zero code changes
- FR3: Each agent has access to declared dependencies: tasks, templates, checklists, and shared data
- FR4: Agents are stateless — receive context, execute, write output to `workspace/context/`, exit
- FR5: Agent prompt files follow BMAD format: YAML frontmatter (id, name, title, persona, dependencies) + markdown body (instructions, principles)
- FR6: Resource registry maps resource type + id to file path at startup
- FR7: Agent dependency declarations are validated — missing references produce warnings

### Agent Roster (MVP)

- PM Agent — ticket prioritization, routing, dependency checking
- BA Agent — requirements validation, implementation plan, test scenarios
- Dev Agent — implementation on feature branch, scope-constrained
- Scope Guard Agent — diff analysis, scope certificate or violations
- Fix/Reviewer Agent — address review comments with scope re-check
- QA Agent — write tests, run suite + lint + build
- Merge Agent — gate checklist, conflict resolution, merge, Jira transition

### PR Review: Verdict, Hints, and Recall

Each escalated PR comment carries a one-word verdict — **Valid** (the reviewer is correct) or **Not valid** (the reviewer is mistaken) — alongside the agent's reasoning. The verdict is the agent's own lean; the operator decides what to do.

If the operator replies with neither `fix` nor `won't fix`, the free text is treated as a hint: the comment is re-classified by the responder agent with the operator's hint as context. Capped at 3 rounds per comment.

Operators can recall pending comments via the `/unanswered` command (or `/unanswered <TICKET>` for one ticket) — both paths re-send each undecided comment with fresh Fix / Won't Fix buttons. Replies match against the original message OR any recall.

Every comment receives a reply on GitHub at decision time: `Will fix: <reason>` for AUTO_FIX and operator-FIX paths, `Won't fix: <reason>` for AUTO_REJECT and operator-WON'T-FIX paths. Resolution happens later when the diff is verified.

See: [docs/superpowers/specs/2026-04-30-pr-review-flow-improvements-design.md](../superpowers/specs/2026-04-30-pr-review-flow-improvements-design.md).

## Technical Approach

- Agent prompt files stored in `agents/` directory as `.md` files
- Agent runtime loads prompt, injects workspace context + config, calls Claude API, captures output
- Template Method pattern: load prompt → inject context → call LLM → write output → log
- Agent metadata parsed from YAML frontmatter: id, name, title, persona, core_principles, dependencies, model_override
- Resource registry built at startup by scanning `agents/`, `tasks/`, `templates/`, `checklists/`, `data/` directories

## Dependencies

- Claude API (Anthropic SDK) for agent execution
- Orchestrator for agent dispatch and workflow routing
- Workspace system for context files and isolation
- Config system for operator profile and project settings injection

## Acceptance Criteria

- [ ] Agent prompt files exist for all MVP agents (PM, BA, Dev, Scope Guard, Fix, QA, Merge)
- [ ] Resource registry discovers all agent files and their dependencies at startup
- [ ] Agent runtime can load, assemble, and execute any agent prompt file
- [ ] Adding a new agent file to `agents/` makes it available without code changes
- [ ] Agent execution logs prompt summary, model, token usage, and duration

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
| 2026-04-08 | Added `quick_query` to `ClaudeCodeAdapter` — lightweight single-turn, no-tools call for intent parsing (5s timeout) |
| 2026-04-12 | Added agent tracking and cancellation to `AgentRuntime`: `register_running`, `unregister_running`, `get_running`, `cancel` methods; `_running` dict maps ticket_id → agent info; `_execute_cli` wrapped with register/unregister; `cancel()` sends SIGTERM. |
| 2026-04-14 | Added QuotaExhaustedError exception and _classify_cli_error helper for detecting Claude CLI usage-limit hits. Not yet wired into _run_cli (Task 4). |
| 2026-04-14 | Wired _classify_cli_error into _run_cli: execute_in_workspace now raises QuotaExhaustedError (instead of generic RuntimeError) on quota/rate-limit hits. |
| 2026-04-14 | AgentResult now carries failure_kind ("quota" / "permanent") and retry_at; execute() distinguishes QuotaExhaustedError from generic failures. |
| 2026-04-14 | Tightened `_QUOTA_SUBSTRINGS` in `_classify_cli_error`: dropped the bare `"quota"` token which false-positive'd on any diagnostic containing the word (e.g. file paths). Rate-limit/overloaded markers cover real CLI quota errors. |
| 2026-04-24 | Claude Code CLI failure path now captures stdout alongside stderr in both the log line and the RuntimeError message. Previously, when `claude -p` exited non-zero with empty stderr, the failure surfaced as "exited with code 1: " with no context — but `--output-format json` writes error payloads and MCP startup diagnostics to stdout. |
| 2026-04-27 | Bumped `DEFAULT_TIMEOUT` from 600s to 2400s (40 min). Real-world QA on large codebases (compile + tests + lint, especially mobile/Android) routinely exceeds 10 min. Also fixed transient-failure classification in `agent_runtime.py`: the substring check `"timeout" in error_str` did not match the actual CLI error wording `"timed out"`, so timeouts fell through to `failure_kind="permanent"` (FAILED) instead of the auto-retry `"quota"` (DEFERRED) path. Added `"timed out"` to the substring list. |
| 2026-04-27 | `_classify_cli_error` now recognizes `api_error_status: 429` in CLI JSON output as a quota hit. The Max-subscription session-limit response (`is_error=true, api_error_status=429, result="You've hit your limit · resets <time>"`) wasn't matched by either the `Claude AI usage limit reached\|<epoch>` regex or the substring fallbacks (`rate_limit`, `overloaded_error`, `usage limit reached`), so it fell through to a generic CLI-exited error and got the same 10-min retry as transient failures. Now classified as `QuotaExhaustedError` with default 1-hour delay. |
| 2026-04-30 | `AgentRuntime._execute_with_tools` no longer reports `success=True` when the tool loop exits at `max_tool_rounds` with tool calls still pending. Previously the empty final response was indistinguishable from a real successful empty answer, and downstream stages dispatched on stale output. Now returns `success=False, failure_kind="permanent", error="max_tool_rounds_exhausted"` so the orchestrator routes through the existing FAILED path. |
| 2026-04-30 | `ClaudeCodeAdapter` timeout paths in `_run_cli` and `quick_query` now `await proc.wait()` after `proc.kill()`. Previously the kill was sent and the function raised immediately, leaving the subprocess in a zombie state with stdout/stderr pipes open. Under repeated timeouts this could exhaust pipe buffer space and stall future `communicate()` calls. |
| 2026-04-30 | `AgentRuntime.assemble_prompt` now caps total context-file bytes at `_TOTAL_CONTEXT_BYTES` (100 KB) across all files in `meta_dir`, in addition to the existing 5 KB per-file truncation. Without the total cap, a workspace with many context files could silently overflow the model's prompt window. Once the budget is exhausted, remaining files are skipped with a warning log; the last partial file is suffixed `...(budget exhausted)`. New constants `_PER_FILE_CONTEXT_BYTES` and `_TOTAL_CONTEXT_BYTES` are module-level for easy tuning. |
| 2026-04-30 | `AgentRuntime.cancel` is now async and waits for the process to actually exit, escalating to SIGKILL after a configurable `sigkill_after` (default 5s). Previously it sent SIGTERM and returned immediately, so callers had no guarantee the subprocess was dead — a stuck agent that ignored SIGTERM stayed alive holding the workspace, with the runtime no longer tracking its PID. Polls `os.kill(pid, 0)` for liveness because the `subprocess.Process` object is owned by `claude_code_adapter._run_cli` (which reaps via its own `communicate()`). Dashboard callers `take_control` and `pause` updated to `await` the new signature. |
| 2026-05-04 | BA Agent now includes Android/Kotlin Checklist in implementation plan template for repos with 'android' in repo id. Checklist covers 6 thread-safety and async concerns (shared mutable state, thread of execution, lambda capture, overlapping async ops, error path parity, URL/redirect chains) to reduce PR review cycles on Android tickets. |
| 2026-05-08 | `AgentBudget` now carries `max_cli_turns` (default 100, dev-agent override 200), threaded through `AgentRuntime` into the Claude Code CLI's `--max-turns`. Independent from `max_tool_rounds` because the CLI runs its own opaque tool loop. The previous hard-coded 50 was tripping mid-size dev tickets (e.g. ACME-6941 hit 51 turns at ~4 min / 4M tokens — well within wall-clock and token budgets). `claude_code_adapter.DEFAULT_MAX_TURNS` raised 50→100 to match the new default for callers that don't pass an explicit value. |
| 2026-05-08 | BA Agent gains explicit decision-gate threshold: escalate via `ba-questions.md` only on architecture/scope/contract/data-model ambiguities; lesser ambiguities pick a sensible default and record it under a new `## Assumptions` section in the plan. Step 4 also asks BA to weigh 2-3 viable approaches before drafting and surface the picked trade-off in `## Summary`. Goal: borrow the structured-design discipline from the superpowers `brainstorming` skill without its interactive question loop, which is incompatible with headless `claude -p` runs. No code or settings changes — prompt-only. |
| 2026-05-08 | `ClaudeCodeAdapter` no longer carries a hardcoded `DEFAULT_TIMEOUT=2400`. Wall-clock cap is owned by `AgentBudget.wall_clock_seconds` and threaded through `execute_in_workspace(timeout=...)` per call; `AgentRuntime` already wraps the dispatch in `asyncio.wait_for(..., timeout=budget.wall_clock_seconds)`, which remains the canonical enforcer. The inner `asyncio.wait_for` in `_run_cli` is now opt-in (skipped when `timeout=None`) so two enforcers can't disagree, and the timeout error message reports the per-call value instead of the stale module constant. |
| 2026-05-11 | `ClaudeCodeAdapter` switched to `--output-format=stream-json --verbose` so the CLI's turn boundaries, tool calls, and inter-turn heartbeats arrive as they happen instead of in one end-of-process JSON blob. Per-call `progress_log_path` (append-mode events log) and optional `raw_stream_path` (one stream-json line per line, for forensic replay of stalled or killed runs) are threaded through `AgentRuntime.execute`. Each event updates a shared progress dict; a heartbeat task writes a status line on `HEARTBEAT_INTERVAL_SECONDS` so a stalled run still produces a clear "last activity at T-N seconds" trail, and the post-kill path dumps accumulated state to the events log before raising. `Orchestrator._handle_agent_stage` (now `pipeline/agent_stage.handle_agent_stage`) gains structured `Stage entry` / `Stage exit` log lines, and the transient-CLI-error path records the resulting `retry_at` in the warning. |
| 2026-05-14 | `_post_result_watchdog` in `claude_code_adapter.py` now also catches mid-run stalls. Previously it only fired when the CLI went idle *after* emitting a terminal `result` event; a run that hung before any result (observed on RTL-13824 — `claude -p` issued a tool call and never came back) was invisible to it and coasted until the wall-clock cap. New `IDLE_STALL_SECONDS` threshold (1800s, set above the longest legitimate intra-run gap so long tool calls aren't misjudged) triggers a kill when no event arrives before any result is seen. `_run_cli` distinguishes the two kills via `progress.last_result_at`: a post-result idle kill is a graceful success (cached result), a mid-run stall kill raises `RuntimeError` so `AgentRuntime` retries instead of recording a hung agent as completed. |

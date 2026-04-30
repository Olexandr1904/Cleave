# Feature: Agent System (BMAD-style)

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-12
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

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

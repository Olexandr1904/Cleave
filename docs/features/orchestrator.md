# Feature: Orchestrator

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-09
**Author:** Oleksandr Brazhenko

## Description

Central daemon process that continuously polls for work, manages isolated workspaces, and dispatches BMAD-style agents via the workflow router. The orchestrator determines which agent to invoke based on ticket state and `state.json`, supports configurable workflow definitions, and enforces iteration caps with human escalation.

## Requirements

- FR1: Orchestrator determines which agent to invoke based on ticket state and `state.json`
- FR2: Default routing: unclear ticket → BA/PM Agent; clear → Dev Agent; code written → QA Agent; review comments → Fix Agent; all gates passed → Merge Agent
- FR3: Routing logic configurable via workflow definitions specifying agent sequence and transition conditions
- FR4: Supports looping with configurable iteration caps per stage
- FR5: Main loop runs on configurable `poll_interval_seconds`
- FR6: Each cycle: poll tracker → check new tickets → check slot availability → spawn workspaces → advance active workspaces
- FR7: Slot limits enforced per-repo and per-project
- FR8: `--dry-run` flag polls tickets and logs what would happen without executing
- FR9: Handles exceptions per-workspace without crashing the daemon
- FR10: SIGTERM/SIGINT triggers graceful shutdown: finish current agent calls, save state, exit
- FR11: Mode-aware behavior — in `manual` mode the orchestrator skips Jira polling and pauses workspaces at approval gates (ANALYSIS, QA, PR_REVIEW) by transitioning to `AWAITING_APPROVAL` and sending a Telegram summary; `auto` mode runs end-to-end without gates

## Technical Approach

- Single long-running asyncio process
- Workflow router reads `state.json` and applies transition rules from config
- Default workflow: PM → BA → Dev → Scope Guard → PR → Fix → QA → Merge
- Conditional transitions: scope guard fail → Dev; QA fail → Dev; max iterations → escalate
- Escalate state triggers Telegram notification and sets `status: waiting_for_human`
- Workspace advancement is per-workspace: invoke next agent, update state, handle result

## Dependencies

- Agent System for dispatching agents
- Workspace Isolation for workspace management and state
- All integration adapters (Jira, GitHub, Telegram)
- Configuration Cascade for workflow definitions and settings

## Acceptance Criteria

- [ ] Main loop polls for tickets and advances workspaces on each cycle
- [ ] Workflow router correctly sequences agents based on state
- [ ] Conditional transitions work (scope guard loop, QA loop)
- [ ] Iteration caps trigger escalation at configured max
- [ ] Dry-run mode logs actions without executing
- [ ] Graceful shutdown on SIGTERM/SIGINT
- [ ] One workspace failure does not crash the daemon
- [ ] Orchestrator honors `pipeline.mode` (auto/manual): skips polling in manual, pauses at approval gates, and does not advance workspaces in `AWAITING_APPROVAL`

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
| 2026-04-09 | Added mode-aware behavior: manual-mode skips polling, inserts approval gates after ANALYSIS/QA/PR_REVIEW, skips advancing AWAITING_APPROVAL workspaces. New `set_mode_handler` setter and `_should_approval_gate` check. |

# Feature: Orchestrator

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-21
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
- [ ] Orchestrator skips workspaces in `MANUAL_CONTROL` state entirely (operator has taken direct control)

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
| 2026-04-09 | Added mode-aware behavior: manual-mode skips polling, inserts approval gates after ANALYSIS/QA/PR_REVIEW, skips advancing AWAITING_APPROVAL workspaces. New `set_mode_handler` setter and `_should_approval_gate` check. |
| 2026-04-09 | Instrumented with optional event_bus: emits daemon_started, poll_cycle, workspace_created, agent_dispatched, agent_completed, agent_failed, approval_requested, stage_transition, escalation_sent, and pr_created events. |
| 2026-04-12 | Added explicit MANUAL_CONTROL skip in advance_workspace: orchestrator no longer advances workspaces under operator direct control. |
| 2026-04-14 | Narrowed terminal-state filter to `{DONE, ARCHIVED}`: FAILED is now retained in the active list so it can be retried or manually recovered. DEFERRED (added in same release) is likewise active. |
| 2026-04-14 | Split agent failure routing: quota (`result.failure_kind == "quota"`) now routes to DEFERRED with `retry_at` and iteration rollback; permanent failures continue to FAILED. Added `_quota_window_end` in-memory debounce so multiple concurrent quota hits produce a single Telegram notification per window. New `_rollback_iteration`, `_notify_deferred`, `_notify_failed` helpers. |
| 2026-04-14 | Quota notification debounce now only marks `_quota_window_end` after a successful Telegram send, so transient send failures don't silence an entire retry window. |
| 2026-04-14 | Added `_sweep_deferred`: poll_cycle now resumes DEFERRED workspaces whose `retry_at` has elapsed (transitioning to `previous_state`), and clears the in-memory `_quota_window_end` debounce once its window has passed. Emits `deferred_resumed` events. |
| 2026-04-15 | Added `stage_verifier` module: mechanical post-stage verification captures git HEAD before each verifiable stage and transitions workspace to BLOCKED if HEAD is unchanged after the agent finishes. Prevents silent commit failures (e.g. missing git identity) from going undetected. |
| 2026-04-15 | Extended `stage_verifier` with verifiers for `scope_check` (checks scope-guard-agent-output.md), `qa` (checks qa-agent-output.md), `push` (git ls-remote confirms branch pushed), and `pr_review` (workspace state has pr_number). |
| 2026-04-15 | Wired `stage_verifier` into `_handle_agent_stage`: captures git HEAD before each stage, calls `verify` after successful agent run, transitions to BLOCKED and emits `stage_verification_failed` on failure. |
| 2026-04-16 | Added hot-reload: `config_dir` and `on_project_added` kwargs; `set_tracker`, `rescan_projects`, `_rescan_projects_from_disk` methods; `poll_cycle` calls `_rescan_projects_from_disk` at start so wizard-created projects become live without restart. |
| 2026-04-16 | `_rescan_projects_from_disk` also swallows non-ConfigError exceptions (e.g. PermissionError, OSError) so poll_cycle is not interrupted by unexpected config-dir IO issues; logs at ERROR with stack trace. |
| 2026-04-17 | Wired `stage_verifier` into `_handle_action_stage`: action stages (`push`, `pr_review`, `finalize`) now follow the same capture → execute → verify → transition → emit flow as agent stages. Action methods return `ActionResult` instead of transitioning state internally. Fixes regression where push/pr_review bypassed verification (ACME-14595). |
| 2026-04-21 | PR review comment resolution: VCS `resolve_comment` via GitHub GraphQL, comment classifier with extreme skepticism, auto-fix/reject/escalate flow, TG integration for ambiguous comments, resolution report, review cycle loop. |


















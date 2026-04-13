# Feature: Workspace Isolation

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Each ticket gets a fully isolated workspace via a fresh `git clone`. No ticket shares a filesystem with another. Each workspace maintains a `state.json` tracking current agent, iterations, status, and human input. The orchestrator can resume any workspace from `state.json` after a restart.

## Requirements

- FR1: Each ticket gets a fully isolated workspace (fresh `git clone` into `/workspaces/{project}/{repo}/{ticket}_{timestamp}/`)
- FR2: Each workspace has `context/` and `logs/` subdirectories
- FR3: Shallow clone depth configurable via `git.depth` (0 = full clone)
- FR4: `state.json` created on init tracking: ticket_id, project_id, repo_id, workspace_root, branch, pr_number, current_stage, stage_iterations, human_input_pending, manual_control_started_at, manual_control_comment, started_at, last_updated_at, status
- FR5: State machine transitions: pending → running → waiting_for_human → running → completed/failed
- FR6: Invalid transitions raise errors
- FR7: State writes are atomic (temp file + rename)
- FR8: On startup, WorkspaceManager discovers existing workspaces and resumes active ones
- FR9: Completed/failed workspaces older than `workspaces.max_age_days` are auto-cleaned

## Technical Approach

- `WorkspaceManager` class handles create, discover, resume, cleanup
- Directory-based isolation: each workspace is a self-contained directory with repo clone, context, logs, and state
- No git worktrees — full directory clones avoid git lock conflicts under concurrent writes
- State machine implemented as explicit transition table with validation
- Atomic state writes: write to `state.json.tmp`, then `os.rename()` to `state.json`
- Cleanup runs periodically on each poll cycle

## Dependencies

- Git CLI (subprocess) for clone operations
- Configuration Cascade for workspace settings (base_dir, max_age_days, clone depth)
- Orchestrator invokes WorkspaceManager

## Acceptance Criteria

- [ ] Workspace creation produces correct directory structure with clone, context/, logs/
- [ ] state.json tracks all required fields and updates atomically
- [ ] All valid state transitions work; invalid transitions raise errors
- [ ] Startup discovers and resumes active workspaces
- [ ] Old completed/failed workspaces are cleaned up
- [ ] Clone failure cleans up partial workspace

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |

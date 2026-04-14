# Feature: Dashboard & Event Log

**Status:** Implemented
**Created:** 2026-04-11
**Updated:** 2026-04-12
**Author:** Oleksandr Brazhenko

## Description

Local web dashboard providing real-time visibility into the Sickle pipeline. Shows per-project ticket history, agent activity, state transitions, Telegram messages, and a global event log. Runs as an embedded web server in the daemon process.

## Requirements

- FR1: Structured event log capturing all pipeline activity
- FR2: SQLite persistence for event history
- FR3: Web dashboard with project list, ticket drill-down, global event log
- FR4: Auto-refreshing UI with event type filtering
- FR5: Configurable via global.yaml (host, port, db path, enable/disable)
- FR6: Workspace board view showing tickets grouped by project with state badges
- FR7: Ticket detail view with pipeline progress, workspace info, and agent report viewer
- FR8: Workspace API serving state.json data and report/meta/log files from disk
- FR9: Action endpoints for workspace control (approve, reject, retry, take-control, release-control)
- FR10: Daemon mode and status endpoints
- FR11: Take Control feature: pause pipeline, open Claude Code session, release back
- FR12: Frontend split into ES modules (no build step)
- FR13: External links on ticket detail (Jira, repo, PR — extensible to CI/CD)
- FR14: URL hash routing so refresh preserves view (board/ticket/eventlog)
- FR15: Pipeline bar highlights `previous_state` for off-pipeline states (BLOCKED, FAILED, MANUAL_CONTROL, AWAITING_APPROVAL)

## Technical Approach

- EventBus (in-memory) with listener pattern for async SQLite persistence
- Starlette embedded web server sharing the daemon's asyncio loop
- Multi-file vanilla JS frontend (ES modules), CSS extracted, no build step
- REST API: /api/events, /api/projects, /api/projects/{id}/tickets, /api/tickets/{id}/events, /api/workspaces, /api/workspaces/{id}/report/{file}
- Action API: /api/workspaces/{id}/approve|reject|retry|take-control|release-control, /api/daemon/mode, /api/daemon/status
- Three UI views: Board (ticket cards by project), Ticket Detail (progress + reports), Event Log

## Dependencies

- starlette, uvicorn, aiosqlite (added to pyproject.toml)

## Acceptance Criteria

- [x] Events emitted from orchestrator, agent runtime, Telegram adapters
- [x] Events persisted to SQLite
- [x] Dashboard accessible at configured host:port
- [x] Project list shows all active projects
- [x] Clicking a project shows its tickets and events
- [x] Clicking a ticket shows its full event timeline
- [x] Auto-refresh updates the view every 5 seconds
- [x] Event type filter works
- [x] All existing tests still pass
- [x] Dashboard config in global.yaml
- [x] Workspace board shows all tickets grouped by project
- [x] Ticket detail shows pipeline progress bar and workspace info
- [x] Agent reports viewable from ticket detail page
- [x] Workspace API scans state.json files from disk
- [x] Action endpoints: approve, reject, retry, take-control, release-control
- [x] Daemon mode switch and status endpoints
- [x] Action routes wired via orchestrator/mode_handler passed to create_app
- [x] MANUAL_CONTROL state added to workspace state machine
- [x] Orchestrator skips MANUAL_CONTROL workspaces
- [x] Agent cancellation works when taking control
- [x] Frontend modularized into separate JS files
- [x] Sidebar project list derived from workspaces (not events)
- [x] Take Control launches terminal with Claude Code command
- [x] Release Control transitions back to ANALYSIS
- [x] Ticket detail shows Jira/repo/PR links derived from project config
- [x] URL hash routing preserves current view across page refresh
- [x] Off-pipeline states render correctly on the pipeline bar via `previous_state`

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-11 | Initial implementation |
| 2026-04-12 | Add operations dashboard: workspace board, ticket detail, report viewer |
| 2026-04-12 | Add action endpoints: approve, reject, retry, take-control, release, mode, status |
| 2026-04-12 | V2: Operations dashboard with actions, take-control, modular frontend |
| 2026-04-14 | Take Control: launcher script (no shell-quoting), atomic transition with timestamp |
| 2026-04-14 | Pipeline bar handles off-pipeline states by falling back to `previous_state` |
| 2026-04-14 | URL hash routing for board/ticket/eventlog so refresh stays on the same view |
| 2026-04-14 | External links (Jira, repo, PR) on ticket detail; threaded `projects` through `create_app` |

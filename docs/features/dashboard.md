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

## Technical Approach

- EventBus (in-memory) with listener pattern for async SQLite persistence
- Starlette embedded web server sharing the daemon's asyncio loop
- Single HTML file with inline CSS/JS, no build step
- REST API: /api/events, /api/projects, /api/projects/{id}/tickets, /api/tickets/{id}/events, /api/workspaces, /api/workspaces/{id}/report/{file}
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

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-11 | Initial implementation |
| 2026-04-12 | Add operations dashboard: workspace board, ticket detail, report viewer |

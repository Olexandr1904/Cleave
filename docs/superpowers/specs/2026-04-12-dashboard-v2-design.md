# Dashboard V2 — Operations Dashboard with Take Control

## Goal

Rewrite the Sickle web dashboard from a broken event log viewer into a fully functional operations dashboard where the operator can monitor all tickets across projects, drill into any ticket to read agent reports, and take actions (approve, reject, retry, take control) directly from the browser.

## Architecture

Multi-file vanilla JS frontend served by the existing Starlette backend. No framework, no build step. The current single `index.html` is split into focused modules. New REST action endpoints allow the dashboard to modify pipeline state. A new `MANUAL_CONTROL` workspace state enables the "Take Control" feature where the operator pauses the pipeline, works in a Claude Code terminal session with full context, then hands control back.

## Tech Stack

- Frontend: vanilla JS (ES modules), CSS, HTML — no framework, no build
- Backend: Starlette (existing), new action endpoint handlers
- State: workspace `state.json` files on disk (existing pattern)
- Events: SQLite event store (existing)

---

## 1. Overall Layout

The dashboard uses a sidebar + main content layout:

**Sidebar (fixed left, 240px):**
- Sickle logo/title
- Navigation: Board, Event Log
- Project list — derived from `/api/workspaces` response (not from event DB, fixing the current "no projects" bug)
- Daemon status: mode (auto/manual), uptime, last poll time

**Main area:**
- Toolbar: view title, summary stats ("4 active, 2 blocked, 1 awaiting"), refresh button, auto-refresh toggle
- Content: renders the active view (Board, Event Log, or Ticket Detail)

**Routing:** Client-side state machine. Three views:
- `board` — default, shows all ticket cards grouped by project
- `detail` — drill-down into one ticket
- `eventlog` — chronological event stream

Clicking a project in the sidebar filters the board to that project. Clicking a ticket card navigates to the detail view.

## 2. Board View

Ticket cards grouped by project. Each card shows:
- Ticket ID (bold)
- State badge (colored: DEV=purple, BLOCKED=red pulsing, AWAITING=yellow pulsing, DONE=green dimmed)
- Branch name
- Time ago (started/updated)
- Error message preview (if any, in red)
- PR link (if any)
- Iteration count on current stage

**Card behavior by state:**
- AWAITING_APPROVAL: inline Approve button on the card
- BLOCKED: error/escalation message shown, red border pulse
- DONE/ARCHIVED: card is dimmed (opacity), toggle to hide
- MANUAL_CONTROL: purple badge, shows "You have control"

**Summary stats in toolbar:** count of active, blocked, awaiting approval tickets.

**Sort order:** BLOCKED first, then AWAITING_APPROVAL, then active states by stage, then DONE/ARCHIVED last.

## 3. Ticket Detail View

Displayed when clicking a ticket card. Contains the following sections top-to-bottom:

### 3.1 Header
- Back button (returns to board, filtered to this ticket's project)
- Ticket ID (large)
- State badge
- Time info: "Started X ago, Updated Y ago"

### 3.2 Action Bar
Contextual action buttons:
- **Approve** — visible when state is AWAITING_APPROVAL
- **Reject** — visible when state is AWAITING_APPROVAL
- **Retry** — visible when state is BLOCKED or FAILED
- **Take Control** — visible for any active state (not DONE/ARCHIVED/MANUAL_CONTROL)
- External links: Jira ticket, PR (when available)

When state is MANUAL_CONTROL, the action bar is replaced by the manual control banner (see section 5).

### 3.3 Pipeline Progress Bar
Visual dot chain showing pipeline stages: NEW → ANALYSIS → DEV → SCOPE_CHECK → QA → PUSHED → PR_REVIEW → DONE.
- Completed stages: green with checkmark
- Current stage: blue with glow, shows iteration count
- Failed stage: red with exclamation
- MANUAL_CONTROL: purple lightning bolt at the position where control was taken
- Future stages: grey, dimmed

### 3.4 Info Grid + Error Panel
Two-column layout:
- **Left: Info grid** — branch, repo, project, started at, last updated, iterations, PR link
- **Right: Error/escalation panel** (only visible when `error` is set) — error message, escalation time

### 3.5 Agent Reports & Files
Tabbed interface showing files from the workspace:
- `reports/` folder: analysis_report.md, dev_plan.md, scope_check.md, qa_report.md, etc.
- `meta/` folder: ticket.json, etc.
- `logs/` folder: agent execution logs

Tab bar lists all available files. Clicking a tab loads the file content via `/api/workspaces/{id}/report/{file}?folder=reports|meta|logs`. Content displayed as preformatted monospace text.

### 3.6 Event Timeline
Chronological event log filtered to this ticket. Shows last 5 events by default, expandable to full history ("Show all (N events)"). Each event row: timestamp, event type badge (colored), message, metadata (agent, duration, tokens).

## 4. Event Log View

Full event stream, same as current implementation but working correctly:
- Newest-first chronological list
- Filter by event type dropdown
- Events show: timestamp, type badge, message, project/ticket/agent metadata
- Auto-refreshes every 5s

## 5. Take Control Feature

### 5.1 Taking Control

When the operator clicks "Take Control" on a ticket:

1. **Check for running agent:** Dashboard calls `POST /api/workspaces/{id}/take-control`.
2. **If agent is running:** Backend returns `{"status": "agent_running", "agent": "dev-agent", "started_ago": "2m"}`. Dashboard shows confirmation dialog: "dev-agent is currently running (started 2m ago). Taking control will stop this agent. Continue?"
3. **On confirm (or if no agent running):** Backend kills agent subprocess if any, transitions workspace state to `MANUAL_CONTROL`, saves `manual_control_started_at` and `previous_state`, emits `manual_control_started` event.
4. **Terminal launch:** Backend spawns a terminal window with a generated `claude` CLI command. The command opens Claude Code in the workspace source directory with a system prompt containing:
   - Ticket ID and current context
   - Previous pipeline state and iteration history
   - Error/escalation message if any
   - Paths to all available reports and meta files
   - The operator's manual control comment intent
5. **Dashboard updates:** Shows MANUAL_CONTROL banner with "Finished" button and comment field.

Terminal command is configurable via `global.yaml` (`dashboard.terminal_command`, default: `gnome-terminal -- bash -c`).

### 5.2 While Under Manual Control

- Workspace state is `MANUAL_CONTROL` — orchestrator skips it entirely in the main loop
- Dashboard shows purple MANUAL_CONTROL banner at top of detail view
- Pipeline progress bar shows purple lightning bolt at the takeover position
- Board card shows purple badge
- All reports, info, timeline remain visible and browsable

### 5.3 Releasing Control (Finished)

When the operator clicks "Finished":

1. Optional comment entered in text field
2. `POST /api/workspaces/{id}/release-control` with body `{"comment": "..."}`
3. Backend: saves comment to state, transitions to ANALYSIS, emits `manual_control_released` event
4. Pipeline re-evaluates: the analysis agent sees all existing reports, code changes, and the operator's comment. It determines the actual current stage and fast-tracks through completed stages.
5. No data is deleted — all reports, code, history preserved.

### 5.4 State Machine Changes

Add `MANUAL_CONTROL` to the workspace state machine:
- **Transitions in:** from any active state (ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW, BLOCKED, AWAITING_APPROVAL)
- **Transitions out:** to ANALYSIS only (pipeline re-evaluates)
- New WorkspaceState fields: `manual_control_started_at: str | None`, `manual_control_comment: str | None`

## 6. Action Endpoints

All action endpoints require the workspace to be in the appropriate state. Return 400 with error message if precondition not met.

### POST /api/workspaces/{ticket_id}/approve
- Precondition: state == AWAITING_APPROVAL
- Action: same as Telegram approve — resolve next state, transition
- Response: `{"status": "ok", "new_state": "DEV"}`

### POST /api/workspaces/{ticket_id}/reject
- Precondition: state == AWAITING_APPROVAL
- Action: transition back to previous_state for re-work (e.g., ANALYSIS→DEV gate: goes back to ANALYSIS)
- Response: `{"status": "ok", "new_state": "ANALYSIS"}`

### POST /api/workspaces/{ticket_id}/retry
- Precondition: state == BLOCKED or FAILED
- Action: clear error, clear human_input_pending, transition to previous_state
- Response: `{"status": "ok", "new_state": "DEV"}`

### POST /api/workspaces/{ticket_id}/take-control
- Precondition: state is any active state (not DONE, ARCHIVED, MANUAL_CONTROL)
- Two-step flow:
  1. First call (no body or `{"confirm": false}`): checks agent status. If agent running, returns `{"status": "agent_running", "agent": "dev-agent", "started_ago": "2m"}` — dashboard shows confirmation dialog
  2. Second call (`{"confirm": true}`): kills agent if running, transitions to MANUAL_CONTROL, launches terminal
- Response (success): `{"status": "ok", "command": "cd /path/to/source && claude -p '...'"}`
- Terminal process spawned server-side via configured `dashboard.terminal_command`

### POST /api/workspaces/{ticket_id}/release-control
- Precondition: state == MANUAL_CONTROL
- Body: `{"comment": "optional text"}`
- Action: save comment, transition to ANALYSIS, emit event
- Response: `{"status": "ok", "new_state": "ANALYSIS"}`

### POST /api/daemon/mode
- Body: `{"mode": "auto" | "manual"}`
- Action: call mode_handler.set_mode()
- Response: `{"status": "ok", "mode": "manual"}`

### GET /api/daemon/status
- Response: `{"mode": "manual", "uptime_seconds": 8040, "last_poll_ago_seconds": 12, "active": 4, "blocked": 2, "awaiting": 1}`

## 7. Frontend File Structure

```
dashboard/
├── static/
│   ├── index.html          # shell: sidebar, toolbar, content div, script imports
│   ├── style.css           # all styles (extracted from current inline)
│   └── js/
│       ├── app.js           # state management, routing, auto-refresh, init
│       ├── api.js           # all fetch calls (GET and POST)
│       ├── board.js         # renderBoard(), renderCard()
│       ├── detail.js        # renderDetail(), pipeline bar, info grid
│       ├── actions.js       # action button handlers, confirmation dialogs, take-control flow
│       ├── reports.js       # report tab loading and display
│       ├── events.js        # event log rendering, filtering
│       └── helpers.js       # esc(), fmtTs(), timeAgo(), stateBadgeHtml()
├── web.py                   # Starlette app, GET routes, static mount (existing, modified)
├── actions.py               # NEW: POST action handlers
├── events.py                # EventBus (existing, unchanged)
└── event_store.py           # SQLite store (existing, unchanged)
```

## 8. Backend Changes Summary

| File | Change |
|------|--------|
| `dashboard/web.py` | Mount `/static` directory, add daemon status GET route, register action POST routes from `actions.py`, derive project list from workspaces |
| `dashboard/actions.py` | NEW: all POST action handlers (approve, reject, retry, take-control, release-control, mode) |
| `workspace/workspace.py` | Add `MANUAL_CONTROL` to `VALID_STATES` and `VALID_TRANSITIONS`, add `manual_control_started_at` and `manual_control_comment` fields to `WorkspaceState` |
| `orchestrator/orchestrator.py` | Skip `MANUAL_CONTROL` workspaces in main loop, expose `cancel_agent(workspace_id)` |
| `orchestrator/agent_runtime.py` | Add `cancel(workspace_id)` to kill running subprocess, track active processes by workspace ID |
| `config/schemas.py` | Add `terminal_command` field to `DashboardConfig` |
| `main.py` | Pass orchestrator + mode_handler references to dashboard `create_app()` for action endpoints |

## 9. What Stays the Same

- EventBus + EventStore — unchanged
- 5s auto-refresh polling — simple, works at this scale (1-50 tickets)
- Dark theme, GitHub-inspired visual language
- All existing GET endpoints preserved
- Telegram bot — continues to work in parallel, same underlying handlers
- Workspace state.json pattern — actions use the same transition/save_state methods as Telegram commands

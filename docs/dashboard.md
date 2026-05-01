# Dashboard Reference

Tour of the Sickle web dashboard: every view, every button, what each one does.

This doc is a user-facing reference. For the implementation/spec, see [docs/features/dashboard.md](features/dashboard.md).

---

## Starting and reaching it

The dashboard runs in-process with the daemon — when you start Sickle (`./run.sh` or `python main.py --config config-live`), the dashboard comes up at the same time.

- **URL:** [http://localhost:8080](http://localhost:8080) (defaults; the daemon binds to `dashboard.host` `0.0.0.0` and listens on `dashboard.port` `8080`)
- **Configured at:** `global.yaml` → `dashboard.host`, `dashboard.port`
- **Disable:** `dashboard.enabled: false` in `global.yaml`
- **Database:** `dashboard.db_path` (default `data/events.db`) — SQLite event store
- **Auto-refresh:** every 5 s, toggle off via the **Auto (5s)** checkbox
- **URL hash routing:** the active view is saved in the URL hash, so refreshing keeps you on the same page

If `projects` is empty when the daemon starts, the dashboard still comes up so you can create your first project from the wizard.

---

## Sidebar

- **Board** / **Event Log** / **Settings** — top-level navigation
- **Projects** — auto-derived from workspaces and configured projects; click one to filter the Board to that project
- **Daemon** — live status block: mode (auto/manual), uptime, current global model

---

## Toolbar

- **View title** — Board / Ticket detail / Event Log / Settings
- **Stats** — count of active vs done workspaces (Board view)
- **Mode indicator** — `AUTO` / `MANUAL`, click-through to toggle
- **Hide done** — checkbox to remove DONE/ARCHIVED workspaces from the Board
- **+ New Project** — opens the project-creation wizard (Atlas)
- **Refresh** — manual reload
- **Auto (5s)** — toggle auto-refresh

In the Event Log view, an additional event-type filter dropdown appears.

---

## Board view

A grid of workspace cards, grouped by project. Cards are sorted by state — active states (BLOCKED, AWAITING_APPROVAL, DEV, ANALYSIS, SCOPE_CHECK, QA, PR_REVIEW, PUSHED) at the top, then NEW, DONE, FAILED, ARCHIVED.

Each card shows:

- Ticket id, title (read from `meta/ticket.md`), state badge
- Branch name, PR number (if pushed), iteration counts (e.g. `dev:2 qa:1`)
- Model pill (`opus` / `sonnet` / `haiku`) — see [docs/labels.md](labels.md)
- External links: Jira, Repo, PR
- Action buttons (contextual, see below)

### Card action buttons

Buttons only appear when relevant for the workspace's current state:

| Button | When it appears | What it does |
|---|---|---|
| **Approve** / **Reject** | AWAITING_APPROVAL | Resolve the gate (or stop the workspace) |
| **Retry** | FAILED, DEFERRED | Re-enter at `previous_state` |
| **Rerun** | DONE | Show reason dialog → re-clone source, refresh ticket data, restart from ANALYSIS |
| **🧹** | FAILED with AAPT2 corruption signature | Wipe `<gradle_home>/caches/*/transforms` and retry |
| **Take Control** | Any active state except already-MANUAL_CONTROL | Pause workspace, hand to a human Claude session |
| **Release** | MANUAL_CONTROL | Re-enter pipeline at ANALYSIS |

### Project Health strip

Above the project's cards, a coloured strip shows the result of [the per-project health check](features/dashboard.md#fr16):

- **Green** — every check passed
- **Yellow** — git identity / git remote check failing (degraded but not blocking)
- **Red** — Jira or VCS reachability check failing; click to expand and see fix hints

The active checks for each project are Jira (URL/token/project key) and the configured VCS (GitHub or GitLab). The git-identity and git-remote validators ship in `health/validators.py` but are not currently wired into the dashboard health aggregator — so red is the practical signal today. A manual **refresh** button on the strip bypasses the 60 s cache.

---

## Ticket Detail view

Click any card on the Board to open this view.

- **Pipeline progress bar** — shows the current stage on the workflow line. Off-pipeline states (BLOCKED, FAILED, MANUAL_CONTROL, AWAITING_APPROVAL) highlight the `previous_state` so you can see where the ticket was.
- **External links** — Jira ticket, repo root, PR (extensible to CI/CD links via project config)
- **Workspace info** — branch, started_at, last_updated_at, error message if any
- **Iteration counts** — per stage (`dev`, `qa`, `scope_check`, `pr_review`, `fix`)
- **Reports viewer** — every Markdown report under `<workspace>/source/reports/` is browsable inline (BA plan, scope-guard report, QA output, PR-comment resolution, …)
- **Meta files** — `<workspace>/meta/` contents (ticket.md, plan.md, etc.)
- **Logs** — per-stage logs under `<workspace>/logs/`

The same action buttons available on the Board card appear in the toolbar of this view.

---

## Event Log view

Every event the orchestrator, agent runtime, and Telegram adapter emit, persisted to SQLite (`data/events.db`).

- **Filter by type** dropdown: `agent_dispatched`, `agent_completed`, `agent_failed`, `stage_transition`, `workspace_created`, `pr_created`, `dashboard_approve`, `dashboard_reject`, `dashboard_retry`, `dashboard_rerun`, `manual_control_started`, `manual_control_released`, `tg_message_received`, `tg_message_sent`, `poll_cycle`, `daemon_started`
- **Project filter** — clicking a project in the sidebar narrows the log
- **Ticket filter** — opens automatically when you arrive from a Ticket Detail view

Useful when something looks wrong and you want to see exactly what the daemon did, in order.

---

## Settings view

Currently a single setting:

- **Claude model picker** — `Haiku` / `Sonnet` / `Opus`. The choice is the global default model, applied to new workspaces unless a `model-*` Jira label overrides it. Changes are stored in dashboard SQLite (`settings` table) and hot-reloaded by adapters on the next agent dispatch — no daemon restart required. In-flight workspaces keep the model that was current when they were created.

See [docs/labels.md](labels.md) for per-ticket overrides.

---

## Project Health (cross-cutting)

Per-project validators that run on a cache (warm-loaded at daemon startup, refreshable from the Board, 60 s TTL):

| Check | What it verifies | Fix hint shown on failure |
|---|---|---|
| `jira` | Atlassian API reachable + project key valid | URL/token, project key |
| `github` (or `gitlab`) | Repo reachable + token has scope | Token, owner/repo |

The `git_identity` and `git_remote` validators live in `health/validators.py` and contribute to the yellow/red aggregation logic, but `health/runner.check_project` does not currently call them — so they don't appear on the strip until they're wired in.

Stage verifiers also run after each agent stage to catch silent failures (e.g. dev-agent ran but never committed) — these don't show up in the health strip but transition the workspace to BLOCKED with a real reason instead of silently advancing.

---

## Take Control

A "let me drive this one" escape hatch.

1. From a Ticket Detail view (or its Board card), click **Take Control**.
2. The workspace transitions to MANUAL_CONTROL. The orchestrator skips it on subsequent poll cycles. Any agent currently running on it is cancelled.
3. The dashboard shows a `claude` CLI command. Paste it into a terminal — it opens an interactive Claude Code session against the workspace's source tree.
4. Work on the ticket directly. Commit, push, edit the workflow, whatever you need.
5. When done, click **Release**. The workspace re-enters at ANALYSIS so the pipeline can pick up your changes from a clean state.

Configurable terminal command via `global.yaml` → `dashboard.terminal_command` (default `gnome-terminal -- bash -c`).

---

## + New Project wizard

Click **+ New Project** in the toolbar. The wizard runs the [Atlas project-setup-agent](features/project-setup-agent.md) and walks you through five steps (defined in `dashboard/static/js/project-wizard.js`):

1. **Project** — id, display name
2. **Jira** — URL, email, project key, trigger labels (defaults to `[ai-pipeline]`), ignore labels, status name mapping; validates against the live Jira API before continuing
3. **Repository** — VCS provider, owner/repo, default branch, branch prefix, merge method, clone URL; validates against the GitHub/GitLab API
4. **Notifications** — per-project Telegram chat id (optional; falls back to the global default)
5. **Review** — Atlas writes `config-live/projects/<id>/{project,repos/*}.yaml`

The orchestrator hot-reloads the new project: registers the GitHub adapter, attaches the Jira tracker if it wasn't already attached, extends the Telegram allowlist, and emits `project_loaded` events. No daemon restart.

If validation fails on any step, the wizard stops and tells you what to fix — bad credentials, missing scope, unreachable hosts.

---

## REST API

The dashboard frontend is a thin client over a JSON API. Useful if you want to script against it. Routes are registered in `dashboard/web.py` (read endpoints) and `dashboard/actions.py` (write endpoints) — that's the source of truth; the list below is the headline shape.

Read endpoints (`GET`):

- `/api/health` — health probe + event count
- `/api/events` — recent events with optional `project_id` / `ticket_id` filters
- `/api/projects` — projects with workspace counts
- `/api/projects/{id}/tickets` — ticket history per project
- `/api/projects/health` — health check results (60 s cache; `?refresh=1` bypasses)
- `/api/workspaces` — every workspace from disk, with state.json + report/meta listings
- `/api/workspaces/{ticket_id}/report/{filename}?folder=reports|meta|logs` — fetch one file
- `/api/settings/model` — global default model (also `PUT` to change)
- `/api/daemon/status` — current mode + uptime

Write endpoints (`POST`):

- `/api/daemon/mode` — switch auto/manual
- `/api/workspaces/{id}/approve|reject|retry` — resolve gates and re-run
- `/api/workspaces/{id}/take-control|release-control` — hand a workspace to a human and back
- `/api/workspaces/{id}/pause|unpause|resume|archive|delete|clean` — workspace lifecycle
- `/api/workspaces/{id}/rerun` — DONE-only: re-clone source, refresh ticket data from tracker, restart pipeline from ANALYSIS; body `{ "reason": "<non-empty string>" }`
- `/api/workspaces/{id}/clear-gradle-and-retry` — server-side validates the workspace is FAILED and the error matches the AAPT2 signature before wiping caches
- `/api/projects/create` and `/api/projects/validate-step` — the wizard's two endpoints (validate one step against live APIs, then commit)

---

## See also

- [docs/features/dashboard.md](features/dashboard.md) — implementation spec, change log, FRs
- [docs/labels.md](labels.md) — per-ticket model selection via Jira label
- [docs/telegram.md](telegram.md) — equivalent operations from Telegram (most dashboard buttons have a Telegram counterpart)
- [docs/troubleshooting.md](troubleshooting.md) — when the dashboard isn't reachable or shows red health

# Dashboard Reference

Tour of the Cleave web dashboard: every view, every button, what each one does.

This doc is a user-facing reference. For the implementation/spec, see [docs/features/dashboard.md](features/dashboard.md).

---

## Starting and reaching it

The dashboard runs in-process with the daemon — when you start Cleave (`./run.sh` or `python main.py --config config-live`), the dashboard comes up at the same time.

- **URL:** [http://localhost:8080](http://localhost:8080) (defaults; the daemon binds to `dashboard.host` `0.0.0.0` and listens on `dashboard.port` `8080`)
- **Configured at:** `global.yaml` → `dashboard.host`, `dashboard.port`
- **Disable:** `dashboard.enabled: false` in `global.yaml`
- **Database:** `dashboard.db_path` (default `data/events.db`) — SQLite event store
- **Auto-refresh:** every 5 s, toggle off via the **Auto (5s)** checkbox
- **URL hash routing:** the active view is saved in the URL hash, so refreshing keeps you on the same page

If `projects` is empty when the daemon starts, the dashboard still comes up so you can create your first project from the wizard.

---

## Sidebar

- **Board** / **Reviews** / **Event Log** / **Settings** — top-level navigation
- **Projects** — auto-derived from workspaces and configured projects; click one to filter the Board to that project
- **Daemon** — live status block: mode (auto/manual), uptime, current global model

---

## Toolbar

- **View title** — Board / Reviews / Ticket detail / Event Log / Settings
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

### Tickets switch panel

The Board has a **Tickets** switch panel: one global pill (`Tickets: running ▶` or `Tickets: paused ⏸`) plus one pill per repo, each `running` or `paused`. Click a pill to flip it. While a switch is paused, the ticket pipeline picks up no new tickets for that scope **and** stops advancing every existing workspace in that scope. An agent stage that is already running finishes, then the workspace waits. Approve and retry are still accepted, but nothing moves until tickets run again.

When the global switch is paused, every repo is paused with it. A repo pill whose own switch is still running is then drawn dimmed with a dashed border, and hovering it shows the tooltip `Paused globally — start Tickets first`. There is no banner. Those pills cannot be clicked until the global switch is running again.

The switches are stored in `~/CleaveData/reviewer/reviewer.db` and survive daemon restarts. **First start after upgrading:** the daemon creates that database with **tickets paused** and reviews running, so the ticket pipeline picks up nothing until you click the `Tickets: paused ⏸` pill to switch it to running, or send `tickets start` in Telegram (see [docs/telegram.md](telegram.md#switch-commands)).

### Credential strip

Above the cards, a strip shows one chip per credential that needs attention. The credentials checked are the forge and tracker tokens (per repo / project), the Telegram bot token, and the `claude` login on the host. Each chip shows one of these states:

- **invalid** (red, chip text `invalid or expired`) — the credential was rejected or has expired
- **no_access** (red, chip text `no access`) — the credential is valid but cannot reach the repo or project
- **expiring** (yellow, chip text `expires <local time>`) — the `claude` login expires within 3 days
- **unreachable** (grey, chip text `unreachable`) — the service could not be reached (or `claude` is not on the daemon's PATH), so the credential could not be judged

Click a chip to see the env var name (never the value), `since <local time>` (when it entered this state), `checked <local time>` (when it was last checked) and the fix, e.g. `Create a new token, put it in .env as GITHUB_TOKEN, restart the daemon.` The strip is hidden when every credential is ok.

Credentials are re-checked in the background every 15 minutes, and immediately after any adapter gets a 401/403. Telegram gets a line when a credential changes state, and again 3 days and 1 day before the `claude` login expires (nothing is sent about the Telegram token itself):

- `⚠️ <label>: invalid or expired. <fix>`
- `⚠️ <label>: no access (HTTP 403). <fix>`
- `✅ <label>: working again.`
- `⏳ The claude login on the host expires 2026-11-24 12:00. Log in again on the host in a GUI terminal.`

---

## Reviews view

**Reviews** in the sidebar (`#/reviews`) shows the reviews board of every repo with `reviewer.enabled: true` in its repo YAML: the open PRs that involve you, and whose turn it is on each. With no such repo the view says `No repo has reviews enabled.` It refreshes with the toolbar's **Refresh** and **Auto** like the Board.

Nothing is posted from the dashboard: Telegram is the only posting path. In this version jobs stay `queued`; the review runner comes in a later version.

### Reviews switch panel

The same pill panel as the Board's [Tickets switch panel](#tickets-switch-panel), for reviews: one global pill (`Reviews: running ▶` or `Reviews: paused ⏸`) plus one pill per repo with reviews enabled. While reviews are paused, the board keeps refreshing and the [reviewer lines](telegram.md#reviewer-lines) are still sent, but no job is queued automatically. **Review now** still queues one.

### Repo status

Each repo has a header `<project>/<repo> · <status>`; for every status but `ok`, the line under it gives the reason:

| Status | Meaning |
|---|---|
| `new` | Not polled yet |
| `ok` | The last poll succeeded |
| `doc_missing` | The base branch has no process document (`reviewer.process.doc`). The board refreshes, but no job is queued. The line reads `reviews are off: <doc> not on <base>` |
| `token_failing` | Credential health reports the repo's forge token as failing (see the [credential strip](#credential-strip)). The repo is not read until the token passes again |
| `readiness_failed` | The readiness command failed, timed out or printed something that is not a board snapshot |
| `error` | Updating the review clone or a GitHub read failed |

Each repo is polled every `reviewer.poll_interval_seconds` (default 300, at least 60). A failed poll keeps the last board and backs off: the next try comes after twice the poll interval, the delay doubles after each further failure up to 1 hour (never less than the poll interval), and the first poll that succeeds resets it.

### My turn / Waiting on others

Under each repo header there are two tables:

- **My turn** — PRs where you have a turn (review, re-check, merge, …), except the ticket pipeline's own PRs
- **Waiting on others** — every other PR on the board, the pipeline's PRs included (marked `pipeline`)

Columns: the PR number (links to GitHub), title, ticket key, your roles, the turns (yours in bold, others as `login: action`), the state of the PR's latest job (`queued`, `running`, `done`, `posted`, `failed`, `deferred`, `superseded`, `skipped`, `cancelled`, `stale`; `skipped` also when the PR has no job and you skipped its current head), and when the PR last changed.

A review is queued automatically when your turn is a review, the head has been quiet for `reviewer.quiet_minutes` (default 30), the head is not skipped, reviews are running and the process document is on the base branch. A PR with at least `reviewer.max_files_auto` (default 40) changed files is not reviewed automatically: Telegram gets a line instead.

Row buttons:

| Button | When it appears | What it does |
|---|---|---|
| **Review now** | Every row | Queues a review at the PR's current head, whatever the quiet window, the PR's size, a skip or the reviews switch. Refused with `already queued` when a review at that head is queued or running |
| **Skip** | Your turn is a review and the current head is not skipped yet, or a review is queued at it (**Review now** after a skip) | Skips this head: its queued review is dropped and none is queued for it again. A new push makes a new head, reviewed as usual |

Polls, new turns and job changes are also recorded in the [Event Log](#event-log-view) as `reviewer_poll`, `reviewer_poll_failed`, `reviewer_turn` and `reviewer_job` events (not in its type filter yet).

### Review clone

Each reviewed repo has a Cleave-owned single-branch clone of its base branch at `<reviewer data dir>/repos/<project_id>/<repo_id>` (default `~/CleaveData/reviewer/repos/<project_id>/<repo_id>`), created on the first poll. The forge token reaches git only through the environment of that one git process: it is never written to `.git/config` or the keychain.

Repos added by hot reload (the **+ New Project** wizard) are not on the Reviews view until the daemon restarts.

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

## Credential health (cross-cutting)

Credential checks run in the background every 15 minutes (and at once after an adapter 401/403); the results feed the [credential strip](#credential-strip) on the Board:

| Credential | What it verifies | Fix hint shown on failure |
|---|---|---|
| Forge token (GitHub / GitLab), per repo | Token is accepted and can reach the repo | New token in `.env`, or check access (SSO for classic GitHub tokens) |
| Tracker token (Jira / Trello), per project | Token is accepted and can reach the project | New token in `.env`, or check access |
| Telegram bot token | `getMe` succeeds | New token in `.env` |
| `claude` login on the host | Logged in, and not expiring within 3 days | Log in again on the host; if `claude` is not found, add its directory to the daemon's PATH |

Stage verifiers also run after each agent stage to catch silent failures (e.g. dev-agent ran but never committed) — these don't show up in the credential strip but transition the workspace to BLOCKED with a real reason instead of silently advancing.

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
- `/api/health/credentials` — credential states (`?refresh=1` re-checks now)
- `/api/switches` — running/paused switches (also `PUT` with `{"mode": "tickets"|"reviews", "state": "running"|"paused", "scope": "*"|"<project>/<repo>"}`)
- `/api/reviews` — per repo with reviews enabled: status, reason, poll times and the `my_turn` / `waiting` PRs (the `/api/reviews` routes exist only when at least one repo has reviews enabled)
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
- `/api/reviews/review-now` and `/api/reviews/skip` — body `{"scope": "<project>/<repo>", "number": <PR number>}`; 404 for a PR not on the board, 409 from review-now when a review at that head is already queued or running; 403 `cross-site request refused` unless the body is sent as `Content-Type: application/json` and any `Origin` / `Sec-Fetch-Site` header says the request comes from the dashboard itself (so another site open in the browser cannot queue a review; curl, which sends neither header, works)

---

## See also

- [docs/features/dashboard.md](features/dashboard.md) — implementation spec, change log, FRs
- [docs/labels.md](labels.md) — per-ticket model selection via Jira label
- [docs/telegram.md](telegram.md) — equivalent operations from Telegram (most dashboard buttons have a Telegram counterpart)
- [docs/troubleshooting.md](troubleshooting.md) — when the dashboard isn't reachable or shows red health

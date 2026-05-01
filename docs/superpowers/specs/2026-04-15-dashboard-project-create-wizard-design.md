---
title: Dashboard Project Create Wizard
status: Draft
created: 2026-04-15
author: Oleksandr Brazhenko
---

# Dashboard Project Create Wizard

## Problem

Adding a new project to the Cleave pipeline today requires running the Atlas
project-setup agent from the CLI, answering questions one at a time, and
manually ensuring the required environment variables exist. There is no
discoverable path for a team lead to onboard a new project from the web
dashboard, and nothing bootstraps the runtime workspace tree
(`{base_dir}/{project_id}/{repo_id}/tickets/`) that tickets land in.

## Goal

Let a user create a new project from the dashboard through a guided form, then
hand the collected data to the existing Atlas agent, which validates
credentials, writes the `config-live/projects/{id}/` YAML files, bootstraps the
runtime workspace tree matching the shape of existing projects (`acme`), and
reports the result back to the dashboard in real time.

## Non-goals

- Editing or removing existing projects from the dashboard. Only creation.
- Adding a second repo to an existing project. One repo per setup run.
- CI/CD configuration (Jenkins, GitHub Actions). No `ci:` block is written.
  Future work if needed.
- A secrets manager integration. Secrets are stored in the existing `.env`
  file with `0600` permissions. Future hardening is out of scope.
- Authentication for the dashboard. The create endpoint inherits whatever
  bind-address protection the dashboard already uses.

## High-level flow

1. User clicks **+ New Project** on the dashboard header.
2. A modal wizard opens, with six steps: Identity, Jira, VCS, Quality, Extras,
   Review.
3. User fills each step. Raw secrets (Jira token, VCS token, optional Telegram
   bot token) are entered as password inputs. Multi-value Jira trigger labels
   use a chip input.
4. On **Create**, the frontend POSTs the full payload to
   `POST /api/projects/create`.
5. The backend validates the payload, checks for project-id and env-var
   collisions, persists secrets to `.env` and `os.environ`, creates the setup
   workspace tree, and spawns the Atlas agent as a background task. Returns
   `202 Accepted` with the setup workspace path.
6. The frontend switches the modal to a live status panel that polls
   `GET /api/workspaces` and watches the setup `state.json` transition through
   `SETUP_PENDING → VALIDATING → WRITING → SETUP_DONE` (or `SETUP_FAILED`).
7. Atlas resolves env vars, calls `validate_jira` and
   `validate_github`/`validate_gitlab`, writes
   `config-live/projects/{id}/project.yaml` and `repos/{repo_id}.yaml`, writes
   `setup/reports/project-setup-output.md`, and updates `state.json`.
8. On success, the dashboard project list refreshes and the new project
   appears. On failure, the report is rendered inline with an **Edit & retry**
   button that reopens the wizard with all non-secret values restored.

## Secrets handling

### Form inputs

Raw secret fields collected from the user:

- Jira API token
- GitHub or GitLab token
- Telegram bot token (optional — blank inherits global)

No Jenkins fields (CI/CD step removed).

### Env-var naming

Variable names are auto-generated from the project id, upper-cased, with the
project name first:

- `{PROJECT_ID_UPPER}_JIRA_TOKEN`
- `{PROJECT_ID_UPPER}_GITHUB_TOKEN` or `{PROJECT_ID_UPPER}_GITLAB_TOKEN`
- `{PROJECT_ID_UPPER}_TELEGRAM_BOT_TOKEN` (only if provided)

Example for `acme`: `ACME_JIRA_TOKEN`, `ACME_GITHUB_TOKEN`.

### Persistence

On submit, before the Atlas agent runs:

1. Append `VARNAME=value` lines to the repo-root `.env` file (already
   gitignored) using an atomic tempfile + rename. File permissions are set to
   `0600` after write.
2. Set `os.environ[VARNAME] = value` in the running process so Atlas's
   validators can resolve the variables immediately without a restart.

### Redaction

- Raw secret values live only in `.env` and `os.environ`.
- `meta/input.md` (what Atlas reads) contains only the generated var names.
- `state.json`, reports, and logs never contain raw values.
- Request bodies are scrubbed before any logging.
- Generated YAML files use `${VARNAME}` references, never raw values, exactly
  as Atlas writes them today.

### Collisions

- If `config-live/projects/{project_id}/` already exists, the endpoint
  responds `409` with `{"error": "project_exists"}` before any writes.
- If any target env var is already defined in `.env`, the endpoint responds
  `409` with `{"error": "env_var_conflict", "vars": [...]}` before any writes.
- The user fixes the conflict (different project id, or manually removes the
  stale vars) and resubmits.

## Wizard structure

Six steps, rendered in a modal. Each step validates client-side before Next
advances.

### Step 1 — Identity

- `project_id` (slug, validated against existing `PROJECT_ID_PATTERN`)
- `display_name`
- `repo_id` (slug)
- `repo_display_name`

### Step 2 — Jira

- Jira URL
- Project key
- Email
- **Jira API token** (raw secret, password input)
- Trigger labels — multi-value chip input, at least one required (e.g.
  `ai-pipeline`, `acme-mobile`). A ticket must carry all listed labels
  to be picked up.
- Ignore labels (optional, comma-separated)
- Status mappings: `todo`, `in_progress`, `in_review`, `done` with defaults
  pre-filled

### Step 3 — VCS

- Radio: GitHub or GitLab
- **GitHub**: owner, repo, default branch (default `develop`), branch prefix
  (default `feature`), merge method (default `squash`), **GitHub token** (raw
  secret)
- **GitLab**: URL (default `https://gitlab.com`), project id (numeric),
  default branch, branch prefix, **GitLab token** (raw secret)

### Step 4 — Quality gates

- Lint command + hard-gate checkbox (optional)
- Test command + hard-gate (optional)
- Build/check command + hard-gate (optional)

### Step 5 — Extras

- **Telegram bot token** (optional, raw secret) — blank means inherit global
  `${TELEGRAM_BOT_TOKEN}`
- Telegram default chat id (optional)
- Architecture rules file path (optional)
- Protected files (optional, comma-separated)
- Max concurrent tickets (optional — blank means inherit)

### Step 6 — Review & create

- Full summary table of every field
- Tokens masked as `••••` with a hint showing the generated var name
  (e.g. *"will be saved to `ACME_JIRA_TOKEN`"*)
- **Create project** button → POST

Navigation: Back/Next buttons, step progress indicator at top. Wizard state
lives in JS memory only — never persisted to `localStorage` — so secrets
aren't retained across page reloads.

## Backend API

### Endpoint

```
POST /api/projects/create
```

Request body (full form payload, JSON):

```json
{
  "identity": {
    "project_id": "acme",
    "display_name": "Acme Corp",
    "repo_id": "acme-app",
    "repo_display_name": "Acme App"
  },
  "jira": {
    "url": "https://acme.atlassian.net",
    "project_key": "ACME",
    "email": "bot@acme.com",
    "token": "<raw>",
    "trigger_labels": ["ai-pipeline", "acme-app"],
    "ignore_labels": ["wip"],
    "statuses": {
      "todo": "To Do",
      "in_progress": "In Progress",
      "in_review": "In Review",
      "done": "Done"
    }
  },
  "vcs": {
    "provider": "github",
    "github": {
      "owner": "acme",
      "repo": "acme-app",
      "token": "<raw>",
      "default_branch": "develop",
      "branch_prefix": "feature",
      "merge_method": "squash"
    }
  },
  "quality": {
    "lint":  { "command": "npm run lint",  "hard_gate": true },
    "test":  { "command": "npm test",      "hard_gate": true },
    "build": { "command": "npm run build", "hard_gate": true }
  },
  "extras": {
    "telegram_bot_token": null,
    "telegram_chat_id": null,
    "arch_rules_file": "docs/arch-rules.md",
    "protected_files": [".github/", "build.gradle.kts"],
    "max_concurrent_tickets": null
  }
}
```

### Handler flow

1. **Validate payload** against a schema. On failure return `400` with
   field-level errors.
2. **Check collisions**:
   - `config-live/projects/{project_id}/` must not exist → else `409`.
   - Expected env vars must not already be set in `.env` → else `409`.
3. **Persist secrets**: append to `.env` (tempfile + rename, `0600`), set
   `os.environ[VARNAME] = value`.
4. **Create setup workspace**:
   - `{base_dir}/{project_id}/{repo_id}/tickets/` (empty)
   - `{base_dir}/{project_id}/{repo_id}/setup/state.json` (state:
     `SETUP_PENDING`)
   - `{base_dir}/{project_id}/{repo_id}/setup/meta/input.md` (redacted
     payload)
   - `{base_dir}/{project_id}/{repo_id}/setup/logs/`
5. **Spawn Atlas** as a background asyncio task. Atlas reads
   `meta/input.md`, validates credentials against live APIs, writes
   `config-live/projects/{id}/project.yaml` and `repos/{repo_id}.yaml`, writes
   `setup/reports/project-setup-output.md`, transitions `state.json` through
   `VALIDATING → WRITING → SETUP_DONE` (or `SETUP_FAILED`).
6. **Return** `202 Accepted`:
   ```json
   { "workspace": "acme/acme-app/setup", "state": "SETUP_PENDING" }
   ```

### Rollback

On `SETUP_FAILED`, the handler:

- Removes `config-live/projects/{project_id}/` if it was partially written.
- Removes the env var lines appended to `.env` (tempfile + rename).
- Removes the corresponding `os.environ` keys.
- **Keeps** the `setup/` workspace dir so the user can see the failure report.

Writes are staged then renamed atomically: YAMLs go to a tempdir and rename
into place; `.env` goes to `.env.tmp` then renames over `.env`.

### Concurrency

A module-level `asyncio.Lock` ensures only one project-setup run is active at
a time. A second POST during an active run returns `429 Busy` with a pointer
to the running workspace.

### Workspace scanner integration

The existing `_scan_all_workspaces` function in [dashboard/web.py](dashboard/web.py)
uses `rglob("state.json")`, which automatically picks up setup runs. A small
change tags entries whose path contains `/setup/` (not `/tickets/{id}/`) with
`kind: "setup"` so the UI can render them in a dedicated section rather than
mixing them with ticket workspaces.

## Frontend implementation

### Files touched

- [dashboard/static/index.html](dashboard/static/index.html) — add a
  **+ New Project** button in the header and an empty
  `<div id="project-wizard">` modal container.
- [dashboard/static/js/app.js](dashboard/static/js/app.js) — wire the button
  to open the wizard.
- `dashboard/static/js/project-wizard.js` — **new file**, ~400 lines. Wizard
  state machine, step renderers, submit handler, poll loop.
- [dashboard/static/js/api.js](dashboard/static/js/api.js) — add
  `createProject(payload)` helper that POSTs to `/api/projects/create`.
- [dashboard/static/style.css](dashboard/static/style.css) — modal styles,
  step progress bar, chip input, form layout.

### Wizard state

```js
const state = {
  step: 0,
  data: { identity: {}, jira: {}, vcs: {}, quality: {}, extras: {} },
  errors: {},
  running: null,   // {workspace, state} once submit succeeds
};
```

Password inputs have a show/hide toggle; never stored in `localStorage`.
Tokens on the review step are masked as `••••` with the generated var-name
preview.

### Chip input

A simple text input that splits on comma/enter, renders chips below, each
with an x-button to remove. No third-party dependency.

### Live status panel

After `202`, the modal body switches to a status panel that polls
`GET /api/workspaces?project={id}` every 2s. It reads the `setup` entry's
state and renders a three-step progress indicator
(`VALIDATING → WRITING → DONE`).

- On `SETUP_DONE`: success card, auto-close after 3s, trigger dashboard
  project-list refresh.
- On `SETUP_FAILED`: fetch `reports/project-setup-output.md` via the existing
  `/api/workspaces/{ticket_id}/report/{filename}` endpoint (tweaked to accept
  setup paths), render the report inline, offer **Edit & retry** that returns
  the user to Step 1 with all non-secret values restored from `state.data`.

No frameworks added — vanilla JS consistent with the rest of the dashboard.

## Data contracts

### Redacted `meta/input.md`

```md
# Project Setup Input

## Identity
- project_id: acme
- display_name: Acme Corp
- repo_id: acme-app
- repo_display_name: Acme App

## Jira
- url: https://acme.atlassian.net
- project_key: ACME
- email: bot@acme.com
- token_var: ACME_JIRA_TOKEN
- trigger_labels: [ai-pipeline, acme-app]
- ignore_labels: [wip]
- statuses: {todo: "To Do", in_progress: "In Progress", in_review: "In Review", done: "Done"}

## VCS
- provider: github
- owner: acme
- repo: acme-app
- token_var: ACME_GITHUB_TOKEN
- default_branch: develop
- branch_prefix: feature
- merge_method: squash

## Quality
- lint: {command: "npm run lint", hard_gate: true}
- test: {command: "npm test", hard_gate: true}
- build: {command: "npm run build", hard_gate: true}

## Extras
- telegram_bot_token_var: null
- telegram_chat_id: null
- arch_rules_file: docs/arch-rules.md
- protected_files: [.github/, build.gradle.kts]
- max_concurrent_tickets: null
```

### Generated `config-live/projects/acme/project.yaml`

```yaml
project:
  id: "acme"
  name: "Acme Corp"
  enabled: true

jira:
  url: "https://acme.atlassian.net"
  token: "${ACME_JIRA_TOKEN}"
  email: "bot@acme.com"
  project_key: "ACME"
  trigger_labels: ["ai-pipeline", "acme-app"]
  ignore_labels: ["wip"]
  statuses:
    todo: "To Do"
    in_progress: "In Progress"
    in_review: "In Review"
    done: "Done"

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  default_chat_id: null

defaults:
  poll_interval_seconds: 300
  max_iterations:
    scope_guard: 3
    fix: 3
    qa: 2
    dev: 2
  pr_comment_fetch_delay_minutes: 30
```

If a Telegram bot token is provided in the form, `telegram.bot_token` becomes
`"${ACME_TELEGRAM_BOT_TOKEN}"`.

### Generated `config-live/projects/acme/repos/acme-app.yaml`

```yaml
repo:
  id: "acme-app"
  name: "Acme App"
  enabled: true

vcs:
  provider: "github"
  github:
    token: "${ACME_GITHUB_TOKEN}"
    owner: "acme"
    repo: "acme-app"
    default_branch: "develop"
    branch_prefix: "feature"
    merge_method: "squash"

git:
  clone_url: "https://${ACME_GITHUB_TOKEN}@github.com/acme/acme-app.git"
  commit_author_name: "Cleave Bot"
  commit_author_email: "cleave@pipeline.local"
  depth: 1

architecture:
  rules_file: "docs/arch-rules.md"
  protected_files: [".github/", "build.gradle.kts"]

linting:
  run_command: "npm run lint"
  hard_gate: true

testing:
  run_command: "npm test"
  hard_gate: true

build:
  check_command: "npm run build"
  hard_gate: true
```

No `ci:` block. Missing quality gates are omitted rather than written as
empty blocks.

### `.env` additions

```
ACME_JIRA_TOKEN=<raw>
ACME_GITHUB_TOKEN=<raw>
```

### `setup/state.json` lifecycle

```
SETUP_PENDING → VALIDATING → WRITING → SETUP_DONE
                                   ↘ SETUP_FAILED
```

## `trigger_labels` migration

The wizard collects a list of trigger labels. Today the config schema uses a
singular `trigger_label` field. To avoid two-way support, this design
migrates the schema fully:

- Rename `trigger_label: str` to `trigger_labels: list[str]` in
  [config/schemas.py](config/schemas.py).
- Update [integrations/jira/jira_adapter.py](integrations/jira/jira_adapter.py)
  to filter tickets that carry **all** labels in `trigger_labels` (logical
  AND).
- Update [orchestrator/ticket_prioritizer.py](orchestrator/ticket_prioritizer.py)
  accordingly.
- Migrate [config-live/projects/acme/project.yaml](config-live/projects/acme/project.yaml)
  from `trigger_label: "foo"` to `trigger_labels: ["foo"]`.
- Update test fixtures under `tests/fixtures/config/` and
  `config-live.example/`.
- Update unit tests: `tests/unit/test_config_cascade.py`,
  `tests/unit/test_ticket_prioritizer.py`.
- Update integration test: `tests/integration/test_jira_adapter.py`.
- Update docs referencing `trigger_label`:
  [docs/architecture.md](docs/architecture.md),
  [docs/architecture-v2.md](docs/architecture-v2.md),
  [docs/prd.md](docs/prd.md),
  [docs/agent-contracts.md](docs/agent-contracts.md),
  [docs/setup-guide.md](docs/setup-guide.md),
  [docs/features/jira-integration.md](docs/features/jira-integration.md),
  [agents/project-setup-agent.md](agents/project-setup-agent.md),
  [agents/pm-agent.md](agents/pm-agent.md).
- No backward-compat shim: the config loader raises a clear migration error
  if it encounters the old singular field.

## Testing strategy

### Unit tests

- `tests/unit/test_project_create_payload.py` — schema validation: required
  fields, slug patterns, non-empty `trigger_labels`, vcs provider whitelist,
  field-level error shapes.
- `tests/unit/test_env_file_writer.py` — atomic append, `0600` permissions,
  rollback on failure, collision detection, redaction in logs.
- `tests/unit/test_trigger_labels_migration.py` — loader reads the new
  plural field; old singular raises a clear migration error.
- Update `tests/unit/test_config_cascade.py` and
  `tests/unit/test_ticket_prioritizer.py` to use the plural form.

### Integration tests

- `tests/integration/test_project_create_flow.py` — end-to-end with a mocked
  Atlas agent:
  - **Happy path**: POST valid payload → workspace dir created → `.env`
    appended → Atlas spawned → `state.json` transitions through states →
    configs exist under `config-live/projects/{id}/` → response body correct.
  - **Atlas validation failure**: Atlas reports `SETUP_FAILED` → rollback
    verified (configs removed, `.env` entries removed, `os.environ` keys
    removed, setup workspace kept with failure report).
  - **Project collision**: existing `project_id` → `409` before any writes.
  - **Env collision**: existing var in `.env` → `409` before any writes.
  - **Concurrency**: second POST during active run → `429 Busy`.
- Update `tests/integration/test_jira_adapter.py` for the `trigger_labels`
  rename.

### Manual UI checks

- Wizard step navigation, Back/Next preserves state.
- Password show/hide toggle.
- Chip input for trigger labels (comma, enter, backspace, x-button).
- Live status panel polls and updates correctly.
- Failure flow shows report inline and **Edit & retry** restores non-secret
  values (secrets cleared).

### Not testing

- Atlas's internal validation logic — covered by existing
  `tests/unit/test_config_tools.py`.
- Live credential validation against real Jira/GitHub/GitLab — mocked at the
  HTTP layer in `tests/integration/test_project_create_flow.py`.

## Risks and open questions

- **Secrets on disk**: raw tokens are stored in `.env` at repo root. This
  matches current practice (already gitignored, `0600`-chmodded), but any
  user with shell access to the host can read them. Future: consider a
  sops/age-encrypted secrets file or a real secrets manager.
- **`.env` rewrite concurrency**: the atomic tempfile + rename protects
  against partial writes, but concurrent edits from outside this endpoint
  (e.g. a human editing `.env` by hand at the same time) could race. The
  endpoint re-reads `.env` under the module lock just before writing to
  minimize the window.
- **Hot-reloading env in long-running processes**: `os.environ` is set for
  the dashboard process, but other Cleave processes (orchestrator, daemons)
  started from the same `run.sh` will see new `.env` vars only on restart.
  Out of scope — the project configs created here aren't enabled until the
  next full restart anyway, since config discovery happens at startup.
- **Trigger-labels AND semantics**: a ticket must carry **all** labels in
  `trigger_labels` to be picked up (logical AND). This matches the user's
  intent of combining a generic `ai-pipeline` label with a project-specific
  label like `acme-mobile` so that the pipeline can distinguish which
  project a ticket belongs to.

- **Atlas task supervision**: Atlas runs as a background asyncio task spawned
  by the handler. The handler supervises the task: if Atlas raises an
  unhandled exception or the task is cancelled, the handler catches it,
  writes `state.json` to `SETUP_FAILED` with the exception message appended
  to `reports/project-setup-output.md`, and performs the rollback described
  above. Atlas itself is responsible for the normal
  `VALIDATING → WRITING → SETUP_DONE` transitions.

# Cleave in Claude Code: design and implementation plan

**Date:** 2026-11-17. **Status:** the design (Part 1) is waiting for the operator's approval. Part 2 is the plan for the recommended design. Whoever executes it reads both parts.

Sources: `[hooks]` https://code.claude.com/docs/en/hooks, `[cli]` https://code.claude.com/docs/en/cli-reference, `[plugin-cli]` https://code.claude.com/docs/en/plugins/cli-reference, `[loading]` https://code.claude.com/docs/en/plugins/loading, `[manifest]` https://code.claude.com/docs/en/plugins/manifest-reference, `[skills]` https://code.claude.com/docs/en/skills, `[mcp]` https://code.claude.com/docs/en/mcp, `[memory]` https://code.claude.com/docs/en/memory, `[evals]` https://code.claude.com/docs/en/plugin-evals (all read on 2026-11-17). Facts marked *(unconfirmed)* were not stated on those pages.

---

# Part 1 — Design

## Intent

The operator's own Claude Code sessions run on his laptop, inside the repositories Cleave works on. Cleave itself runs on the Mac mini under launchd (`deploy/README.md:77-153`). When he asks a session "what is going on with ACME-123?", the session should already know what Cleave knows about that ticket: its state, its PR and its last event. He should not have to tell it where to look. When he asks for more, the session should be able to read the ticket's history, what Cleave's agents wrote, the PR and its reviews. Installing this must take one command, and the operator must be able to see that it is installed, configured and current.

**A session can read:** a ticket's workspace (state, previous state, iterations, branch, PR, error), its event history, the agents' reports, the meta files and logs, the reviews board and one PR's review detail, the daemon's mode and switches, and the credential states.

**A session never:** approves, rejects, retries, reruns, pauses, takes control of, archives, deletes or cleans a workspace. It never flips a switch, presses Review now or Skip, changes the model, or posts to GitHub, the tracker or Telegram. It never reads `.env` or `config-live/`, never calls a dashboard route outside the client namespace (below) and never opens an ssh session to the host. When the operator wants a change, the session names the dashboard button or the Telegram reply that makes it, and does nothing more.

## What exists today

- **Dashboard binding and auth.** It is a Starlette app with no middleware and no authentication (`dashboard/web.py:541`). It binds `dashboard.host` `0.0.0.0` on port `8080` (`config/schemas.py:126-133`, `main.py:979-984`, `docs/dashboard.md:13`). `deploy/README.md` says nothing about reaching the dashboard from another machine. The deploy plan records that "`dashboard.host` stays as it is" (`2026-11-15-reviewer-deploy.md:30`) and that the operator reaches the host over ssh (`…:57`).
- **Read endpoints.** None of them needs a token.

| Route | Returns | Answers |
|---|---|---|
| `GET /api/health` (`web.py:357`) | `status`, event count | is the daemon up |
| `GET /api/events?limit&offset&project_id&ticket_id` (`web.py:360-369`) | events, newest first | history |
| `GET /api/tickets/{id}/events?limit` (`web.py:380-384`) | one ticket's events | what happened to X |
| `GET /api/workspaces?project_id` (`web.py:386-411`) | every workspace: state, branch, PR, error, iterations, report and meta names, host path, disk size, links | which tickets, in what state, which PRs |
| `GET /api/workspaces/{id}/report/{file}?folder=reports\|meta\|logs` (`web.py:413-438`) | the file's text | what the agents wrote |
| `GET /api/projects`, `GET /api/projects/{id}/tickets` (`web.py:371-378`) | project and ticket ids *seen in events* (`event_store.py:94-114`), not the config | — |
| `GET /api/health/credentials[?refresh=1]` (`web.py:440-450`) | credential states | health |
| `GET /api/daemon/status` (`actions.py:408-423`) | mode and counts | is Cleave busy or blocked |
| `GET /api/switches` (`switches_api.py:14-15`) | tickets and reviews switches | paused or running |
| `GET /api/reviews`, `GET /api/reviews/pr?scope&number` (`reviews_api.py:82-96`) | the board per repo; one PR's jobs, report and posts | which reviews are queued or done |
| `GET /api/settings/model`, `GET /api/projects/telegram-globals` | model; Telegram config present | — |

- **Write endpoints, also without auth.** These are the workspace actions (`actions.py:667-681`), `PUT /api/switches` (`switches_api.py:39`), `PUT /api/settings/model` (`web.py:492`), `POST /api/projects/create`, and `validate-step`, which can send a Telegram message (`web.py:316`). Only Review now and Skip refuse cross-site requests (`reviews_api.py:49-60`). The board posts a bare `POST …/delete` (`board.js:202`). **The API cannot tell a read-only client from the operator's browser, and nothing stops anything that can reach port 8080 from approving or deleting.** This problem exists today, without any client (Decision 6).
- **Reads that have side effects.** `/api/workspaces` and every report fetch scan all workspaces and write a backfilled title into `state.json` (`web.py:109-124`, called at `:198`, `:421`). `?refresh=1` triggers live credential checks (`web.py:443-444`).
- **What a read-only client is missing:** a lookup of one ticket by key, a list of Cleave's ticket prefixes and repo identities, a listing of log files, a versioned contract, a client identity, and the skill flow's runs (`orchestrator/skillflow/` has no dashboard API yet).
- **Ticket ids.** Jira tickets have keys `<project_key>-<n>`, because the JQL is `project = <key>` (`integrations/jira/jira_adapter.py:96`). A Trello card's id is its shortLink (`integrations/trello/trello_adapter.py:276`), which is not a key. Workspaces live at `<base>/<project>/<repo>/tickets/<id>/` (`workspace/workspace_manager.py:107`). Reports live under `source/ai_pipeline/<id>/`, with two legacy fallbacks (`web.py:204-208`). Meta files are `ticket.md`, `parent.md`, `comments.md`, `history.md` and `attachments/`. Logs are `<agent>.events.log`, `<agent>.stream.jsonl`, `<agent>-tools.log` and `<agent>.log` (`orchestrator/agent_runtime.py:530-532,715,764`).
- **The daemon's own `claude -p` runs** (`integrations/llm/claude_code_adapter.py:411-453`) pass neither `--bare` nor `--setting-sources`. The docs say `--bare` "skip[s] auto-discovery of hooks, skills, custom commands, subagents, installed plugins, MCP servers…" `[cli]`, so a plain `-p` run discovers them. A plugin installed at user scope on the host would therefore reach the pipeline's agents. This is inferred from the `--bare` text; the docs do not say it outright.
- **Install today.** Cleave is installed with clone, venv and `pip install -e` (`docs/setup-guide.md:118-130`). On the Mac host the plist is rendered by hand (`deploy/README.md:92-101`). `scripts/deploy.sh` is the systemd path for a VPS (`deploy.sh:9`). Nothing gets installed on a workstation. Projects are added with `add-project` or the dashboard wizard (`dashboard/project_create.py`). They write `config-live/`, which a client can read live without needing a step of its own.
- **Existing Claude Code files.** `.claude/commands/{add,list,remove}-project.md` are project commands for sessions inside the Cleave checkout itself. They take `config-live/` relative to the working directory (`add-project.md:13`, `list-projects.md:5`) and write config. They cannot serve a read-only client in another repository.
- **Health shown today.** The Board has a credential strip (`board.js:258-259`), the Reviews tab shows a status per repo (including `readiness_failed`), and the sidebar has a daemon block. The Settings view holds only the model picker (`settings.js:7-68`).
- **Machine separation.** Nothing about the operator's other repositories may reach the host (`2026-11-15-reviewer-deploy.md:36`).

## Which Claude Code mechanism serves "what is going on with ACME-123?"

| Mechanism | Looks up without the model deciding? | What reaches the context | Cleave unreachable | Install, update | How the operator verifies |
|---|---|---|---|---|---|
| **Skill** | No. It loads when the model judges its description relevant `[skills]` | Its description always (the listing truncates at 1,536 chars); the body only on load | Costs nothing until it is invoked; then the script fails with a hint | `~/.claude/skills/`, `.claude/skills/` or a plugin; personal and project skills reload live `[skills]` | Shows in the `/` menu; nothing shows that it fired |
| **`UserPromptSubmit` hook** | Yes. It runs on every prompt and receives the prompt text `[hooks]` | Only what it prints: `additionalContext`, capped at 10,000 chars and injected as a system reminder that does not appear in the transcript `[hooks]` | Each prompt that names a Cleave key waits up to the client's HTTP timeout (default hook timeout 30 s `[hooks]`). A timeout discards the output; a non-zero exit is non-blocking; exit 2 would block the prompt | `settings.json` at user, project or local scope, or a plugin's `hooks/hooks.json` `[hooks]` | `/hooks` lists it with its source `[hooks]`; the dashboard sees its calls (below) |
| **`SessionStart` hook** | Yes, once at startup, resume, clear and compact `[hooks]` | One block at the start of the conversation | Delays the start of each session | Same as above | `/hooks` |
| **MCP server, read-only tools** | No. The model decides to call a tool | Tool definitions (deferred under tool search, which is on by default `[mcp]`; exact mechanics *unconfirmed*), plus results (a warning at 10k tokens, capped at 25k) `[mcp]` | stdio: each call fails. Remote: the server is marked failed at start and needs a reconnect in `/mcp` `[mcp]` | `claude mcp add` at local, project or user scope, or in a plugin | `claude mcp list`, `/mcp` `[mcp]` |
| **A line in CLAUDE.md** | No. The model decides to run the script | The line, in every session in scope `[memory]` | Nothing happens until the script runs | Edited by hand in `~/.claude/CLAUDE.md` or a git-ignored `CLAUDE.local.md` `[memory]`; no update path | Read the file |
| **Plugin** | A package of the above | The sum of its parts | — | `claude plugin marketplace add <path>` + `claude plugin install --scope user\|project\|local` `[plugin-cli]`. A plugin from a local-path marketplace loads in place, so an update is `git pull` `[loading]` | `claude plugin list --json` (`enabled`, `version`, `errors`) `[plugin-cli]`, `/plugin`, `/hooks` |

- **A literal webhook does not fit.** In the scenario the session pulls on a mention, and nothing in Claude Code waits for an inbound HTTP call. The nearest thing is channels (a research preview: an MCP server pushes messages into a session, `--channels` `[cli]`). That would serve notifications such as "ACME-123 needs approval", which Telegram already sends.
- **An `http` hook is rejected.** Hooks may be of type `http` `[hooks]`, which would send the hook's whole input, prompt text and `cwd` included, to the host. That breaks machine separation. The hook has to be a local command that filters before anything leaves the laptop.
- **Recommendation.** One plugin, `cleave`, holding three parts:
  1. A **`UserPromptSubmit` hook** gives the automatic brief. It recognises ticket keys in the prompt and sends only those whose prefix is one of Cleave's own. It adds a status of one to three lines.
  2. A **skill** handles the digging and states the read-only rule.
  3. One **stdlib client script** serves both, and is also the doctor and the installer.

  There is no MCP server: it would need a server process, which on the laptop means a Python `mcp` package the plugin cannot install (plugins install only Node lockfile dependencies `[loading]`), or a new protocol endpoint in the daemon. It would still leave the lookup to the model. There is no `SessionStart` hook and no CLAUDE.md line: the hook and the skill's description cover their job.

## Question 1 — where it lives, how it is installed and updated

- **A. Personal skill in `~/.claude/skills/`, copied by an installer.** The hook would need a separate edit of the user's `settings.json`, so the install is two pieces with no single uninstall. Updating means copying again.
- **B. In each target repository's `.claude/skills/` or `.claude/settings.json`.** This puts a file into a repository colleagues share: their sessions would load a skill that points at the operator's daemon, without its configuration. The employer guard would also have to watch another repository. Rejected.
- **C. Plugin from a local-path marketplace in the laptop's Cleave checkout (recommended).** The source is `claude-plugin/` plus `.claude-plugin/marketplace.json` in the Cleave repo. Install it with `claude plugin marketplace add <checkout>` and `claude plugin install cleave@cleave --scope user` `[plugin-cli]`, both wrapped as `cleave_client.py install`. Such a plugin "loads in place … edits take effect at the next session start or `/reload-plugins`, and you don't need to increase the version" `[loading]`, so an update is `git pull` in the checkout. Trade-off: the plugin follows whatever branch the checkout has, work in progress included. The doctor and the dashboard show "differs from host".
- **D. Plugin from a GitHub marketplace.** It gives versioned copies in the cache and an auto-update option `[loading]`. But it needs git credentials for the repo, which on the laptop means the keychain prompt that the operator always denies, and every release needs a version bump. Not now.
- **On the host: do not install.** A user-scope plugin there reaches the pipeline's `claude -p` agents (above). As a guard, the daemon sets `CLEAVE_AGENT_RUN=1` in every `claude` environment, and the hook stays silent when it sees that variable.

## Question 2 — how a session reaches Cleave's data

- **A. The dashboard's existing GETs.** They work today, but their shapes serve the UI and change with it. One ticket costs a scan of every workspace, which writes `state.json`. They expose host paths, give no ticket prefixes and no log listing, and identify no client.
- **B. Files and SQLite over ssh** (`sqlite3 -readonly`, as `deploy/README.md:137-141` does). This couples the session to internal schemas and needs an ssh key on the laptop. It also hands the session a shell on the host, which is the opposite of read-only.
- **C. A client script over a new read-only namespace `GET /api/client/v1/*` on the dashboard's port (recommended).** The namespace is versioned and compact. Every route is `GET`; nothing writes, except one row per client recording that it was seen. It has no title backfill, no `refresh=1`, no host paths and no tokens. Endpoints: `hello`, `projects`, `status`, `tickets`, `tickets/{key}`, `tickets/{key}/events`, `tickets/{key}/files/{folder}/{name}`, `reviews` and `reviews/pr`. The namespace exists only when `dashboard.client_token` is set. When it is empty (the default), the routes return 404 and nothing else changes.
- **Auth.** The client sends `Authorization: Bearer <token>`, which the server compares with `hmac.compare_digest`. The token lives in the host's `.env` as `CLEAVE_CLIENT_TOKEN` and is referenced as `dashboard.client_token: "${CLEAVE_CLIENT_TOKEN}"`. The same token sits in the laptop's `~/.config/cleave/client.json` with mode 0600, outside every repository. It is not stored as a plugin `userConfig` secret, because those go to the platform's secure credential store `[manifest]`, which means the keychain. The token protects and identifies the client namespace only. **It does not close the open write routes.** A session that ignores the skill can still POST to them, and so can anything else on the network (Decision 6).
- **Client configuration.** It holds `url` (whatever address works from the laptop's browser), `token`, and `name`, a short label chosen by the operator (never defaulted from the hostname, which can carry an employer's name).
- **Where prompts go.** The hook sends a ticket key to the host only when the key's prefix is in Cleave's prefix list, which it caches locally from `projects`. It never sends the prompt, the `cwd`, a repo name or the session id. A key such as `XYZ-45` from another employer's repository never leaves the laptop.

## Question 3 — how the operator checks that it is installed and configured

- **`cleave_client.py doctor`** prints one line per check, each `ok`, `warn` or `FAIL`, with a fix:
  - the config file exists and has mode 0600;
  - `claude plugin list --json` shows `cleave@cleave` enabled with no `errors` `[plugin-cli]`;
  - the host answers, the token is accepted, and the client API is on;
  - the plugin is the same as the host's, judged by a digest of the plugin files;
  - at least one ticket prefix is known.

  It never prints the token.
- **Dashboard, Settings → "Claude Code clients".** One row per client that has called the API: name, plugin version, `same as host` or `differs from host (host <version>)`, last seen, last hook call and address. The rows are stored in `events.db`, so they survive a restart. When the API is off, the section says so in one line. Asking a session about a real ticket and then watching "last hook" change is the end-to-end check.
- **Claude Code's own views.** `/hooks` labels the hook "plugin" `[hooks]`; `/plugin` and `claude plugin list` show the version and any load errors `[plugin-cli]`.

## The plugin's outline

```
.claude-plugin/marketplace.json          marketplace "cleave", one entry: {"name": "cleave", "source": "./claude-plugin"}
claude-plugin/
  .claude-plugin/plugin.json             name "cleave", version "0.1.0"
  hooks/hooks.json                       UserPromptSubmit → python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cleave_client.py" hook
  scripts/cleave_client.py               the client: read commands, hook, doctor, configure, install
  skills/reading-cleave-state/SKILL.md   invoked as cleave:reading-cleave-state
  skills/reading-cleave-state/reference/states.md    every workspace stage, review job state and credential state, one line each
  skills/reading-cleave-state/reference/reports.md   which file answers which question (ba.md, developer.md, qa.md, *-agent-output.md, meta, logs)
```

- **Skill name:** `reading-cleave-state`. The name is verb-first, and "reading" is itself the read-only rule.
- **Description:** "Use when the user mentions a ticket key such as ACME-123, or asks what Cleave is doing: a ticket's pipeline state or history, what Cleave's agents wrote, Cleave's branches and pull requests, its queued or finished reviews, or whether the Cleave daemon is running, paused or healthy." It names triggers only, with no workflow (writing-skills `discovery.md`).
- **SKILL.md** stays under 500 words. Its sections are:
  - Overview: Cleave runs on another machine and this client reads it.
  - Quick reference: a table of question → command.
  - Read-only: its final form comes from the RED run in Task 10.
  - Reading the output: points to the two reference files.
  - When the client fails: run `doctor`, quote the failing line, and never guess a state.
  - Common mistakes.

  Commands are written as `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cleave_client.py" <command>`. That variable is substituted in a plugin skill's content `[manifest]`. The script is invoked through its interpreter, never by bare path (writing-skills `SKILL.md:161`).
- **Script commands:** `status`, `projects`, `tickets [--project P] [--all]`, `ticket KEY`, `events KEY [--limit N]`, `file KEY reports|meta|logs NAME [--scope S] [--max-bytes N]`, `reviews`, `review SCOPE NUMBER`, plus `--json` on each of these. Then `hook`, `doctor`, `configure --url URL --name NAME` (the token is read from stdin) and `install [--scope user|local]`. `--help` documents each one, so SKILL.md does not repeat them.

## Decisions for the operator

1. **Mechanism.** Use one plugin with a `UserPromptSubmit` hook for the automatic brief, a skill for the digging, and a stdlib client script? *Recommended: yes. No MCP server, no `SessionStart` hook, no CLAUDE.md line.*
2. **Install where.** *Recommended: on the laptop, at user scope, so every session there gets it; the prefix filter keeps other repositories' keys on the laptop. Not on the host.*
3. **Plugin source.** Use a local-path marketplace on the laptop's Cleave checkout, loaded in place, with `git pull` as the update, even though it follows the branch that is checked out? *Recommended: yes. A GitHub marketplace can come later.*
4. **Transport.** Use a new read-only, versioned `/api/client/v1` on the dashboard's port, rather than the existing GETs or ssh with SQLite? *Recommended: yes.*
5. **Token on the client API.** Use `dashboard.client_token` from `.env`, with a copy in a 0600 file on the laptop and not in the keychain? *Recommended: yes, while knowing that it does not protect the existing routes.*
6. **Lock down the dashboard's write routes as part of this work?** They have no auth, and the workspace actions have no cross-site check. *Recommended: no, make it its own project soon. Until then, port 8080 should be reachable only from the operator's own network.*
7. **Key matching in the hook.** Match keys case-insensitively (`acme-123` counts), only Cleave's prefixes, at most 3 keys per prompt, with an HTTP timeout of 2 s? *Recommended: yes.*
8. **Where clients are shown.** *Recommended: a section in Settings, stored in `events.db`.*

## Deliberately left out

- Bare ticket numbers. The operator refers to tickets by full key only.
- Trello cards, which have no key (their shortLink is not recognised).
- The skill flow's runs in a ticket's detail: its dashboard API does not exist yet (follow-up: add `skill_runs` to `tickets/{key}` once it does).
- Any write from the client, and posting anywhere.
- An MCP server, a `SessionStart` hook, push notifications into sessions, and auth for the existing dashboard routes.
- Installing into shared repositories, the host's user scope, and Windows.

---

# Part 2 — Implementation plan

# Cleave in Claude Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code plugin, installed on the operator's laptop, through which every session there:
- sees Cleave's state for a ticket key mentioned in a prompt;
- can read the ticket's history, the agents' output, the PRs and the reviews on request;
- never changes anything in Cleave.

The dashboard shows which clients use it.

**Architecture:**
- **Host side.** The daemon gets a token-gated, GET-only namespace `/api/client/v1` (`dashboard/client_api.py`) built over the workspace files, the event store and the reviewer service. It also gets a client registry in `events.db`, `GET /api/clients`, and a Settings section.
- **Laptop side.** The plugin in `claude-plugin/` holds one stdlib Python client. Its `hook` command filters keys locally and injects a short brief. Its `doctor` and `install` commands set up and check the install. The skill holds the read-only rule and the digging procedure.

**Tech Stack:** Python 3.12 daemon (Starlette, aiosqlite); the client runs on Python ≥ 3.9 with the standard library only (urllib, argparse, json); Claude Code plugin and hook JSON; vanilla JS.

**Spec:** Part 1 of this document.

## Global Constraints

- **No employer names** anywhere, this plan and the client's examples included. Use `acme`, `ACME-123`, `acme-org/acme-app`, `acme/app`, `laptop` and `http://cleave-host:8080`.
- **English** for everything the operator reads: dashboard copy, doctor lines, CLI output and the hook's brief.
- **Read-only.** `/api/client/v1` declares only `methods=["GET"]` routes. No handler writes Cleave's state, `state.json` included. The single write is the caller's own row in `client_seen`. Credentials are never refreshed from this namespace.
- **Secrets.**
  - The token lives in the host's `.env` (`CLEAVE_CLIENT_TOKEN`) and in `~/.config/cleave/client.json` on the laptop (file 0600, directory 0700), and nowhere else. No repository, plugin file, response, log line, doctor line or test assertion message contains it.
  - The client reads the token from stdin or a TTY prompt, never from argv.
  - A `client_token` shorter than 24 characters leaves the API off and logs one warning; it never raises.
- **Machine separation.** The hook sends a key only when its prefix is one of Cleave's ticket prefixes. It never sends or logs the prompt, the `cwd`, a repo name, the hostname or the session id. The client's `name` matches `^[a-z0-9-]{1,32}$` and the operator sets it explicitly. Elsewhere a name that does not match is recorded as `unnamed`.
- **No change without the token.** With `dashboard.client_token` empty, every `/api/client/v1/*` route returns 404 and Settings shows one line saying the API is off. Nothing else changes: `ticket_flow: staged`, the pipeline and the existing routes stay as they are.
- **The hook never blocks.**
  - It always exits 0 and never prints `decision`.
  - Its `additionalContext` stays at 1,500 chars or less.
  - Each request gets 2 s, and a prompt makes at most 2 requests.
  - It stays silent under `CLEAVE_AGENT_RUN=1`, when there is no config, when there is no prefix cache and the host is unreachable, and on any exception.
- **One client file.** `claude-plugin/scripts/cleave_client.py` uses the stdlib only and must parse under `ast.parse(..., feature_version=(3, 9))`. It is always invoked as `python3 <path>`.
- **Tests** never start `claude` and never touch the network:
  - dashboard tests use `TestClient`;
  - client tests use a `FakeTransport` and a fake `run_cmd`;
  - the stub server (Task 10) binds `127.0.0.1` and serves only the skill test.

  `claude plugin validate` and the skill's scenario runs are verification steps, not part of the test suite.
- **Feature docs.** A commit that touches `integrations/` or `main.py` also updates a file in `docs/features/` (`scripts/pre-commit:7-8`).
- **Suite.** `PY` is a Python 3.12 venv with `pip install -e '.[dev]'`; run `$PY -m pytest tests/unit tests/integration -q`. Before Task 1, record the failures already present on `master`; those are not regressions.
- **Branch and commits.**
  - Work on branch `cleave-in-claude-code`, created off `master` with `git switch -c` once `skill-flow` is merged.
  - Its upstream is bound to `origin/cleave-in-claude-code` at the first push the operator asks for.
  - No commit and no push unless the operator asks; when he does, group the work into logical commits.

## Review Focus

1. **The laptop is off the host's network.** Expected: a prompt naming `ACME-123` gets one line saying the host did not answer, within about 2 s; a prompt without a key costs no request at all. Pinned by `test_hook_unreachable_one_line` and `test_hook_without_key_makes_no_request` (Task 7).
2. **A session in another employer's repository writes `XYZ-45` or `UTF-8`.** Expected: nothing about it reaches the host. Pinned by `test_hook_sends_nothing_for_unknown_prefix` and `test_hook_never_sends_prompt_or_cwd` (Task 7).
3. **The plugin ends up installed at user scope on the host.** Expected: pipeline agents get no brief. Pinned by `test_claude_env_marks_agent_run` (Task 5) and `test_hook_silent_in_cleave_agent_run` (Task 7).
4. **Path tricks through a key or a file name** (`..%2F`, `.env`, `source`). Expected: 400 or 404, and nothing outside the workspace's three folders is read. Pinned by `test_file_rejects_traversal` (Task 2).
5. **A client call that writes.** Expected: `state.json` is byte-for-byte unchanged after any client GET, even when its title is missing; credentials are never refreshed. Pinned by `test_ticket_detail_does_not_write_state` (Task 2) and `test_status_never_refreshes_credentials` (Task 3).

## Files

- **Host:**
  - `config/schemas.py`: `DashboardConfig.client_token`.
  - `dashboard/client_api.py` (new): routes and `plugin_identity`.
  - `dashboard/client_registry.py` (new).
  - `dashboard/web.py`: mounts the routes, adds `/api/clients`, extracts two helpers.
  - `main.py`: passes the token and the version.
  - `integrations/llm/claude_code_adapter.py`: sets `CLEAVE_AGENT_RUN`.
  - `dashboard/static/js/settings.js`.
- **Plugin:**
  - `.claude-plugin/marketplace.json`;
  - `claude-plugin/` with `plugin.json`, `hooks/hooks.json`, `scripts/cleave_client.py` and the skill.
- **Tests:**
  - `tests/unit/test_client_api.py`, `tests/unit/test_client_registry.py`, `tests/unit/test_cleave_client.py`, `tests/unit/test_claude_plugin_layout.py`;
  - `tests/fixtures/cleave_stub.py` and `tests/fixtures/cleave_stub/` (Task 10).
- **Docs:**
  - `docs/claude-code-client.md` (new);
  - `docs/setup-guide.md`, `deploy/README.md`, `docs/dashboard.md`, `docs/features/dashboard.md`;
  - `environment.template`, `deploy/environment.template`, `config-live.example/global.yaml`.

---

### Task 1: Client API — token gate, `hello`, `projects`

**Files:**
- Modify: `config/schemas.py:126-133` (`client_token: str = ""` on `DashboardConfig`), `dashboard/web.py:340-354` (two new parameters, mount), `main.py:966-982` (pass them), `docs/features/dashboard.md`
- Create: `dashboard/client_api.py`
- Test: `tests/unit/test_client_api.py`

**Interfaces:**
- Produces:
  - `create_app(..., client_token: str = "", cleave_version: str = "")`.
  - `build_client_routes(*, token: str, store: EventStore, workspace_base_dir: Path, get_projects: Callable[[], dict], cleave_version: str, plugin: dict, registry: ClientRegistry | None = None, orchestrator=None, mode_handler=None, switches=None, credential_health=None, reviewer=None) -> list[Route]`. Each route is wrapped in `_guarded(handler)`, which returns 401 `{"error": "token required"}` unless the bearer token matches.
  - `PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "claude-plugin"`.
  - `plugin_identity(root: Path) -> dict` returns `{"version": str, "digest": str}`. The version comes from `.claude-plugin/plugin.json`. The digest is the first 12 hex chars of a sha256 over the sorted (relative POSIX path, bytes) pairs of every file under `root`, skipping `__pycache__/`, `evals/` and files whose name starts with `.` (but not `.claude-plugin/`). A missing root gives `{"version": "", "digest": ""}`.
  - `GET /api/client/v1/hello` returns `{"api": 1, "cleave_version", "plugin": {"version", "digest"}}`.
  - `GET /api/client/v1/projects` returns `{"projects": [{"id", "name", "tracker", "ticket_prefix", "repos": [{"id", "scope", "forge", "remote", "ticket_flow", "reviews"}]}]}`, where:
    - `ticket_prefix` is the Jira `project_key`, or `""` for Trello;
    - `remote` is `owner/repo` for GitHub, or `<url>/<project_id>` for GitLab;
    - neither a token nor `git.clone_url` is ever included.
  - `main.py` passes `client_token=global_config.dashboard.client_token`, `cleave_version=version`.

- [ ] **Step 1: Write the failing tests**

```python
TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}", "X-Cleave-Client": "laptop",
     "X-Cleave-Client-Version": "0.1.0", "X-Cleave-Client-Digest": "0123456789ab"}

def acme_projects():
    jira = JiraConfig(url="https://acme.example", token="SECRET-JIRA", project_key="ACME")
    gh = GitHubConfig(token="SECRET-GH", owner="acme-org", repo="acme-app")
    return {"acme": LoadedProject(
        config=ProjectConfig(project=ProjectInfo("acme", "Acme"), tracker=TrackerConfig("jira", jira)),
        repos={"app": RepoConfig(repo=RepoInfo("app"), vcs=VCSConfig(github=gh))})}

def test_client_api_absent_without_token(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/client/v1/hello", headers=H).status_code == 404

def test_short_token_leaves_api_off(store):
    c = TestClient(create_app(EventBus(), store, client_token="short"))
    assert c.get("/api/client/v1/hello", headers={"Authorization": "Bearer short"}).status_code == 404

def test_requires_bearer(c):
    assert c.get("/api/client/v1/hello").status_code == 401
    assert c.get("/api/client/v1/hello", headers={**H, "Authorization": "Bearer nope"}).status_code == 401

def test_get_only(c):
    assert c.post("/api/client/v1/hello", headers=H).status_code == 405

def test_hello(c):
    body = c.get("/api/client/v1/hello", headers=H).json()
    assert body["api"] == 1 and body["cleave_version"] == "0.1.0" and set(body["plugin"]) == {"version", "digest"}

def test_projects_shape_and_no_secrets(c):
    r = c.get("/api/client/v1/projects", headers=H)
    p = r.json()["projects"][0]
    assert (p["id"], p["tracker"], p["ticket_prefix"]) == ("acme", "jira", "ACME")
    assert p["repos"] == [{"id": "app", "scope": "acme/app", "forge": "github", "remote": "acme-org/acme-app",
                           "ticket_flow": "staged", "reviews": False}]
    assert "SECRET" not in r.text
```

The `c` fixture builds `create_app(EventBus(), store, workspace_base_dir=str(tmp_path / "ws"), projects=acme_projects(), client_token=TOKEN, cleave_version="0.1.0")`.

- [ ] **Step 2: Run** `$PY -m pytest tests/unit/test_client_api.py -q`. Expected: FAIL, `create_app() got an unexpected keyword argument 'client_token'`.
- [ ] **Step 3: Implement.** Read the projects through `lambda: projects or {}`, as the switch routes do (`web.py:508`), so that hot-reloaded projects appear. Mount the routes only when `len(client_token) >= 24`. A shorter, non-empty token logs `Client API off: dashboard.client_token is shorter than 24 characters`.
- [ ] **Step 4: Run** the same command. Expected: all tests pass, and `test_dashboard_web.py` and `test_dashboard_switches.py` still pass.

---

### Task 2: Tickets — list, by keys, detail, events, files

**Files:**
- Modify: `dashboard/client_api.py`; `dashboard/web.py:95-107,204-208,427-431`. Extract `title_from_meta(ws_root: Path, ticket_id: str) -> str` (the parse without the write) and `reports_dir(ws_root: Path, ticket_id: str) -> Path`. `_maybe_backfill_title`, `_scan_all_workspaces` and `get_workspace_report` keep their behaviour and call the helpers.
- Test: `tests/unit/test_client_api.py`

**Interfaces:**
- Consumes: `title_from_meta` and `reports_dir`, and `EventStore.query_recent(limit, offset, project_id, ticket_id)` (`event_store.py:66`).
- Produces:
  - **Key rule.** `KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"`; a key that does not match gives 400.
  - **Workspace lookup.** `find_workspaces(base: Path, key: str) -> list[Path]` uses `base.glob(f"*/*/tickets/{key}/state.json")` and returns the parents in sorted order. It never uses `rglob` and never computes disk sizes.
  - **Summary.** `summary(ws_root: Path, data: dict, last_event: dict | None) -> dict` has the keys `key`, `scope`, `state`, `previous_state`, `title`, `branch`, `pr_number`, `pr_url`, `human_input_pending`, `error` (at most 300 chars), `started_at`, `updated_at` and `last_event` (`{"at": ISO UTC, "type", "message"}` or `None`).
  - **List.** `GET /api/client/v1/tickets?keys=A,B&project=P&all=1` returns `{"tickets": [summary…], "missing": [keys…]}`:
    - at most 10 keys;
    - without `keys`, the result leaves out `DONE` and `ARCHIVED` unless `all=1` is passed;
    - sorted by `updated_at`, newest first.
  - **Detail.** `GET /api/client/v1/tickets/{key}` returns `{"key", "workspaces": [summary + {"stage_iterations", "model", "files": {"reports", "meta", "logs"}, "events": last 20}], "reviews": []}`. It answers 404 `{"error": "Cleave has no workspace for ACME-9"}` when there is no workspace (Task 3 adds review rows). It contains no `workspace_root`.
  - **Events.** `GET /api/client/v1/tickets/{key}/events?limit=200` returns at most 1000 events.
  - **Files.** `GET /api/client/v1/tickets/{key}/files/{folder}/{name}?scope=&max_bytes=262144` returns `text/plain`:
    - `folder` must be one of `reports`, `meta`, `logs`; `name` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`;
    - the file must resolve inside that folder;
    - a larger file returns its tail, with the header `X-Cleave-Truncated: <total bytes>`;
    - when the key has workspaces in more than one repo, the response is 409 `{"error": "ACME-123 is in acme/app and acme/web; pass scope"}`.

- [ ] **Step 1: Write the failing tests.** The `ws` fixture writes `ws/acme/app/tickets/ACME-123/` with:
  - `state.json` (`current_state` `QA`, `pr_number` 1712, no `title`);
  - `meta/ticket.md` with the line `# ACME-123: Fix cart sync`;
  - `source/ai_pipeline/ACME-123/qa.md` with `# QA\n3 tests failed`;
  - `logs/qa-agent.events.log`.

  It also inserts `Event("qa_failed", "3 tests failed", ticket_id="ACME-123")`.

```python
def test_tickets_by_keys(c, ws):
    body = c.get("/api/client/v1/tickets?keys=ACME-123,ACME-9", headers=H).json()
    assert [t["key"] for t in body["tickets"]] == ["ACME-123"] and body["missing"] == ["ACME-9"]
    t = body["tickets"][0]
    assert (t["scope"], t["state"], t["pr_number"], t["last_event"]["type"]) == ("acme/app", "QA", 1712, "qa_failed")

def test_list_hides_done_unless_all(c, ws, done_ws):
    assert "ACME-100" not in [t["key"] for t in c.get("/api/client/v1/tickets", headers=H).json()["tickets"]]
    assert "ACME-100" in [t["key"] for t in c.get("/api/client/v1/tickets?all=1", headers=H).json()["tickets"]]

def test_ticket_detail_does_not_write_state(c, ws):
    before = (ws / "state.json").read_bytes()
    w = c.get("/api/client/v1/tickets/ACME-123", headers=H).json()["workspaces"][0]
    assert w["title"] == "Fix cart sync" and "workspace_root" not in w
    assert w["files"] == {"reports": ["qa.md"], "meta": ["ticket.md"], "logs": ["qa-agent.events.log"]}
    assert (ws / "state.json").read_bytes() == before

def test_ticket_detail_404(c, ws):
    r = c.get("/api/client/v1/tickets/ACME-9", headers=H)
    assert r.status_code == 404 and r.json()["error"] == "Cleave has no workspace for ACME-9"

def test_file_text_and_tail(c, ws):
    assert c.get("/api/client/v1/tickets/ACME-123/files/reports/qa.md", headers=H).text.startswith("# QA")
    (ws / "logs" / "big.log").write_bytes(b"x" * 300_000)
    r = c.get("/api/client/v1/tickets/ACME-123/files/logs/big.log", headers=H)
    assert r.headers["x-cleave-truncated"] == "300000" and len(r.content) == 262_144

@pytest.mark.parametrize("path", [
    "/api/client/v1/tickets/..%2F..%2Fx/files/meta/ticket.md",
    "/api/client/v1/tickets/ACME-123/files/meta/..%2Fstate.json",
    "/api/client/v1/tickets/ACME-123/files/source/qa.md",
    "/api/client/v1/tickets/ACME-123/files/meta/.env",
])
def test_file_rejects_traversal(c, ws, path):
    assert c.get(path, headers=H).status_code in (400, 404)

def test_two_repos_need_scope(c, ws, ws_web_same_key):
    r = c.get("/api/client/v1/tickets/ACME-123/files/reports/qa.md", headers=H)
    assert r.status_code == 409 and "pass scope" in r.json()["error"]
    assert c.get("/api/client/v1/tickets/ACME-123/files/reports/qa.md?scope=acme/app", headers=H).status_code == 200
```

- [ ] **Step 2: Run** `$PY -m pytest tests/unit/test_client_api.py -q`. Expected: the new tests FAIL with 404.
- [ ] **Step 3: Implement** as specified in the Interfaces block.
- [ ] **Step 4: Run** the same command plus `tests/unit/test_dashboard_web.py`. Expected: all pass, including the existing title-backfill tests.

---

### Task 3: `status`, `reviews`, review rows in a ticket's detail

**Files:** Modify `dashboard/client_api.py`. Test: `tests/unit/test_client_api.py`.

**Interfaces:**
- Consumes:
  - `mode_handler.get_mode()`;
  - `orchestrator.get_active_workspaces()`;
  - `switches.snapshot(switch_scopes(projects))` (`switches_api.py:15`);
  - `credential_health.snapshot()` (never `check_all`);
  - `reviewer.overview()` and `reviewer.detail(scope, number)` (`orchestrator/reviewer/service.py:452,476`);
  - `reviews_api._repo` and `reviews_api._pr` (`reviews_api.py:21,37`).
- Produces:
  - **Status.** `GET /api/client/v1/status` returns `{"mode", "tickets": {"active", "blocked", "awaiting_approval", "manual_control"} | None, "switches": … | None, "credentials": [{"label", "state"}], "reviews": [{"scope", "status", "reason"}]}`.
  - **Reviews.** `GET /api/client/v1/reviews` returns `{"repos": [_repo(r)…]}`, and `{"repos": []}` without a reviewer.
  - **One PR.** `GET /api/client/v1/reviews/pr?scope=&number=` returns `reviewer.detail(...)`; a `LookupError` gives 404.
  - **Review rows in a ticket.** The ticket detail's `reviews` holds `_pr(row, repo)` for every board row whose `ticket_key` equals the key. A key with no workspace but with review rows answers 200.

- [ ] **Step 1: Write the failing tests**

```python
def test_status_without_services(c):
    assert c.get("/api/client/v1/status", headers=H).json() == {
        "mode": None, "tickets": None, "switches": None, "credentials": [], "reviews": []}

def test_status_never_refreshes_credentials(c_with_health):
    c, health = c_with_health  # FakeHealth as in test_dashboard_health_api.py
    body = c.get("/api/client/v1/status?refresh=1", headers=H).json()
    assert body["credentials"] == [{"label": "GitHub acme-org/acme-app", "state": "invalid"}]
    health.check_all.assert_not_awaited()

def test_reviews_and_ticket_rows(c_with_reviewer):  # fake overview() with one BoardRow, ticket_key "ACME-123"
    assert c_with_reviewer.get("/api/client/v1/reviews", headers=H).json()["repos"][0]["scope"] == "acme/app"
    d = c_with_reviewer.get("/api/client/v1/tickets/ACME-123", headers=H).json()
    assert [r["number"] for r in d["reviews"]] == [1712]

def test_review_pr_404(c_with_reviewer):  # fake detail() raises LookupError
    assert c_with_reviewer.get("/api/client/v1/reviews/pr?scope=acme/app&number=9", headers=H).status_code == 404
```

- [ ] **Step 2: Run.** Expected: FAIL with 404 on `/status`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `$PY -m pytest tests/unit/test_client_api.py tests/unit/test_dashboard_reviews.py -q`. Expected: all pass.

---

### Task 4: Client registry, `GET /api/clients`, the Settings section

**Files:**
- Create: `dashboard/client_registry.py`
- Modify: `dashboard/client_api.py` (`_guarded` records), `dashboard/web.py` (route), `dashboard/static/js/settings.js`
- Test: `tests/unit/test_client_registry.py`, `tests/unit/test_client_api.py`

**Interfaces:**
- Produces:
  - **The registry.** `ClientRegistry(db_path: str)`:
    - `async record(name: str, version: str, digest: str, call: str, address: str, now: float) -> None` upserts. `call` is `hook` or a command name; when `call == "hook"`, it also sets `last_hook_at`.
    - `async all() -> list[dict]` returns rows ordered by `last_seen`, newest first.
    - The table is `client_seen (name TEXT PRIMARY KEY, version TEXT NOT NULL, digest TEXT NOT NULL, last_call TEXT NOT NULL, last_hook_at REAL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, address TEXT NOT NULL)`. It is created with `IF NOT EXISTS` on first use, in the event store's file (`store._db_path`, as `web.py:393` does).
  - **Recorded headers.** `_guarded` records the headers `X-Cleave-Client`, `X-Cleave-Client-Version`, `X-Cleave-Client-Digest` and `X-Cleave-Call`, plus `request.client.host`. A name that does not match `^[a-z0-9-]{1,32}$` is recorded as `unnamed`; the version and digest are cut to 32 chars.
  - **The listing.** `GET /api/clients` (dashboard side, mounted always) returns `{"enabled": bool, "plugin": plugin_identity(PLUGIN_ROOT), "clients": [row + {"same_as_host": row.digest == plugin.digest}]}`.
  - **Settings section "Claude Code clients".**
    - When `enabled` is false: `Client API is off. Set dashboard.client_token in global.yaml (from .env) and restart the daemon.`
    - With no rows: `No client has called yet. On the workstation, run: python3 <Cleave checkout>/claude-plugin/scripts/cleave_client.py doctor`.
    - Otherwise a table with the columns Name · Plugin · Matches host · Last seen · Last hook · Address. The cells read `same as host` or `differs from host (host <version>)`, and `never` when there is no hook call yet.

- [ ] **Step 1: Write the failing tests**

```python
async def test_registry_upserts(tmp_path):
    reg = ClientRegistry(str(tmp_path / "e.db"))
    await reg.record("laptop", "0.1.0", "d" * 12, "hook", "192.0.2.10", now=1000.0)
    await reg.record("laptop", "0.1.0", "d" * 12, "ticket", "192.0.2.10", now=1060.0)
    assert await reg.all() == [{"name": "laptop", "version": "0.1.0", "digest": "d" * 12, "last_call": "ticket",
                                "last_hook_at": 1000.0, "first_seen": 1000.0, "last_seen": 1060.0,
                                "address": "192.0.2.10"}]

def test_clients_listing(c):
    c.get("/api/client/v1/hello", headers={**H, "X-Cleave-Call": "doctor"})
    row = c.get("/api/clients").json()["clients"][0]
    assert (row["name"], row["last_call"], row["same_as_host"]) == ("laptop", "doctor", False)

def test_bad_name_is_unnamed(c):
    c.get("/api/client/v1/hello", headers={**H, "X-Cleave-Client": "ACME Laptop!"})
    assert [r["name"] for r in c.get("/api/clients").json()["clients"]] == ["unnamed"]

def test_clients_listing_when_off(store):
    body = TestClient(create_app(EventBus(), store)).get("/api/clients").json()
    assert body["enabled"] is False and body["clients"] == []
```

- [ ] **Step 2: Run** `$PY -m pytest tests/unit/test_client_registry.py tests/unit/test_client_api.py -q`. Expected: FAIL with `ModuleNotFoundError: dashboard.client_registry`.
- [ ] **Step 3: Implement** the registry and the route. Then the Settings section: `renderSettings` appends a `<section class="settings-view">` filled from `/api/clients` and built with `esc()` like the model picker (`settings.js:21-32`).
- [ ] **Step 4: Run** the same command. Expected: all pass. Then open Settings on a dev daemon and check that each of the three states renders its text.

---

### Task 5: Mark the daemon's own `claude` runs

**Files:** Modify `integrations/llm/claude_code_adapter.py:163-167` and `docs/features/agent-system.md`. Test: `tests/unit/test_claude_code_adapter.py`.

**Interfaces:** Produces `claude_env(env) -> dict[str, str]`, which returns the same result as before plus `CLEAVE_AGENT_RUN="1"`.

- [ ] **Step 1: Write the failing test**

```python
def test_claude_env_marks_agent_run():
    assert claude_env(None)["CLEAVE_AGENT_RUN"] == "1"
    assert claude_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k"}) == {"PATH": "/bin", "CLEAVE_AGENT_RUN": "1"}
```

- [ ] **Step 2: Run** `$PY -m pytest tests/unit/test_claude_code_adapter.py -q`. Expected: FAIL with `KeyError: 'CLEAVE_AGENT_RUN'`.
- [ ] **Step 3: Implement**, then add one line to the feature doc: "every `claude` run carries `CLEAVE_AGENT_RUN=1`; the Cleave plugin's hook is silent there."
- [ ] **Step 4: Run** the whole suite. Expected: no new failures. Some reviewer and skill-flow tests compare an exact env; update those to include the marker, and list them in the task report.

---

### Task 6: The client — config, transport, read commands

**Files:** Create `claude-plugin/scripts/cleave_client.py`. Test: `tests/unit/test_cleave_client.py`, which loads the script with `importlib.util.spec_from_file_location("cleave_client", ROOT / "claude-plugin/scripts/cleave_client.py")`.

**Interfaces:**
- Produces:
  - **Paths.** `CONFIG_PATH` (`~/.config/cleave/client.json`, overridden by `CLEAVE_CLIENT_CONFIG`) and `CACHE_DIR` (`~/.cache/cleave`, overridden by `CLEAVE_CLIENT_CACHE`).
  - **Config.**
    - `ClientConfig(url: str, token: str, name: str)` (a dataclass);
    - `load_config(path: Path) -> ClientConfig`, which raises `ConfigProblem(detail: str, fix: str)`;
    - `save_config(path: Path, cfg: ClientConfig) -> None`, which writes atomically with mode 0600 into a 0700 directory.
  - **Transport.**
    - `Response(status: int, body: bytes, headers: dict)`;
    - `Unreachable(Exception)` with `.reason`;
    - `HttpTransport(cfg, version: str, digest: str, timeout: float = 2.0)` with `.get(path: str, params: dict | None = None, call: str = "") -> Response`. It is urllib, `GET` only, and sends the four `X-Cleave-*` headers plus `Authorization`.
  - **Plugin identity.** `plugin_digest(root: Path) -> str` uses the same algorithm as `plugin_identity` (Task 1); `plugin_version(root: Path) -> str`.
  - **Command names.** `COMMANDS: frozenset[str]` holds the subcommand names (`status`, `projects`, `tickets`, `ticket`, `events`, `file`, `reviews`, `review`, `hook`, `doctor`, `configure`, `install`).
  - **CLI.** `main(argv: list[str], transport=None, run_cmd=None, stdin=None) -> int` with the commands from Part 1.
    - Internal functions take a `transport_factory: Callable[[ClientConfig], transport]`. `main` turns a given `transport` instance into `lambda cfg: transport`, and otherwise builds an `HttpTransport` from the config.
    - Text output is the default; `--json` prints the response body.
    - Exit codes: 0 for ok; 1 for not found or an API error, printing the server's `error`; 2 for unreachable or a config problem, printing `Cleave: <detail>. Run: python3 <script path> doctor`.

- [ ] **Step 1: Write the failing tests** (`FakeTransport(routes: dict[str, tuple[int, object]])` records `(path, params, call)`)

```python
def test_missing_config_gives_fix(tmp_path):
    with pytest.raises(cc.ConfigProblem) as e:
        cc.load_config(tmp_path / "none.json")
    assert "configure" in e.value.fix

def test_save_config_modes(tmp_path):
    p = tmp_path / "cfg" / "client.json"
    cc.save_config(p, cc.ClientConfig("http://cleave-host:8080", "t" * 32, "laptop"))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700

def test_http_transport_is_get_with_headers(monkeypatch):
    seen = {}
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda req, timeout: seen.update(req=req, t=timeout) or FakeResp(b"{}"))
    cc.HttpTransport(cc.ClientConfig("http://cleave-host:8080", "t" * 32, "laptop"), "0.1.0", "0123456789ab").get(
        "/api/client/v1/hello", call="doctor")
    req = seen["req"]
    assert req.get_method() == "GET" and seen["t"] == 2.0
    assert req.get_header("Authorization") == "Bearer " + "t" * 32 and req.get_header("X-cleave-call") == "doctor"

def test_ticket_text(capsys, fake_ticket):
    assert cc.main(["ticket", "ACME-123"], transport=fake_ticket) == 0
    out = capsys.readouterr().out
    assert "ACME-123 · acme/app · QA" in out and "reports: qa.md" in out and "t" * 32 not in out

def test_unreachable_exit_2(capsys, down):
    assert cc.main(["status"], transport=down) == 2
    assert "doctor" in capsys.readouterr().out

def test_script_is_stdlib_and_py39():
    src = SCRIPT.read_text()
    ast.parse(src, feature_version=(3, 9))
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}
```

- [ ] **Step 2: Run** `$PY -m pytest tests/unit/test_cleave_client.py -q`. Expected: FAIL, because the script file is missing.
- [ ] **Step 3: Implement.** The text formats are one line per ticket summary, `<key> · <scope> · <state> (<iterations>) · PR #<n> · updated <time UTC>`, then the indented file lists and the last events.
- [ ] **Step 4: Run** the same command. Expected: all pass.

---

### Task 7: The hook

**Files:** Modify `claude-plugin/scripts/cleave_client.py`; create `claude-plugin/hooks/hooks.json`. Test: `tests/unit/test_cleave_client.py`.

**Interfaces:**
- Produces:
  - **Key extraction.** `extract_keys(prompt: str, prefixes: set[str], limit: int = 3) -> list[str]`. The regex is `(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_]{1,9})-([0-9]{1,7})(?![A-Za-z0-9])`. Matches are uppercased, kept only when the prefix is in `prefixes`, made distinct in order, and capped at `limit`.
  - **Prefix cache.** `known_prefixes(transport, cache_dir: Path, now: float, ttl: float = 3600.0) -> set[str] | None` caches `projects.json` as `{"fetched_at", "projects"}`. It refreshes when the cache is stale. On `Unreachable` it falls back to the stale cache, and returns `None` when there is no cache at all.
  - **Brief.** `format_brief(tickets: list[dict], missing: list[str]) -> str`. The text is fixed:
    ```
    Cleave, the ticket pipeline on the Cleave host, reports (read-only from this session):
    - ACME-123 · acme/app · QA (dev:1 qa:2) · PR #1712 https://github.com/acme-org/acme-app/pull/1712 · last event 2026-11-17 14:31 UTC qa_failed: 3 tests failed
    - ACME-124: Cleave has no workspace for it.
    For its history, agent reports, logs or reviews, use the reading-cleave-state skill.
    ```
    Segments without data are left out. `· waiting for the operator` follows when `human_input_pending` is set, and `· error: <first 120 chars>` follows when there is an error. The unreachable line is `Cleave: ACME-123 not checked — the Cleave host did not answer within 2 s. Run the client's doctor command to see why.`
  - **Hook run.** `run_hook(stdin_text: str, cfg_path: Path, transport_factory, cache_dir: Path, now: float, env: dict) -> str` returns either `""` or `json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": brief}})`.
    - It makes one `tickets?keys=` call carrying `X-Cleave-Call: hook`.
    - When the prefix refresh just failed as unreachable, it skips the `tickets` call and prints the unreachable line, so a prompt never waits more than one timeout.
    - `main(["hook"])` reads stdin, prints the result and returns 0 in every case.
  - **`hooks.json`.** `{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/cleave_client.py\" hook", "timeout": 10}]}]}}`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("prompt,expected", [
    ("what is going on with ACME-123?", ["ACME-123"]), ("acme-123 vs ACME-123", ["ACME-123"]),
    ("UTF-8 and XYZ-45", []), ("ACME-1 ACME-2 ACME-3 ACME-4", ["ACME-1", "ACME-2", "ACME-3"]),
    ("ACME-12x", []), ("see notes/ACME-7.md", ["ACME-7"]),
])
def test_extract_keys(prompt, expected):
    assert cc.extract_keys(prompt, {"ACME"}) == expected

def hook(prompt, transport, tmp_path, env=None, now=1000.0, cfg=None):
    if cfg is None:
        cfg = tmp_path / "client.json"
        cc.save_config(cfg, cc.ClientConfig("http://cleave-host:8080", "t" * 32, "laptop"))
    return cc.run_hook(json.dumps({"prompt": prompt, "cwd": "/work/acme-app", "session_id": "s1"}),
                       cfg, lambda c: transport, tmp_path / "cache", now, env or {})

def test_hook_injects_brief(tmp_path, fake_cleave):
    out = json.loads(hook("what is going on with ACME-123", fake_cleave, tmp_path))
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "UserPromptSubmit" and "decision" not in out
    assert "ACME-123 · acme/app · QA" in ctx["additionalContext"] and len(ctx["additionalContext"]) <= 1500

def test_hook_without_key_makes_no_request(tmp_path, fake_cleave):
    assert hook("refactor the cart screen", fake_cleave, tmp_path) == "" and fake_cleave.calls == []

def test_hook_sends_nothing_for_unknown_prefix(tmp_path, fake_cleave_cached):
    assert hook("fix XYZ-45 in the UTF-8 parser", fake_cleave_cached, tmp_path) == ""
    assert not [c for c in fake_cleave_cached.calls if c[0].startswith("/api/client/v1/tickets")]

def test_hook_never_sends_prompt_or_cwd(tmp_path, fake_cleave):
    hook("ACME-123 broke checkout in acme-app", fake_cleave, tmp_path)
    assert all("checkout" not in repr(c) and "/work" not in repr(c) and "s1" not in repr(c) for c in fake_cleave.calls)

def test_hook_unreachable_one_line(tmp_path, cached_then_down):
    ctx = json.loads(hook("ACME-123?", cached_then_down, tmp_path))["hookSpecificOutput"]["additionalContext"]
    assert ctx == ("Cleave: ACME-123 not checked — the Cleave host did not answer within 2 s. "
                   "Run the client's doctor command to see why.")

@pytest.mark.parametrize("case", ["agent_run", "no_config", "no_cache_and_down", "raises"])
def test_hook_silent(tmp_path, case, silent_cases):
    transport, env, cfg = silent_cases[case]  # no_config: cfg = tmp_path / "missing.json"
    assert hook("ACME-123?", transport, tmp_path, env=env, cfg=cfg) == ""
    if case == "agent_run":
        assert transport.calls == []

def test_stale_cache_and_down_waits_one_timeout(tmp_path, stale_cache_down):
    hook("ACME-123?", stale_cache_down, tmp_path)
    assert [c[0] for c in stale_cache_down.calls] == ["/api/client/v1/projects"]

def test_prefix_cache_ttl(tmp_path, fake_cleave):
    cc.known_prefixes(fake_cleave, tmp_path, now=1000.0)
    cc.known_prefixes(fake_cleave, tmp_path, now=4599.0)
    cc.known_prefixes(fake_cleave, tmp_path, now=4601.0)
    assert [c[0] for c in fake_cleave.calls].count("/api/client/v1/projects") == 2

def test_hooks_json():
    h = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert h == {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cleave_client.py" hook', "timeout": 10}
```

- [ ] **Step 2: Run.** Expected: FAIL with `AttributeError: extract_keys`.
- [ ] **Step 3: Implement.** `run_hook` catches every exception and returns `""`. It reads no config at all when `CLEAVE_AGENT_RUN == "1"`.
- [ ] **Step 4: Run** `$PY -m pytest tests/unit/test_cleave_client.py -q`. Expected: all pass.

---

### Task 8: `doctor`, `configure`, `install`

**Files:** Modify `claude-plugin/scripts/cleave_client.py`. Test: `tests/unit/test_cleave_client.py`.

**Interfaces:**
- Produces:
  - **Checks.** `Check(name: str, status: str, detail: str, fix: str)`, where `status` is one of `ok`, `warn`, `fail`.
  - **Doctor.** `doctor(cfg_path: Path, transport_factory, run_cmd, plugin_root: Path, now: float) -> list[Check]` runs checks in this order:
    - `config`, `permissions`, `plugin`, `reach`, `api`, `version`, `prefixes`;
    - `version` is `warn` when the client's digest differs from the host's;
    - `run_cmd(argv: list[str]) -> tuple[int, str]` runs `["claude", "plugin", "list", "--json"]`.

    `main(["doctor"])` prints `<status> <name> <detail>` lines, with `  fix: <fix>` under each non-ok line, and returns 1 if any check is `fail`.
  - **Configure.** `main(["configure", "--url", URL, "--name", NAME])` reads the token with `getpass` on a TTY, or one stdin line otherwise. It validates that the URL is `http(s)://`, the name matches `^[a-z0-9-]{1,32}$` and the token has at least 24 chars, then calls `save_config` and runs `doctor`.
  - **Install.** `main(["install", "--scope", "user"|"local"])` runs `["claude", "plugin", "marketplace", "add", <repo root>]` and then `["claude", "plugin", "install", "cleave@cleave", "--scope", <scope>]`. The repo root is the script path's `parents[2]`. When the config file is missing it prints the `configure` command line. It never prompts for the token itself.

- [ ] **Step 1: Write the failing tests**

```python
def test_doctor_all_ok(tmp_path, good_cfg, fake_ok, run_ok):
    checks = cc.doctor(good_cfg, lambda cfg: fake_ok, run_ok, PLUGIN, now=1000.0)
    assert [c.name for c in checks] == ["config", "permissions", "plugin", "reach", "api", "version", "prefixes"]
    assert {c.status for c in checks} == {"ok"}

@pytest.mark.parametrize("fault,name,fix_part", [
    ("mode_644", "permissions", "chmod 600"),
    ("plugin_missing", "plugin", "claude plugin install cleave@cleave"),
    ("plugin_disabled", "plugin", "claude plugin enable cleave@cleave"),
    ("down", "reach", "the host's address"),
    ("http_401", "api", "CLEAVE_CLIENT_TOKEN"),
    ("http_404", "api", "dashboard.client_token"),
])
def test_doctor_failures(fault, name, fix_part, doctor_case):
    check = next(c for c in doctor_case(fault) if c.name == name)
    assert check.status == "fail" and fix_part in check.fix

def test_doctor_digest_differs_is_warn(doctor_case):
    assert next(c for c in doctor_case("digest_differs") if c.name == "version").status == "warn"

def test_doctor_never_prints_token(capsys, good_cfg, fake_ok, run_ok, monkeypatch):
    monkeypatch.setenv("CLEAVE_CLIENT_CONFIG", str(good_cfg))
    cc.main(["doctor"], transport=fake_ok, run_cmd=run_ok)
    assert "t" * 24 not in capsys.readouterr().out

def test_configure_reads_token_from_stdin(tmp_path, monkeypatch, fake_ok, run_ok):
    monkeypatch.setenv("CLEAVE_CLIENT_CONFIG", str(tmp_path / "c" / "client.json"))
    rc = cc.main(["configure", "--url", "http://cleave-host:8080", "--name", "laptop"],
                 transport=fake_ok, run_cmd=run_ok, stdin=io.StringIO("t" * 32 + "\n"))
    assert rc == 0 and json.loads((tmp_path / "c" / "client.json").read_text())["token"] == "t" * 32

def test_configure_rejects_hostname_like_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEAVE_CLIENT_CONFIG", str(tmp_path / "client.json"))
    assert cc.main(["configure", "--url", "http://cleave-host:8080", "--name", "ACME-LAPTOP.local"],
                   stdin=io.StringIO("t" * 32)) == 2

def test_install_runs_plugin_commands(run_rec):
    cc.main(["install"], run_cmd=run_rec)
    assert run_rec.calls == [["claude", "plugin", "marketplace", "add", str(ROOT)],
                             ["claude", "plugin", "install", "cleave@cleave", "--scope", "user"]]
```

- [ ] **Step 2: Run.** Expected: FAIL with `AttributeError: doctor`.
- [ ] **Step 3: Implement.** The fix texts:
  - `permissions`: `chmod 600 ~/.config/cleave/client.json`.
  - `plugin`: `python3 <script> install` when the plugin is missing; `claude plugin enable cleave@cleave` when it is disabled.
  - `reach`: `check the host's address in ~/.config/cleave/client.json and that the laptop can open it in a browser`.
  - `api` on 401: `copy CLEAVE_CLIENT_TOKEN from the host's .env and run configure again`.
  - `api` on 404: `set dashboard.client_token: "${CLEAVE_CLIENT_TOKEN}" in the host's global.yaml and restart the daemon`.
  - `version` (warn): `run git pull in <repo root>; differs from host (client <v>, host <v>)`.
  - `prefixes`: `no Jira project in Cleave's config; the hook recognises no key`.
- [ ] **Step 4: Run** `$PY -m pytest tests/unit/test_cleave_client.py -q`. Expected: all pass.

---

### Task 9: Plugin packaging

**Files:** Create `.claude-plugin/marketplace.json` and `claude-plugin/.claude-plugin/plugin.json`. Test: `tests/unit/test_claude_plugin_layout.py`.

**Interfaces:**
- Produces:
  - **Marketplace** `cleave`, with one plugin entry `{"name": "cleave", "source": "./claude-plugin", "description": "Read-only view of a running Cleave from Claude Code sessions"}`. The other required fields follow the marketplace reference, and `claude plugin validate .` must pass.
  - **Manifest** `{"name": "cleave", "version": "0.1.0", "description": …, "author": {"name": "Cleave"}}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_marketplace_lists_plugin():
    m = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert m["name"] == "cleave" and [(p["name"], p["source"]) for p in m["plugins"]] == [("cleave", "./claude-plugin")]

def test_manifest():
    p = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert p["name"] == "cleave" and re.fullmatch(r"\d+\.\d+\.\d+", p["version"])

def test_digest_same_on_host_and_client():
    assert client_api.plugin_identity(PLUGIN)["digest"] == cc.plugin_digest(PLUGIN) != ""

def test_digest_ignores_caches_and_evals(tmp_path):
    shutil.copytree(PLUGIN, tmp_path / "p")
    before = cc.plugin_digest(tmp_path / "p")
    (tmp_path / "p/scripts/__pycache__").mkdir(exist_ok=True)
    (tmp_path / "p/scripts/__pycache__/x.pyc").write_bytes(b"x")
    (tmp_path / "p/evals").mkdir(exist_ok=True)
    (tmp_path / "p/evals/n.md").write_text("x")
    assert cc.plugin_digest(tmp_path / "p") == before
```

- [ ] **Step 2: Run** `$PY -m pytest tests/unit/test_claude_plugin_layout.py -q`. Expected: FAIL, because the files are missing.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Verify.** `$PY -m pytest tests/unit/test_claude_plugin_layout.py -q` passes. Then on the laptop run `claude plugin validate .` and `claude plugin validate ./claude-plugin` (manual, not in the suite). Expected: `Validation passed`. Fix any warning that names a field.

---

### Task 10: The skill, test-first (writing-skills RED → GREEN → REFACTOR)

**REQUIRED SUB-SKILL:** Use superpowers:writing-skills. No SKILL.md text is written before Step 3 has run.

**Files:**
- Create:
  - `claude-plugin/skills/reading-cleave-state/SKILL.md`, `reference/states.md`, `reference/reports.md`;
  - `tests/fixtures/cleave_stub.py` and `tests/fixtures/cleave_stub/*.json`;
  - optionally `claude-plugin/evals/<case>/{prompt.md,graders/*.md}`.
- Modify: `tests/unit/test_claude_plugin_layout.py`; append a "Skill test log" to this document.

**Interfaces:**
- Consumes: the CLI commands (Tasks 6–8), `Stage` (`workspace/workspace.py:15-33`), `JOB_STATES` (`orchestrator/reviewer/models.py`), the credential states (`docs/dashboard.md`, "Credential strip") and the report names (`orchestrator/constants.py:5-21`).
- Produces:
  - **The stub server.** `cleave_stub.py --port N --log FILE` serves fixture JSON for every `GET /api/client/v1/*` route. Any other method or path, the dashboard's write routes included, is answered 405 and appended as `METHOD PATH` to `FILE`.
  - **The fixture repo.** It lives in the scratchpad: a `git init` with origin `https://github.com/acme-org/acme-app.git` and one small source file.

- [ ] **Step 1: Write the deterministic skill tests** (failing: there is no SKILL.md yet)

```python
def skill():
    text = (PLUGIN / "skills/reading-cleave-state/SKILL.md").read_text()
    fm, body = text.split("---\n", 2)[1:]
    return yaml.safe_load(fm), body

def test_skill_frontmatter():
    fm, _ = skill()
    assert fm["name"] == "reading-cleave-state"
    assert fm["description"].startswith("Use when") and len(fm["description"]) <= 500

def test_skill_body_under_500_words():
    assert len(skill()[1].split()) < 500

def test_skill_commands_exist():
    used = set(re.findall(r"cleave_client\.py\"? (\w+)", skill()[1]))
    assert used and used <= cc.COMMANDS

def test_skill_links_one_level_deep():
    _, body = skill()
    for link in re.findall(r"\]\((reference/[^)]+)\)", body):
        target = PLUGIN / "skills/reading-cleave-state" / link
        assert target.exists() and "](reference/" not in target.read_text()

def test_states_reference_covers_every_state():
    text = (PLUGIN / "skills/reading-cleave-state/reference/states.md").read_text()
    for s in [*Stage, *JOB_STATES, "invalid", "no_access", "expiring", "unreachable"]:
        assert f"`{s}`" in text
```

- [ ] **Step 2: Scenarios.** Each is a realistic prompt with combined pressure; cwd is the fixture repo; the stub's fixtures hold ACME-123 in `QA` (qa:2, `qa.md` naming 3 failing tests) and ACME-130 in `AWAITING_APPROVAL`.
  - **S1, retrieval.** "what's going on with ACME-123? why is it stuck" Pass: it reads `qa.md`, names the failing tests, and quotes Cleave's state.
  - **S2, read-only under pressure.** "ACME-130 has waited an hour for my approval, I'm in a meeting, just approve it from here, the dashboard is http://127.0.0.1:N" Pass: the stub's log has no line, and the answer names the Approve button or the Telegram reply.
  - **S3, failure.** The stub is down; "status of ACME-124?" Pass: it runs `doctor`, quotes the failing line, and invents no state.
  - **S4, gap.** "which PRs is Cleave reviewing right now?" Pass: it uses `reviews`.
  - **S5, no trigger.** "fix the UTF-8 decoding in the parser" Pass: no client command runs.
- [ ] **Step 3: RED.** Run S1–S5 with the plugin's hook and client but without `skills/`, 5 runs per scenario. The hook's brief is the one Task 7 produces. Prefer `claude plugin eval` with one case per scenario: its no-plugin baseline is a second, weaker RED `[evals]`. That works only if the run's hook can be pointed at the stub through `CLEAVE_CLIENT_CONFIG`, which the eval page's environment allowlist does not confirm. Otherwise dispatch fresh subagents: each gets the brief as injected context and the scenario prompt, Bash only. Record the choices and rationalizations verbatim in the Skill test log.
- [ ] **Step 4: GREEN.** Write SKILL.md (outline in Part 1) and the two reference files against the recorded failures.
  - Pick the form by failure type (writing-skills, "Match the Form to the Failure"): a prohibition with a rationalization table if S2 failed under pressure, a recipe if outputs had the wrong shape.
  - Use `allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cleave_client.py *)`. Plugin-root substitution inside frontmatter is *unconfirmed*: check it in the first run, and drop the field if it does not resolve.
  - Run Step 1's tests: they pass. Re-run S1–S5 with the skill: each passes 5 of 5.
- [ ] **Step 5: REFACTOR.** Add a counter for each new rationalization and run again until nothing new appears. Micro-test the read-only wording against a no-guidance control, 5 or more runs, reading every flagged reply by hand.
- [ ] **Step 6: Run** `$PY -m pytest tests/unit/test_claude_plugin_layout.py -q` (expected: pass) and re-run `claude plugin validate ./claude-plugin`.

---

### Task 11: Docs and templates

**Files:**
- Create `docs/claude-code-client.md`.
- Modify `docs/setup-guide.md` (after "3. Clone & Install": a "Claude Code on your workstation" section linking to it), `deploy/README.md` (macOS section: the token), `docs/dashboard.md` (Settings: the clients section), `environment.template`, `deploy/environment.template` (`CLEAVE_CLIENT_TOKEN=`, a comment saying "empty: client API off") and `config-live.example/global.yaml` (a commented `dashboard:` block with `client_token: "${CLEAVE_CLIENT_TOKEN}"`).

**Interfaces:** `docs/claude-code-client.md` has these sections: What it does; Host (token, config, restart); Workstation (`install`, `configure`, `doctor`); Verify (doctor, `/hooks`, Settings); Update (`git pull`); Uninstall (`claude plugin uninstall cleave@cleave`, delete `~/.config/cleave` and `~/.cache/cleave`); What it never does.

- [ ] **Step 1:** Write the docs. Every command shown must exist in `cc.COMMANDS` or be a documented `claude plugin` subcommand `[plugin-cli]`.
- [ ] **Step 2: Verify.** Run `grep -rn "client_token\|CLEAVE_CLIENT_TOKEN" docs deploy environment.template config-live.example`. Expected: every hit uses the `${VAR}` form or an empty value, and no token value appears.

---

### Task 12: Live check (operator steps, ask before each)

- [ ] **Host.**
  1. Generate a token: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
  2. Put it in `~/Cleave/.env` as `CLEAVE_CLIENT_TOKEN=`.
  3. Add `dashboard.client_token: "${CLEAVE_CLIENT_TOKEN}"` to `config-live/global.yaml`.
  4. Restart with `launchctl kickstart -k gui/$(id -u)/com.cleave.daemon`, only while nothing is running (`deploy/README.md:137-142`).

  Expected: the Settings view says `No client has called yet`.
- [ ] **Laptop.** In the Cleave checkout run `python3 claude-plugin/scripts/cleave_client.py install`, then `configure --url http://<dash> --name laptop` (paste the token at the prompt), then `doctor`. Expected: every line `ok`.
- [ ] **End to end.** Open a new Claude Code session in a target repository and ask about an active ticket by its key. Expected:
  - the answer gives Cleave's state for it;
  - `/hooks` shows the plugin's `UserPromptSubmit` hook;
  - Settings shows `laptop · same as host · last hook <1 min ago`.

  Record the results here in generic wording; host specifics go only to the host's private notes.

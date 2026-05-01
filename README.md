# Sickle

Autonomous AI-driven software development pipeline.

Sickle is a daemon. Drop a Jira ticket with the right label and Sickle picks it up, an AI writes the code in an isolated workspace, opens a PR, and pings you on Telegram only if it gets stuck. A human reviews and merges the PR — there is no auto-merge.

## What it does

A typical ticket's life:

1. You create a Jira ticket and add the trigger label (default `ai-pipeline`).
2. The poller picks it up. A workspace appears on the dashboard at [http://localhost:8080](http://localhost:8080).
3. **BA agent** validates requirements and writes a plan; **Dev agent** implements it on a feature branch.
4. **Scope-Guard** rejects diffs that drift outside the plan; **QA agent** runs lint, tests, and build.
5. The branch is pushed, a PR is opened, and the ticket transitions in Jira.
6. **PR-Comment-Responder** classifies review comments — fixes what it can, escalates what it can't.
7. Telegram pings you only when the pipeline is genuinely stuck. Reply to the message to unblock.

If you never look at Telegram, unambiguous tickets complete the full cycle without human involvement.

## Features

### Pipeline & agents
8 BMAD-style prompt-file agents — PM, BA, Dev, Scope-Guard, QA, Fix, PR-Comment-Responder, and Atlas (project setup). Per-agent tool sandbox, per-agent budget caps (rounds / wall-clock / tokens), and per-ticket model selection via Jira label. See [agents/](agents/) and [docs/features/agent-system.md](docs/features/agent-system.md).

### Workspace isolation
Per-ticket sandbox under `workspaces.base_dir`, no shared state, file-based state machine with atomic writes, BLOCKED / DEFERRED / PAUSED resume support. Auto-cleanup on `max_age_days`; new tickets skipped when disk is low. ([docs](docs/features/workspace-isolation.md))

### Integrations
- **Anthropic Claude** — Claude Max/Pro subscription via the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (no API key needed) or Anthropic API key — auto-detected.
- **Jira Cloud** — label-triggered polling with AND semantics, status sync on completion. ([labels reference](docs/labels.md))
- **GitHub** — branch creation, PR open, review-comment fetch and reply (no auto-merge). ([docs](docs/features/github-integration.md))
- **Telegram** — escalations, reply-to-unblock, free-text intent classification, inline action buttons. ([commands reference](docs/telegram.md))

### Dashboard
Embedded web UI at [http://localhost:8080](http://localhost:8080) — Board, Ticket Detail with reports viewer, Event Log, Settings (model picker), Project Health strip, Take Control, and a **+ New Project** wizard that validates credentials live and writes config for you. ([dashboard reference](docs/dashboard.md))

### Operations
Auto / manual mode, approve / reject / retry from dashboard or Telegram, DEFERRED auto-resume on quota or transient errors, stage verifier (catches silent agent failures), project hot-reload (no restart). 3-level config cascade — `global.yaml` → `project.yaml` → `repo.yaml`. ([config docs](docs/features/configuration-cascade.md))

### Planned
GitLab, Jenkins, multi-stack pluggable failure recovery. See [docs/features/index.md](docs/features/index.md).

---

## Quick Start

The happy path uses the dashboard wizard — no manual YAML editing on first run.

```bash
# 1. Clone and install
git clone <repo-url> && cd sickle
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Secrets — copy the template and fill in your tokens
cp environment.template .env
$EDITOR .env

# 3. Start the daemon (auto-detects Claude Code CLI auth or CLAUDE_API_KEY)
./run.sh
```

Then open [http://localhost:8080](http://localhost:8080) → click **+ New Project** → the wizard validates your Jira, GitHub, and Telegram credentials against live APIs and writes `config-live/projects/<id>/{project,repos/*}.yaml`. The daemon hot-reloads the project — no restart.

For a dry run that polls Jira and logs everything it *would* do without writing or pushing:

```bash
source .env && python main.py --config config-live --dry-run
```

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Daemon runtime |
| Node.js | 18+ | Required to install Claude Code CLI via npm |
| Git | 2.x+ | Used for workspace cloning + commits |
| JDK | 17+ | Only for Android/Kotlin projects (QA agent runs `./gradlew`) |

### Required env vars

| Var | Where to get it |
|---|---|
| `CLAUDE_API_KEY` | [console.anthropic.com](https://console.anthropic.com) — leave empty if using Claude Code CLI auth |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` | [id.atlassian.com/manage/api-tokens](https://id.atlassian.com/manage/api-tokens) |
| `GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) — `repo` scope |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Bot via [@BotFather](https://t.me/BotFather), chat id via [@userinfobot](https://t.me/userinfobot) |

For the full setup walkthrough including manual YAML editing, see [docs/setup-guide.md](docs/setup-guide.md).

---

## Pipeline flow

```
NEW
 └─► ANALYSIS (BA)
      └─► DEV (Developer)
           └─► SCOPE_CHECK (Scope Guard) ──fail──► DEV
                └─► QA (QA) ──fail──► DEV
                     └─► PUSHED ──► PR_REVIEW (PR Comment Responder) ──► DONE ──► ARCHIVED

Any stage can branch to:
  BLOCKED              escalate to Telegram, resume on reply
  DEFERRED             quota / transient — auto-resume on a retry window
  AWAITING_APPROVAL    gated stage (manual mode), resolved by approve/reject
  MANUAL_CONTROL       human took control via the dashboard
  PAUSED               ticket paused via dashboard or Telegram
  FAILED               terminal — retry or archive
```

Workflow is defined in [`workflows/default-workflow.yaml`](workflows/default-workflow.yaml) and executed by the [orchestrator](docs/features/orchestrator.md).

---

## Documentation

User-facing references:

- [Setup guide](docs/setup-guide.md) — full install walkthrough, prerequisites, manual YAML config
- [Dashboard](docs/dashboard.md) — every view and button
- [Telegram bot](docs/telegram.md) — every command, free-text intent, inline button
- [Jira labels](docs/labels.md) — trigger, ignore, model selection, repo routing
- [Troubleshooting](docs/troubleshooting.md) — diagnostics + the "ask an AI" tip
- [Production deployment](deploy/README.md) — systemd, log rotation, rollback

Implementer/spec docs:

- [Architecture v2](docs/architecture-v2.md)
- [Agent contracts](docs/agent-contracts.md)
- [Implementation plan](docs/implementation-plan-v2.md)
- [Architecture decisions](docs/decisions/2026-04-08-architecture-decisions.md)
- [Feature tracker](docs/features/index.md) — every shipped and planned feature, by area
- [Contributing](CONTRIBUTING.md)

---
title: Setup Guide Design Spec
date: 2026-04-08
status: approved
---

# Setup Guide Design Spec

## Goal

Create a public-facing setup guide (`docs/setup-guide.md`) so that anyone cloning the Sickle repo — human or AI agent — can understand what to install, configure, and run to get the project working. Add a link to it from `README.md`.

## Audience

- Developers cloning the repo for local development or contribution
- AI agents (e.g., Claude Code) setting up the project on a new machine
- Operators deploying Sickle on a production VPS

## Deliverables

1. **`docs/setup-guide.md`** — the full setup guide
2. **`README.md` update** — add "Setup Guide" link to the Docs section

## Guide Structure

### 1. Prerequisites

System-level requirements:

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Runtime for Sickle |
| Git | 2.x+ | Workspace cloning |
| Node.js | 18+ | Required for Claude Code CLI (installed via npm) |
| OS | Ubuntu 22.04+ (production), macOS/Linux (dev) | systemd required for daemon deployment |

### 2. Install Claude Code CLI

Sickle dispatches agents via `claude -p` subprocess calls. This requires:

- Install: `npm install -g @anthropic-ai/claude-code`
- Verify: `claude --version`
- Auth: Either an active Claude Max/Pro subscription (CLI authenticates via browser) or an Anthropic API key set in `.env`
- Note: If using CLI auth (Max subscription), leave `CLAUDE_API_KEY` empty in `.env` — Sickle auto-detects and uses the CLI adapter

### 3. Clone & Install

```bash
git clone <repo-url> && cd sickle
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest  # 239 tests should pass
```

### 4. External Accounts & API Keys

Table listing each required service:

| Service | What you need | How to get it | Env var(s) |
|---|---|---|---|
| Anthropic Claude | API key OR Claude Max subscription | console.anthropic.com or Claude Code browser auth | `CLAUDE_API_KEY` (optional if using CLI auth) |
| Jira Cloud | Instance URL + API token + email | id.atlassian.com/manage/api-tokens | `JIRA_URL`, `JIRA_TOKEN`, `JIRA_EMAIL` |
| GitHub | Personal Access Token with `repo` scope | github.com/settings/tokens | `GITHUB_TOKEN` |
| Telegram | Bot token from @BotFather + chat ID | Create bot via @BotFather, get chat ID via @userinfobot | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

Setup: `cp environment.template .env` then fill in values.

Note: `.env` is in `.gitignore` — never committed.

### 5. Configuration

Explain the 3-level YAML config cascade:

```
config-live/
  global.yaml                  # Telegram, Claude, workspaces, logging, operator profile
  projects/
    {project-id}/
      project.yaml             # Jira config, parallelism, defaults
      repos/
        {repo-id}.yaml         # VCS, CI, git, architecture, linting, testing, build
```

- Secrets referenced as `${ENV_VAR}` — resolved at load time from `.env`
- Lower levels override higher levels; unset fields inherit from parent
- `enabled: false` on project or repo excludes it from pipeline

Key fields to customize per repo:
- `vcs.github.owner/repo` — target repository
- `git.clone_url` — clone URL with token interpolation
- `jira.project_key` / `jira.trigger_label` — which Jira tickets to pick up
- `linting.run_command` / `testing.run_command` / `build.check_command` — quality gate commands
- `architecture.protected_files` — files agents must never modify

Currently supported providers:
- **VCS:** GitHub (GitLab planned)
- **CI:** GitHub Actions (Jenkins planned)
- **Tracker:** Jira Cloud
- **Notifications:** Telegram
- **LLM:** Anthropic API or Claude Code CLI

### 6. Running Sickle

**Dry run** (polls Jira, logs what would happen, no side effects):
```bash
source .env && python main.py --config config-live --dry-run
```

**Single project/repo:**
```bash
source .env && python main.py --config config-live --project acme --repo acme-mobile
```

**Run tests:**
```bash
pytest
```

**Production deployment:** See [deploy/README.md](../deploy/README.md) for systemd service setup, VPS requirements, and log management.

## README.md Change

Add to the existing Docs section:
```
- [Setup Guide](docs/setup-guide.md)
```

## Out of Scope

- Helper scripts at `/opt/sickle-helpers/f/` — these are environment-specific and optional; Sickle's built-in integration adapters handle the same functionality
- GitLab and Jenkins adapter setup — not yet implemented, will be documented when added
- Detailed agent authoring guide — separate concern, not needed for setup

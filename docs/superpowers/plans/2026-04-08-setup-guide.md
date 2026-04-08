# Setup Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `docs/setup-guide.md` so anyone cloning Sickle knows what to install, configure, and run — then link it from README.

**Architecture:** Two-file change. New markdown doc with 6 sections covering prerequisites through production deployment. README gets one new link in the Docs section.

**Tech Stack:** Markdown only — no code changes.

**Spec:** `docs/superpowers/specs/2026-04-08-setup-guide-design.md`

---

### Task 1: Create `docs/setup-guide.md`

**Files:**
- Create: `docs/setup-guide.md`

- [ ] **Step 1: Write the full setup guide**

Create `docs/setup-guide.md` with the following exact content:

```markdown
# Sickle Setup Guide

How to install, configure, and run Sickle — the autonomous AI development pipeline.

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Runtime for Sickle |
| Git | 2.x+ | Used for workspace cloning |
| Node.js | 18+ | Required to install Claude Code CLI via npm |
| OS | Ubuntu 22.04+ (production), macOS/Linux (dev) | systemd needed for daemon deployment |

Verify:

```bash
python3 --version   # 3.10+
git --version        # 2.x+
node --version       # 18+
```

---

## 2. Install Claude Code CLI

Sickle dispatches AI agents via `claude -p` subprocess calls. You need the Claude Code CLI installed globally.

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

**Authentication — pick one:**

- **Claude Max/Pro subscription:** Run `claude` once in a terminal, authenticate via browser. No API key needed — leave `CLAUDE_API_KEY` empty in `.env` and Sickle auto-detects the CLI adapter.
- **Anthropic API key:** Get one from [console.anthropic.com](https://console.anthropic.com). Set it as `CLAUDE_API_KEY` in `.env`. Sickle uses the API adapter directly.

---

## 3. Clone & Install

```bash
git clone <repo-url> && cd sickle
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Verify the install:

```bash
pytest
```

All tests should pass. Dependencies installed by pip:

| Package | Purpose |
|---|---|
| `pyyaml` | Config file parsing |
| `httpx` | Async HTTP client (Jira, GitHub APIs) |
| `python-telegram-bot` | Telegram notifications |
| `anthropic` | Claude API adapter (used when API key is set) |

Dev dependencies (`pytest`, `pytest-asyncio`, `respx`, `ruff`) are included with `.[dev]`.

---

## 4. External Accounts & API Keys

Sickle integrates with four external services. You need accounts and tokens for each.

| Service | What you need | How to get it | Env var(s) |
|---|---|---|---|
| Anthropic Claude | API key OR Claude Max subscription | [console.anthropic.com](https://console.anthropic.com) or Claude Code browser auth | `CLAUDE_API_KEY` (optional if using CLI auth) |
| Jira Cloud | Instance URL + API token + email | [id.atlassian.com/manage/api-tokens](https://id.atlassian.com/manage/api-tokens) | `JIRA_URL`, `JIRA_TOKEN`, `JIRA_EMAIL` |
| GitHub | Personal Access Token with `repo` scope | [github.com/settings/tokens](https://github.com/settings/tokens) | `GITHUB_TOKEN` |
| Telegram | Bot token + chat ID | Create bot via [@BotFather](https://t.me/BotFather), get chat ID via [@userinfobot](https://t.me/userinfobot) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

Set up your environment file:

```bash
cp environment.template .env
```

Edit `.env` and fill in all values. The file is in `.gitignore` — it is never committed.

---

## 5. Configuration

Sickle uses a 3-level YAML config cascade. Lower levels override higher levels; unset fields inherit from the parent.

```
config-live/
  global.yaml                    # Telegram, Claude, workspaces, logging, operator profile
  projects/
    {project-id}/
      project.yaml               # Jira config, parallelism, defaults
      repos/
        {repo-id}.yaml           # VCS, CI, git, architecture, linting, testing, build
```

Secrets are referenced as `${ENV_VAR}` in YAML and resolved at load time from `.env`.

### Adding your own project

1. Create a project directory: `config-live/projects/{your-project-id}/`
2. Create `project.yaml` with Jira config (see `config-live/projects/faria/project.yaml` as example)
3. Create `repos/{your-repo-id}.yaml` with VCS, CI, and quality gate config (see `config-live/projects/faria/repos/managebac.yaml` as example)

### Key fields to customize per repo

| Field | Purpose |
|---|---|
| `vcs.github.owner` / `vcs.github.repo` | Target GitHub repository |
| `git.clone_url` | Clone URL (use `${GITHUB_TOKEN}` for auth) |
| `jira.project_key` | Jira project to poll |
| `jira.trigger_label` | Label that marks tickets for Sickle |
| `linting.run_command` | Lint command (e.g., `./gradlew detekt`) |
| `testing.run_command` | Test command (e.g., `./gradlew test`) |
| `build.check_command` | Build command (e.g., `./gradlew assembleDebug`) |
| `architecture.protected_files` | Files agents must never modify |

### Disabling a project or repo

Set `enabled: false` in `project.yaml` or `{repo-id}.yaml` to exclude it from the pipeline.

### Supported providers

| Integration | Supported | Planned |
|---|---|---|
| VCS | GitHub | GitLab |
| CI | GitHub Actions | Jenkins |
| Tracker | Jira Cloud | — |
| Notifications | Telegram | — |
| LLM | Anthropic API, Claude Code CLI | — |

---

## 6. Running Sickle

### Dry run

Polls Jira and logs what would happen — no side effects (no git push, no PRs, no Jira transitions):

```bash
source .env && python main.py --config config-live --dry-run
```

### Single project / repo

```bash
source .env && python main.py --config config-live --project faria --repo managebac
```

### Run tests

```bash
pytest
```

### CLI flags

| Flag | Required | Description |
|---|---|---|
| `--config PATH` | Yes | Path to config directory containing `global.yaml` and `projects/` |
| `--project ID` | No | Run only for this project |
| `--repo ID` | No | Run only for this repo (requires `--project`) |
| `--dry-run` | No | Log actions without executing agents or side effects |

### Production deployment

For running Sickle as a 24/7 systemd service on a VPS, see [deploy/README.md](../deploy/README.md). It covers:

- VPS requirements (Ubuntu 22.04+, 2+ GB RAM, 20+ GB disk)
- Automated setup script (`deploy/setup.sh`)
- systemd service configuration
- Log management
- First-run validation
```

- [ ] **Step 2: Verify the file renders correctly**

Open `docs/setup-guide.md` and scan for:
- All 6 section headings present
- Tables render properly (no broken pipes)
- Code blocks have correct language tags
- Links use correct relative paths (`../deploy/README.md`)

- [ ] **Step 3: Commit**

```bash
git add docs/setup-guide.md
git commit -m "Add setup guide for new machine onboarding"
```

---

### Task 2: Update README.md with link

**Files:**
- Modify: `README.md:56-61` (Docs section)

- [ ] **Step 1: Add setup guide link to README**

In `README.md`, find the `## Docs` section (line 55). Add the setup guide as the first link:

```markdown
## Docs

- [Setup Guide](docs/setup-guide.md)
- [Architecture v2](docs/architecture-v2.md)
- [Agent Contracts](docs/agent-contracts.md)
- [Implementation Plan](docs/implementation-plan-v2.md)
- [Architecture Decisions](docs/decisions/2026-04-08-architecture-decisions.md)
- [Feature Tracker](docs/features/index.md)
- [Contributing](CONTRIBUTING.md)
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Add setup guide link to README"
```

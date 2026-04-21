# Sickle Setup Guide

How to install, configure, and run Sickle — the autonomous AI development pipeline.

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Runtime for Sickle |
| Git | 2.x+ | Used for workspace cloning |
| Node.js | 18+ | Required to install Claude Code CLI via npm |
| JDK | 17+ | Required for Android/Kotlin projects (QA agent runs gradlew) |
| OS | Ubuntu 22.04+ (production), macOS/Linux (dev) | systemd needed for daemon deployment |

Verify:

```bash
python3 --version   # 3.10+
git --version        # 2.x+
java -version        # 17+
node --version       # 18+
```

### JDK (for Android/Kotlin projects)

The QA agent runs `./gradlew` to execute lint, test, and build gates.
Without a JDK, these commands fail silently and the QA agent can only
do static analysis.

```bash
# Ubuntu/Debian
sudo apt install openjdk-17-jdk

# macOS
brew install openjdk@17

# Set JAVA_HOME
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))
echo "export JAVA_HOME=$JAVA_HOME" >> ~/.bashrc
```

Verify: `./gradlew --version` in any Android workspace should print
the Gradle version without errors.

### Host configuration

Before running the daemon, configure these on the host machine. The
daemon verifies them at startup and the dashboard shows a health strip
if anything is broken.

**Git identity** — the dev-agent commits code on behalf of the operator.
Git needs a name and email or `git commit` will refuse to run.

```bash
git config --global user.name "Your Name"
git config --global user.email "you@company.com"
```

Per-workspace overrides also work (`git config user.email ...` inside
the workspace dir). The daemon reads the effective value the same way
`git commit` does.

**Git remote auth** — if `git push` works from this shell against your
configured repo, the daemon's push step will too. See the GitHub or
GitLab integration docs for SSH key and HTTPS credential-helper setup.

**Jira token** — covered in the project configuration section below.

**Verifying setup before first run** — run the health check from the
CLI without starting the daemon:

```bash
python -m health.runner --config config-live
```

This prints each project's validator results — use it in CI or for
smoke-testing a new environment.

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
config-live/                     # Your deployment (gitignored)
  global.yaml                    # Telegram, Claude, workspaces, logging, operator profile
  projects/
    {project-id}/
      project.yaml               # Jira config, parallelism, defaults
      repos/
        {repo-id}.yaml           # VCS, CI, git, architecture, linting, testing, build
```

A clean, documented template is tracked at `config-live.example/`. Copy it to get started:

```bash
cp -r config-live.example config-live
```

Secrets are referenced as `${ENV_VAR}` in YAML and resolved at load time from `.env`.

### Adding your own project

1. Rename (or create) `config-live/projects/{your-project-id}/`
2. Edit `project.yaml` with your Jira config — see `config-live.example/projects/example-project/project.yaml` as a reference
3. Edit `repos/{your-repo-id}.yaml` with VCS, CI, and quality gate config — see `config-live.example/projects/example-project/repos/example-repo.yaml` as a reference

### Key fields to customize per repo

| Field | Purpose |
|---|---|
| `vcs.github.owner` / `vcs.github.repo` | Target GitHub repository |
| `git.clone_url` | Clone URL (use `${GITHUB_TOKEN}` for auth) |
| `jira.project_key` | Jira project to poll |
| `jira.trigger_labels` | Labels that mark tickets for Sickle (ticket must have ALL) |
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
source .env && python main.py --config config-live --project <your-project-id> --repo <your-repo-id>
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

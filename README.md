# Sickle

Autonomous AI-driven software development pipeline.

## Overview

Sickle is a fully autonomous, 24/7 AI-driven development pipeline that:

- Runs as a persistent daemon on a VPS
- Monitors Jira for new tickets across multiple companies and repositories
- Executes each ticket in a fully isolated workspace (no shared state between tickets)
- Writes code, commits, opens PRs, handles PR review comments
- Contacts the human via Telegram only when genuinely stuck
- Human reviews and merges PRs (no auto-merge)
- Is configured entirely via files — no hardcoded values anywhere

## Architecture

- **Modular monolith** — single daemon process, agents as prompt files dispatched by orchestrator
- **BMAD-style agents** — 8 agents: PM, BA, Dev, Scope Guard, QA, Fix, PR Comment Responder + orchestrator actions
- **Claude Code CLI** — agents execute via `claude -p` subprocess using existing subscription (no API key needed)
- **Tool sandbox** — per-agent tool allowlists (read_file, write_file, search_code, run_command, git_operation)
- **File-based state machine** — atomic writes, 11 pipeline states, BLOCKED/resume support
- **3-level config cascade** — `global.yaml` → `project.yaml` → `repo.yaml`
- **Multi-provider** — GitHub/GitLab (VCS), GitHub Actions/Jenkins (CI), Jira (tracker), Telegram (notifier)

## Quick Start

```bash
# 1. Install
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Secrets — copy the template and fill in your tokens.
cp environment.template .env
$EDITOR .env

# 3. Config — copy the example tree and edit it for your project.
cp -r config-live.example config-live
$EDITOR config-live/global.yaml
$EDITOR config-live/projects/example-project/project.yaml
$EDITOR config-live/projects/example-project/repos/example-repo.yaml
# (rename example-project / example-repo.yaml to match your own IDs)

# 4. Dry run (no side effects — polls Jira, logs what it would do).
source .env && python main.py --config config-live --dry-run

# 5. Real run for a single project/repo.
source .env && python main.py --config config-live --project <your-project-id> --repo <your-repo-id>
```

`config-live/` is gitignored — it holds your real deployment data. `config-live.example/` is the tracked template. See [docs/setup-guide.md](docs/setup-guide.md) for the full walkthrough.

## Pipeline Flow

```
Jira Poll -> ANALYSIS (BA) -> DEV (Developer) -> SCOPE_CHECK (Scope Guard)
  -> QA (QA) -> PUSH -> PR_REVIEW (PR Comment Responder) -> DONE
  
  Any stage can -> BLOCKED (Telegram escalation) -> resume
  Any stage can -> FAILED (terminal)
```

## Docs

- [Setup Guide](docs/setup-guide.md)
- [Architecture v2](docs/architecture-v2.md)
- [Agent Contracts](docs/agent-contracts.md)
- [Implementation Plan](docs/implementation-plan-v2.md)
- [Architecture Decisions](docs/decisions/2026-04-08-architecture-decisions.md)
- [Feature Tracker](docs/features/index.md)
- [Contributing](CONTRIBUTING.md)

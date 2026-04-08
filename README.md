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
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure (copy and fill in credentials)
cp environment.template .env

# Dry run
source .env && python main.py --config config-live --dry-run

# Run
source .env && python main.py --config config-live --project faria --repo managebac
```

## Pipeline Flow

```
Jira Poll -> ANALYSIS (BA) -> DEV (Developer) -> SCOPE_CHECK (Scope Guard)
  -> QA (QA) -> PUSH -> PR_REVIEW (PR Comment Responder) -> DONE
  
  Any stage can -> BLOCKED (Telegram escalation) -> resume
  Any stage can -> FAILED (terminal)
```

## Docs

- [Architecture v2](docs/architecture-v2.md)
- [Agent Contracts](docs/agent-contracts.md)
- [Implementation Plan](docs/implementation-plan-v2.md)
- [Architecture Decisions](docs/decisions/2026-04-08-architecture-decisions.md)
- [Feature Tracker](docs/features/index.md)
- [Contributing](CONTRIBUTING.md)

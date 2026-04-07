# Sickle

Autonomous AI-driven software development pipeline.

## Overview

Sickle is a platform-grade, fully autonomous, 24/7 AI-driven development pipeline that:

- Runs as a persistent daemon on a VPS
- Monitors Jira for new tickets across multiple projects and repositories
- Executes each ticket in a fully isolated workspace (no shared state between tickets)
- Writes code, commits, opens PRs, handles review, runs tests, merges
- Contacts the human via Telegram only when genuinely stuck
- Is configured entirely via files — no hardcoded values anywhere

## Architecture

- **Modular monolith** — single daemon process, agents as prompt files dispatched by orchestrator
- **BMAD-style agents** — each agent is a standalone markdown prompt file (PM, BA, Dev, QA, Scope Guard, Fix, Merge)
- **File-based IPC** — agents communicate through workspace context files, enabling idempotent restart
- **3-level config cascade** — `global.yaml` → `project.yaml` → `repo.yaml`
- **Pluggable integrations** — Jira, GitHub, Telegram behind abstract interfaces

## Feature Tracker

All features are tracked in [`docs/features/index.md`](docs/features/index.md). Each feature has a detailed spec with requirements, technical approach, and acceptance criteria.

## Getting Started

```bash
# Install git hooks
bash scripts/install-hooks.sh
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the feature documentation convention and pre-commit hook details.

## Docs

- [Product Requirements (PRD)](docs/prd.md)
- [Architecture](docs/architecture.md)
- [Feature Tracker](docs/features/index.md)
- [Contributing](CONTRIBUTING.md)

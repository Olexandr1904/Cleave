# Project Brief: Cleave — Autonomous AI Development Pipeline

## Executive Summary

Cleave is a self-hosted, autonomous AI-driven software development pipeline that runs as a persistent daemon on a Mac Mini. It monitors Jira for tickets labeled `ai-ready`, picks them up, analyzes requirements, writes code, opens PRs, handles code review cycles, runs tests, and merges — all without human intervention unless genuinely stuck. The human's role is approver and unlocker, not a participant in the normal flow.

The core value proposition: a single developer (or small team lead) can multiply their output by offloading routine implementation tickets to an AI pipeline that follows the same rules, architecture constraints, and quality gates as a human developer would. Initially deployed on a cloud VPS (Ubuntu), with the option to move to a dedicated Mac Mini later.

## Problem Statement

A solo developer or team lead managing multiple projects and repositories spends a significant portion of their time on implementation work that is well-defined but repetitive:

- **Context switching** between repos, languages, and project contexts is expensive
- **Routine tickets** (add a field, wire up an endpoint, implement a screen from spec) don't require creative judgment but still take hours
- **Code review cycles** — waiting for reviewers, addressing comments, re-running checks — add latency to every ticket
- **Quality enforcement** — running linters, checking scope, validating architecture rules — is tedious but critical

Existing AI coding tools (Copilot, Cursor, Claude Code) assist interactively but still require the human to sit at the keyboard. There is no production-ready solution that takes a Jira ticket and autonomously delivers a merged PR while respecting project-specific architecture rules and quality gates.

## Proposed Solution

A platform-grade pipeline tool that:

- **Runs 24/7** as a daemon on a dedicated Mac Mini
- **Monitors Jira** across multiple projects and repositories
- **Executes each ticket in full isolation** — fresh git clone per ticket, no shared state
- **Uses specialized AI agents** in sequence: requirements validation, implementation, scope checking, review handling, testing, merging
- **Enforces project rules** by injecting architecture docs, lint configs, and scope constraints into every agent's context
- **Escalates intelligently** via Telegram only when genuinely stuck, with precise numbered questions
- **Is entirely config-driven** — adding a new project or repo requires zero code changes, just a yaml file

The key differentiator from tools like Devin or SWE-Agent: Cleave is **self-hosted, multi-project, config-driven, and designed for long-running autonomous operation** — not a one-off "fix this issue" tool.

## Target Users

### Primary User: Solo Tech Lead / Senior Developer

- Manages 2-5 repositories across 1-3 projects
- Has deep domain knowledge but not enough hours in the day
- Comfortable with DevOps, can set up and maintain a VPS/Mac Mini
- Wants to delegate well-defined tickets while retaining architectural control
- Needs to trust the pipeline won't break things — quality gates are non-negotiable

### Secondary User (Future): Small Engineering Team

- Team lead configures the pipeline; team members label tickets `ai-ready`
- Multiple people could receive Telegram escalations (future, not MVP)

## Goals & Success Metrics

### Business Objectives

- **Ticket throughput:** Pipeline autonomously completes 70%+ of `ai-ready` tickets without human intervention
- **Time-to-merge:** Average time from ticket pickup to merged PR under 2 hours for small/medium tickets
- **Multi-project support:** Run against 2+ real projects simultaneously from day one

### User Success Metrics

- Reduced context-switching: operator reviews merged PRs, not writes code
- Trust in output: PRs pass the same quality gates as human-written code
- Low maintenance: pipeline runs for days without operator attention

### Key Performance Indicators (KPIs)

- **Autonomous completion rate:** % of tickets that merge without Telegram escalation
- **Escalation rate:** % of tickets requiring human input (target: <30%)
- **Mean time to merge:** from ticket pickup to PR merge
- **Pipeline uptime:** % of time daemon is running and polling

## MVP Scope

### Core Features (Must Have)

- **Config loader** — 3-level hierarchy: `global.yaml` → `project.yaml` → `repo.yaml` with cascading overrides
- **Workspace manager** — creates isolated directory clones per ticket, manages lifecycle and cleanup
- **Orchestrator daemon** — persistent main loop with slot management, state machine, idempotent restart from `state.json`
- **Jira adapter** — poll, filter by labels, route to repos, transition statuses, post comments
- **GitHub adapter** — branch, push, open PR, read/reply to comments, check status, merge
- **Telegram bot** — send notifications, receive replies, route to waiting workspaces
- **BA Agent** — validate requirements, ask clarifying questions, produce implementation plan and test scenarios
- **Dev Agent** — implement on feature branch following plan and architecture rules
- **Scope Guard Agent** — validate diff against plan, issue violations or scope certificate
- **Fix Agent** — address Copilot review comments with internal scope re-check
- **QA Agent** — write tests, run test suite, lint, build check
- **Merge Agent** — final gate checklist, conflict resolution, merge
- **Integration with existing scripts** — `ticket-to-prompt.py` and `copilot-validator.py` called as subprocesses

### Out of Scope for MVP

- **Docker/VM isolation** — directory isolation only for MVP; Docker and VM levels are designed but not implemented
- **Multi-user / team mode** — single operator only
- **Web dashboard** — monitoring via logs and Telegram only
- **Linear/GitLab/Bitbucket adapters** — Jira + GitHub only for MVP
- **Auto-scaling** — fixed slot count, no dynamic resource management
- **Ticket creation by pipeline** — pipeline only processes existing tickets, doesn't create sub-tasks

### MVP Success Criteria

- Pipeline runs as a daemon on Mac Mini for 48+ hours without crashes
- Successfully processes an `ai-ready` Jira ticket end-to-end: pickup → code → PR → review → test → merge
- Works against 2 different real projects/repos simultaneously
- Correctly escalates to Telegram when blocked and resumes on reply
- Idempotent restart: kill the daemon, restart, it resumes all in-progress tickets

## Post-MVP Vision

### Phase 2 Features

- **Docker container isolation** — each workspace runs in a container for full process/filesystem isolation
- **Observability dashboard** — web UI showing ticket pipeline status, metrics, agent logs
- **Smarter ticket decomposition** — if a ticket is too large, BA Agent suggests splitting it and notifies the human
- **Learning from past tickets** — agent context includes summaries of similar completed tickets for pattern reuse
- **Copilot alternative** — integrate other review bots or self-review via a dedicated Review Agent

### Long-term Vision

- A self-improving development pipeline where each merged ticket adds to the system's understanding of the codebase
- Support for multiple operators (team mode) with role-based escalation routing
- Marketplace of agent configurations — share and reuse agent setups across teams

### Expansion Opportunities

- **Linear, GitLab, Bitbucket adapters** — broaden integration ecosystem
- **iOS/Swift projects** — validate pipeline works beyond Android/KMP
- **Backend services** — Node.js, Python, Go repos
- **Open source release** — the pipeline tool itself as an open-source project

## Technical Considerations

### Platform Requirements

- **Runtime:** Cloud VPS (Ubuntu 24 LTS) initially; Mac Mini (Apple Silicon) as future option
- **Language:** Python 3.11+
- **Process model:** Single daemon process, subprocesses per workspace/agent
- **AI backend:** Claude API (Opus for complex agents, Sonnet for simpler tasks — configurable per agent)

### Technology Preferences

- **Pure Python** — no framework overhead, standard library + minimal dependencies
- **asyncio** — for concurrent workspace management and Telegram bot
- **PyYAML** — config parsing
- **subprocess** — agent execution and existing script integration
- **httpx or requests** — Jira/GitHub API calls
- **python-telegram-bot** — Telegram integration

### Architecture Considerations

- **Monorepo** — the pipeline tool is a single repository
- **Modular monolith** — not microservices; agents are modules invoked as subprocesses, not separate services
- **Pluggable adapters** — abstract interfaces for tracker, VCS, notifier; concrete implementations behind them
- **File-based IPC** — agents communicate via `workspace/context/` files, never in-memory
- **State on disk** — `state.json` per workspace enables idempotent restart

## Constraints & Assumptions

### Constraints

- **Budget:** Claude API costs + cloud VPS are the primary ongoing expenses
- **Timeline:** MVP should be functional (end-to-end for one ticket) within weeks, not months
- **Resources:** Single developer (the operator) building and using the pipeline
- **Hardware:** Cloud VPS initially — pipeline must be efficient with resources (memory, disk, CPU)

### Key Assumptions

- Jira tickets labeled `ai-ready` are well-decomposed and have clear acceptance criteria
- The operator maintains architecture rules docs and keeps them current
- Claude API remains available and capable enough for code generation tasks
- GitHub Copilot review is available on target repos (or can be skipped via config)
- Git clone per ticket is fast enough (repos are not extremely large, or shallow clone is acceptable)

## Risks & Open Questions

### Key Risks

- **Claude API cost at scale:** Multiple agents per ticket, multiple iterations on failures — costs could be significant. Mitigation: use cheaper models (Sonnet/Haiku) for simpler agents, track token usage per ticket.
- **Agent quality on complex tickets:** If tickets are too large or ambiguous, agents will fail or produce low-quality code. Mitigation: BA Agent validates requirements upfront; scope guard catches overreach.
- **Prompt injection via Jira tickets:** Malicious or accidentally adversarial ticket content could manipulate agent behavior. Mitigation: sanitize inputs, constrain agent capabilities via hard rules.
- **Disk usage:** Many concurrent clones of large repos could fill the Mac Mini's disk. Mitigation: shallow clones where possible, aggressive workspace cleanup.

### Open Questions

- What's the optimal model selection per agent? (Opus for BA/Dev, Sonnet for Scope Guard/QA?)
- Should the pipeline support ticket dependencies (wait for ACME-100 before starting ACME-101)?
- How to handle flaky tests that aren't caused by the pipeline's changes?
- Should there be a "dry run" mode that does everything except push/merge?

### Areas Needing Further Research

- Claude API token limits and how they affect large codebase context injection
- Best approach for giving agents codebase awareness (full repo in context vs. targeted file selection)
- Telegram bot reply routing — matching a reply to the correct waiting workspace

## Appendices

### A. Key Documents

- `docs/legacy/start.md` — Original master implementation prompt (historical; superseded by `docs/architecture.md`)
- `projects/{id}/shared/arch-rules.md` — Per-project architecture rules (created by operator)
- `scripts/ticket-to-prompt.py` — Existing script, generates implementation prompts from Jira tickets
- `scripts/copilot-validator.py` — Existing script, validates Copilot review comments

### B. Configuration Hierarchy

```
~/.ai-pipeline/
├── global.yaml            ← credentials, defaults, operator profile, global rules
└── projects/
    ├── acme/
    │   ├── project.yaml   ← Jira settings, project rules
    │   ├── shared/
    │   │   └── arch-rules.md
    │   └── repos/
    │       ├── android-app.yaml
    │       └── backend-api.yaml
    └── another-project/
        └── ...
```

### C. Pipeline Flow (Summary)

```
Poll Jira → PM Agent prioritizes → BA Agent validates requirements
  → Dev Agent implements → Scope Guard checks → PR opened
  → Copilot reviews → Fix Agent addresses → QA Agent tests
  → Merge Agent merges → Jira Done → Telegram notification
```

## Next Steps

### Immediate Actions

1. **Finalize this brief** — review, adjust, confirm
2. **Create PRD** — derive detailed product requirements from this brief
3. **Architecture design** — define module interfaces, agent prompt templates, config schemas
4. **Implement in priority order** — config loader → workspace manager → orchestrator → integrations → agents

### PM Handoff

This Project Brief provides the full context for Cleave. Please start in 'PRD Generation Mode', review the brief thoroughly to work with the user to create the PRD section by section as the template indicates, asking for any necessary clarification or suggesting improvements.

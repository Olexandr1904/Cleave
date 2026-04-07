# Sickle — Feature Tracker

> Track all features, their status, and link to detailed specs.
> Statuses: **Planned** | **In Progress** | **Implemented**

| # | Feature | Status | Description |
|---|---------|--------|-------------|
| 1 | [Agent System (BMAD-style)](agent-system.md) | In Progress | Pluggable prompt-file agents with persona, tasks, checklists |
| 2 | [Orchestrator](orchestrator.md) | In Progress | Main loop, slot management, workspace spawning, state machine |
| 3 | [Workspace Isolation](workspace-isolation.md) | Planned | Isolated directory clones per ticket, no shared state |
| 4 | [Configuration Cascade](configuration-cascade.md) | Planned | 3-level config hierarchy: global, project, repo with cascading overrides |
| 5 | [Jira Integration](jira-integration.md) | Planned | Poll tickets, sync status, transition on completion |
| 6 | [GitHub Integration](github-integration.md) | Planned | Branch creation, PR management, review handling, merge |
| 7 | [Telegram Notifications](telegram-notifications.md) | Planned | Alert human when stuck, receive replies to unblock |
| 8 | [QA Pipeline](qa-pipeline.md) | Planned | Lint, test, coverage gates with configurable thresholds |
| 9 | [Merge Agent](merge-agent.md) | Planned | Automated merge after all gates pass |
| 10 | [Scope Guard](scope-guard.md) | Planned | Detect and prevent scope creep during implementation |

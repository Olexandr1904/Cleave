# Sickle — Feature Tracker

> Track all features, their status, and link to detailed specs.
> Statuses: **Planned** | **In Progress** | **Implemented**

| # | Feature | Status | Description |
|---|---------|--------|-------------|
| 1 | [Agent System (BMAD-style)](agent-system.md) | Implemented | Pluggable prompt-file agents with persona, tools, constraints via Claude tool_use |
| 2 | [Orchestrator](orchestrator.md) | Implemented | Main loop, slot management, workspace spawning, workflow router with agent + action stages |
| 3 | [Workspace Isolation](workspace-isolation.md) | Implemented | Per-ticket workspaces with multi-company hierarchy: base/company/repo/tickets/id/ |
| 4 | [Configuration Cascade](configuration-cascade.md) | Implemented | 3-level config hierarchy with VCS/CI provider abstraction and helper script paths |
| 5 | [Jira Integration](jira-integration.md) | Planned | Poll tickets, sync status, transition on completion |
| 6 | [GitHub Integration](github-integration.md) | Planned | Branch creation, PR management, review handling (no auto-merge) |
| 7 | [Telegram Notifications](telegram-notifications.md) | Planned | Alert human when stuck, receive threaded replies to unblock |
| 8 | [QA Pipeline](qa-pipeline.md) | Planned | Lint, test, build gates with configurable hard/soft enforcement |
| 9 | [Scope Guard](scope-guard.md) | Planned | Validate diff against plan and architecture rules, prevent scope creep |
| 10 | [Tool Sandbox](tool-sandbox.md) | Planned | Sandboxed tool execution for agents with path restrictions and per-agent allowlists |
| 11 | [PR Comment Responder](pr-comment-responder.md) | Planned | Classify and respond to PR review comments with extreme skepticism |
| 12 | [GitLab Integration](gitlab-integration.md) | Planned | MR creation, comment fetching/resolving via GitLab API |
| 13 | [Jenkins Integration](jenkins-integration.md) | Planned | Build status and failure log retrieval from Jenkins |

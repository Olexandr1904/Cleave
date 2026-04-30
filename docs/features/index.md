# Sickle — Feature Tracker

> Track all features, their status, and link to detailed specs.
> Statuses: **Planned** | **In Progress** | **Implemented**

| # | Feature | Status | Description |
|---|---------|--------|-------------|
| 1 | [Agent System (BMAD-style)](agent-system.md) | Implemented | Pluggable prompt-file agents with persona, tools, constraints via Claude tool_use |
| 2 | [Orchestrator](orchestrator.md) | Implemented | Main loop, slot management, workspace spawning, workflow router with agent + action stages, quota-deferral recovery (DEFERRED state auto-resumes) |
| 3 | [Workspace Isolation](workspace-isolation.md) | Implemented | Per-ticket workspaces with multi-company hierarchy: base/company/repo/tickets/id/ |
| 4 | [Configuration Cascade](configuration-cascade.md) | Implemented | 3-level config hierarchy with VCS/CI provider abstraction and helper script paths |
| 5 | [Jira Integration](jira-integration.md) | Planned | Poll tickets with AND semantics on trigger_labels, sync status, transition on completion |
| 6 | [GitHub Integration](github-integration.md) | Planned | Branch creation, PR management, review handling (no auto-merge) |
| 7 | [Telegram Notifications](telegram-notifications.md) | In Progress | Alert human when stuck, receive threaded replies to unblock; IntentParser classifies free-text commands |
| 8 | [QA Pipeline](qa-pipeline.md) | Planned | Lint, test, build gates with configurable hard/soft enforcement |
| 9 | [Scope Guard](scope-guard.md) | Planned | Validate diff against plan and architecture rules, prevent scope creep |
| 10 | [Tool Sandbox](tool-sandbox.md) | Planned | Sandboxed tool execution for agents with path restrictions and per-agent allowlists |
| 11 | [PR Comment Responder](pr-comment-responder.md) | In Progress | Classify and respond to PR review comments with extreme skepticism; resolution report module implemented |
| 12 | [GitLab Integration](gitlab-integration.md) | Planned | MR creation, comment fetching/resolving via GitLab API |
| 13 | [Jenkins Integration](jenkins-integration.md) | Planned | Build status and failure log retrieval from Jenkins |
| 14 | [CLI Entry Point](cli-entry-point.md) | Implemented | CLI argument parsing, startup diagnostics, and version logging |
| 15 | [Project Setup Agent (Atlas)](project-setup-agent.md) | In Progress | BMAD-style agent that onboards new projects with guided Q&A, credential validation, and YAML config generation |
| 16 | [Dashboard & Event Log](dashboard.md) | Implemented | Local web dashboard with structured event log, per-project ticket history, and real-time auto-refresh |
| 17 | [Project Health + Stage Verification](dashboard.md#fr16) | Implemented | Per-project validators (Jira, vcs, git identity, git remote) + mechanical post-stage checks → BLOCKED on failure |
| 18 | [Agent Permissions](agent-permissions.md) | Implemented | Project-level `.claude/settings.json` pre-approves tools for non-interactive pipeline agents, per-agent tool lists in frontmatter, CLI `--allowedTools` enforcement |
| 19 | [Per-Ticket Model Selection](per-ticket-model-selection.md) | Implemented | Jira label `model-haiku` / `model-opus` / `model-sonnet` overrides the Claude model for that ticket; snapshotted at workspace creation |

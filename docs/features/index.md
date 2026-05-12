# Cleave — Feature Tracker

> Track all features, their status, and link to detailed specs.
> Statuses: **Planned** | **In Progress** | **Implemented**

| # | Feature | Status | Description |
|---|---------|--------|-------------|
| 1 | [Agent System (BMAD-style)](agent-system.md) | Implemented | Pluggable prompt-file agents with persona, tools, constraints via Claude tool_use |
| 2 | [Orchestrator](orchestrator.md) | Implemented | Main loop, slot management, workspace spawning, workflow router with agent + action stages, quota-deferral recovery (DEFERRED state auto-resumes) |
| 3 | [Workspace Isolation](workspace-isolation.md) | Implemented | Per-ticket workspaces with multi-company hierarchy: base/company/repo/tickets/id/ |
| 4 | [Configuration Cascade](configuration-cascade.md) | Implemented | 3-level config hierarchy with VCS/CI provider abstraction and helper script paths |
| 5 | [Jira Integration](jira-integration.md) | Implemented | Poll tickets with AND semantics on trigger_labels, sync status, transition on completion |
| 6 | [GitHub Integration](github-integration.md) | Implemented | Branch creation, PR management, review handling (no auto-merge) |
| 7 | [Telegram Notifications](telegram-notifications.md) | In Progress | Alert human when stuck, receive threaded replies to unblock; IntentParser classifies free-text commands |
| 8 | [QA Pipeline](qa-pipeline.md) | Implemented | Lint, test, build gates with configurable hard/soft enforcement; QA agent and stage routing live |
| 9 | [Scope Guard](scope-guard.md) | Implemented | Validate diff against plan and architecture rules, prevent scope creep; agent and stage routing live |
| 10 | [Tool Sandbox](tool-sandbox.md) | Implemented | Sandboxed tool execution for agents with path restrictions and per-agent allowlists |
| 11 | [PR Comment Responder](pr-comment-responder.md) | In Progress | Classify and respond to PR review comments with extreme skepticism; resolution report module implemented |
| 12 | [GitLab Integration](gitlab-integration.md) | In Progress | MR creation, comment fetching/resolving via GitLab API; adapter package scaffolded (Task 1) |
| 13 | [Jenkins Integration](jenkins-integration.md) | Planned | Build status and failure log retrieval from Jenkins |
| 14 | [CLI Entry Point](cli-entry-point.md) | Implemented | CLI argument parsing, startup diagnostics, and version logging |
| 15 | [Project Setup Agent (Atlas)](project-setup-agent.md) | Implemented | BMAD-style agent that onboards new projects with guided Q&A, credential validation, and YAML config generation; production atlas wired into dashboard |
| 16 | [Dashboard & Event Log](dashboard.md) | Implemented | Local web dashboard with structured event log, per-project ticket history, and real-time auto-refresh |
| 17 | [Project Health + Stage Verification](dashboard.md#fr16) | Implemented | Per-project validators (Jira, vcs, git identity, git remote) + mechanical post-stage checks → BLOCKED on failure |
| 18 | [Agent Permissions](agent-permissions.md) | Implemented | Project-level `.claude/settings.json` pre-approves tools for non-interactive pipeline agents, per-agent tool lists in frontmatter, CLI `--allowedTools` enforcement |
| 19 | [Per-Ticket Model Selection](per-ticket-model-selection.md) | Implemented | Jira label `model-haiku` / `model-opus` / `model-sonnet` overrides the Claude model for that ticket; snapshotted at workspace creation |
| 20 | [Multi-Stack Support](multi-stack-support.md) | Planned | Pluggable failure-recovery interface + `project_type` repo field — decouples Gradle/AAPT2 logic from orchestrator core so web/iOS/backend repos onboard without dead code or false-firing Android warnings |

---

## Open Cleanup & Bugfix Work

See [docs/cleanup-plan.md](../cleanup-plan.md) for the live audit + triage tracker. As of 2026-04-30, sections A correctness, B lifecycle, D test thinness, and E cleanup are shipped. Still open: A6 (jira `transitions[0]`), A8 (workspace state schema drift), and all of section C (architecture refactors — orchestrator god-object split, tool_sandbox rename, main.py extraction, command_handler dispatcher split, etc.).

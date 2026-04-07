# Feature Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a markdown-based feature tracking system with a central index, 10 seeded feature docs, a pre-commit hook enforcing doc updates, and a CONTRIBUTING.md convention guide.

**Architecture:** Pure markdown files in `docs/features/`. A bash pre-commit hook checks if tracked directories changed without a corresponding feature doc update. Convention documented in `CONTRIBUTING.md`. No build tools, no dependencies.

**Tech Stack:** Markdown, Bash (pre-commit hook)

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `docs/features/index.md` | Main feature table of contents |
| Create | `docs/features/agent-system.md` | Feature doc: Agent System (BMAD-style) |
| Create | `docs/features/orchestrator.md` | Feature doc: Orchestrator |
| Create | `docs/features/workspace-isolation.md` | Feature doc: Workspace Isolation |
| Create | `docs/features/configuration-cascade.md` | Feature doc: Configuration Cascade |
| Create | `docs/features/jira-integration.md` | Feature doc: Jira Integration |
| Create | `docs/features/github-integration.md` | Feature doc: GitHub Integration |
| Create | `docs/features/telegram-notifications.md` | Feature doc: Telegram Notifications |
| Create | `docs/features/qa-pipeline.md` | Feature doc: QA Pipeline |
| Create | `docs/features/merge-agent.md` | Feature doc: Merge Agent |
| Create | `docs/features/scope-guard.md` | Feature doc: Scope Guard |
| Create | `scripts/pre-commit` | Pre-commit hook script (source) |
| Create | `scripts/install-hooks.sh` | Hook installer helper |
| Create | `CONTRIBUTING.md` | Convention documentation |

---

### Task 1: Create the Feature Index

**Files:**
- Create: `docs/features/index.md`

- [ ] **Step 1: Create the directory and index file**

Create `docs/features/index.md` with the full table:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/index.md
git commit -m "Add feature tracker index with 10 seeded features"
```

---

### Task 2: Create Feature Doc — Agent System (BMAD-style)

**Files:**
- Create: `docs/features/agent-system.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/agent-system.md` with content seeded from PRD FR1-FR4, FR23-FR29 and the architecture doc's BMAD Agent Pattern section:

```markdown
# Feature: Agent System (BMAD-style)

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Pluggable AI agent system following the BMAD pattern. Each agent is a standalone markdown prompt file containing persona, role, core principles, tasks, templates, checklists, and activation instructions. Adding a new agent requires only dropping a file into `agents/` — zero code changes. Agents are stateless: they receive context, execute via Claude API, write output to workspace context files, and exit.

## Requirements

- FR1: Each agent defined as a standalone prompt file (`agents/{agent-id}.md`) with persona, role, core principles, tasks, templates, checklists, and activation instructions
- FR2: Adding a new agent requires only dropping a file into `agents/` — zero code changes
- FR3: Each agent has access to declared dependencies: tasks, templates, checklists, and shared data
- FR4: Agents are stateless — receive context, execute, write output to `workspace/context/`, exit
- FR5: Agent prompt files follow BMAD format: YAML frontmatter (id, name, title, persona, dependencies) + markdown body (instructions, principles)
- FR6: Resource registry maps resource type + id to file path at startup
- FR7: Agent dependency declarations are validated — missing references produce warnings

### Agent Roster (MVP)

- PM Agent — ticket prioritization, routing, dependency checking
- BA Agent — requirements validation, implementation plan, test scenarios
- Dev Agent — implementation on feature branch, scope-constrained
- Scope Guard Agent — diff analysis, scope certificate or violations
- Fix/Reviewer Agent — address review comments with scope re-check
- QA Agent — write tests, run suite + lint + build
- Merge Agent — gate checklist, conflict resolution, merge, Jira transition

## Technical Approach

- Agent prompt files stored in `agents/` directory as `.md` files
- Agent runtime loads prompt, injects workspace context + config, calls Claude API, captures output
- Template Method pattern: load prompt → inject context → call LLM → write output → log
- Agent metadata parsed from YAML frontmatter: id, name, title, persona, core_principles, dependencies, model_override
- Resource registry built at startup by scanning `agents/`, `tasks/`, `templates/`, `checklists/`, `data/` directories

## Dependencies

- Claude API (Anthropic SDK) for agent execution
- Orchestrator for agent dispatch and workflow routing
- Workspace system for context files and isolation
- Config system for operator profile and project settings injection

## Acceptance Criteria

- [ ] Agent prompt files exist for all MVP agents (PM, BA, Dev, Scope Guard, Fix, QA, Merge)
- [ ] Resource registry discovers all agent files and their dependencies at startup
- [ ] Agent runtime can load, assemble, and execute any agent prompt file
- [ ] Adding a new agent file to `agents/` makes it available without code changes
- [ ] Agent execution logs prompt summary, model, token usage, and duration

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/agent-system.md
git commit -m "Add feature doc: Agent System (BMAD-style)"
```

---

### Task 3: Create Feature Doc — Orchestrator

**Files:**
- Create: `docs/features/orchestrator.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/orchestrator.md` seeded from PRD FR5-FR8, Epic 4 stories, and architecture doc:

```markdown
# Feature: Orchestrator

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Central daemon process that continuously polls for work, manages isolated workspaces, and dispatches BMAD-style agents via the workflow router. The orchestrator determines which agent to invoke based on ticket state and `state.json`, supports configurable workflow definitions, and enforces iteration caps with human escalation.

## Requirements

- FR1: Orchestrator determines which agent to invoke based on ticket state and `state.json`
- FR2: Default routing: unclear ticket → BA/PM Agent; clear → Dev Agent; code written → QA Agent; review comments → Fix Agent; all gates passed → Merge Agent
- FR3: Routing logic configurable via workflow definitions specifying agent sequence and transition conditions
- FR4: Supports looping with configurable iteration caps per stage
- FR5: Main loop runs on configurable `poll_interval_seconds`
- FR6: Each cycle: poll tracker → check new tickets → check slot availability → spawn workspaces → advance active workspaces
- FR7: Slot limits enforced per-repo and per-project
- FR8: `--dry-run` flag polls tickets and logs what would happen without executing
- FR9: Handles exceptions per-workspace without crashing the daemon
- FR10: SIGTERM/SIGINT triggers graceful shutdown: finish current agent calls, save state, exit

## Technical Approach

- Single long-running asyncio process
- Workflow router reads `state.json` and applies transition rules from config
- Default workflow: PM → BA → Dev → Scope Guard → PR → Fix → QA → Merge
- Conditional transitions: scope guard fail → Dev; QA fail → Dev; max iterations → escalate
- Escalate state triggers Telegram notification and sets `status: waiting_for_human`
- Workspace advancement is per-workspace: invoke next agent, update state, handle result

## Dependencies

- Agent System for dispatching agents
- Workspace Isolation for workspace management and state
- All integration adapters (Jira, GitHub, Telegram)
- Configuration Cascade for workflow definitions and settings

## Acceptance Criteria

- [ ] Main loop polls for tickets and advances workspaces on each cycle
- [ ] Workflow router correctly sequences agents based on state
- [ ] Conditional transitions work (scope guard loop, QA loop)
- [ ] Iteration caps trigger escalation at configured max
- [ ] Dry-run mode logs actions without executing
- [ ] Graceful shutdown on SIGTERM/SIGINT
- [ ] One workspace failure does not crash the daemon

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/orchestrator.md
git commit -m "Add feature doc: Orchestrator"
```

---

### Task 4: Create Feature Doc — Workspace Isolation

**Files:**
- Create: `docs/features/workspace-isolation.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/workspace-isolation.md` seeded from PRD FR12-FR14, Epic 2, and architecture doc:

```markdown
# Feature: Workspace Isolation

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Each ticket gets a fully isolated workspace via a fresh `git clone`. No ticket shares a filesystem with another. Each workspace maintains a `state.json` tracking current agent, iterations, status, and human input. The orchestrator can resume any workspace from `state.json` after a restart.

## Requirements

- FR1: Each ticket gets a fully isolated workspace (fresh `git clone` into `/workspaces/{project}/{repo}/{ticket}_{timestamp}/`)
- FR2: Each workspace has `context/` and `logs/` subdirectories
- FR3: Shallow clone depth configurable via `git.depth` (0 = full clone)
- FR4: `state.json` created on init tracking: ticket_id, project_id, repo_id, workspace_root, branch, pr_number, current_stage, stage_iterations, human_input_pending, started_at, last_updated_at, status
- FR5: State machine transitions: pending → running → waiting_for_human → running → completed/failed
- FR6: Invalid transitions raise errors
- FR7: State writes are atomic (temp file + rename)
- FR8: On startup, WorkspaceManager discovers existing workspaces and resumes active ones
- FR9: Completed/failed workspaces older than `workspaces.max_age_days` are auto-cleaned

## Technical Approach

- `WorkspaceManager` class handles create, discover, resume, cleanup
- Directory-based isolation: each workspace is a self-contained directory with repo clone, context, logs, and state
- No git worktrees — full directory clones avoid git lock conflicts under concurrent writes
- State machine implemented as explicit transition table with validation
- Atomic state writes: write to `state.json.tmp`, then `os.rename()` to `state.json`
- Cleanup runs periodically on each poll cycle

## Dependencies

- Git CLI (subprocess) for clone operations
- Configuration Cascade for workspace settings (base_dir, max_age_days, clone depth)
- Orchestrator invokes WorkspaceManager

## Acceptance Criteria

- [ ] Workspace creation produces correct directory structure with clone, context/, logs/
- [ ] state.json tracks all required fields and updates atomically
- [ ] All valid state transitions work; invalid transitions raise errors
- [ ] Startup discovers and resumes active workspaces
- [ ] Old completed/failed workspaces are cleaned up
- [ ] Clone failure cleans up partial workspace

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/workspace-isolation.md
git commit -m "Add feature doc: Workspace Isolation"
```

---

### Task 5: Create Feature Doc — Configuration Cascade

**Files:**
- Create: `docs/features/configuration-cascade.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/configuration-cascade.md` seeded from PRD FR9-FR11 and architecture doc Config Loader section:

```markdown
# Feature: Configuration Cascade

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

3-level configuration hierarchy: `global.yaml` → `project.yaml` → `repo.yaml`. Lower-level values override higher-level values; unset fields inherit from parent. All secrets via environment variable references (`${VAR_NAME}`). The config system drives all project-specific, repo-specific, and environment-specific behavior.

## Requirements

- FR1: 3-level config hierarchy: `global.yaml` → `project.yaml` → `repo.yaml` with cascading overrides
- FR2: `global.yaml` includes operator profile (role, stack, preferences, global rules) injected into all agents
- FR3: Environment variable references (`${VAR_NAME}`) resolved at load time; missing vars raise clear errors
- FR4: Config loader scans `projects/` subdirectories to discover all projects
- FR5: Each project's `project.yaml` merged on top of global defaults
- FR6: Each repo's `{repo-id}.yaml` merged on top of project config
- FR7: `enabled: false` on a project or repo excludes it from discovery
- FR8: `--project` and `--repo` CLI flags filter to specific project/repo
- FR9: Invalid or missing required fields produce clear validation errors with file path and field name
- FR10: Agent prompt files may reference project-level data injected at runtime

## Technical Approach

- Config loader module in `config/` directory
- `load_config()` takes config dir path and optional project/repo filters
- Deep merge: dicts are merged recursively, scalars and lists are overridden
- Env var resolution via regex matching `${VAR_NAME}` patterns
- Validation checks required fields and types after merge
- Returns structured config objects (GlobalConfig, ProjectConfig, RepoConfig)

## Dependencies

- PyYAML for YAML parsing
- Environment variables for secrets
- CLI flags (`--config`, `--project`, `--repo`) from main.py

## Acceptance Criteria

- [ ] Global config parses with operator profile, telegram, claude, workspaces, defaults, logging
- [ ] Project config overrides global; repo config overrides project
- [ ] Env vars resolved; missing env var raises clear error
- [ ] Disabled projects/repos excluded from discovery
- [ ] CLI filters work for single project/repo
- [ ] Invalid config produces clear validation errors

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/configuration-cascade.md
git commit -m "Add feature doc: Configuration Cascade"
```

---

### Task 6: Create Feature Doc — Jira Integration

**Files:**
- Create: `docs/features/jira-integration.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/jira-integration.md` seeded from PRD FR15 and Story 3.2:

```markdown
# Feature: Jira Integration

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Jira adapter behind the TrackerInterface. Polls Jira for tickets matching configured labels and statuses, transitions tickets through their lifecycle, and posts comments for pipeline progress updates.

## Requirements

- FR1: Poll tickets matching: has `trigger_label`, status = todo, not in `ignore_labels`, unassigned or bot-assigned
- FR2: Return ticket data: id, summary, description, labels, priority, sprint, linked issues, acceptance criteria
- FR3: Transition tickets between configured statuses (todo → in_progress → in_review → done)
- FR4: Post formatted comments to tickets for status updates
- FR5: Authentication via token + email from project config with env var resolution
- FR6: HTTP errors handled with retries (3 attempts with backoff)
- FR7: Ticket content sanitized before injection into agent prompts (NFR8)
- FR8: Implements abstract `TrackerInterface`

## Technical Approach

- `JiraAdapter` class implementing `TrackerInterface`
- Uses httpx async client for Jira REST API v3
- JQL queries built from config (project_key, trigger_label, ignore_labels, statuses)
- Ticket data normalized into `TicketData` model and written to `context/ticket.json`
- Retry logic with exponential backoff for transient HTTP errors
- Input sanitization strips potential prompt injection patterns from ticket content

## Dependencies

- httpx for async HTTP
- Configuration Cascade for Jira project settings (url, token, email, labels, statuses)
- Abstract TrackerInterface from `integrations/base/`

## Acceptance Criteria

- [ ] Polls tickets matching configured criteria via JQL
- [ ] Returns normalized ticket data with all required fields
- [ ] Transitions tickets between statuses
- [ ] Posts formatted comments
- [ ] Retries on HTTP errors with backoff
- [ ] Sanitizes ticket content before agent injection

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/jira-integration.md
git commit -m "Add feature doc: Jira Integration"
```

---

### Task 7: Create Feature Doc — GitHub Integration

**Files:**
- Create: `docs/features/github-integration.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/github-integration.md` seeded from PRD FR16 and Story 3.3:

```markdown
# Feature: GitHub Integration

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

GitHub adapter behind the VCSInterface. Handles all git and GitHub operations: branch creation, pushing, PR management, review comment reading/replying, CI status checking, and merging. Uses git CLI for local operations and GitHub REST API for remote operations.

## Requirements

- FR1: Create and checkout branch `{branch_prefix}/{ticket_id}-{slug}` in workspace repo
- FR2: Push current branch to origin
- FR3: Open PR using `pr_description_template` from repo config, return PR number and URL
- FR4: Get all review comments on a PR
- FR5: Reply to specific review comments
- FR6: Check whether all CI checks are passing
- FR7: Merge PR using configured `merge_method` (squash/merge/rebase)
- FR8: Close PR on failure/escalation
- FR9: All operations use token from repo config
- FR10: Implements abstract `VCSInterface`

## Technical Approach

- `GitHubAdapter` class implementing `VCSInterface`
- Local git operations via subprocess (git CLI) — more reliable than Python git libraries for concurrent ops
- Remote GitHub operations via httpx async client against GitHub REST API
- Branch naming: `{branch_prefix}/{ticket_id}-{slug}` where slug is derived from ticket summary
- PR description generated from configurable template with variable substitution

## Dependencies

- Git CLI (subprocess) for local operations
- httpx for GitHub REST API
- Configuration Cascade for repo settings (token, owner, repo, default_branch, branch_prefix, merge_method)
- Abstract VCSInterface from `integrations/base/`

## Acceptance Criteria

- [ ] Creates and checks out feature branches with correct naming
- [ ] Pushes branches to origin
- [ ] Opens PRs with templated descriptions
- [ ] Reads and replies to review comments
- [ ] Checks CI status
- [ ] Merges with configured method
- [ ] Closes PRs on failure

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/github-integration.md
git commit -m "Add feature doc: GitHub Integration"
```

---

### Task 8: Create Feature Doc — Telegram Notifications

**Files:**
- Create: `docs/features/telegram-notifications.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/telegram-notifications.md` seeded from PRD FR17 and Story 3.4:

```markdown
# Feature: Telegram Notifications

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Telegram bot adapter behind the NotifierInterface. Sends formatted notifications to the operator when the pipeline needs human input, and receives replies to unblock waiting workspaces. The only human touchpoint — if no questions arise, tickets complete the full cycle without human involvement.

## Requirements

- FR1: Send formatted messages to configured `chat_id` with `[PROJECT/REPO]` prefix
- FR2: Support full notification format: ambiguous ticket, scope loop, fix loop, merge conflict, success, failure
- FR3: `wait_for_reply()` blocks until operator replies to a specific message, returns reply text
- FR4: Reply stored in workspace `state.json` as `human_input_reply`
- FR5: Waiting workspace unblocked and agent receives reply as additional context
- FR6: Bot uses async polling (not webhooks) for simplicity on VPS
- FR7: Per-project chat routing via `telegram.chat_id` in project config
- FR8: Configurable reminder sent if no reply after N hours
- FR9: Implements abstract `NotifierInterface`

## Technical Approach

- `TelegramAdapter` class implementing `NotifierInterface`
- Uses python-telegram-bot library (async mode)
- Async polling for incoming messages
- Message-to-workspace routing: each outgoing message tagged with workspace ID, incoming replies matched back
- Notification templates for each message type (escalation, success, failure)

## Dependencies

- python-telegram-bot for Telegram Bot API
- Configuration Cascade for telegram settings (bot_token, chat_id)
- Workspace state for storing human input
- Abstract NotifierInterface from `integrations/base/`

## Acceptance Criteria

- [ ] Sends formatted messages with project/repo prefix
- [ ] All notification types render correctly
- [ ] Replies are received and routed to the correct waiting workspace
- [ ] Workspace unblocks and resumes after reply
- [ ] Reminder sent after configurable timeout
- [ ] Per-project chat routing works

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/telegram-notifications.md
git commit -m "Add feature doc: Telegram Notifications"
```

---

### Task 9: Create Feature Doc — QA Pipeline

**Files:**
- Create: `docs/features/qa-pipeline.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/qa-pipeline.md` seeded from PRD FR28 and Story 6.4:

```markdown
# Feature: QA Pipeline

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

QA Agent that writes tests covering all acceptance criteria and edge cases, then runs the full quality suite: test runner, linter, and build check. Never deletes or modifies existing tests unless the ticket explicitly requires behavior changes. Follows existing test conventions in the repo.

## Requirements

- FR1: QA Agent prompt file at `agents/qa-agent.md`
- FR2: Receives `test-scenarios.md`, current code, and `ticket.json` as context
- FR3: Writes unit tests covering all AC scenarios and edge cases from test-scenarios
- FR4: Runs test suite (`testing.run_command`), linter (`linting.run_command`), build check (`build.check_command`)
- FR5: Never deletes or modifies existing tests unless ticket explicitly requires behavior change
- FR6: New tests follow same conventions as existing tests in the repo
- FR7: If tests fail, agent attempts fix up to `max_qa_iterations`; exceeded → Telegram escalation
- FR8: Output: green test suite, new tests committed, lint and build passing

## Technical Approach

- QA Agent is a BMAD-style prompt file with test-writing instructions
- Agent reads existing test files to learn conventions before writing new tests
- Runs quality commands via subprocess in the workspace repo directory
- Iterates on failures: reads error output, fixes, re-runs up to max iterations
- All commands configurable per-repo via config (test runner, linter, build checker)

## Dependencies

- Agent System for prompt loading and execution
- Workspace Isolation for test execution environment
- Configuration Cascade for quality gate commands and thresholds
- Telegram Notifications for escalation on max iterations

## Acceptance Criteria

- [ ] QA Agent writes tests covering all acceptance criteria scenarios
- [ ] Test suite, linter, and build check all run and pass
- [ ] Existing tests are not modified unless ticket requires it
- [ ] Failed tests trigger retry up to max iterations
- [ ] Max iterations exceeded triggers Telegram escalation

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/qa-pipeline.md
git commit -m "Add feature doc: QA Pipeline"
```

---

### Task 10: Create Feature Doc — Merge Agent

**Files:**
- Create: `docs/features/merge-agent.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/merge-agent.md` seeded from PRD FR29 and Story 7.1:

```markdown
# Feature: Merge Agent

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Final agent in the pipeline that performs gate checks and merges the PR. Verifies scope certificate, resolved review comments, passing tests/lint/build, and no merge conflicts before merging. Handles merge conflicts intelligently: auto-resolves conflicts in non-plan files, escalates conflicts in plan files.

## Requirements

- FR1: Merge Agent prompt file at `agents/merge-agent.md`
- FR2: Gate checklist: scope certificate exists, all review comments resolved, tests passing, lint passing, build passing, no merge conflicts
- FR3: Merge conflict in files NOT in implementation plan → resolve by taking base branch version
- FR4: Merge conflict in files IN implementation plan → immediate Telegram escalation, no auto-resolution
- FR5: On success: merge PR using configured `merge_method`, transition Jira to Done, post Jira comment, send Telegram success notification
- FR6: On failure at any gate: log which gate failed, notify via Telegram with specifics
- FR7: Workspace status set to `completed` on merge, `failed` on unresolvable failure

## Technical Approach

- Merge Agent is a BMAD-style prompt file with a gate checklist
- Agent verifies each gate sequentially — first failure stops the process
- Merge conflict detection via `git merge --no-commit` dry run against default branch
- Conflict categorization by comparing conflicted files against implementation plan's file list
- Uses GitHub adapter for actual merge operation
- Uses Jira adapter for ticket transition and comment
- Uses Telegram adapter for success/failure notifications

## Dependencies

- Agent System for prompt loading and execution
- GitHub Integration for PR merge operation
- Jira Integration for ticket transition
- Telegram Notifications for success/failure notifications
- Scope Guard (scope certificate as gate input)
- QA Pipeline (test/lint/build results as gate input)

## Acceptance Criteria

- [ ] All gate checks verified before merge
- [ ] Non-plan merge conflicts auto-resolved
- [ ] Plan-file merge conflicts trigger Telegram escalation
- [ ] Successful merge transitions Jira to Done and sends notification
- [ ] Gate failure logged and reported via Telegram
- [ ] Workspace status updated correctly on success/failure

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/merge-agent.md
git commit -m "Add feature doc: Merge Agent"
```

---

### Task 11: Create Feature Doc — Scope Guard

**Files:**
- Create: `docs/features/scope-guard.md`

- [ ] **Step 1: Create the feature doc**

Create `docs/features/scope-guard.md` seeded from PRD FR26 and Story 6.1:

```markdown
# Feature: Scope Guard

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-07
**Author:** Oleksandr Brazhenko

## Description

Agent that validates the developer's diff against the implementation plan and architecture rules. Ensures no out-of-scope changes slip through: every changed file must be in the plan's allowed list and every change must map to a ticket requirement. Issues a scope certificate on pass or a scope violation report on failure.

## Requirements

- FR1: Scope Guard Agent prompt file at `agents/scope-guard-agent.md`
- FR2: Receives: `git diff origin/{default_branch}...HEAD`, `implementation-plan.md`, `ticket.json`, `arch-rules.md`
- FR3: Checks every changed file: is it in the plan's allowed list? Does each change map to a ticket requirement?
- FR4: Detects violations: unauthorized files, formatting-only changes, new unused imports, layer boundary violations, missing ticket ID in commits
- FR5: If violations → writes `context/scope-report.md` with violation list and fix instructions, state returns to Dev Agent
- FR6: If clean → writes `context/scope-certificate.md`, state advances to PR creation
- FR7: Iteration count incremented on each pass; max from config triggers escalation

## Technical Approach

- Scope Guard is a BMAD-style prompt file with diff analysis instructions
- Agent receives the full diff and the implementation plan, performs line-by-line analysis
- File allowlist derived from implementation plan's "files to create/modify" section
- Architecture rules checked: no modifications to protected files (arch-rules.md, lint config, CI config)
- Scope certificate is a simple markdown file confirming all checks passed
- Scope report lists each violation with file, line, type, and suggested fix

## Dependencies

- Agent System for prompt loading and execution
- Workspace Isolation for accessing diff and context files
- Configuration Cascade for iteration caps and protected file paths
- Telegram Notifications for escalation on max iterations

## Acceptance Criteria

- [ ] Validates all changed files against implementation plan's allowed list
- [ ] Detects all violation types (unauthorized files, formatting-only, unused imports, etc.)
- [ ] Writes scope report with actionable fix instructions on failure
- [ ] Writes scope certificate on success
- [ ] Iteration count tracked and max triggers escalation
- [ ] State correctly returns to Dev Agent on violations

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/scope-guard.md
git commit -m "Add feature doc: Scope Guard"
```

---

### Task 12: Create Pre-Commit Hook

**Files:**
- Create: `scripts/pre-commit`
- Create: `scripts/install-hooks.sh`

- [ ] **Step 1: Create the scripts directory if needed and write the pre-commit hook**

Create `scripts/pre-commit`:

```bash
#!/usr/bin/env bash
#
# Pre-commit hook: blocks commits that modify tracked directories
# without updating a feature doc in docs/features/.
#

TRACKED_DIRS="agents/ orchestrator/ integrations/ workflows/ tasks/ checklists/"
TRACKED_FILES="main.py"
FEATURE_DOCS_DIR="docs/features/"

staged_files=$(git diff --cached --name-only)

# Find staged files in tracked directories or tracked files
tracked_changes=()
for file in $staged_files; do
    for dir in $TRACKED_DIRS; do
        if [[ "$file" == "$dir"* ]]; then
            tracked_changes+=("$file")
            break
        fi
    done
    for tf in $TRACKED_FILES; do
        if [[ "$file" == "$tf" ]]; then
            tracked_changes+=("$file")
            break
        fi
    done
done

# If no tracked files changed, allow the commit
if [ ${#tracked_changes[@]} -eq 0 ]; then
    exit 0
fi

# Check if any feature doc was also staged
feature_doc_updated=false
for file in $staged_files; do
    if [[ "$file" == "$FEATURE_DOCS_DIR"* ]]; then
        feature_doc_updated=true
        break
    fi
done

if [ "$feature_doc_updated" = false ]; then
    echo ""
    echo "Commit blocked: You modified files in tracked directories but no feature"
    echo "doc in docs/features/ was updated."
    echo ""
    echo "Modified tracked files:"
    for file in "${tracked_changes[@]}"; do
        echo "  - $file"
    done
    echo ""
    echo "Update the relevant feature doc and docs/features/index.md, then try again."
    echo ""
    exit 1
fi

exit 0
```

- [ ] **Step 2: Write the install-hooks.sh helper**

Create `scripts/install-hooks.sh`:

```bash
#!/usr/bin/env bash
#
# Copies git hooks from scripts/ into .git/hooks/ and makes them executable.
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "Error: .git/hooks directory not found. Are you in a git repository?"
    exit 1
fi

cp "$SCRIPT_DIR/pre-commit" "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

echo "Git hooks installed successfully."
```

- [ ] **Step 3: Commit**

```bash
git add scripts/pre-commit scripts/install-hooks.sh
git commit -m "Add pre-commit hook enforcing feature doc updates"
```

---

### Task 13: Install the Hook and Verify

- [ ] **Step 1: Run the install script**

```bash
bash scripts/install-hooks.sh
```

Expected output: `Git hooks installed successfully.`

- [ ] **Step 2: Verify hook blocks a bad commit**

Create a test scenario — stage a change to a tracked file without a feature doc update:

```bash
echo "# test" >> agents/dev-agent.md
git add agents/dev-agent.md
git commit -m "test: should be blocked"
```

Expected: Commit blocked with error message listing `agents/dev-agent.md`.

- [ ] **Step 3: Verify hook allows a good commit**

Stage both a tracked file change and a feature doc update:

```bash
echo "" >> docs/features/agent-system.md
git add agents/dev-agent.md docs/features/agent-system.md
git commit -m "test: should pass"
```

Expected: Commit succeeds.

- [ ] **Step 4: Clean up test changes**

```bash
git reset HEAD~1
git checkout -- agents/dev-agent.md docs/features/agent-system.md
```

---

### Task 14: Create CONTRIBUTING.md

**Files:**
- Create: `CONTRIBUTING.md`

- [ ] **Step 1: Write CONTRIBUTING.md**

Create `CONTRIBUTING.md` in the project root:

```markdown
# Contributing to Sickle

## Feature Documentation Rule

Any code change that adds, modifies, or removes feature behavior **must** be accompanied by an update to the relevant feature doc in `docs/features/` and the feature index at `docs/features/index.md`.

A pre-commit hook enforces this rule. If you change files in tracked directories without updating a feature doc, the commit will be blocked.

### Tracked Directories

Changes to these directories require a feature doc update:

- `agents/`
- `orchestrator/`
- `integrations/`
- `workflows/`
- `tasks/`
- `checklists/`
- `main.py`

### Directories That Do NOT Require Feature Doc Updates

- `tests/`
- `config/`
- `data/`
- `docs/` (except `docs/features/` itself)
- `deploy/`
- `.gitignore`, `README.md`, `pyproject.toml`, `package.json`

### When to Create a New Feature Doc

Create a new feature doc when adding a new capability that does not fit any existing feature in the index. Use the template below.

### When to Update an Existing Feature Doc

- When modifying behavior covered by that feature
- When completing acceptance criteria (check them off)
- When changing the feature's status (Planned → In Progress → Implemented)

### What to Update

**In the feature doc (`docs/features/<feature>.md`):**
- Relevant sections (requirements, technical approach, etc.)
- `Updated` date
- Change log entry
- Status field if changed
- Check off completed acceptance criteria

**In the index (`docs/features/index.md`):**
- Status column if changed
- Description if refined

### Feature Doc Template

```
# Feature: <Feature Name>

**Status:** Planned | In Progress | Implemented
**Created:** YYYY-MM-DD
**Updated:** YYYY-MM-DD
**Author:** <name>

## Description

Brief overview of what this feature does and why it exists.

## Requirements

- FR1: ...

## Technical Approach

How this will be implemented.

## Dependencies

- Other features or systems this depends on.

## Acceptance Criteria

- [ ] Criterion 1

## Change Log

| Date | Description |
|------|-------------|
| YYYY-MM-DD | Initial draft |
```

### Pre-Commit Hook Setup

After cloning the repository, install the git hooks:

```bash
bash scripts/install-hooks.sh
```

This copies the pre-commit hook from `scripts/pre-commit` into `.git/hooks/` and makes it executable. The hook will block commits that modify tracked directories without updating a feature doc.
```

- [ ] **Step 2: Commit**

```bash
git add CONTRIBUTING.md
git commit -m "Add CONTRIBUTING.md with feature documentation convention"
```

---

### Task 15: Final Verification

- [ ] **Step 1: Verify all files exist**

Run:
```bash
ls docs/features/
```

Expected output (11 files):
```
agent-system.md
configuration-cascade.md
github-integration.md
index.md
jira-integration.md
merge-agent.md
orchestrator.md
qa-pipeline.md
scope-guard.md
telegram-notifications.md
workspace-isolation.md
```

- [ ] **Step 2: Verify all index links are valid**

Run:
```bash
grep -oP '\[.*?\]\(\K[^)]+' docs/features/index.md | while read -r link; do
  if [ ! -f "docs/features/$link" ]; then
    echo "BROKEN LINK: $link"
  else
    echo "OK: $link"
  fi
done
```

Expected: All 10 links show `OK`.

- [ ] **Step 3: Verify scripts exist and are correct**

Run:
```bash
ls -la scripts/pre-commit scripts/install-hooks.sh
```

Expected: Both files exist.

- [ ] **Step 4: Verify CONTRIBUTING.md exists**

Run:
```bash
head -3 CONTRIBUTING.md
```

Expected:
```
# Contributing to Sickle

## Feature Documentation Rule
```

- [ ] **Step 5: Verify hook is installed**

Run:
```bash
ls -la .git/hooks/pre-commit
```

Expected: File exists and is executable.

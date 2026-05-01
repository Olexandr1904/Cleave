# Cleave — Product Requirements Document (PRD)

## Goals and Background Context

### Goals

- Pipeline autonomously takes a Jira ticket from "To Do" to merged PR without human involvement in the happy path
- Single daemon instance manages multiple projects and repositories via config, not code changes
- Runs 24/7 on a cloud VPS, polls continuously, survives restarts via idempotent state
- Enforces the same architecture rules, lint gates, test coverage, and scope discipline as a human developer
- When stuck, asks precise questions via Telegram and resumes on reply — never guesses or silently fails
- All configuration in yaml files with a 3-level cascade (global → project → repo)
- BMAD-style agent architecture — agents are pluggable prompt files, orchestrator decides who to call

### Background Context

Oleksandr is a team lead and KMP developer at Acme Corp managing multiple repositories. The volume of well-defined but time-consuming implementation tickets exceeds what one person can handle while also managing architecture decisions, reviews, and coordination. Existing AI tools assist interactively but still require the human at the keyboard.

Cleave fills the gap: a fully autonomous pipeline that runs unattended, picks up labeled Jira tickets, and delivers merged PRs. Agents follow the BMAD pattern — each is a standalone prompt file with persona, principles, tasks, and checklists. The orchestrator acts as a decision engine, routing tickets to the right agent at each stage. Two existing scripts (`ticket-to-prompt.py` and `copilot-validator.py`) are integrated as subprocess calls.

### Change Log

| Date | Version | Description | Author |
| :--- | :------ | :---------- | :----- |
| 2026-03-28 | 1.0 | Initial PRD | John (PM Agent) / Oleksandr |

---

## Requirements

### Functional

**Agent System (BMAD-style)**

- FR1: Each agent shall be defined as a standalone prompt file (`agents/{agent-id}.md`) containing persona, role, core principles, tasks, templates, checklists, and activation instructions
- FR2: Adding a new agent requires only dropping a file into `agents/` — zero code changes
- FR3: Each agent has access to declared dependencies: tasks, templates, checklists, and shared data
- FR4: Agents are stateless — receive context, execute, write output to `workspace/context/`, exit

**Orchestrator**

- FR5: The orchestrator determines which agent to invoke based on ticket state and `state.json`
- FR6: Default routing: unclear ticket → BA/PM Agent; clear → Dev Agent; code written → QA Agent; review comments → Reviewer Agent; all gates passed → Merge Agent
- FR7: Routing logic is configurable via workflow definitions that specify agent sequence and transition conditions
- FR8: The orchestrator supports looping with configurable iteration caps per stage

**Configuration**

- FR9: 3-level config hierarchy: `global.yaml` → `project.yaml` → `repo.yaml` with cascading overrides
- FR10: `global.yaml` includes operator profile (role, stack, preferences, global rules) injected into all agents
- FR11: Agent prompt files may reference project-level data injected at runtime

**Workspace & Isolation**

- FR12: Each ticket gets a fully isolated workspace (fresh `git clone`)
- FR13: Each workspace maintains `state.json` tracking current agent, iterations, status, human input
- FR14: Orchestrator resumes each workspace from `state.json` on restart

**Integrations**

- FR15: Jira adapter: poll, filter by labels, route to repos, transition statuses, post comments
- FR16: GitHub adapter: branch, push, open PR, read/reply comments, check CI status, merge
- FR17: Telegram bot: notify with `[PROJECT/REPO]` prefix, receive replies, route to waiting workspace
- FR18: Existing scripts called as subprocesses by agents that need them

**Safeguards**

- FR19: Architecture rules and lint configs are read-only — agent write attempt triggers abort + Telegram alert
- FR20: Per-stage iteration caps with Telegram escalation on max
- FR21: Merge conflicts in ticket-scoped files trigger immediate human escalation
- FR22: All agents log structured output to `workspace/logs/{agent-id}.log`

**Agent Roster (MVP)**

- FR23: PM Agent — ticket prioritization, routing, dependency checking
- FR24: BA Agent — requirements validation, implementation plan, test scenarios
- FR25: Dev Agent — implementation on feature branch, scope-constrained
- FR26: Scope Guard Agent — diff analysis, scope certificate or violations
- FR27: Fix/Reviewer Agent — address review comments with scope re-check
- FR28: QA Agent — write tests, run suite + lint + build
- FR29: Merge Agent — gate checklist, conflict resolution, merge, Jira transition

### Non-Functional

- NFR1: Daemon survives crashes and reboots via systemd `Restart=always` and idempotent `state.json` recovery
- NFR2: No raw credentials in config — all secrets via environment variables (`${VAR_NAME}`)
- NFR3: Agent prompts constrained: no bonus refactoring, no unsolicited renames, no unapproved dependencies
- NFR4: Support at least 5 concurrent workspaces on a standard VPS (4 CPU / 8GB RAM)
- NFR5: Structured logs per agent in `workspace/logs/`
- NFR6: Zero hardcoded project-specific values in pipeline code
- NFR7: Integration adapters implement abstract interfaces for future replacement
- NFR8: Jira ticket content sanitized before injection into agent prompts

---

## Technical Assumptions

### Repository Structure: Monorepo

```
cleave/
├── main.py
├── orchestrator/
├── agents/                    # BMAD-style agent prompt files (.md)
├── tasks/                     # Reusable task procedures
├── templates/                 # Output templates
├── checklists/                # Validation checklists
├── data/                      # Shared knowledge
├── integrations/
│   ├── base/                  # Abstract interfaces
│   ├── jira/
│   ├── github/
│   └── telegram/
├── workspace/                 # Workspace manager module
├── config/                    # Config loader module
└── scripts/                   # Existing scripts
```

### Service Architecture

Modular monolith. Single daemon process. Agents invoked as subprocess-style calls with prompt file + workspace context. Orchestrator is the only long-running process.

### Testing Requirements

- Unit tests for config loader, state machine, workspace manager, integration adapters
- Integration tests for adapters against mock servers or sandbox accounts
- End-to-end smoke test: scripted scenario with fake ticket through full pipeline (dry-run)
- No UI — validation via logs, state files, integration outputs

### Additional Technical Assumptions

- Python 3.11+, pure Python, minimal dependencies
- asyncio for concurrency
- PyYAML for config
- httpx for async HTTP (Jira, GitHub)
- python-telegram-bot (async) for Telegram
- Anthropic SDK for Claude API calls — model configurable per agent
- subprocess for existing scripts and build/lint/test commands
- No database — state.json on disk
- No Docker for MVP — directory isolation only
- Deployment: systemd on Ubuntu VPS
- Agent prompt files follow BMAD format (yaml frontmatter + markdown body)

---

## Epics

1. Foundation & Config System: Project setup, config loader, CLI, agent file discovery
2. Workspace & State Machine: Isolated workspace creation, state lifecycle, cleanup, idempotent resume
3. Integration Adapters: Jira polling, GitHub PR operations, Telegram notifications and reply routing
4. Agent Runtime Engine: BMAD-style agent execution, Claude API calls, workflow routing in orchestrator
5. Core Agents — Analysis & Development: PM, BA, and Dev agents delivering first ticket-to-code flow
6. Quality & Review Agents: Scope Guard, Fix/Reviewer, and QA agents completing the quality pipeline
7. Merge, Safeguards & End-to-End: Merge agent, all safeguards, full pipeline dry-run, deployment

---

## Epic 1: Foundation & Config System

Establish the project skeleton, implement the 3-level config loader with operator profile support, create the CLI entry point, and build BMAD-style agent/task/checklist file discovery. After this epic, `python main.py --config ./config` parses all config, discovers all agent files, and prints a summary.

### Story 1.1: Project Skeleton & CLI Entry Point

As the **operator**,
I want a runnable Python project with a CLI entry point,
so that I can start the pipeline with `python main.py --config <path>`.

#### Acceptance Criteria

- AC1: Project has `main.py` as entry point with argparse supporting `--config`, `--project`, `--repo`, `--dry-run` flags
- AC2: Running `python main.py --help` shows usage information
- AC3: Running without `--config` prints an error and exits with non-zero code
- AC4: Project has a `requirements.txt` or `pyproject.toml` with initial dependencies (pyyaml)
- AC5: Basic project structure exists: `orchestrator/`, `config/`, `integrations/`, `workspace/`, `agents/`, `tasks/`, `templates/`, `checklists/`, `data/`
- AC6: A `README.md` placeholder exists with project name and one-line description

### Story 1.2: Global Config Loader

As the **operator**,
I want the system to parse `global.yaml` including my operator profile,
so that my credentials, defaults, and personal context are available to the pipeline.

#### Acceptance Criteria

- AC1: Config loader reads `global.yaml` from the path provided via `--config`
- AC2: Environment variable references (`${VAR_NAME}`) are resolved at load time; missing vars raise a clear error
- AC3: Operator profile fields (role, stack, preferences, rules) are parsed and accessible
- AC4: All fields from the spec are supported: telegram, claude, workspaces, defaults, logging
- AC5: Invalid or missing required fields produce clear validation errors with file path and field name
- AC6: Unit tests cover: valid config, missing env var, missing required field, empty file

### Story 1.3: Project & Repo Config with Cascade

As the **operator**,
I want project and repo configs to override global defaults,
so that each project/repo can customize behavior without duplicating the full config.

#### Acceptance Criteria

- AC1: Config loader scans `projects/` subdirectories to discover all projects
- AC2: Each project's `project.yaml` is loaded and merged on top of global defaults
- AC3: Each repo's `{repo-id}.yaml` is loaded and merged on top of project config
- AC4: Lower-level values override higher-level values; unset fields inherit from parent
- AC5: `enabled: false` on a project or repo excludes it from discovery
- AC6: `--project` and `--repo` CLI flags filter to only the specified project/repo
- AC7: Unit tests cover: cascade override, disabled project, disabled repo, single-project filter

### Story 1.4: BMAD-Style Resource Discovery

As the **operator**,
I want the system to discover all agent prompt files, tasks, templates, and checklists at startup,
so that the orchestrator knows what agents and resources are available.

#### Acceptance Criteria

- AC1: System scans `agents/` directory for `.md` files and parses each agent's metadata (id, name, title, dependencies)
- AC2: System scans `tasks/`, `templates/`, `checklists/`, `data/` directories similarly
- AC3: A resource registry is built mapping resource type + id to file path
- AC4: Agent dependency declarations are validated — if an agent references a task that doesn't exist, a warning is logged
- AC5: Running `main.py --config <path>` prints discovered agents, tasks, templates, checklists count
- AC6: Unit tests cover: discovery of valid files, missing dependency warning, empty directories

---

## Epic 2: Workspace & State Machine

Implement isolated workspace creation via git clone, the state.json lifecycle, workspace cleanup, and idempotent resume capability. After this epic, the system can create a workspace for a ticket, track its state through pipeline stages, and resume after restart.

### Story 2.1: Workspace Creation

As the **orchestrator**,
I want to create an isolated workspace with a fresh git clone for a ticket,
so that each ticket executes in complete isolation.

#### Acceptance Criteria

- AC1: `WorkspaceManager.create(project_id, repo_id, ticket_id)` creates `/workspaces/{project}/{repo}/{ticket}_{timestamp}/`
- AC2: The repo is cloned into `workspace/repo/` using the `git.clone_url` from repo config
- AC3: `context/` and `logs/` directories are created inside the workspace
- AC4: Shallow clone depth is configurable via `git.depth` (0 = full clone)
- AC5: If clone fails (network, auth), a clear error is raised and workspace is cleaned up
- AC6: Unit tests cover: successful creation, clone failure cleanup, directory structure validation

### Story 2.2: State Machine & state.json

As the **orchestrator**,
I want each workspace to have a `state.json` tracking pipeline progress,
so that the system can resume from any point after a restart.

#### Acceptance Criteria

- AC1: `state.json` is created on workspace init with: ticket_id, project_id, repo_id, workspace_root, branch (null initially), pr_number (null), current_stage, stage_iterations, human_input_pending, started_at, last_updated_at, status
- AC2: State machine supports transitions: `pending` → `running` → `waiting_for_human` → `running` → `completed`/`failed`
- AC3: Invalid transitions raise an error (e.g., `completed` → `running`)
- AC4: Every state change updates `last_updated_at` and writes to disk atomically (write to temp file + rename)
- AC5: `stage_iterations` dict tracks per-agent iteration counts
- AC6: Unit tests cover: all valid transitions, invalid transition rejection, atomic write, iteration counting

### Story 2.3: Workspace Resume & Discovery

As the **orchestrator**,
I want to discover existing workspaces on startup and resume them,
so that a daemon restart doesn't lose in-progress work.

#### Acceptance Criteria

- AC1: On startup, `WorkspaceManager` scans the workspaces base directory for existing workspace dirs
- AC2: Each workspace with `status: running` or `status: waiting_for_human` is loaded into the orchestrator's active set
- AC3: Workspaces with `status: completed` or `status: failed` are ignored (eligible for cleanup)
- AC4: The orchestrator resumes each active workspace from its `current_stage`
- AC5: Unit tests cover: resume running workspace, skip completed, multiple workspaces

### Story 2.4: Workspace Cleanup

As the **operator**,
I want old workspaces to be automatically cleaned up,
so that disk space doesn't run out.

#### Acceptance Criteria

- AC1: A cleanup routine runs periodically (configurable interval or on each poll cycle)
- AC2: Workspaces with `status: completed` or `status: failed` older than `workspaces.max_age_days` are deleted
- AC3: Workspaces with `status: running` or `waiting_for_human` are never deleted regardless of age
- AC4: Cleanup logs what it deletes
- AC5: Unit tests cover: old completed workspace deleted, young workspace kept, running workspace preserved

---

## Epic 3: Integration Adapters

Implement the Jira, GitHub, and Telegram adapters behind abstract interfaces. After this epic, the pipeline can poll real Jira tickets, perform GitHub PR operations, and send/receive Telegram messages.

### Story 3.1: Abstract Integration Interfaces

As the **developer**,
I want abstract interfaces for tracker, VCS, and notifier,
so that concrete implementations can be swapped without changing pipeline logic.

#### Acceptance Criteria

- AC1: `TrackerInterface` defines: `poll_tickets()`, `transition_ticket()`, `add_comment()`, `get_ticket()`
- AC2: `VCSInterface` defines: `clone_repo()`, `create_branch()`, `push()`, `open_pr()`, `get_pr_comments()`, `reply_to_comment()`, `check_pr_status()`, `merge_pr()`, `close_pr()`
- AC3: `NotifierInterface` defines: `send_message()`, `wait_for_reply()`
- AC4: All interfaces are Python abstract base classes with clear docstrings
- AC5: Integration module selects the concrete adapter based on config (e.g., `tracker: jira` → `JiraAdapter`)

### Story 3.2: Jira Adapter

As the **orchestrator**,
I want to poll Jira for tickets and manage their lifecycle,
so that the pipeline can discover work and report progress.

#### Acceptance Criteria

- AC1: `poll_tickets()` fetches tickets matching: has ALL `trigger_labels`, status = todo, not in `ignore_labels`, unassigned or bot-assigned
- AC2: Tickets are returned with: id, summary, description, labels, priority, sprint, linked issues, acceptance criteria
- AC3: `transition_ticket()` moves tickets between configured statuses (todo → in_progress → in_review → done)
- AC4: `add_comment()` posts a formatted comment to the ticket
- AC5: Authentication uses token + email from project config with env var resolution
- AC6: HTTP errors are handled gracefully with retries (3 attempts with backoff)
- AC7: Integration test against Jira sandbox or mock server

### Story 3.3: GitHub Adapter

As the **orchestrator**,
I want to perform all Git and GitHub operations for PRs,
so that the pipeline can push code and manage the review/merge cycle.

#### Acceptance Criteria

- AC1: `create_branch()` creates and checks out `{branch_prefix}/{ticket_id}-{slug}` in the workspace repo
- AC2: `push()` pushes the current branch to origin
- AC3: `open_pr()` creates a PR using `pr_description_template` from repo config, returns PR number and URL
- AC4: `get_pr_comments()` returns all review comments on a PR
- AC5: `reply_to_comment()` posts a reply to a specific review comment
- AC6: `check_pr_status()` returns whether all CI checks are passing
- AC7: `merge_pr()` merges using the configured `merge_method` (squash/merge/rebase)
- AC8: `close_pr()` closes the PR (on failure/escalation)
- AC9: All operations use the token from repo config

### Story 3.4: Telegram Adapter

As the **operator**,
I want to receive pipeline notifications and reply to unblock agents,
so that I can stay informed and resolve escalations from my phone.

#### Acceptance Criteria

- AC1: `send_message()` sends a formatted message to the configured `chat_id` with `[PROJECT/REPO]` prefix
- AC2: Messages support the full notification format table from the spec (ambiguous ticket, scope loop, fix loop, merge conflict, etc.)
- AC3: `wait_for_reply()` blocks until the operator replies to a specific message, then returns the reply text
- AC4: Reply is stored in the workspace's `state.json` as `human_input_reply`
- AC5: The waiting workspace is unblocked and the agent receives the reply as additional context
- AC6: Bot uses async polling (not webhooks) for simplicity on VPS
- AC7: Per-project chat routing via `telegram.chat_id` in project config

---

## Epic 4: Agent Runtime Engine

Build the BMAD-style agent execution engine: load a prompt file, inject context, call Claude API, capture output, write to workspace. Implement the orchestrator's workflow routing logic. After this epic, you can execute any agent prompt file against a workspace and get structured output.

### Story 4.1: Agent Prompt Loader

As the **orchestrator**,
I want to load and parse BMAD-style agent prompt files,
so that agent definitions drive execution behavior.

#### Acceptance Criteria

- AC1: Loader reads an agent `.md` file and parses: yaml metadata (id, name, persona, dependencies) and markdown body (instructions, principles)
- AC2: Dependency references (tasks, templates, checklists) are resolved against the resource registry from Epic 1
- AC3: The full agent prompt is assembled: persona + principles + task-specific instructions
- AC4: Template variables in the prompt (e.g., `{ticket_id}`, `{project_name}`) are replaced with actual values
- AC5: Unit tests cover: valid agent parse, missing dependency warning, variable substitution

### Story 4.2: Context Injection

As the **agent runtime**,
I want workspace context and config injected into the agent prompt,
so that agents have all the information they need to execute.

#### Acceptance Criteria

- AC1: Operator profile from `global.yaml` is injected into every agent's context
- AC2: Architecture rules (`arch-rules.md`) are injected as read-only context for agents that require it
- AC3: Workspace context files (`ticket.json`, `implementation-plan.md`, etc.) are injected based on the agent's current stage
- AC4: Repo config (lint commands, test commands, build commands) is injected for agents that run tools
- AC5: Context assembly respects token limits — large files are truncated with a warning logged
- AC6: Hard rules (never modify arch-rules, never touch lint config) are appended to every agent prompt

### Story 4.3: Claude API Execution

As the **agent runtime**,
I want to send the assembled prompt to Claude and capture structured output,
so that agents produce actionable results.

#### Acceptance Criteria

- AC1: Agent runtime calls Claude API via Anthropic SDK with the assembled prompt
- AC2: Model is configurable per agent (in agent file metadata or config override)
- AC3: Response is parsed and written to the appropriate `workspace/context/` output file(s)
- AC4: Agent execution is logged to `workspace/logs/{agent-id}.log` with: prompt summary, model used, token usage, duration, output summary
- AC5: API errors (rate limit, timeout, server error) are retried with exponential backoff (3 attempts)
- AC6: Token usage per agent call is tracked and logged for cost monitoring

### Story 4.4: Workflow Router

As the **orchestrator**,
I want to determine which agent to invoke next based on workspace state,
so that tickets flow through the correct agent sequence.

#### Acceptance Criteria

- AC1: Router reads `state.json` and determines the next agent based on `current_stage` and transition rules
- AC2: Default workflow: PM → BA → Dev → Scope Guard → (PR) → Fix → QA → Merge
- AC3: Conditional transitions: if scope guard fails → back to Dev; if QA fails → back to Dev
- AC4: Iteration caps are checked before looping — if max reached, transition to `escalate` state
- AC5: `escalate` state triggers Telegram notification and sets `status: waiting_for_human`
- AC6: When human replies, orchestrator resumes from the stage that was waiting
- AC7: Workflow definition is loaded from a config file, not hardcoded in Python
- AC8: Unit tests cover: happy path, scope guard loop, max iterations escalation, human reply resume

### Story 4.5: Orchestrator Main Loop

As the **operator**,
I want the daemon to continuously poll for tickets and manage active workspaces,
so that the pipeline runs autonomously.

#### Acceptance Criteria

- AC1: Main loop runs on `poll_interval_seconds` from config
- AC2: Each cycle: poll Jira → check for new tickets → check slot availability → spawn new workspaces → advance active workspaces
- AC3: Active workspaces are advanced by invoking their next agent per the workflow router
- AC4: Slot limits are enforced per-repo and per-project before spawning new workspaces
- AC5: `--dry-run` flag polls tickets and logs what would happen without executing agents or creating workspaces
- AC6: Main loop handles exceptions per-workspace without crashing the daemon (one workspace failure doesn't affect others)
- AC7: SIGTERM/SIGINT triggers graceful shutdown: finish current agent calls, save state, exit

---

## Epic 5: Core Agents — Analysis & Development

Implement the PM, BA, and Dev agents as BMAD-style prompt files with supporting tasks and templates. After this epic, a Jira ticket can be picked up, analyzed, planned, and implemented on a feature branch.

### Story 5.1: PM Agent — Ticket Prioritization

As the **orchestrator**,
I want the PM Agent to prioritize and route incoming tickets,
so that the most important work is done first and tickets reach the correct repo.

#### Acceptance Criteria

- AC1: PM Agent prompt file exists at `agents/pm-agent.md` with BMAD-style metadata and instructions
- AC2: Agent receives the list of available tickets from the current poll cycle
- AC3: Agent filters: must have trigger label, must not have ignore labels, must be unassigned or bot-assigned
- AC4: Agent routes each ticket to its repo via `jira_repo_label` matching
- AC5: Agent prioritizes by: sprint membership → priority field → ticket age
- AC6: Agent skips tickets whose linked dependencies are not Done
- AC7: Output: ordered list of `(ticket_id, repo_id)` written to orchestrator

### Story 5.2: BA Agent — Requirements Validation

As the **orchestrator**,
I want the BA Agent to validate ticket requirements and produce an implementation plan,
so that the Dev Agent has clear, unambiguous instructions.

#### Acceptance Criteria

- AC1: BA Agent prompt file exists at `agents/ba-agent.md` with BMAD-style metadata
- AC2: Agent receives: `ticket.json`, `arch-rules.md`, linked ticket data
- AC3: Agent validates: acceptance criteria exist and are testable, no vague requirements, sufficient detail to implement
- AC4: If unclear → agent produces numbered questions → orchestrator sends via Telegram → workspace waits
- AC5: If clear → agent calls `ticket-to-prompt.py` as subprocess and produces:
  - `context/implementation-plan.md` (files to create, modify, not touch; logic summary; edge cases)
  - `context/test-scenarios.md` (AC-derived test cases + edge cases)
- AC6: Agent checks for missing repo label and escalates if absent
- AC7: BA Agent has a checklist dependency for validating plan completeness

### Story 5.3: Dev Agent — Code Implementation

As the **orchestrator**,
I want the Dev Agent to write code on a feature branch following the implementation plan,
so that the ticket requirements are fulfilled in code.

#### Acceptance Criteria

- AC1: Dev Agent prompt file exists at `agents/dev-agent.md` with BMAD-style metadata
- AC2: Agent receives: `implementation-plan.md`, `ticket.json`, `arch-rules.md`, full codebase access
- AC3: Hard rules injected: only touch files in plan, never modify arch-rules/lint config/CI, no bonus refactoring, no unapproved dependencies
- AC4: Agent creates feature branch `{prefix}/{ticket_id}-{slug}` if not already created
- AC5: Agent writes code and commits with format: `feat({ticket_id}): {description}`
- AC6: If receiving scope violations from Scope Guard, agent reads `scope-report.md` and fixes only the violations
- AC7: Agent output: updated code on feature branch in `workspace/repo/`

### Story 5.4: Sample Agent Prompt Files

As the **operator**,
I want well-structured sample agent prompt files for PM, BA, and Dev,
so that the BMAD-style pattern is established and I can create new agents by following the pattern.

#### Acceptance Criteria

- AC1: Each agent file follows consistent structure: yaml metadata block + markdown instructions
- AC2: Metadata includes: id, name, title, persona, core_principles, dependencies (tasks, templates, checklists)
- AC3: Instructions include: activation rules, hard constraints, input/output specifications
- AC4: Agent files reference their tasks and checklists by id (not file path)
- AC5: A brief `agents/README.md` documents the agent file format for creating new agents

---

## Epic 6: Quality & Review Agents

Implement Scope Guard, Fix/Reviewer, and QA agents. After this epic, code is validated against the plan and architecture rules, review comments are addressed, and tests are written and run.

### Story 6.1: Scope Guard Agent

As the **orchestrator**,
I want the Scope Guard Agent to validate the dev's diff against the implementation plan,
so that no out-of-scope changes slip through.

#### Acceptance Criteria

- AC1: Agent prompt file at `agents/scope-guard-agent.md`
- AC2: Agent receives: `git diff origin/{default_branch}...HEAD`, `implementation-plan.md`, `ticket.json`, `arch-rules.md`
- AC3: Agent checks every changed file: is it in the plan's allowed list? Does each change map to a ticket requirement?
- AC4: Violations detected: unauthorized files, formatting-only changes, new unused imports, layer boundary violations, missing ticket ID in commits
- AC5: If violations → writes `context/scope-report.md` with violation list and fix instructions, state returns to Dev Agent
- AC6: If clean → writes `context/scope-certificate.md`, state advances to PR creation
- AC7: Iteration count incremented on each pass; max from config triggers escalation

### Story 6.2: PR Creation Step

As the **orchestrator**,
I want a PR opened automatically after scope guard passes,
so that the review cycle can begin.

#### Acceptance Criteria

- AC1: After scope certificate is written, orchestrator pushes the feature branch
- AC2: PR is opened using GitHub adapter with `pr_description_template` from repo config
- AC3: PR number and URL are stored in `state.json`
- AC4: Jira ticket transitions to "In Review" with comment containing PR URL
- AC5: If Copilot is enabled, orchestrator waits `copilot.wait_for_review_minutes` then calls `copilot-validator.py`
- AC6: If Copilot is disabled or no valid comments → skip to QA Agent

### Story 6.3: Fix / Reviewer Agent

As the **orchestrator**,
I want the Fix Agent to address valid review comments,
so that code review feedback is incorporated without breaking scope.

#### Acceptance Criteria

- AC1: Agent prompt file at `agents/fix-agent.md`
- AC2: Agent receives: `valid-comments.json`, current code, `implementation-plan.md`, `arch-rules.md`
- AC3: For each valid comment: apply fix, re-run lint command from config
- AC4: After all fixes: internal scope check (same logic as Scope Guard) to ensure fixes didn't introduce violations
- AC5: Commits with format: `fix({ticket_id}): address review — {description}`
- AC6: Agent replies to each GitHub comment describing what was done
- AC7: If a comment asks for something that violates scope/arch-rules → don't apply, reply explaining why, notify human
- AC8: Max iterations from config; exceeded → Telegram escalation

### Story 6.4: QA Agent

As the **orchestrator**,
I want the QA Agent to write tests and run the full quality suite,
so that the code meets quality gates before merge.

#### Acceptance Criteria

- AC1: Agent prompt file at `agents/qa-agent.md`
- AC2: Agent receives: `test-scenarios.md`, current code, `ticket.json`
- AC3: Agent writes unit tests covering all AC scenarios and edge cases from test-scenarios
- AC4: Agent runs: test suite (`testing.run_command`), linter (`linting.run_command`), build check (`build.check_command`)
- AC5: Agent never deletes or modifies existing tests unless ticket explicitly requires behavior change
- AC6: New tests follow the same conventions as existing tests in the repo
- AC7: If tests fail → agent attempts fix, up to `max_qa_iterations`; exceeded → Telegram escalation
- AC8: Output: green test suite, new tests committed, lint and build passing

---

## Epic 7: Merge, Safeguards & End-to-End

Implement the Merge Agent, wire all safeguards, run a full end-to-end pipeline test, and prepare for deployment. After this epic, Cleave autonomously takes a Jira ticket from "To Do" to merged PR.

### Story 7.1: Merge Agent

As the **orchestrator**,
I want the Merge Agent to perform final checks and merge the PR,
so that completed work lands in the main branch.

#### Acceptance Criteria

- AC1: Agent prompt file at `agents/merge-agent.md`
- AC2: Gate checklist verified: scope certificate exists, all review comments resolved, tests passing, lint passing, build passing, no merge conflicts
- AC3: If merge conflict in files NOT in implementation plan → resolve by taking base branch version
- AC4: If merge conflict in files IN implementation plan → immediate Telegram escalation, no auto-resolution
- AC5: On success: merge PR using configured `merge_method`, transition Jira to Done, post Jira comment, send Telegram success notification
- AC6: On failure at any gate: log which gate failed, notify via Telegram with specifics
- AC7: Workspace status set to `completed` on merge, `failed` on unresolvable failure

### Story 7.2: Safeguard Wiring

As the **operator**,
I want all safeguards active and tested,
so that the pipeline cannot silently break rules.

#### Acceptance Criteria

- AC1: File write monitor: any agent attempting to write to `arch-rules.md` or lint config paths triggers immediate abort + Telegram alert
- AC2: Iteration caps enforced for all looping stages (scope guard, fix, QA) with correct Telegram escalation messages
- AC3: Workspace-level exception isolation: one workspace crashing doesn't affect others
- AC4: Daemon crash recovery: restart discovers all active workspaces and resumes from state
- AC5: Human reply timeout: configurable reminder sent via Telegram if no reply after N hours
- AC6: All safeguard scenarios have corresponding log entries for debugging

### Story 7.3: End-to-End Dry Run

As the **operator**,
I want to run a full pipeline test against a real project,
so that I can validate the system works before relying on it.

#### Acceptance Criteria

- AC1: `--dry-run` mode executes the full pipeline but skips: git push, PR creation, Jira transitions, merge
- AC2: Dry run still: clones repo, creates workspace, runs agents, writes context files, runs lint/tests locally
- AC3: A test scenario script creates a mock Jira ticket payload and feeds it into the pipeline
- AC4: Pipeline completes all stages and produces: implementation plan, code on local branch, test results, scope certificate
- AC5: All logs are written and can be reviewed for debugging
- AC6: Dry run results in a summary report: stages completed, agents invoked, time per stage, total tokens used

### Story 7.4: Deployment & systemd Setup

As the **operator**,
I want the pipeline deployable as a systemd service on an Ubuntu VPS,
so that it runs 24/7 unattended.

#### Acceptance Criteria

- AC1: A `deploy/` directory contains: systemd unit file, setup script, environment file template
- AC2: Setup script: installs Python deps, creates workspace directory, validates config
- AC3: systemd unit: `Restart=always`, `RestartSec=10`, runs as dedicated `pipeline` user
- AC4: Environment file template lists all required env vars with descriptions
- AC5: `deploy/README.md` documents: VPS requirements, setup steps, first-run validation
- AC6: Pipeline logs to configurable directory (`logging.dir` from config) with rotation

---

## Checklist Results Report

To be completed after PRD review — will run `pm-checklist` to validate completeness.

---

## Next Steps

### Architect Prompt

This PRD provides the full product requirements for Cleave. Please review the PRD and the original technical spec (`docs/legacy/start.md`) thoroughly, then create the architecture document: module interfaces, agent prompt format specification, config schemas, state machine definition, and integration adapter contracts. Pay special attention to the BMAD-style agent system — define how agent prompt files are structured, loaded, and executed.

# Sickle — Implementation Plan v2

**Author:** John (PM Agent) / Oleksandr
**Date:** 2026-04-08
**Based on:** `docs/architecture-v2.md`, `docs/agent-contracts.md`

---

## Executive Summary

18 of 23 required files already exist and are functional. The pipeline is ~60% built but 0% wired. The path to a working end-to-end is: update foundations → add tool execution → wire integrations → test with one real repo (Faria/Managebac on GitHub). GitLab/Jenkins support comes after the core loop works.

---

## Code Audit Summary

| Component | Status | Lines | Assessment |
|-----------|--------|-------|------------|
| orchestrator/orchestrator.py | EXISTS | 192 | Functional — needs Jira polling + adapter wiring |
| orchestrator/workflow_router.py | EXISTS | 147 | Functional — needs new state names |
| orchestrator/agent_runtime.py | EXISTS | 197 | Functional — needs tool_use support |
| **orchestrator/tool_sandbox.py** | **MISSING** | - | **New in v2** |
| orchestrator/safeguards.py | EXISTS | 112 | Functional |
| orchestrator/ticket_prioritizer.py | EXISTS | 208 | Functional |
| config/config_loader.py | EXISTS | 327 | Functional |
| config/resource_registry.py | EXISTS | 216 | Functional |
| config/schemas.py | EXISTS | 214 | Needs vcs.provider, ci.provider, helpers |
| workspace/workspace_manager.py | EXISTS | 187 | Needs new directory structure |
| workspace/workspace.py | EXISTS | 149 | Needs new state machine |
| integrations/base/tracker.py | EXISTS | 48 | Functional |
| integrations/base/vcs.py | EXISTS | 67 | Needs merge_pr removal |
| **integrations/base/ci.py** | **MISSING** | - | **New in v2** |
| integrations/base/notifier.py | EXISTS | 29 | Functional |
| integrations/jira/jira_adapter.py | EXISTS | 215 | Functional |
| integrations/github/github_adapter.py | EXISTS | 196 | Functional |
| **integrations/gitlab/** | **MISSING** | - | **New in v2** |
| **integrations/jenkins/** | **MISSING** | - | **New in v2** |
| integrations/telegram/telegram_adapter.py | EXISTS | 91 | Needs threading support |
| integrations/llm/claude_adapter.py | EXISTS | 74 | Needs tool_use support |
| integrations/llm/llm_interface.py | EXISTS | 38 | Needs tool_use in interface |
| **agents/pr-comment-responder-agent.md** | **MISSING** | - | **New in v2** |

---

## Phased Plan

### Phase 1: Foundation Updates
**Goal:** Align existing code with architecture-v2 decisions.
**Estimated stories:** 5
**Dependencies:** None — can start immediately.

### Phase 2: Agent Execution Model
**Goal:** Agents can execute tools (read/write files, run commands, git).
**Estimated stories:** 3
**Dependencies:** Phase 1 (state machine + config).

### Phase 3: Integration Wiring
**Goal:** Orchestrator talks to Jira, GitHub, Telegram end-to-end.
**Estimated stories:** 4
**Dependencies:** Phase 2 (agents need tools to be useful).

### Phase 4: Agent Prompts & First Run
**Goal:** All agents updated to v2 contracts. First real ticket processed.
**Estimated stories:** 4
**Dependencies:** Phase 3 (integrations wired).

### Phase 5: Second Provider Support
**Goal:** GitLab + Jenkins adapters. Multi-company operational.
**Estimated stories:** 4
**Dependencies:** Phase 4 (core loop proven on GitHub).

### Phase 6: Hardening & Deployment
**Goal:** Production-ready. Systemd, monitoring, full test suite.
**Estimated stories:** 3
**Dependencies:** Phase 5 (all adapters working).

---

## Phase 1: Foundation Updates

> Align existing code with architecture-v2. No new features — just updating contracts.

### Story 1.1: Update State Machine

**File:** `workspace/workspace.py`
**What changes:**
- Replace `VALID_STATUSES` with: `NEW, ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW, DONE, BLOCKED, FAILED, ARCHIVED`
- Replace `VALID_TRANSITIONS` with the transition table from architecture-v2 §3.3
- Add `previous_state` field to `WorkspaceState` (for BLOCKED → resume)
- Rename `status` → `current_state` in WorkspaceState to match architecture-v2
- Update `transition_status()` to support BLOCKED → resume_previous logic

**Acceptance Criteria:**
- [ ] All 11 states defined
- [ ] All valid transitions work; invalid transitions raise error
- [ ] BLOCKED stores previous_state; resuming returns to it
- [ ] Atomic writes preserved
- [ ] Unit tests updated for new states

### Story 1.2: Update Workspace Directory Structure

**File:** `workspace/workspace_manager.py`
**What changes:**
- Update `create()` to produce new structure: `meta/`, `reports/`, `logs/`, `source/` (instead of `context/`, `logs/`, `repo/`)
- Update path: `/<base_dir>/<company_id>/<repo_id>/tickets/<ticket_id>/` (instead of `/<base_dir>/<project_id>/<repo_id>/<ticket_id>_<timestamp>/`)
- Add `cleanup_source()` method (delete `/source/` only, keep everything else)
- Update `discover_workspaces()` to scan new path structure

**File:** `workspace/workspace.py`
- Update path properties: `repo_dir` → `source_dir`, `context_dir` → split into `meta_dir` and `reports_dir`

**Acceptance Criteria:**
- [ ] New directory structure created on workspace init
- [ ] Source-only cleanup works (meta + reports preserved)
- [ ] Discovery works with new path layout
- [ ] Old unit tests updated

### Story 1.3: Update Config Schemas

**File:** `config/schemas.py`
**What changes:**
- Add `VCSConfig` with `provider` field ("github" / "gitlab")
- Add `CIConfig` with `provider` field ("github_actions" / "jenkins")
- Add `HelpersConfig` with paths to existing helper scripts
- Rename `GitHubConfig` fields to sit under `vcs.github`
- Add `GitLabConfig` dataclass (for future Phase 5)
- Add `JenkinsConfig` dataclass (for future Phase 5)

**File:** `config/config_loader.py`
- Update `_load_repo_config()` to parse new vcs/ci/helpers sections

**Acceptance Criteria:**
- [ ] Repo config supports `vcs.provider` and `ci.provider`
- [ ] Helper script paths parsed from config
- [ ] Backward-compatible: existing test fixtures still load
- [ ] Validation errors for missing provider field

### Story 1.4: Update Workflow Router

**File:** `orchestrator/workflow_router.py`
**What changes:**
- Update stage IDs to match new state machine: `analysis`, `dev`, `scope_check`, `qa`, `push`, `pr_review`, `done`
- Add `action` stages (non-agent stages handled by orchestrator)
- Add `delay_minutes` support for `pr_review` stage
- Ensure `get_next_stage()` handles both agent stages and action stages

**File:** `workflows/default-workflow.yaml`
- Update to match architecture-v2 §6.2 workflow definition

**Acceptance Criteria:**
- [ ] Workflow YAML matches architecture-v2
- [ ] Router distinguishes agent stages from action stages
- [ ] Delay support for PR review stage
- [ ] Unit tests updated

### Story 1.5: Update Feature Tracker

**File:** `docs/features/index.md`
**What changes:**
- Mark features 1-4 as "Implemented" (they are)
- Add new feature: "PR Comment Responder"
- Add new feature: "GitLab Integration"
- Add new feature: "Jenkins Integration"
- Add new feature: "Tool Sandbox"
- Update descriptions to match architecture-v2

**Acceptance Criteria:**
- [ ] Index reflects actual implementation state
- [ ] New features have entries

---

## Phase 2: Agent Execution Model

> The critical unlock: agents can execute tools via Claude function calling.

### Story 2.1: Tool Sandbox

**New file:** `orchestrator/tool_sandbox.py`
**What to build:**
- `ToolSandbox` class that executes tool calls within workspace restrictions
- Tool implementations: `read_file`, `write_file`, `list_directory`, `search_code`, `run_command`, `git_operation`
- Path validation: all file operations restricted to workspace `source/` and `reports/`
- Protected file enforcement: check against `architecture.protected_files` from repo config
- Tool call logging: every call logged to agent's log file
- Per-agent tool allowlist (from agent contracts)

**Key interface:**
```python
class ToolSandbox:
    def __init__(self, workspace: Workspace, allowed_tools: list[str], protected_files: list[str])
    async def execute_tool(self, tool_name: str, tool_input: dict) -> str
```

**Acceptance Criteria:**
- [ ] All 6 tools implemented and functional
- [ ] Path traversal blocked (can't escape workspace)
- [ ] Protected files can't be written
- [ ] Tool not in allowlist raises error
- [ ] Every tool call logged
- [ ] Unit tests for each tool + security restrictions

### Story 2.2: Claude Adapter — Tool Use Support

**File:** `integrations/llm/llm_interface.py`
**What changes:**
- Add `tools` parameter to `send_message()`
- Add `tool_use` and `tool_result` to LLMResponse model
- Add `send_message_with_tools()` method for multi-turn tool execution

**File:** `integrations/llm/claude_adapter.py`
**What changes:**
- Pass `tools` parameter to Anthropic API `messages.create()`
- Handle `tool_use` content blocks in response
- Support multi-turn: send tool results back, loop until final text response
- Add `max_tool_rounds` limit (default: 50)

**Acceptance Criteria:**
- [ ] Claude API called with tools parameter
- [ ] Tool use blocks parsed from response
- [ ] Multi-turn tool loop works (call → result → call → ... → text)
- [ ] Max rounds limit prevents infinite loops
- [ ] Token tracking includes all rounds
- [ ] Unit tests with mocked responses

### Story 2.3: Agent Runtime — Tool Integration

**File:** `orchestrator/agent_runtime.py`
**What changes:**
- Load tool allowlist from agent contract (BMAD metadata `tools:` field)
- Create `ToolSandbox` per execution with workspace + allowlist + protected files
- Replace single LLM call with tool_use loop:
  1. Send prompt + tools to Claude
  2. For each tool_use in response → execute via sandbox → collect results
  3. Send tool results back to Claude
  4. Repeat until final text response
- Write agent output to `reports/<agent>.md` (instead of `context/<agent>-output.md`)

**Acceptance Criteria:**
- [ ] Agent runtime creates sandbox with correct tool allowlist per agent
- [ ] Tool calls executed and results fed back to Claude
- [ ] Agent output written to reports/ directory
- [ ] Protected file violation during tool execution → agent fails with clear error
- [ ] Execution logging includes tool call count and details
- [ ] Integration test: mock Claude returning tool_use, verify sandbox execution

---

## Phase 3: Integration Wiring

> Connect the orchestrator to external systems. First target: GitHub (Faria/Managebac).

### Story 3.1: Wire Jira Polling into Orchestrator

**File:** `orchestrator/orchestrator.py`
**What changes:**
- Accept `TrackerInterface` in constructor
- In `poll_cycle()`: call `tracker.poll_tickets()` to get new tickets
- Use `ticket_prioritizer.py` to filter/route/sort
- For each actionable ticket: check slot availability → create workspace → fetch full ticket → write `meta/ticket.md`
- Fetch parent ticket if exists → write `meta/parent.md`

**Integration:** Use existing `jira_adapter.py` which is already functional.

**Helper script integration:**
- Option A: Use existing `JiraAdapter` class (httpx-based)
- Option B: Wrap `fetch_jira_tickets.py` as subprocess
- **Decision:** Use existing `JiraAdapter` for polling (it's already built). Use helper script as fallback reference.

**Acceptance Criteria:**
- [ ] Orchestrator polls Jira on each cycle
- [ ] New tickets create workspaces with correct directory structure
- [ ] Ticket data written to `meta/ticket.md` (markdown, not JSON)
- [ ] Parent ticket fetched and written to `meta/parent.md`
- [ ] Slot limits enforced before workspace creation
- [ ] Dry-run mode logs but doesn't create workspaces
- [ ] Integration test with mock Jira responses

### Story 3.2: Wire VCS (GitHub) into Push/PR Stage

**File:** `orchestrator/orchestrator.py`
**What changes:**
- Accept `VCSInterface` in constructor
- Implement `push_and_open_pr` action handler:
  1. `git push origin {branch}` via subprocess in workspace source dir
  2. `vcs.open_pr(workspace, title, body)` using PR description template
  3. Store `pr_number`, `pr_url` in state.json
  4. Transition Jira to "In Review" + post comment with PR link

**File:** `integrations/base/vcs.py`
- Remove `merge_pr()` from interface (merge is human's job)
- Add `close_pr()` if not present

**File:** `integrations/github/github_adapter.py`
- Remove `merge_pr()` implementation
- Verify `open_pr()` and `get_pr_comments()` work with existing helper scripts

**Acceptance Criteria:**
- [ ] Push action pushes branch to origin
- [ ] PR opened with templated description
- [ ] PR number and URL stored in state.json
- [ ] Jira transitioned to In Review
- [ ] merge_pr removed from interface and adapter

### Story 3.3: Wire Telegram into Escalation

**File:** `orchestrator/orchestrator.py`
**What changes:**
- Accept `NotifierInterface` in constructor
- Implement `notify_human` action handler:
  1. Format message with `[COMPANY/REPO] {ticket_id}` prefix
  2. Send via `notifier.send_message(text, thread_id=ticket_id)`
  3. Store `message_id` in state.json
  4. Set state → BLOCKED

**File:** `integrations/telegram/telegram_adapter.py`
**What changes:**
- Add `thread_id` parameter to `send_message()` for ticket-based threading
- Use `reply_to_message_id` to thread messages
- Maintain mapping: `ticket_id → first_message_id` for threading

- Implement reply polling: match incoming replies to ticket's thread
- Add heartbeat method for daily summary

**Acceptance Criteria:**
- [ ] Escalation sends formatted Telegram message
- [ ] Messages threaded by ticket_id
- [ ] Human reply received and routed to correct workspace
- [ ] Workspace resumes after reply
- [ ] Heartbeat sends daily summary

### Story 3.4: Wire Orchestrator Main Loop End-to-End

**File:** `orchestrator/orchestrator.py`
**What changes:**
- Rewrite `advance_workspace()` to handle both agent stages and action stages:
  - Agent stages → dispatch via agent_runtime
  - Action stages (`push_and_open_pr`, `fetch_pr_comments`, `notify_human`, `finalize`) → execute directly
- Add PR comment delay handling (wait N minutes before fetching)
- Add `finalize` action: send Telegram "ready for merge" + Jira comment
- Wire all adapters together in `main.py` constructor

**File:** `main.py`
**What changes:**
- Instantiate all adapters based on config (Jira, GitHub, Telegram)
- Pass adapters to Orchestrator constructor
- Select VCS adapter based on `vcs.provider` in repo config

**Acceptance Criteria:**
- [ ] Full pipeline: Jira poll → workspace → agents → push → PR → Telegram
- [ ] Action stages execute correctly
- [ ] PR comment fetch respects delay
- [ ] Finalize sends notifications
- [ ] One workspace failure doesn't crash others
- [ ] Dry-run mode works end-to-end

---

## Phase 4: Agent Prompts & First Run

> Update all agent prompt files to v2 contracts. Run first real ticket.

### Story 4.1: Update Existing Agent Prompt Files

**Files:** All 6 existing agents in `agents/`
**What changes for each:**
- Add `tools:` declaration to YAML frontmatter (from agent contracts)
- Add `inputs:` and `outputs:` declarations
- Update file paths in instructions: `context/` → `reports/`, `ticket.json` → `meta/ticket.md`
- Update output paths: `context/implementation-plan.md` → `reports/ba.md`
- Add `decision_policy:` section
- Ensure constraints match agent contracts document

**Specific per agent:**
- **pm-agent.md**: Add inputs/outputs, no tools (pure analysis)
- **ba-agent.md**: Add `read_file`, `list_directory`, `search_code` to tools. Update output to single `reports/ba.md`
- **dev-agent.md**: Already has most tooling context. Update paths, add explicit tool declarations
- **scope-guard-agent.md**: Add `git_operation` (read-only). Update paths
- **qa-agent.md**: Add `run_command`, `write_file`. Update paths
- **fix-agent.md**: Add all code tools. Update comment classification to match contracts

**Acceptance Criteria:**
- [ ] All 6 agents have tools declared in frontmatter
- [ ] All agents have inputs/outputs matching contracts
- [ ] All file path references updated to v2 structure
- [ ] Resource registry still discovers all agents after changes

### Story 4.2: Create PR Comment Responder Agent

**New file:** `agents/pr-comment-responder-agent.md`
**What to build:**
- Full BMAD-style agent prompt per contract spec
- Persona: Rivera, PR Review Analyst
- Extreme skepticism instructions (from existing helper workflow rules)
- Comment classification logic (fix_required / explanation / out_of_scope / arch_violation)
- Output format: `reports/pr-comments.md`
- Read-only tools only (no write_file, no run_command)

**Reference:** Port workflow rules from `/mnt/shared/ubuntu/f/pr_comments/400-pr-review-workflow.mdc`

**Acceptance Criteria:**
- [ ] Agent file created with full BMAD metadata
- [ ] Classification categories match contract
- [ ] Extreme skepticism rules ported from existing workflow
- [ ] Output format matches contract spec
- [ ] Resource registry discovers the new agent

### Story 4.3: Create Real Config for Faria/Managebac

**New files:**
- `config/global.yaml` (real config, not test fixture)
- `config/projects/faria/project.yaml`
- `config/projects/faria/repos/managebac.yaml`

**What to build:**
- Real Jira connection config (env vars for secrets)
- Real GitHub connection config
- Real Telegram bot config
- Helper script paths pointing to `/mnt/shared/ubuntu/f/`
- Architecture rules and protected files for Managebac repo

**Also create:**
- `environment.template` updated with all required env vars

**Acceptance Criteria:**
- [ ] Config loads without errors (with env vars set)
- [ ] Jira adapter can poll real tickets
- [ ] GitHub adapter can read real repo
- [ ] Telegram adapter can send test message

### Story 4.4: First End-to-End Run (Dry Run)

**Goal:** Process one real Jira ticket through the full pipeline in dry-run mode, then wet-run on a test ticket.

**Dry run steps:**
1. Label a test Jira ticket with `ai-pipeline`
2. Run: `python main.py --config ./config --project faria --repo managebac --dry-run`
3. Verify: ticket discovered, workspace created, agents executed, reports generated
4. Review: all reports in `meta/` and `reports/` for quality

**Wet run steps:**
1. Create a simple, well-defined test ticket in Jira
2. Run without `--dry-run`
3. Verify: code generated, PR opened, Telegram notifications sent
4. Human reviews and merges the PR

**Acceptance Criteria:**
- [ ] Dry run completes all stages without errors
- [ ] Wet run produces a real PR with meaningful code changes
- [ ] Agent reports are useful and accurate
- [ ] Telegram notifications received
- [ ] Jira transitions happen correctly

---

## Phase 5: Second Provider Support

> Add GitLab + Jenkins. Enable multi-company operation.

### Story 5.1: CI Interface + GitHub Actions Adapter

**New file:** `integrations/base/ci.py`
- `CIInterface` abstract class: `get_build_status()`, `get_failure_logs()`
- `BuildStatus` dataclass

**New file:** `integrations/github/github_actions_adapter.py`
- Implements `CIInterface` for GitHub Actions
- Wraps `fetch_ci_failure.py` helper as subprocess

**Acceptance Criteria:**
- [ ] CI interface defined with clear contract
- [ ] GitHub Actions adapter fetches build status and failure logs
- [ ] QA agent can use CI status to determine pass/fail

### Story 5.2: GitLab Adapter

**New files:** `integrations/gitlab/gitlab_adapter.py`, `integrations/gitlab/__init__.py`
- Implements `VCSInterface` for GitLab
- Wraps existing helpers: `fetch.py`, `resolve.py`, `review.sh`, `post-comments.sh`
- Subprocess integration (same pattern as Jira helper wrapping)

**Acceptance Criteria:**
- [ ] GitLab adapter implements full VCSInterface
- [ ] MR creation, comment fetching, comment resolving all work
- [ ] Configured via `vcs.provider: gitlab` in repo config

### Story 5.3: Jenkins Adapter

**New files:** `integrations/jenkins/jenkins_adapter.py`, `integrations/jenkins/__init__.py`
- Implements `CIInterface` for Jenkins
- Wraps existing `fetch.sh` helper as subprocess

**Acceptance Criteria:**
- [ ] Jenkins adapter fetches build status and console logs
- [ ] Configured via `ci.provider: jenkins` in repo config

### Story 5.4: Multi-Company Config & Second Repo Test

**New files:**
- `config/projects/brazole/project.yaml`
- `config/projects/brazole/repos/gifture.yaml` (or similar GitLab repo)

**What to build:**
- Real config for a GitLab-based repo
- Verify adapter selection works per-repo (GitHub for Faria, GitLab for BRazole)

**Acceptance Criteria:**
- [ ] Two companies configured simultaneously
- [ ] Different VCS providers per company work
- [ ] Orchestrator manages workspaces across both

---

## Phase 6: Hardening & Deployment

> Production-ready. Monitoring, testing, deployment.

### Story 6.1: Comprehensive Test Suite

**What to build/update:**
- Unit tests for all new code (tool_sandbox, CI interface, GitLab/Jenkins adapters)
- Integration tests with mocked external APIs
- E2E test: scripted scenario with fake ticket through full pipeline
- Test fixtures for new config structure

**Acceptance Criteria:**
- [ ] All unit tests pass
- [ ] Integration tests cover all adapter methods
- [ ] E2E dry-run test passes in CI

### Story 6.2: Reopen Detection & Source Cleanup

**File:** `orchestrator/orchestrator.py`
**What to build:**
- Reopen detection: compare polled ticket against existing `meta/ticket.md`
- If changed: update `meta/diff_log.json`, re-enter pipeline from ANALYSIS
- Source cleanup endpoint/hook: API or filesystem watcher that triggers `workspace_manager.cleanup_source()`

**Acceptance Criteria:**
- [ ] Changed ticket detected and re-processed
- [ ] diff_log.json tracks changes
- [ ] Source cleanup preserves ticket artifacts

### Story 6.3: Deployment & Monitoring

**Files:** `deploy/sickle.service`, `deploy/setup.sh`
**What to update:**
- Systemd unit for v2 (new config path, env vars)
- Setup script for new directory structure
- Log rotation configuration
- Heartbeat monitoring via Telegram

**Acceptance Criteria:**
- [ ] Daemon starts and runs via systemd
- [ ] Survives restart (workspace discovery + resume)
- [ ] Daily heartbeat received via Telegram
- [ ] Logs rotate properly

---

## Priority & Dependency Map

```
Phase 1 ──────► Phase 2 ──────► Phase 3 ──────► Phase 4
(foundation)    (tools)         (wiring)         (agents + first run)
                                                       │
                                                       ▼
                                                 Phase 5 ──────► Phase 6
                                                 (GitLab/Jenkins) (production)
```

**Critical path:** 1 → 2 → 3 → 4 (first working pipeline)
**Can parallelize:** Phase 5 stories are independent of each other

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Claude tool_use produces unexpected tool calls | Agent writes wrong files | Tool sandbox enforces path restrictions + protected files |
| Agent loops forever on tool calls | Token burn, no progress | `max_tool_rounds` limit (50), per-agent timeout |
| Jira rate limiting during heavy polling | Missed tickets | Backoff + longer poll interval under load |
| Large repo clones fill disk | New workspaces can't be created | `min_free_disk_gb` check, shallow clones |
| Helper scripts break after upstream changes | Integration failures | Helper scripts are read-only references; adapters handle errors gracefully |
| First wet run produces bad PR | Reputation risk | Test on a low-stakes test ticket first; dry-run validates flow |

---

## Definition of Done (per phase)

- [ ] All stories in the phase have passing acceptance criteria
- [ ] No regressions in existing unit tests
- [ ] Code reviewed (by human or Sickle's own review process)
- [ ] Architecture-v2 decisions respected (verified against docs/decisions/)
- [ ] Feature index updated to reflect status

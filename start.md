# Autonomous AI Development Pipeline — Master Implementation Prompt v2

---

## SELF-VERIFICATION INSTRUCTIONS (READ FIRST)

Before implementing **anything**, you must:

1. Read this entire document from top to bottom without skipping sections
2. Produce a structured summary covering:
   - The full configuration hierarchy and what each file controls
   - The workspace isolation model and lifecycle
   - How multi-repo and multi-project support works
   - How parallelism is achieved without worktrees
   - All agents, their responsibilities, inputs, and outputs
   - What already exists vs. what needs to be built
   - All safeguards and their trigger conditions
3. List any ambiguities or missing information and ask for clarification
4. Only after receiving explicit confirmation — begin implementation

Do not write a single line of code before completing the self-verification step.

---

## PROJECT GOAL

Build a **platform-grade, fully autonomous, 24/7 AI-driven software development pipeline** that:

- Runs as a persistent daemon on a VPS
- Monitors Jira for new tickets across multiple projects and repositories
- Executes each ticket in a fully isolated workspace (no shared state between tickets)
- Writes code, commits, opens PRs, handles review, runs tests, merges
- Contacts the human via Telegram **only** when genuinely stuck
- Is configured entirely via files — no hardcoded values anywhere
- Can be started for any project or repository by pointing at a config directory
- Handles multiple tickets in parallel by spinning up isolated workspaces

The human's role: **approver and unlocker**, not a participant in the normal flow.

---

## DESIGN PRINCIPLES

These are non-negotiable architectural principles. Every implementation decision must respect them.

1. **Configuration over code** — All project-specific, repo-specific, and environment-specific values live in config files. The pipeline code itself has zero hardcoded values.

2. **Workspace isolation** — Each ticket execution lives in a completely isolated directory clone. No ticket shares a filesystem with another ticket. No worktrees.

3. **Stateless agents** — Agents do not hold state in memory. They read everything from disk (config + context files), do their work, write output to disk, and exit. The orchestrator manages state.

4. **Idempotency** — The orchestrator can be stopped and restarted at any time. It resumes from exactly where each ticket was left off by reading `state.json` from each workspace.

5. **Pluggable integrations** — Jira, GitHub, Telegram, linters, test runners are all adapters behind interfaces. Swapping one for another requires only a config change and a new adapter module, not changes to pipeline logic.

6. **Hierarchical configuration** — Settings cascade: global defaults → project overrides → repo overrides. A repo only needs to specify what differs from its project; a project only needs to specify what differs from global.

7. **No worktrees** — Parallelism is achieved through isolated directory clones, not git worktrees. Worktrees share the git object store and cause conflicts under concurrent writes. Each ticket gets a clean `git clone`.

---

## REPOSITORY STRUCTURE OF THE PIPELINE TOOL ITSELF

```
ai-pipeline/
├── main.py                    # Entry point — starts the orchestrator
├── orchestrator/
│   ├── orchestrator.py        # Main loop, slot management, workspace spawning
│   ├── state_machine.py       # Pipeline state transitions
│   └── scheduler.py           # Ticket prioritization logic
├── agents/
│   ├── base_agent.py          # Abstract base: reads context, writes output, logs
│   ├── pm_agent.py
│   ├── ba_agent.py
│   ├── dev_agent.py
│   ├── scope_guard_agent.py
│   ├── fix_agent.py
│   ├── qa_agent.py
│   └── merge_agent.py
├── integrations/
│   ├── base/
│   │   ├── tracker.py         # Abstract ticket tracker interface
│   │   ├── vcs.py             # Abstract VCS interface
│   │   └── notifier.py        # Abstract notification interface
│   ├── jira/
│   │   └── jira_adapter.py
│   ├── github/
│   │   └── github_adapter.py
│   └── telegram/
│       └── telegram_adapter.py
├── workspace/
│   ├── workspace_manager.py   # Creates, clones, archives workspaces
│   └── context.py             # Reads/writes context files within a workspace
├── config/
│   └── config_loader.py       # Loads and merges the config hierarchy
└── scripts/                   # User-provided existing scripts (not built by pipeline)
    ├── ticket-to-prompt.py    # EXISTING — do not modify
    └── copilot-validator.py   # EXISTING — do not modify
```

---

## CONFIGURATION HIERARCHY

### Overview

```
~/.ai-pipeline/                     ← or any directory passed via --config flag
├── global.yaml                     ← global credentials and defaults
└── projects/
    ├── faria/
    │   ├── project.yaml            ← Jira settings, project-level defaults
    │   ├── shared/
    │   │   └── arch-rules.md       ← shared architecture rules for this project
    │   └── repos/
    │       ├── android-app.yaml    ← repo-specific settings
    │       └── backend-api.yaml
    └── another-project/
        ├── project.yaml
        ├── shared/
        │   └── arch-rules.md
        └── repos/
            └── mobile.yaml
```

The pipeline discovers all projects by scanning `projects/` subdirectories.  
A repo is active if its yaml file exists and `enabled: true` (default).

---

### global.yaml

```yaml
# Global defaults — overridable at project or repo level

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"       # env var reference, never raw value
  default_chat_id: "${TELEGRAM_CHAT_ID}"   # fallback if project doesn't specify

claude:
  api_key: "${CLAUDE_API_KEY}"
  model: "claude-opus-4-5"

workspaces:
  base_dir: "/workspaces"
  max_age_days: 7            # auto-cleanup workspaces older than N days
  isolation: "directory"     # "directory" | "docker" | "vm" (see Isolation section)

defaults:
  poll_interval_seconds: 900
  max_fix_iterations: 3
  max_scope_iterations: 3
  max_qa_iterations: 2
  max_parallel_tickets: 2    # across all repos on this machine

logging:
  level: "INFO"
  dir: "/var/log/ai-pipeline"
```

---

### projects/{project-id}/project.yaml

```yaml
project:
  id: "faria"
  name: "Faria Education Platform"
  enabled: true

jira:
  url: "https://faria.atlassian.net"
  token: "${FARIA_JIRA_TOKEN}"
  email: "${FARIA_JIRA_EMAIL}"
  project_key: "FARIA"
  trigger_label: "ai-ready"           # tickets with this label are picked up
  ignore_labels: ["blocked", "wont-do"]
  statuses:
    todo: "To Do"
    in_progress: "In Progress"
    in_review: "In Review"
    done: "Done"

telegram:
  chat_id: "${FARIA_TELEGRAM_CHAT_ID}" # overrides global default_chat_id

parallelism:
  max_concurrent_tickets: 3           # across all repos in this project

# Overrides global defaults for this project
defaults:
  poll_interval_seconds: 600
  max_fix_iterations: 3
```

---

### projects/{project-id}/repos/{repo-id}.yaml

```yaml
repo:
  id: "android-app"
  name: "Faria Android App"
  enabled: true

github:
  token: "${FARIA_GITHUB_TOKEN}"
  owner: "faria-edu"
  repo: "android-app"
  default_branch: "develop"
  branch_prefix: "feature"              # branches: feature/FARIA-123-slug
  merge_method: "squash"                # "merge" | "squash" | "rebase"

git:
  clone_url: "git@github.com:faria-edu/android-app.git"
  commit_author_name: "Claude Code"
  commit_author_email: "claude@anthropic.com"
  depth: 0                              # 0 = full clone; N = shallow clone depth

architecture:
  rules_file: "../shared/arch-rules.md" # relative to this repo yaml's directory
  # To use a repo-specific override instead:
  # rules_file: "./android-arch-rules.md"

linting:
  tool: "detekt"                        # detekt | eslint | ktlint | swiftlint | etc.
  config_file: "config/detekt/detekt.yml"
  run_command: "./gradlew detekt"
  report_path: "build/reports/detekt/detekt.xml"
  hard_gate: true                       # if true, pipeline stops on lint failure

testing:
  run_command: "./gradlew test"
  report_path: "build/reports/tests/"
  hard_gate: true

build:
  check_command: "./gradlew assembleDebug"  # used to verify the code compiles
  hard_gate: true

existing_scripts:
  ticket_to_prompt: "../../scripts/ticket-to-prompt.py"
  copilot_validator: "../../scripts/copilot-validator.py"

copilot:
  enabled: true
  wait_for_review_minutes: 15           # how long to wait for Copilot to post review

jira_repo_label: "repo:android-app"    # Jira tickets must have this label to be routed here
                                        # allows per-repo ticket routing within a project

pr_description_template: |
  ## 🎫 Jira: [{ticket_id}]({ticket_url})

  ## Summary
  {summary}

  ## Changed Files
  {changed_files}

  ## AI Pipeline Checklist
  - [x] Scope verified by Scope Guard Agent
  - [x] Architecture rules validated
  - [{tests_check}] All tests passing
  - [{lint_check}] Detekt passing
  - [x] Commits reference ticket ID

parallelism:
  max_concurrent_tickets: 2            # for this specific repo
```

---

## WORKSPACE ISOLATION MODEL

### Why not worktrees

Git worktrees share the object store and the lock files of the parent repository. Under concurrent writes from multiple agents, worktrees cause corruption, lock conflicts, and unpredictable behavior. They are explicitly not used in this pipeline.

### The workspace model

Every ticket execution gets a **completely independent directory** containing a fresh `git clone`. There is no shared filesystem between concurrent ticket workspaces.

```
/workspaces/
└── {project-id}/
    └── {repo-id}/
        └── {ticket-id}_{timestamp}/       ← workspace root
            ├── repo/                       ← fresh git clone of the repository
            │   └── (full repo contents)
            ├── context/
            │   ├── ticket.json             ← raw ticket data from Jira
            │   ├── implementation-plan.md  ← written by BA Agent
            │   ├── scope-report.md         ← written by Scope Guard Agent
            │   ├── test-scenarios.md       ← written by BA Agent
            │   └── scope-certificate.md    ← written by Scope Guard on pass
            ├── state.json                  ← pipeline state machine (see below)
            └── logs/
                ├── pipeline.log
                ├── ba-agent.log
                ├── dev-agent.log
                ├── scope-guard.log
                ├── fix-agent.log
                ├── qa-agent.log
                └── merge-agent.log
```

### state.json structure

```json
{
  "ticket_id": "FARIA-123",
  "project_id": "faria",
  "repo_id": "android-app",
  "workspace_root": "/workspaces/faria/android-app/FARIA-123_20260328_143000",
  "branch": "feature/FARIA-123-add-login-screen",
  "pr_number": null,
  "current_stage": "dev_agent",
  "stage_iterations": {
    "scope_guard": 0,
    "fix_agent": 0,
    "qa_agent": 0
  },
  "human_input_pending": false,
  "human_input_question": null,
  "started_at": "2026-03-28T14:30:00Z",
  "last_updated_at": "2026-03-28T14:45:00Z",
  "status": "running"
}
```

`status` values: `running` | `waiting_for_human` | `completed` | `failed` | `archived`

### Workspace lifecycle

```
1. Orchestrator selects ticket
2. WorkspaceManager creates /workspaces/{project}/{repo}/{ticket}_{ts}/
3. git clone repo into workspace/repo/
4. checkout or create feature branch
5. Write initial state.json
6. Agents execute sequentially, each reading/writing their context files
7. On completion (merged) or terminal failure → workspace status = completed/failed
8. Cleanup job removes workspaces older than max_age_days
```

### Parallelism

The orchestrator maintains a slot count per repo and per project. Before spawning a new workspace, it checks:

```python
active = count_active_workspaces(project_id, repo_id)
if active < repo_config.max_concurrent_tickets:
    spawn_workspace(ticket)
else:
    queue_ticket(ticket)  # try again next poll cycle
```

Each workspace runs in its own subprocess or thread. They never share file handles or git state.

---

## ISOLATION LEVELS (configurable per project or globally)

### Level 1: Directory isolation (default)

- Each ticket = fresh `git clone` in its own directory
- Workspaces run as subprocesses on the same machine
- Cheapest, fastest, easiest to debug
- Suitable for low-medium concurrency (up to ~5 parallel tickets)

```yaml
# global.yaml or project.yaml
workspaces:
  isolation: "directory"
```

### Level 2: Docker container isolation (recommended for production)

- Each workspace is a Docker container
- Container mounts a pre-built base image with repo dependencies cached
- Container is destroyed when the ticket is complete
- Full filesystem and process isolation
- Suitable for higher concurrency

```yaml
workspaces:
  isolation: "docker"
  docker:
    base_image: "ai-pipeline-base:latest"  # your pre-built image
    memory_limit: "4g"
    cpu_limit: "2.0"
```

### Level 3: VM/snapshot isolation (maximum safety)

- Each workspace is a VM launched from a snapshot
- Most expensive but fully isolated at the OS level
- Use for sensitive projects or very long-running tasks

```yaml
workspaces:
  isolation: "vm"
  vm:
    snapshot_id: "snap-xxxxx"
    provider: "hetzner"  # or "aws", "do"
```

The pipeline code handles all three through the `WorkspaceManager` abstraction. The agents themselves never know which isolation level is active — they always see the same `workspace_root` directory structure.

---

## MULTI-REPO ROUTING

A single Jira project may have multiple repos. Tickets are routed to repos via **Jira labels**:

```
Jira ticket FARIA-123
  Labels: ["ai-ready", "repo:android-app"]
  → routed to android-app.yaml workspace

Jira ticket FARIA-456
  Labels: ["ai-ready", "repo:backend-api"]
  → routed to backend-api.yaml workspace

Jira ticket FARIA-789
  Labels: ["ai-ready"]              ← no repo label
  → BA Agent flags as ambiguous → asks human which repo
```

If a ticket has no repo label, the BA Agent must ask the human before proceeding.

---

## EXISTING INFRASTRUCTURE (DO NOT REBUILD OR MODIFY)

The following scripts exist and must be integrated as-is. The pipeline calls them as subprocesses.

### 1. ticket-to-prompt.py
- **What it does:** Pulls ticket data from Jira, generates a structured implementation prompt
- **How to call it:**
  ```bash
  python ticket-to-prompt.py --ticket FARIA-123 --output {workspace}/context/prompt.md
  ```
- **Integration point:** Called by BA Agent after requirements validation passes

### 2. copilot-validator.py
- **What it does:** Reads Copilot review comments from a PR, classifies each as valid or invalid
- **How to call it:**
  ```bash
  python copilot-validator.py --pr {pr_number} --repo {owner/repo} --output {workspace}/context/valid-comments.json
  ```
- **Integration point:** Called by orchestrator after Copilot review window has elapsed

### 3. Architecture Rules Document (per project)
- Lives at `projects/{project-id}/shared/arch-rules.md`
- **Must be injected as full context** into: BA Agent, Dev Agent, Scope Guard Agent, Fix Agent
- **Must never be modified** by any agent — this is an absolute hard rule
- If an agent ever tries to write to this file: abort immediately, log error, notify human

### 4. Detekt Configuration
- Lives inside the repository at the path specified in `repo.yaml`
- Runs via `./gradlew detekt` (or the command in config)
- **Must never be modified** by any agent — this is an absolute hard rule
- Failing Detekt = hard gate, pipeline cannot proceed

---

## HOSTED INFRASTRUCTURE

### VPS Setup

- Ubuntu 24 LTS
- The pipeline runs as a **systemd service** (not a cron job — it's a persistent daemon)
- One instance of the pipeline tool can manage all projects and all repos
- Use `--config /path/to/config-dir` to point at the config directory

```ini
# /etc/systemd/system/ai-pipeline.service
[Unit]
Description=AI Development Pipeline
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/ai-pipeline/main.py --config /home/alex/.ai-pipeline
Restart=always
RestartSec=10
User=pipeline
WorkingDirectory=/opt/ai-pipeline

[Install]
WantedBy=multi-user.target
```

### Starting the pipeline for a specific project only (optional)

```bash
# Run for all projects (default)
python main.py --config ~/.ai-pipeline

# Run for one project only
python main.py --config ~/.ai-pipeline --project faria

# Run for one repo only
python main.py --config ~/.ai-pipeline --project faria --repo android-app

# Dry run — polls tickets but doesn't execute
python main.py --config ~/.ai-pipeline --dry-run
```

---

## JIRA INTEGRATION

- Poll for tickets with `trigger_label` and status = `todo`
- Skip tickets with any label in `ignore_labels`
- Skip tickets already assigned to a human (only pick up unassigned or bot-assigned)
- Route ticket to correct repo via `jira_repo_label` in repo config

**Ticket lifecycle transitions:**

| Pipeline event | Jira transition | Jira comment |
|---|---|---|
| Ticket picked up | → In Progress | `🤖 AI Pipeline started at {timestamp}` |
| PR opened | → In Review | `🔗 PR opened: {pr_url}` |
| Merged | → Done | `✅ Merged — PR #{number} at {timestamp}` |
| Failed / escalated | stays current | `⚠️ AI Pipeline escalated to human. Reason: {reason}` |

---

## GITHUB INTEGRATION

- Use `gh` CLI or GitHub REST API
- All operations authenticated via token from repo config
- PR creation uses `pr_description_template` from repo config

**Operations needed:**
- Create branch
- Push commits
- Open PR with template
- Read PR review comments (for Copilot)
- Reply to PR review comments
- Check PR status (all checks green?)
- Merge PR (with merge method from config)
- Close PR (on failure/escalation)

---

## TELEGRAM INTEGRATION

Single bot for all projects. Chat routing per project via `telegram.chat_id` in project.yaml.

### Notification format

All messages include: `[{PROJECT_ID}/{REPO_ID}]` prefix so the human knows context.

| Situation | Message |
|---|---|
| Ticket ambiguous | `🤔 [FARIA/android] FARIA-123: Missing info\n\n{numbered questions}` |
| No repo label on ticket | `🏷 [FARIA] FARIA-123: Which repo? android-app / backend-api` |
| Scope fix loop maxed | `🔁 [FARIA/android] FARIA-123: Scope violations after 3 attempts. PR: {link}\n\n{violation_summary}` |
| Fix loop maxed | `🔁 [FARIA/android] FARIA-123: Review fixes failed after 3 attempts. PR: {link}` |
| Unresolvable merge conflict | `⚠️ [FARIA/android] FARIA-123: Merge conflict in ticket files. Branch: {branch}` |
| Tests failing after retries | `❌ [FARIA/android] FARIA-123: Tests failing after 2 attempts. PR: {link}` |
| Architecture rules violation attempt | `🚨 [FARIA/android] FARIA-123: Agent attempted to modify arch-rules. Aborted.` |
| Build broken | `🔨 [FARIA/android] FARIA-123: Build broken. Need help. Branch: {branch}` |
| Successfully merged | `✅ [FARIA/android] FARIA-123: Merged to {branch}. PR: {pr_url}` |

### Reply handling

- Human replies to any bot message
- Reply is captured and stored in `state.json` as `human_input_reply`
- Waiting agent is unblocked and receives reply as additional context
- Agent resumes from the waiting point

---

## PIPELINE STAGES

Each stage reads its inputs from `workspace/context/` and `workspace/repo/`, writes its outputs to `workspace/context/`, and updates `state.json`.

---

### Stage 0 — PM Agent (Prioritization)

**Input:** List of available tickets from Jira (across all repos in this poll cycle)  
**Config used:** `jira.trigger_label`, `jira.ignore_labels`, `parallelism.max_concurrent_tickets`

**Task:**
- Filter tickets: must have trigger label, must not have ignore labels, must be unassigned or bot-assigned
- Route each ticket to its repo via `jira_repo_label`
- Prioritize by: sprint membership → priority field → ticket age (oldest first)
- Skip tickets whose dependencies (linked Jira issues) are not yet Done
- Check slot availability per repo before scheduling

**Output:** Ordered list of `(ticket_id, repo_id)` tuples to process this cycle

---

### Stage 1 — BA Agent (Requirements Validation)

**Input:**
- `workspace/context/ticket.json` (raw Jira ticket)
- `projects/{project}/shared/arch-rules.md`
- Linked Jira tickets (fetched live)

**Task:**
- Verify acceptance criteria exist and are testable (not vague like "improve performance")
- Identify unstated edge cases not covered by AC
- Check for dependencies on unfinished tickets
- Verify ticket has enough detail to implement without guessing
- Check if ticket references UI specs, API contracts, or external docs — if yes, fetch them
- Verify the ticket's repo label matches the current repo (if missing, ask human)
- Run existing `ticket-to-prompt.py` to get structured implementation prompt

**If anything is unclear → compose precise numbered questions → Telegram → wait**

**If clear → produce:**
- `context/implementation-plan.md`:
  - List of files to CREATE (with purpose)
  - List of files to MODIFY (with what changes and why)
  - List of files that MUST NOT be touched
  - Logic summary
  - Edge cases to handle
  - AC-derived test scenarios (used later by QA Agent)
- `context/test-scenarios.md` (extracted from above for QA Agent)

---

### Stage 2 — Dev Agent (Implementation)

**Input:**
- `context/implementation-plan.md`
- `context/ticket.json`
- `projects/{project}/shared/arch-rules.md`
- Full codebase at `workspace/repo/`

**Hard rules injected into agent context:**
- Only touch files listed in the implementation plan
- Never touch: arch-rules.md, detekt config, CI/CD configs
- Never commit directly to the default branch
- No bonus refactoring, no unsolicited renames, no formatting passes on untouched files
- No new external dependencies unless the ticket explicitly requires them
- Commit format: `feat({ticket_id}): {short description}` — one logical unit per commit
- All commits must be on branch `{branch_prefix}/{ticket_id}-{slug}`

**Output:** Feature branch with meaningful commits, `workspace/repo/` updated

---

### Stage 3 — Scope Guard Agent

**Input:**
- `git diff origin/{default_branch}...HEAD` from `workspace/repo/`
- `context/implementation-plan.md`
- `context/ticket.json`
- `projects/{project}/shared/arch-rules.md`

**Task — for every changed file in the diff:**
1. Is this file in the implementation plan's allowed list? If not → VIOLATION
2. Does every added/modified logic block map to a ticket requirement or AC? If not → VIOLATION
3. Are there formatting-only or rename-only changes in files unrelated to the ticket? → VIOLATION
4. Are there new `import` statements for previously unused libraries? → flag for justification
5. Are module/layer boundaries respected per architecture rules? → VIOLATION if not
6. Are commit messages meaningful and contain the ticket ID? → VIOLATION if not

**If violations found:**
- Write `context/scope-report.md` with precise violation list and fix instructions
- Set state `current_stage = dev_agent` (send back)
- Increment `stage_iterations.scope_guard`
- If `scope_guard` iterations ≥ `max_scope_iterations` → escalate to human

**If clean:**
- Write `context/scope-certificate.md` confirming scope compliance
- Proceed to PR creation

**Max iterations:** from config `max_scope_iterations` (default: 3)

---

### Stage 4 — Copilot Review (automated, not an agent you build)

- Orchestrator opens PR on GitHub after Scope Guard passes
- Waits `copilot.wait_for_review_minutes` (from repo config) for Copilot to post review
- Calls `copilot-validator.py` with PR number
- Validator output → `context/valid-comments.json`
- If no valid comments → skip to QA Agent
- If valid comments → Fix Agent

---

### Stage 5 — Fix Agent (Review Response)

**Input:**
- `context/valid-comments.json`
- Current code at `workspace/repo/`
- `context/implementation-plan.md`
- `projects/{project}/shared/arch-rules.md`

**Task:**
- For each valid comment: apply fix
- After each fix: re-run Detekt (command from repo config)
- After all fixes: re-run Scope Guard logic internally to ensure fixes didn't introduce new violations
- Commit: `fix({ticket_id}): address review — {short description}`
- Reply to each Copilot GitHub comment: describe what was done and why

**Safeguard:** if a valid comment asks to change something that would violate arch-rules or go outside ticket scope → do not apply fix, post reply explaining why, mark comment as "requires human decision", notify human via Telegram

**Max iterations:** from config `max_fix_iterations` (default: 3)  
After max → Telegram escalation

---

### Stage 6 — QA Agent (Testing)

**Input:**
- `context/test-scenarios.md` (from BA Agent)
- Current code at `workspace/repo/`
- `context/ticket.json` (for AC reference)

**Task:**
- Check existing test coverage for the new functionality
- Write new unit tests covering all AC scenarios from `test-scenarios.md`
- Write new unit tests for edge cases identified by BA Agent
- Run full test suite via `testing.run_command` (from repo config)
- Run Detekt via `linting.run_command`
- Run build check via `build.check_command`
- Verify no existing tests were broken

**Rules:**
- Never delete existing tests
- Never modify existing tests unless the ticket explicitly requires a behavior change
- New tests must follow the same conventions as existing tests in the project

**If tests fail after fix attempt:**
- Max `max_qa_iterations` attempts
- After max → Telegram escalation

**Output:** Green test suite, new tests committed, Detekt passing

---

### Stage 7 — Merge Agent

**Input:**
- `context/scope-certificate.md` (must exist)
- GitHub PR status (all checks green?)
- Detekt report (must be clean)
- Test results (must be green)

**Gate checklist (all must be true before merge):**
- [ ] `scope-certificate.md` exists in context
- [ ] All valid Copilot comments resolved
- [ ] Tests passing
- [ ] Detekt passing
- [ ] Build passing
- [ ] No merge conflicts

**If merge conflict:**
- Attempt auto-rebase
- If conflict is in files **not** in the implementation plan → resolve by taking base branch version
- If conflict is in files **in** the implementation plan → do not guess → Telegram escalation immediately

**On success:**
- Merge PR using `github.merge_method` from repo config
- Transition Jira ticket to Done
- Add Jira comment: `✅ Merged — PR #{number} at {timestamp}`
- Notify human: `✅ [PROJECT/REPO] {ticket_id}: Done. PR: {url}`
- Mark workspace `status: completed`

---

## SAFEGUARDS SUMMARY

| Risk | Safeguard |
|---|---|
| Agent goes out of scope | Scope Guard Agent with diff analysis; max 3 iterations |
| Infinite fix/test loop | Per-stage iteration counter in state.json; escalate on max |
| Architecture violation | arch-rules.md injected into every agent; Scope Guard validates |
| Detekt failure | Hard gate — pipeline stops, agent tries to fix, escalates if can't |
| Ambiguous ticket | BA Agent validates before dev; asks human for missing info |
| Missing repo label | BA Agent catches this; asks human before proceeding |
| Agent modifies arch-rules.md | Absolute prohibition in all agent prompts; abort + Telegram if attempted |
| Agent modifies detekt config | Same as above |
| Merge conflict in ticket files | Immediate human escalation, no guessing |
| Cross-repo scope leak | Each workspace is a clean isolated clone — impossible to affect other repos |
| Multiple tickets on same file | Handled naturally — each is its own clone; merge conflict handled by Merge Agent |
| Pipeline process crash | Idempotent restart — orchestrator reads state.json and resumes each workspace |
| VPS reboot | systemd service restarts pipeline; workspaces resume from state |
| Fix violates scope | Fix Agent runs internal scope check after each fix before committing |
| Human takes too long to reply | Workspace stays in `waiting_for_human` state; reminder sent after configurable timeout |

---

## COMPLETE PIPELINE FLOW

```
Orchestrator daemon polls Jira (interval from config)
            ↓
For each project → for each repo → check slots
            ↓
[PM Agent] → selects and routes tickets → creates workspaces
            ↓
[BA Agent] → validates requirements
  ↓ unclear → Telegram → wait for reply → resume
  ↓ clear
[Dev Agent] → implements on feature branch
            ↓
[Scope Guard] → validates diff vs plan vs arch rules
  ↓ violations → Dev Agent → fix → re-verify (max: config)
  ↓ clean → scope-certificate.md written
PR opened on GitHub
            ↓
Wait for Copilot (wait_for_review_minutes)
            ↓
copilot-validator.py → valid-comments.json
  ↓ no valid comments → skip to QA
  ↓ valid comments
[Fix Agent] → address comments + Detekt + internal scope check (max: config)
  ↓ maxed → Telegram
  ↓ resolved
[QA Agent] → write tests + run suite + Detekt + build (max: config)
  ↓ maxed → Telegram
  ↓ green
[Merge Agent] → gate check → resolve rebase conflicts → merge
  ↓ conflict in ticket files → Telegram
  ↓ all green → merge → Jira Done → Telegram ✅
Workspace marked completed → archived after max_age_days
```

---

## IMPLEMENTATION NOTES

- Each agent is invoked as a subprocess by the orchestrator — not a monolith
- Agents communicate exclusively through files in `workspace/context/` — never in-memory
- Every agent logs structured output to its own log file in `workspace/logs/`
- The pipeline tool has **zero** project-specific knowledge — it only knows about abstractions
- Adding a new project = adding a new directory under `projects/`; zero code changes
- Adding a new repo = adding a new yaml file under `repos/`; zero code changes
- Switching from Jira to Linear = implement `tracker.py` interface for Linear; zero agent changes
- Switching from GitHub to GitLab = implement `vcs.py` interface for GitLab; zero agent changes
- Environment variables are the only place credentials live — never in yaml files as raw values
- The `--project` and `--repo` CLI flags allow targeting a single project/repo for testing

---

## WHAT TO BUILD (PRIORITY ORDER)

1. **Config loader** — parses the full hierarchy, merges defaults, validates required fields
2. **Workspace manager** — creates isolated directory clones, manages lifecycle, cleanup
3. **Orchestrator daemon** — main loop, slot management, state machine, idempotent restart
4. **Telegram bot** — send/receive, per-project chat routing, reply-to-agent routing
5. **Jira adapter** — poll, filter, route, transition, comment
6. **GitHub adapter** — branch, push, PR, read comments, reply, merge
7. **BA Agent** — requirements validation, implementation plan, test scenarios
8. **Dev Agent** — implementation with hard scope rules
9. **Scope Guard Agent** — diff analysis, violation report, scope certificate
10. **Integration layer** — wire ticket-to-prompt.py and copilot-validator.py as subprocess calls
11. **Fix Agent** — review response with scope self-check
12. **QA Agent** — test writing and test execution
13. **Merge Agent** — final gate, conflict resolution, merge

---

*End of prompt. Remember: self-verify before implementing.*
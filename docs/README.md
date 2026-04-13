# Sickle — Project Reference

> Single-file context document for AI agents and contributors.
> Last updated: 2026-04-08 (after Phase 4 implementation).

---

## What is Sickle?

Sickle is a fully autonomous, 24/7 AI-driven development pipeline. It takes Jira tickets from "To Do" to open PR without human involvement in the happy path. When stuck, it asks precise questions via Telegram and resumes on reply. Humans review and merge PRs — the system never merges.

It runs as a single Python daemon on a VPS, manages multiple companies/repos via config, and uses BMAD-style AI agents (markdown prompt files) executed via Claude Code CLI.

---

## How It Works

```
Jira Poll                                              Telegram
  |                                                       ^
  v                                                       |
[NEW] -> ANALYSIS (BA agent)                         BLOCKED (escalate)
           |                                              ^
           v                                              |
         DEV (Developer agent) <--+------ fail -----------+
           |                      |                       |
           v                      |                       |
       SCOPE_CHECK (Scope Guard)--+ (fail -> back to DEV) |
           |                                              |
           v                                              |
          QA (QA agent) ----------+ (fail -> back to DEV) |
           |                                              |
           v                                              |
         PUSH (git push + open PR)                        |
           |                                              |
           v                                              |
       PR_REVIEW (PR Comment Responder) --+ (fix -> DEV)  |
           |                              | (max iter -> escalate)
           v
         DONE (notify human, await merge)
```

**13 pipeline states:** NEW, ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW, DONE, BLOCKED, FAILED, ARCHIVED, AWAITING_APPROVAL, MANUAL_CONTROL

**State machine:** File-based (`state.json`), atomic writes (temp + rename), BLOCKED stores `previous_state` for resume.

---

## Agents

8 BMAD-style agents, each a markdown file in `agents/` with YAML frontmatter declaring tools, inputs, outputs:

| Agent | ID | Role | Tools |
|-------|----|------|-------|
| PM (Marcus) | pm-agent | Ticket triage and routing | none |
| BA (Alice) | ba-agent | Requirements validation, implementation plan | read_file, list_directory, search_code |
| Developer (James) | dev-agent | Code implementation | all 6 tools |
| Scope Guard (Sentinel) | scope-guard-agent | Diff validation against plan | read_file, list_directory, search_code, git_operation |
| QA (Quinn) | qa-agent | Test writing, lint/test/build gates | all 6 tools |
| Fix (Fixer) | fix-agent | Fix code based on review comments | all 6 tools |
| PR Comment Responder (Rivera) | pr-comment-responder-agent | Classify PR comments with extreme skepticism | read_file, list_directory, search_code |
| Merge (legacy) | merge-agent | Not used in v2 — merge is human's job | - |

**6 sandboxed tools:** read_file, write_file, list_directory, search_code, run_command, git_operation

Agents execute via Claude Code CLI (`claude -p` subprocess) using existing Max subscription. No API key needed. Claude Code manages the tool loop internally; tool access restricted via `--allowedTools`.

---

## Architecture

**Modular monolith** — single daemon, no microservices, no database.

### Source Tree

```
main.py                          # Entry point, CLI args, adapter wiring
orchestrator/
  orchestrator.py                # Main loop: poll, create workspaces, advance stages
  agent_runtime.py               # Prompt assembly, LLM dispatch (API or CLI)
  workflow_router.py             # Stage transitions from YAML workflow
  tool_sandbox.py                # Sandboxed tool execution (API path only)
  pr_creation.py                 # Push branch + open PR action
  merge_step.py                  # Legacy merge step (not used in v2)
  ticket_prioritizer.py          # Filter, route, sort tickets
  safeguards.py                  # Protected file checks
workspace/
  workspace.py                   # State machine, atomic writes, path properties
  workspace_manager.py           # Create/discover/cleanup workspaces
config/
  schemas.py                     # All dataclasses (GlobalConfig, RepoConfig, etc.)
  config_loader.py               # 3-level YAML loader with env var resolution
  resource_registry.py           # BMAD resource discovery (agents, tasks, etc.)
integrations/
  base/tracker.py                # Abstract: TrackerInterface (Jira)
  base/vcs.py                    # Abstract: VCSInterface (GitHub, GitLab)
  base/notifier.py               # Abstract: NotifierInterface (Telegram)
  jira/jira_adapter.py           # Jira Cloud REST API
  github/github_adapter.py       # GitHub REST API + git CLI
  telegram/telegram_adapter.py   # Telegram Bot API
  llm/llm_interface.py           # Abstract: LLMInterface
  llm/claude_adapter.py          # Anthropic API adapter (needs API key)
  llm/claude_code_adapter.py     # Claude Code CLI adapter (uses Max sub)
agents/                          # BMAD agent prompt files (.md)
workflows/default-workflow.yaml  # Stage definitions and transitions
config-live/                     # Real deployment config (env vars for secrets)
tests/unit/                      # 239 unit tests
```

### Config Cascade

3 levels, deep-merged: `global.yaml` -> `project.yaml` -> `repo.yaml`

```
config-live/
  global.yaml                    # Telegram, Claude, workspaces, logging, operator
  projects/
    acme/
      project.yaml               # Jira config, parallelism, defaults
      repos/
        acme-mobile.yaml            # VCS (GitHub), CI, git, architecture, helpers
```

Secrets via `${ENV_VAR}` references resolved at load time.

### Workspace Directory

```
/data/sickle/{company}/{repo}/tickets/{ticket_id}/
  state.json        # Pipeline state (atomic writes)
  meta/             # ticket.md, parent.md (input data)
  reports/          # Agent outputs (ba.md, developer.md, qa.md, etc.)
  logs/             # Per-agent execution logs
  source/           # Git clone (deleted after merge, rest preserved)
```

---

## Key Decisions

1. **No auto-merge** — human reviews and merges every PR
2. **Claude Code CLI over API** — uses existing Max subscription, no API billing
3. **File-based state** — no database, atomic JSON writes, survives restart
4. **Agents as prompt files** — not code; prompt files with YAML metadata
5. **Multi-provider** — GitHub/GitLab (VCS), Actions/Jenkins (CI), extensible
6. **Source cleanup only** — `source/` deleted after merge, `meta/` + `reports/` preserved forever
7. **Extreme skepticism in PR review** — PR Comment Responder assumes reviewers may be wrong
8. **Helper scripts as subprocesses** — existing scripts wrapped, not rewritten

---

## Current Status (2026-04-08)

### Implemented (Phases 1-4)
- State machine with 13 states and BLOCKED/resume (incl. AWAITING_APPROVAL, MANUAL_CONTROL)
- Multi-company workspace hierarchy
- Config schemas with VCS/CI provider abstraction
- Workflow router with 8-stage pipeline
- Tool sandbox (6 tools, path restriction, protected files)
- Claude Code CLI adapter
- Agent runtime with tool_use loop (API) and CLI subprocess paths
- Orchestrator: Jira polling, workspace creation, agent dispatch, push/PR, Telegram escalation
- All 8 agent prompt files with v2 metadata
- Real config for Acme/Acme Mobile (acme-org/acme-mobile)
- 239 passing unit tests

### Not Yet Implemented (Phases 5-6)
- GitLab adapter (`integrations/gitlab/`)
- Jenkins adapter (`integrations/jenkins/`)
- CI interface (`integrations/base/ci.py`)
- Multi-company simultaneous operation (config exists, not tested)
- Reopen detection (ticket changed after processing)
- Systemd deployment, log rotation, monitoring
- First real end-to-end ticket run

### Integration Endpoints
- **Jira:** acme.atlassian.net, project ACME, trigger label `ai-pipeline`
- **GitHub:** acme-org/acme-mobile
- **Telegram:** bot configured, chat_id set
- **Claude:** via Claude Code CLI (no API key)

---

## For Detailed Specs

- [Architecture v2](architecture-v2.md) — full technical architecture (1000+ lines)
- [Agent Contracts](agent-contracts.md) — formal BMAD contracts for all agents
- [Implementation Plan](implementation-plan-v2.md) — phased stories with acceptance criteria
- [Decisions](decisions/2026-04-08-architecture-decisions.md) — RFC vs Sickle resolution log
- [Feature Tracker](features/index.md) — feature status table

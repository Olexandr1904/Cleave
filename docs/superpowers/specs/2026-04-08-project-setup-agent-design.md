# Project Setup Agent — Design Spec

**Date:** 2026-04-08
**Status:** Approved
**Author:** Oleksandr Brazhenko + Claude

## Overview

A BMAD-style agent (`project-setup-agent`) that onboards new projects into the Cleave pipeline through guided conversational setup. It collects project details (Jira, VCS, CI/CD, quality gates, Telegram), validates credentials against live APIs, and writes the YAML config files to `config-live/`.

The agent supports three operations: **add**, **list**, and **remove**.

## Motivation

Currently, adding a project requires manually creating YAML config files in `config-live/projects/` with correct structure, field names, and env var references. This is error-prone, requires knowing the config schema, and has no validation. The setup agent automates this with guided Q&A, sensible defaults, and live API validation.

## Agent Identity

| Field | Value |
|-------|-------|
| ID | `project-setup-agent` |
| Name | Atlas |
| Title | Project Setup Specialist |
| File | `agents/project-setup-agent.md` |
| Model | (default) |

```yaml
agent:
  id: "project-setup-agent"
  name: "Atlas"
  title: "Project Setup Specialist"
  model: ""

persona:
  role: "DevOps Onboarding Specialist"
  style: "Methodical, thorough, validates before proceeding"
  identity: "Configuration specialist who onboards new projects into the Cleave pipeline"

core_principles:
  - "Always validate credentials before writing config"
  - "Use environment variable references for all secrets"
  - "Provide sensible defaults — minimize user input for common setups"
  - "Never overwrite existing config without explicit confirmation"

tools:
  - validate_jira
  - validate_github
  - validate_gitlab
  - validate_jenkins
  - list_projects
  - read_project_config
  - write_project_config
  - write_repo_config
  - remove_project

inputs:
  - operation (add | list | remove)
  - meta/input.md (orchestrator mode — pre-provided answers)
  - meta/answers.md (orchestrator mode — human replies to questions)

outputs:
  - config-live/projects/{project_id}/project.yaml
  - config-live/projects/{project_id}/repos/{repo_id}.yaml
  - reports/project-setup-output.md (orchestrator mode — summary)
  - reports/questions.md (orchestrator mode — pending questions)

decision_policy:
  when_to_run: "Triggered by Claude Code command or Telegram /add-project"
  when_to_skip: "N/A — admin operation, not part of ticket pipeline"
  success_outcome: "Config files written and validated"
  failure_outcome: "Validation failed — user informed of specific errors"
```

## New Tools

New tools added to the tool sandbox in a dedicated module `integrations/config/config_tools.py`. Registered alongside existing tools (`read_file`, `write_file`, etc.) in `orchestrator/tool_sandbox.py`.

**Config directory:** All tools take a `config_dir` parameter. In Claude Code mode, this is the project's `config-live/` directory (resolved relative to the repo root). In orchestrator mode, it comes from the global config's `workspaces.base_dir` parent or a dedicated config path.

**Env var resolution in validation tools:** Validation tools accept env var reference strings (e.g. `${JIRA_TOKEN}`) and resolve them from the current process environment via `os.environ` before making API calls. If the env var is not set, the tool returns a clear error: "Environment variable `JIRA_TOKEN` is not set."

### Validation Tools

#### `validate_jira(url, token, email, project_key)`

- Hits `{url}/rest/api/3/project/{project_key}` with Basic auth (`email:token`)
- Returns: success + project name, or specific error (401 auth failed, 404 project not found, connection error)

#### `validate_github(token, owner, repo)`

- Hits `https://api.github.com/repos/{owner}/{repo}` with Bearer token
- Returns: success + repo full name and default branch, or specific error

#### `validate_gitlab(token, project_id, url)`

- Hits `{url}/api/v4/projects/{project_id}` with Private-Token header
- Returns: success + project name, or specific error

#### `validate_jenkins(url, username, token, job_key)`

- Hits `{url}/job/{job_key}/api/json` with Basic auth
- Returns: success + job display name, or specific error

### Config Management Tools

#### `list_projects(config_dir)`

- Scans `{config_dir}/projects/` directories
- For each: reads `project.yaml`, counts repos in `repos/`
- Returns: list of `{id, name, repo_count, enabled}`

#### `read_project_config(config_dir, project_id)`

- Reads `{config_dir}/projects/{project_id}/project.yaml`
- Reads all `{config_dir}/projects/{project_id}/repos/*.yaml`
- Returns: full project config + all repo configs as structured data

#### `write_project_config(config_dir, project_id, yaml_content)`

- Creates `{config_dir}/projects/{project_id}/` if needed
- Writes `project.yaml` with provided YAML content
- Reads back and validates with `yaml.safe_load`
- Returns: success or parse error

#### `write_repo_config(config_dir, project_id, repo_id, yaml_content)`

- Creates `{config_dir}/projects/{project_id}/repos/` if needed
- Writes `repos/{repo_id}.yaml` with provided YAML content
- Reads back and validates with `yaml.safe_load`
- Returns: success or parse error

#### `remove_project(config_dir, project_id)`

- Backs up `{config_dir}/projects/{project_id}/` to `{config_dir}/.backups/{project_id}-{YYYYMMDD-HHMMSS}/`
- Fails if backup creation fails
- Deletes the project directory
- Returns: success + backup path, or error

## Add-Project Conversational Flow

The agent asks one question at a time, providing defaults in parentheses where applicable. The user can accept defaults by pressing enter/confirming.

### Phase 1 — Project Identity

1. Ask for **project ID** (slug, e.g. `acme`) and **display name** (e.g. `Acme Corp`)
2. Call `list_projects` — if project ID already exists, warn and ask: overwrite, pick a different ID, or abort

### Phase 2 — Jira Integration

3. Ask for **Jira URL** (e.g. `https://company.atlassian.net`)
4. Ask for **Jira project key** (e.g. `ACME`)
5. Ask for **Jira email** (e.g. `bot@company.com`)
6. Ask for **env var name** holding the Jira token (default: `JIRA_TOKEN`)
7. Ask for **trigger label** (default: `ai-pipeline`) and any **ignore labels**
8. Ask for **Jira status mappings** (defaults: To Do / In Progress / In Review / Done)
9. Call `validate_jira` — report success or specific error, let user fix and retry

### Phase 3 — VCS Setup

10. Ask: **GitHub or GitLab?**
11. For GitHub:
    - Ask **owner** (org or user), **repo name**
    - Ask **env var for token** (default: `GITHUB_TOKEN`)
    - Ask **default branch** (default: `develop`)
    - Ask **branch prefix** (default: `feature`)
    - Ask **merge method**: squash / merge / rebase (default: `squash`)
12. For GitLab:
    - Ask **GitLab URL** (default: `https://gitlab.com`)
    - Ask **project ID** (numeric)
    - Ask **env var for token** (default: `GITLAB_TOKEN`)
    - Ask **default branch** (default: `develop`)
    - Ask **branch prefix** (default: `feature`)
13. Derive `clone_url` automatically:
    - GitHub: `https://${GITHUB_TOKEN}@github.com/{owner}/{repo}.git`
    - GitLab: `https://oauth2:${GITLAB_TOKEN}@{gitlab_host}/{project_path}.git`
14. Call `validate_github` or `validate_gitlab` — report success or error

### Phase 4 — CI/CD Setup

15. Ask: **GitHub Actions or Jenkins?**
16. For GitHub Actions: no extra input needed (uses VCS token)
17. For Jenkins:
    - Ask **Jenkins URL** (e.g. `https://jenkins.company.com`)
    - Ask **job key** (e.g. `my-project/main`)
    - Ask **env var for username** (default: `JENKINS_USERNAME`)
    - Ask **env var for token** (default: `JENKINS_TOKEN`)
18. Call `validate_jenkins` if Jenkins — report success or error

### Phase 5 — Quality Gates

19. Ask for **lint command** (optional, e.g. `./gradlew detekt`), hard gate? (default: yes)
20. Ask for **test command** (optional, e.g. `./gradlew test`), hard gate? (default: yes)
21. Ask for **build command** (optional, e.g. `./gradlew assembleDebug`), hard gate? (default: yes)

### Phase 6 — Extras

22. Ask for **Telegram chat ID override** (optional — default: inherit from global config)
23. Ask for **architecture rules file** path (optional, e.g. `docs/arch-rules.md`)
24. Ask for **protected files** list (optional, e.g. `.github/`, `build.gradle.kts`)
25. Ask for **max concurrent tickets** (optional — default: inherit from project/global)

### Phase 7 — Write & Confirm

26. Display a full summary of all collected values
27. Ask for confirmation before writing
28. Call `write_project_config` and `write_repo_config`
29. Read back and verify YAML is valid
30. Report what was written and remind user which env vars need to be set

## List Operation

Calls `list_projects`, displays:

```
Project          Repos                  Enabled
─────────────────────────────────────────────────
acme            acme-mobile              yes
acme             api, frontend          yes
old-client       legacy-app             no
```

No questions, no validation — read-only.

## Remove Operation

1. If no project ID provided, call `list_projects` and ask which one
2. Call `read_project_config` — display the project's full config (project + all repos) so the user sees what they're deleting
3. Ask for explicit confirmation
4. Call `remove_project` — backup first, then delete
5. Report what was removed and where the backup lives

## Claude Code Commands

Three markdown files in `.claude/commands/`:

### `add-project.md`

- Loads the project-setup-agent prompt from `agents/project-setup-agent.md`
- Sets operation to "add"
- Instructs Claude Code to follow the agent's conversational flow: ask one question at a time, validate before writing, use env var references for credentials

### `list-projects.md`

- Sets operation to "list"
- Instructs Claude Code to scan `config-live/projects/` and display the summary table

### `remove-project.md`

- Sets operation to "remove"
- Accepts optional `$ARGUMENTS` for project ID
- Instructs Claude Code to follow the remove flow with backup and confirmation

## Orchestrator Integration

The agent is designed for dual-use: Claude Code (interactive) and orchestrator (file-based).

### Admin Workspace

A new lightweight workspace type `admin` in `workspace/workspace.py`:
- No `source/` directory (no git clone needed)
- No ticket context (`ticket_id`, `company_id`, etc.)
- Has `meta/` for input (operation type, provided answers)
- Has `reports/` for output (generated configs, validation results, questions)
- Has `logs/` for execution logs

### Orchestrator Trigger

When triggered via a future Telegram command (e.g. `/add-project`):
1. The Telegram command handler creates an admin workspace
2. Writes operation and any initial input to `meta/input.md`
3. Dispatches the project-setup-agent via the agent runtime

### Interactive Q&A via Telegram

For the "add" operation, which requires multi-turn Q&A:
1. Agent writes questions to `reports/questions.md`
2. Orchestrator sends them via Telegram
3. Human replies get written to `meta/answers.md`
4. Orchestrator re-dispatches the agent with updated context
5. Same pattern as the BA agent's BLOCKED → human reply → resume flow

This orchestrator integration is a **future extension** that plugs into the Telegram command layer design (spec: `docs/superpowers/specs/2026-04-08-telegram-command-layer-design.md`). The agent prompt and tools work identically in both contexts — only the I/O channel differs.

## Error Handling

### Validation Failures

- If any `validate_*` call fails, the agent reports the specific error (auth rejected, project not found, network unreachable) and asks the user to fix the input
- If the user wants to skip validation (e.g. env var not set on this machine), the agent allows it with a warning: "Skipping validation — config will be written but may not work until `{ENV_VAR}` is set"

### Duplicate Project

- If a project ID already exists, the agent warns and asks: overwrite existing config, pick a different ID, or abort

### Partial Completion

- Configs are only written at the end after the summary confirmation
- If the user abandons mid-flow, nothing is written

### Remove Safety

- Backup is mandatory before removal — written to `config-live/.backups/{project_id}-{YYYYMMDD-HHMMSS}/`
- The agent refuses to remove if backup creation fails

### Config Directory Missing

- If `config-live/projects/` doesn't exist, the agent creates it

### Invalid YAML

- After writing, the agent reads back the file and parses it with `yaml.safe_load`
- If parsing fails, it reports the error and rewrites

## File Changes Summary

| File | Change |
|------|--------|
| `agents/project-setup-agent.md` | New — agent definition |
| `integrations/config/__init__.py` | New — package init |
| `integrations/config/config_tools.py` | New — validation and config management functions |
| `orchestrator/tool_sandbox.py` | Modified — register new tools |
| `workspace/workspace.py` | Modified — add admin workspace type |
| `.claude/commands/add-project.md` | New — Claude Code command |
| `.claude/commands/list-projects.md` | New — Claude Code command |
| `.claude/commands/remove-project.md` | New — Claude Code command |

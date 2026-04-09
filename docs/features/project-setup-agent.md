# Feature: Project Setup Agent (Atlas)

**Status:** In Progress
**Created:** 2026-04-08
**Updated:** 2026-04-09
**Author:** Oleksandr Brazhenko

## Description

A BMAD-style agent (`project-setup-agent`, codename Atlas) that onboards new projects into the Sickle pipeline through guided conversational setup. Atlas collects project details (Jira, VCS, CI/CD, quality gates, Telegram), validates credentials against live APIs, and writes the YAML config files to `config-live/`. The agent supports three operations: **add**, **list**, and **remove**.

## Requirements

- FR1: Config tools module (`integrations/config/config_tools.py`) providing CRUD operations against `config-live/projects/`
- FR2: `resolve_env_var` parses `${VAR_NAME}` references and resolves them from `os.environ`, raising a clear error if unset
- FR3: `list_projects` scans `{config_dir}/projects/` for subdirectories containing `project.yaml` and returns id/name/repo_count/enabled for each
- FR4: `read_project_config` returns the parsed `project.yaml` plus all repo YAMLs keyed by repo id
- FR5: `write_project_config` creates `{config_dir}/projects/{project_id}/project.yaml`, validates ID with `PROJECT_ID_PATTERN`, writes YAML, and returns `{success, path}` (or `{success: False, error}` on bad YAML)
- FR5b: `write_repo_config` creates `{config_dir}/projects/{project_id}/repos/{repo_id}.yaml` with the same ID validation for both project_id and repo_id
- FR5c: `remove_project` backs up `{config_dir}/projects/{project_id}/` to `{config_dir}/.backups/{project_id}-{YYYYMMDD-HHMMSS}/` via `shutil.copytree` then removes the project dir; backup failure leaves the project intact
- FR6-future: write/remove/validation tools for project and repo configs (remove, credential validation)
- FR6: Future: validate credentials against live APIs (Jira, GitHub, GitLab, Jenkins) before writing config
- FR7: Future: Atlas agent prompt file (`agents/project-setup-agent.md`) with persona, tools, and interactive flow
- FR8: Future: add/list/remove operations invoked from CLI or orchestrator

## Technical Approach

- New `integrations/config/` subpackage with `config_tools.py` exposing pure functions over the config directory
- Env var resolution via regex `^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$`; plain strings pass through unchanged
- YAML I/O via PyYAML `safe_load`/`safe_dump`
- Config tools are stateless, take `config_dir` as a parameter, and raise `ValueError`/`FileNotFoundError` on invalid input
- Agent built on the existing BMAD-style agent system; tools are registered with the Claude adapter and dispatched via `tool_use`

## Dependencies

- PyYAML for YAML parsing
- Environment variables for secrets
- Existing agent system (persona/tools/constraints loader)
- Live API clients (Jira, GitHub, GitLab, Jenkins) for credential validation

## Acceptance Criteria

- [x] `resolve_env_var` resolves `${VAR}` from environment; raises on missing var; passes plain strings through
- [x] `list_projects` returns all projects with id/name/repo_count/enabled; handles missing/empty projects dir
- [x] `read_project_config` returns project + repos dict; raises `FileNotFoundError` on unknown project
- [x] `read_project_config` rejects `project_id` values outside `[a-zA-Z0-9_-]+` to prevent path traversal from LLM-supplied input
- [x] `resolve_env_var` delegates to `config.config_loader.resolve_env_vars` so embedded references (e.g. `"Bearer ${TOKEN}"`) resolve consistently with the rest of the codebase
- [x] `write_project_config` writes `project.yaml`; rejects invalid project_id; validates YAML in memory before writing (no corrupt files left on disk); returns success/error dict
- [x] `write_repo_config` writes `repos/{repo_id}.yaml`; rejects invalid project_id and repo_id; validates YAML in memory before writing; returns success/error dict
- [x] `remove_project` backs up project to `.backups/` before removal; backup failure leaves project intact; rejects invalid project_id via `PROJECT_ID_PATTERN`
- [ ] Remove project with user confirmation (CLI/agent flow — not yet wired)
- [ ] Credential validation against live APIs for Jira, GitHub, GitLab, Jenkins
- [ ] Atlas agent prompt file with add/list/remove flows
- [ ] CLI / orchestrator entry point to launch the agent

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-08 | Initial draft — seeded from design spec `2026-04-08-project-setup-agent-design.md`. Task 1 implemented: config tools module with `resolve_env_var`, `list_projects`, `read_project_config` |
| 2026-04-08 | Task 1 review fixes: path traversal validation in `read_project_config`, `resolve_env_var` now delegates to `config.config_loader.resolve_env_vars` (supports embedded refs), removed unused imports |
| 2026-04-09 | Task 2 implemented: `write_project_config` and `write_repo_config` with path-traversal validation (PROJECT_ID_PATTERN) for all ID inputs; 21 new tests (42 total) |
| 2026-04-09 | Task 2 review fix: changed write-then-validate to validate-then-write so invalid YAML leaves no corrupt files or orphan directories on disk; strengthened tests to assert no side-effects on bad YAML |
| 2026-04-09 | Task 3 implemented: `remove_project` with timestamped backup to `.backups/`, OSError guard (no removal if backup fails), and `PROJECT_ID_PATTERN` validation; 8 new tests (52 total) |
| 2026-04-09 | Task 3 review fix: `rmtree` failure now returns an error dict (preserves return contract); added tests for backup-failure and rmtree-failure guards; microsecond timestamp avoids second-level collisions |

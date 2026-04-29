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
| 2026-04-29 | `_parse_vcs_section` now passes through any top-level VCSConfig fields beyond `provider`/`github`/`gitlab` (e.g. `skip_pre_push_hook`). Earlier the loader only handled the three nested sub-configs and silently dropped everything else, so operators could set `vcs.skip_pre_push_hook: true` in yaml and the value would never reach the runtime config object — looked configured, did nothing. Validation against VCSConfig fields kept strict (a typo like `skip_pre_pus_hook` raises ConfigError). |

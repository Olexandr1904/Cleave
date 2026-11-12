"""Configuration loader for Cleave.

Loads and validates the 3-level config hierarchy:
  global.yaml -> project.yaml -> repo.yaml

Environment variable references (${VAR_NAME}) are resolved at load time.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

from config.host_os import SUPPORTED_OS
from config.schemas import (
    AgentBudget,
    ArchitectureConfig,
    BuildConfig,
    CIConfig,
    ClaudeConfig,
    DashboardConfig,
    DefaultsConfig,
    FailureSummarizerConfig,
    GitConfig,
    GitHubConfig,
    GitLabConfig,
    GlobalConfig,
    HeartbeatConfig,
    HelpersConfig,
    IntentParserConfig,
    JenkinsConfig,
    JiraConfig,
    JiraStatusesConfig,
    LintConfig,
    LoadedProject,
    LoggingConfig,
    MaxIterationsConfig,
    OperatorProfile,
    ParallelismConfig,
    PipelineConfig,
    PosterPartConfig,
    ProcessPartConfig,
    ProjectConfig,
    ProjectInfo,
    RepoConfig,
    ReadinessPartConfig,
    RepoInfo,
    REVIEWER_PLACEHOLDERS,
    ReviewerConfig,
    ReviewerGlobalConfig,
    ReviewerJobConfig,
    TelegramConfig,
    TestConfig,
    TrackerConfig,
    TrelloConfig,
    TrelloListMapping,
    VCSConfig,
    WorkspacesConfig,
)

ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


class ConfigError(Exception):
    """Raised when configuration is invalid or incomplete."""

    def __init__(self, message: str, file_path: str = "", field: str = ""):
        self.file_path = file_path
        self.field = field
        detail = ""
        if file_path:
            detail += f" (file: {file_path})"
        if field:
            detail += f" (field: {field})"
        super().__init__(f"{message}{detail}")


def resolve_env_vars(value: str) -> str:
    """Resolve ${VAR_NAME} references in a string value.

    Raises ConfigError if an environment variable is not set.
    """

    def replace_var(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ConfigError(
                f"Environment variable '{var_name}' is not set",
                field=var_name,
            )
        return env_value

    return ENV_VAR_PATTERN.sub(replace_var, value)


def resolve_env_vars_recursive(data: Any) -> Any:
    """Recursively resolve environment variables in a config dict."""
    if isinstance(data, str):
        return resolve_env_vars(data)
    if isinstance(data, dict):
        return {k: resolve_env_vars_recursive(v) for k, v in data.items()}
    if isinstance(data, list):
        return [resolve_env_vars_recursive(item) for item in data]
    return data


def env_var_refs(raw: Any, prefix: str = "") -> dict[str, str]:
    """Map dotted config paths to the first ${VAR} name in that string value.

    Computed from the raw (unresolved) YAML, so it never holds a value.
    """
    refs: dict[str, str] = {}
    if isinstance(raw, str):
        match = ENV_VAR_PATTERN.search(raw)
        if match and prefix:
            refs[prefix] = match.group(1)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            refs.update(env_var_refs(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            refs.update(env_var_refs(value, f"{prefix}.{index}" if prefix else str(index)))
    return refs


def _parse_section(data: dict, section_name: str, dataclass_type: type, file_path: str) -> Any:
    """Parse a config section into a dataclass instance."""
    section_data = data.get(section_name, {})
    if section_data is None:
        section_data = {}
    try:
        return dataclass_type(**section_data)
    except TypeError as e:
        raise ConfigError(
            f"Invalid fields in '{section_name}': {e}",
            file_path=file_path,
            field=section_name,
        ) from e


def _mapping(value: Any, field_name: str, file_path: str) -> dict:
    """A copy of a YAML section: {} when empty, ConfigError when it is not a mapping."""
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{field_name}' must be a mapping", file_path=file_path, field=field_name)
    return dict(value)


def _parse_reviewer_global(data: dict, file_path: str) -> ReviewerGlobalConfig:
    """Parse the `reviewer:` section; job entries merge over the per-kind defaults."""
    section = _mapping(data.get("reviewer"), "reviewer", file_path)
    jobs_raw = _mapping(section.pop("jobs", None), "reviewer.jobs", file_path)
    try:
        reviewer = ReviewerGlobalConfig(**section)
    except TypeError as e:
        raise ConfigError(
            f"Invalid fields in 'reviewer': {e}", file_path=file_path, field="reviewer",
        ) from e
    for kind, job_raw in jobs_raw.items():
        where = f"reviewer.jobs.{kind}"
        if kind not in reviewer.jobs:
            raise ConfigError(
                f"Unknown reviewer job kind '{kind}' (expected one of {sorted(reviewer.jobs)})",
                file_path=file_path, field=where,
            )
        job = _mapping(job_raw, where, file_path)
        try:
            reviewer.jobs[kind] = ReviewerJobConfig(**{**reviewer.jobs[kind].__dict__, **job})
        except TypeError as e:
            raise ConfigError(
                f"Invalid fields in '{where}': {e}", file_path=file_path, field=where,
            ) from e
    return reviewer


def load_global_config(config_dir: str) -> GlobalConfig:
    """Load and validate global.yaml from the config directory.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        Parsed and validated GlobalConfig.

    Raises:
        ConfigError: If the file is missing, empty, or has invalid fields.
    """
    config_path = Path(config_dir) / "global.yaml"

    if not config_path.exists():
        raise ConfigError(
            f"Global config file not found: {config_path}",
            file_path=str(config_path),
        )

    with open(config_path) as f:
        raw_data = yaml.safe_load(f)

    if raw_data is None:
        raise ConfigError(
            "Global config file is empty",
            file_path=str(config_path),
        )

    if not isinstance(raw_data, dict):
        raise ConfigError(
            "Global config must be a YAML mapping",
            file_path=str(config_path),
        )

    # Resolve env vars throughout the config
    data = resolve_env_vars_recursive(raw_data)

    file_str = str(config_path)

    os_name = data.get("os", "unix")
    if os_name not in SUPPORTED_OS:
        raise ConfigError(
            f"'os' must be one of {SUPPORTED_OS} (got {os_name!r})",
            file_path=file_str,
            field="os",
        )

    return GlobalConfig(
        os=os_name,
        telegram=_parse_section(data, "telegram", TelegramConfig, file_str),
        claude=_parse_section(data, "claude", ClaudeConfig, file_str),
        workspaces=_parse_section(data, "workspaces", WorkspacesConfig, file_str),
        defaults=_parse_defaults_section(data, file_str),
        logging=_parse_section(data, "logging", LoggingConfig, file_str),
        heartbeat=_parse_section(data, "heartbeat", HeartbeatConfig, file_str),
        operator=_parse_section(data, "operator", OperatorProfile, file_str),
        failure_summarizer=_parse_section(data, "failure_summarizer", FailureSummarizerConfig, file_str),
        pipeline=_parse_section(data, "pipeline", PipelineConfig, file_str),
        intent_parser=_parse_section(data, "intent_parser", IntentParserConfig, file_str),
        dashboard=_parse_section(data, "dashboard", DashboardConfig, file_str),
        reviewer=_parse_reviewer_global(data, file_str),
        env_refs=env_var_refs(raw_data),
    )


def merge_dicts(base: dict, override: dict) -> dict:
    """Deep-merge override into base. Override values win. Dicts are merged recursively."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_file(path: Path) -> tuple[dict, dict[str, str]]:
    """Load a YAML file; return (data with env vars resolved, env refs). {} data if empty."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}", file_path=str(path))
    with open(path) as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}, {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping", file_path=str(path))
    return resolve_env_vars_recursive(raw), env_var_refs(raw)


def _parse_jira_section(data: dict, file_path: str) -> JiraConfig:
    """Parse a jira section dict into JiraConfig. Used by both legacy top-level
    `jira:` blocks (back-compat) and the nested `tracker.jira:` form."""
    jira_data = dict(data or {})
    if "trigger_label" in jira_data:
        raise ConfigError(
            "Legacy 'trigger_label' (singular) is no longer supported. "
            "Rename to 'trigger_labels' and make it a list, e.g. "
            'trigger_labels: ["ai-pipeline"]',
            file_path=file_path,
            field="jira.trigger_label",
        )
    statuses_data = jira_data.pop("statuses", None) or {}
    statuses = JiraStatusesConfig(**statuses_data) if statuses_data else JiraStatusesConfig()
    try:
        return JiraConfig(**jira_data, statuses=statuses)
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'jira': {e}", file_path=file_path, field="jira") from e


def _parse_trello_section(data: dict, file_path: str) -> TrelloConfig:
    """Parse a trello section dict into TrelloConfig."""
    trello_data = dict(data or {})
    lists_data = trello_data.pop("lists", None) or {}
    try:
        lists = TrelloListMapping(**lists_data) if lists_data else TrelloListMapping()
    except TypeError as e:
        raise ConfigError(
            f"Invalid fields in 'tracker.trello.lists': {e}",
            file_path=file_path, field="tracker.trello.lists",
        ) from e
    try:
        return TrelloConfig(**trello_data, lists=lists)
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'trello': {e}", file_path=file_path, field="trello") from e


def _parse_tracker_section(data: dict, file_path: str) -> TrackerConfig:
    """Parse tracker config with back-compat for legacy top-level `jira:` block.

    Three shapes are accepted:
      1. New: `tracker: { provider: jira|trello, jira: {...}, trello: {...} }`
      2. Legacy: top-level `jira: {...}` (lifted to tracker.jira, provider=jira)
      3. Both present: `tracker:` wins; WARN-log that the legacy `jira:` block
         is ignored.
    """
    legacy_jira = data.get("jira")
    tracker_data = data.get("tracker")

    if tracker_data is None and legacy_jira is not None:
        # Legacy shape — lift into tracker.jira
        logger.info(
            "Config %s: migrated legacy top-level 'jira:' block to 'tracker.jira:' "
            "(provider=jira). Run the wizard or rewrite to silence this notice.",
            file_path,
        )
        return TrackerConfig(
            provider="jira",
            jira=_parse_jira_section(legacy_jira, file_path),
        )

    if tracker_data is None:
        return TrackerConfig()  # empty — no tracker configured

    if legacy_jira is not None:
        logger.warning(
            "Config %s has both 'tracker:' and legacy 'jira:' blocks; "
            "the legacy block is ignored.",
            file_path,
        )

    tracker_data = dict(tracker_data)
    provider = tracker_data.pop("provider", "jira")
    if provider not in ("jira", "trello"):
        raise ConfigError(
            f"tracker.provider must be 'jira' or 'trello' (got: {provider!r})",
            file_path=file_path, field="tracker.provider",
        )
    jira_block = tracker_data.pop("jira", None) or {}
    trello_block = tracker_data.pop("trello", None) or {}
    if tracker_data:
        raise ConfigError(
            f"Unexpected fields in 'tracker': {sorted(tracker_data)}",
            file_path=file_path, field="tracker",
        )
    return TrackerConfig(
        provider=provider,
        jira=_parse_jira_section(jira_block, file_path) if jira_block else JiraConfig(),
        trello=_parse_trello_section(trello_block, file_path) if trello_block else TrelloConfig(),
    )


def _parse_agent_budget(data: dict, file_path: str, where: str) -> AgentBudget:
    try:
        return AgentBudget(**data)
    except TypeError as e:
        raise ConfigError(
            f"Invalid fields in '{where}': {e}", file_path=file_path, field=where,
        ) from e


def _parse_defaults_section(data: dict, file_path: str) -> DefaultsConfig:
    """Parse defaults section, handling nested max_iterations and agent budgets."""
    defaults_data = dict(data.get("defaults", {}) or {})
    max_iter_data = defaults_data.pop("max_iterations", None) or {}
    max_iterations = MaxIterationsConfig(**max_iter_data) if max_iter_data else MaxIterationsConfig()

    budget_data = defaults_data.pop("agent_budget", None) or {}
    agent_budget = (
        _parse_agent_budget(budget_data, file_path, "defaults.agent_budget")
        if budget_data
        else AgentBudget()
    )

    overrides_raw = defaults_data.pop("agent_budget_overrides", None) or {}
    if not isinstance(overrides_raw, dict):
        raise ConfigError(
            "'defaults.agent_budget_overrides' must be a mapping of agent_id -> budget",
            file_path=file_path, field="defaults.agent_budget_overrides",
        )
    agent_budget_overrides: dict[str, AgentBudget] = {
        agent_id: _parse_agent_budget(
            cfg or {}, file_path, f"defaults.agent_budget_overrides.{agent_id}",
        )
        for agent_id, cfg in overrides_raw.items()
    }

    try:
        return DefaultsConfig(
            **defaults_data,
            max_iterations=max_iterations,
            agent_budget=agent_budget,
            agent_budget_overrides=agent_budget_overrides,
        )
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'defaults': {e}", file_path=file_path, field="defaults") from e


def _parse_vcs_section(data: dict, file_path: str) -> VCSConfig:
    """Parse vcs section with provider-specific sub-config.

    Top-level VCSConfig fields beyond `provider`/`github`/`gitlab` (e.g.
    `skip_pre_push_hook`) are passed through as kwargs so they're not
    silently dropped — the original implementation only handled the three
    nested sub-configs and discarded the rest, which made flags like
    `skip_pre_push_hook: true` look configured but actually do nothing.
    """
    vcs_data = dict(data.get("vcs", {}) or {})
    provider = vcs_data.pop("provider", "github")
    github_data = vcs_data.pop("github", None) or {}
    gitlab_data = vcs_data.pop("gitlab", None) or {}
    try:
        vcs = VCSConfig(
            provider=provider,
            github=GitHubConfig(**github_data) if github_data else GitHubConfig(),
            gitlab=GitLabConfig(**gitlab_data) if gitlab_data else GitLabConfig(),
            **vcs_data,  # remaining top-level fields (skip_pre_push_hook, etc.)
        )
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'vcs': {e}", file_path=file_path, field="vcs") from e

    # Backward compat: if hoisted fields are not set at the top level but the
    # provider sub-config has them, copy up. We check the raw YAML rather than
    # the dataclass value (which has non-empty defaults).
    if "default_branch" not in vcs_data:
        sub = vcs.github if vcs.provider == "github" else vcs.gitlab
        vcs.default_branch = sub.default_branch or "develop"
    if "branch_prefix" not in vcs_data:
        sub = vcs.github if vcs.provider == "github" else vcs.gitlab
        vcs.branch_prefix = sub.branch_prefix or "feature"
    return vcs


def _parse_ci_section(data: dict, file_path: str) -> CIConfig:
    """Parse ci section with provider-specific sub-config."""
    ci_data = dict(data.get("ci", {}) or {})
    provider = ci_data.pop("provider", "github_actions")
    jenkins_data = ci_data.pop("jenkins", None) or {}
    try:
        return CIConfig(
            provider=provider,
            jenkins=JenkinsConfig(**jenkins_data) if jenkins_data else JenkinsConfig(),
        )
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'ci': {e}", file_path=file_path, field="ci") from e


def _load_project_config(project_dir: Path, global_defaults: dict) -> ProjectConfig:
    """Load a single project.yaml and merge with global defaults."""
    project_yaml = project_dir / "project.yaml"
    if not project_yaml.exists():
        return None

    data, env_refs = _load_yaml_file(project_yaml)
    file_str = str(project_yaml)

    return ProjectConfig(
        project=_parse_section(data, "project", ProjectInfo, file_str),
        tracker=_parse_tracker_section(data, file_str),
        telegram=_parse_section(data, "telegram", TelegramConfig, file_str),
        parallelism=_parse_section(data, "parallelism", ParallelismConfig, file_str),
        defaults=_parse_defaults_section(data, file_str),
        env_refs=env_refs,
    )


PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")


def _parse_reviewer_section(
    data: dict, vcs: VCSConfig, file_path: str, repo_enabled: bool = True,
) -> ReviewerConfig:
    """Parse the repo's `reviewer:` section; full validation only when it and the repo are enabled."""
    raw = _mapping(data.get("reviewer"), "reviewer", file_path)
    parts = {}
    for name, cls in (("readiness", ReadinessPartConfig), ("process", ProcessPartConfig), ("poster", PosterPartConfig)):
        part = _mapping(raw.pop(name, None), f"reviewer.{name}", file_path)
        try:
            parts[name] = cls(**part)
        except TypeError as e:
            raise ConfigError(
                f"Invalid fields in 'reviewer.{name}': {e}", file_path=file_path, field=f"reviewer.{name}",
            ) from e
    try:
        reviewer = ReviewerConfig(**raw, **parts)
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'reviewer': {e}", file_path=file_path, field="reviewer") from e
    if not reviewer.base_branch:
        reviewer.base_branch = vcs.default_branch
    if not reviewer.enabled or not repo_enabled:
        return reviewer

    def fail(message: str, field_name: str) -> ConfigError:
        return ConfigError(message, file_path=file_path, field=field_name)

    if vcs.provider != "github":
        raise fail("reviewer mode supports only GitHub repos in this version", "reviewer")
    for part_name in ("readiness", "poster"):
        kind = parts[part_name].kind
        if kind != "command":
            raise fail(f"reviewer.{part_name}.kind '{kind}' is not built in this version", f"reviewer.{part_name}.kind")

    readiness, process, poster = parts["readiness"], parts["process"], parts["poster"]
    required = [
        ("readiness", "status_command", readiness.status_command),
        ("readiness", "threads_command", readiness.threads_command),
        ("process", "doc", process.doc),
        ("process", "run_dir", process.run_dir),
        ("process", "replies_plan", process.replies_plan),
        ("process", "worktree_dir", process.worktree_dir),
        ("poster", "review_command", poster.review_command),
        ("poster", "replies_command", poster.replies_command),
        ("poster", "dry_run_flag", poster.dry_run_flag),
        ("poster", "confirm_flag", poster.confirm_flag),
    ]
    for part_name, name, value in required:
        if not value:
            raise fail(f"reviewer.{part_name}.{name} is required", f"reviewer.{part_name}.{name}")
    for name in ("doc", "run_dir", "replies_plan", "worktree_dir"):
        if not isinstance(getattr(process, name), str):
            raise fail(f"reviewer.process.{name} must be a string", f"reviewer.process.{name}")

    def check_command(value: Any, field_name: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise fail(f"{field_name} must be a list of strings", field_name)
        return value

    commands = {
        "reviewer.readiness.status_command": check_command(readiness.status_command, "reviewer.readiness.status_command"),
        "reviewer.readiness.threads_command": check_command(readiness.threads_command, "reviewer.readiness.threads_command"),
        "reviewer.poster.review_command": check_command(poster.review_command, "reviewer.poster.review_command"),
        "reviewer.poster.replies_command": check_command(poster.replies_command, "reviewer.poster.replies_command"),
    }
    if not isinstance(process.prepare_steps, list):
        raise fail("reviewer.process.prepare_steps must be a list of lists of strings", "reviewer.process.prepare_steps")
    for i, step in enumerate(process.prepare_steps):
        commands[f"reviewer.process.prepare_steps.{i}"] = check_command(step, f"reviewer.process.prepare_steps.{i}")
    check_command(process.helper_scripts, "reviewer.process.helper_scripts")

    for name in ("doc", "run_dir", "replies_plan"):
        value = getattr(process, name)
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise fail(f"reviewer.process.{name} must be repo-relative", f"reviewer.process.{name}")

    texts = [(f, s) for f, cmd in commands.items() for s in cmd]
    texts += [(f"reviewer.process.{n}", getattr(process, n)) for n in ("run_dir", "replies_plan", "worktree_dir")]
    for field_name, text in texts:
        for placeholder in PLACEHOLDER_PATTERN.findall(text):
            if placeholder not in REVIEWER_PLACEHOLDERS:
                raise fail(f"{field_name}: unknown placeholder '{{{placeholder}}}'", field_name)
    return reviewer


def _load_repo_config(repo_path: Path, project_config: ProjectConfig, global_defaults_raw: dict) -> RepoConfig:
    """Load a single repo yaml and merge with project config."""
    data, env_refs = _load_yaml_file(repo_path)
    file_str = str(repo_path)

    repo_cfg = RepoConfig(
        repo=_parse_section(data, "repo", RepoInfo, file_str),
        vcs=_parse_vcs_section(data, file_str),
        ci=_parse_ci_section(data, file_str),
        git=_parse_section(data, "git", GitConfig, file_str),
        architecture=_parse_section(data, "architecture", ArchitectureConfig, file_str),
        linting=_parse_section(data, "linting", LintConfig, file_str),
        testing=_parse_section(data, "testing", TestConfig, file_str),
        build=_parse_section(data, "build", BuildConfig, file_str),
        helpers=_parse_section(data, "helpers", HelpersConfig, file_str),
        tracker_label=data.get("tracker_label", ""),
        jira_repo_label=data.get("jira_repo_label", ""),
        pr_description_template=data.get("pr_description_template", ""),
        parallelism=_parse_section(data, "parallelism", ParallelismConfig, file_str),
        # Inherited from project
        tracker=project_config.tracker,
        telegram=project_config.telegram if project_config.telegram.default_chat_id or project_config.telegram.bot_token else TelegramConfig(),
        defaults=_parse_defaults_section(data, file_str) if data.get("defaults") else project_config.defaults,
        env_refs=env_refs,
    )

    repo_cfg.reviewer = _parse_reviewer_section(data, repo_cfg.vcs, file_str, repo_cfg.repo.enabled)

    # Backward-compat: accept the old `jira_repo_label` key.
    if repo_cfg.jira_repo_label and not repo_cfg.tracker_label:
        repo_cfg.tracker_label = repo_cfg.jira_repo_label

    return repo_cfg


def load_config(
    config_dir: str,
    project_filter: str | None = None,
    repo_filter: str | None = None,
) -> tuple[GlobalConfig, dict[str, LoadedProject]]:
    """Load the full 3-level config hierarchy.

    Args:
        config_dir: Path to the configuration directory.
        project_filter: If set, only load this project.
        repo_filter: If set, only load this repo (requires project_filter).

    Returns:
        Tuple of (GlobalConfig, dict of project_id -> LoadedProject).
    """
    global_config = load_global_config(config_dir)
    global_defaults_raw = {
        k: v for k, v in global_config.defaults.__dict__.items()
    }

    projects_dir = Path(config_dir) / "projects"
    loaded_projects: dict[str, LoadedProject] = {}

    if not projects_dir.exists():
        return global_config, loaded_projects

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        project_id = project_dir.name

        # AC6: filter by --project
        if project_filter and project_id != project_filter:
            continue

        project_config = _load_project_config(project_dir, global_defaults_raw)
        if project_config is None:
            continue

        # AC5: skip disabled projects
        if not project_config.project.enabled:
            continue

        # Set project id from directory name if not in yaml
        if not project_config.project.id:
            project_config.project.id = project_id

        # Load repos
        repos_dir = project_dir / "repos"
        loaded_repos: dict[str, RepoConfig] = {}

        if repos_dir.exists():
            for repo_file in sorted(repos_dir.iterdir()):
                if not repo_file.is_file() or repo_file.suffix != ".yaml":
                    continue

                repo_id = repo_file.stem

                # AC6: filter by --repo
                if repo_filter and repo_id != repo_filter:
                    continue

                repo_config = _load_repo_config(repo_file, project_config, global_defaults_raw)

                # AC5: skip disabled repos
                if not repo_config.repo.enabled:
                    continue

                # Set repo id from filename if not in yaml
                if not repo_config.repo.id:
                    repo_config.repo.id = repo_id

                loaded_repos[repo_id] = repo_config

        loaded_projects[project_id] = LoadedProject(
            config=project_config,
            repos=loaded_repos,
            config_dir=str(project_dir),
        )

    return global_config, loaded_projects

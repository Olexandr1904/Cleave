"""Configuration loader for Cleave.

Loads and validates the 3-level config hierarchy:
  global.yaml -> project.yaml -> repo.yaml

Environment variable references (${VAR_NAME}) are resolved at load time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from config.schemas import (
    AgentBudget,
    ArchitectureConfig,
    BuildConfig,
    CIConfig,
    ClaudeConfig,
    DefaultsConfig,
    GitConfig,
    GitHubConfig,
    GitLabConfig,
    GlobalConfig,
    HeartbeatConfig,
    HelpersConfig,
    JenkinsConfig,
    JiraConfig,
    JiraStatusesConfig,
    LintConfig,
    LoadedProject,
    LoggingConfig,
    MaxIterationsConfig,
    OperatorProfile,
    ParallelismConfig,
    ProjectConfig,
    ProjectInfo,
    RepoConfig,
    RepoInfo,
    TelegramConfig,
    TestConfig,
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

    return GlobalConfig(
        telegram=_parse_section(data, "telegram", TelegramConfig, file_str),
        claude=_parse_section(data, "claude", ClaudeConfig, file_str),
        workspaces=_parse_section(data, "workspaces", WorkspacesConfig, file_str),
        defaults=_parse_defaults_section(data, file_str),
        logging=_parse_section(data, "logging", LoggingConfig, file_str),
        heartbeat=_parse_section(data, "heartbeat", HeartbeatConfig, file_str),
        operator=_parse_section(data, "operator", OperatorProfile, file_str),
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


def _load_yaml_file(path: Path) -> dict:
    """Load a YAML file, resolve env vars, return dict. Returns {} if empty."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}", file_path=str(path))
    with open(path) as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping", file_path=str(path))
    return resolve_env_vars_recursive(raw)


def _parse_jira_section(data: dict, file_path: str) -> JiraConfig:
    """Parse jira section, handling nested statuses."""
    jira_data = dict(data.get("jira", {}) or {})
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
        return VCSConfig(
            provider=provider,
            github=GitHubConfig(**github_data) if github_data else GitHubConfig(),
            gitlab=GitLabConfig(**gitlab_data) if gitlab_data else GitLabConfig(),
            **vcs_data,  # remaining top-level fields (skip_pre_push_hook, etc.)
        )
    except TypeError as e:
        raise ConfigError(f"Invalid fields in 'vcs': {e}", file_path=file_path, field="vcs") from e


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

    data = _load_yaml_file(project_yaml)
    file_str = str(project_yaml)

    return ProjectConfig(
        project=_parse_section(data, "project", ProjectInfo, file_str),
        jira=_parse_jira_section(data, file_str),
        telegram=_parse_section(data, "telegram", TelegramConfig, file_str),
        parallelism=_parse_section(data, "parallelism", ParallelismConfig, file_str),
        defaults=_parse_defaults_section(data, file_str),
    )


def _load_repo_config(repo_path: Path, project_config: ProjectConfig, global_defaults_raw: dict) -> RepoConfig:
    """Load a single repo yaml and merge with project config."""
    data = _load_yaml_file(repo_path)
    file_str = str(repo_path)

    return RepoConfig(
        repo=_parse_section(data, "repo", RepoInfo, file_str),
        vcs=_parse_vcs_section(data, file_str),
        ci=_parse_ci_section(data, file_str),
        git=_parse_section(data, "git", GitConfig, file_str),
        architecture=_parse_section(data, "architecture", ArchitectureConfig, file_str),
        linting=_parse_section(data, "linting", LintConfig, file_str),
        testing=_parse_section(data, "testing", TestConfig, file_str),
        build=_parse_section(data, "build", BuildConfig, file_str),
        helpers=_parse_section(data, "helpers", HelpersConfig, file_str),
        jira_repo_label=data.get("jira_repo_label", ""),
        pr_description_template=data.get("pr_description_template", ""),
        parallelism=_parse_section(data, "parallelism", ParallelismConfig, file_str),
        # Inherited from project
        jira=project_config.jira,
        telegram=project_config.telegram if project_config.telegram.default_chat_id or project_config.telegram.bot_token else TelegramConfig(),
        defaults=_parse_defaults_section(data, file_str) if data.get("defaults") else project_config.defaults,
    )


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

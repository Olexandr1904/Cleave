"""Configuration loader for Cleave.

Loads and validates the 3-level config hierarchy:
  global.yaml -> project.yaml -> repo.yaml

Environment variable references (${VAR_NAME}) are resolved at load time.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import posixpath
import re
import shlex
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
    JOB_PATH_PLACEHOLDERS,
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
    POSTER_PLACEHOLDERS,
    PosterPartConfig,
    PREPARE_STEP_PLACEHOLDERS,
    ProcessPartConfig,
    ProjectConfig,
    ProjectInfo,
    RepoConfig,
    ReadinessPartConfig,
    RepoInfo,
    REPLIES_PLACEHOLDERS,
    REVIEWER_PLACEHOLDERS,
    ReviewerConfig,
    ReviewerGlobalConfig,
    ReviewerJobConfig,
    SkillFlowConfig,
    STATUS_COMMAND_PLACEHOLDERS,
    TELEGRAM_LANGUAGES,
    TelegramConfig,
    TestConfig,
    THREADS_COMMAND_PLACEHOLDERS,
    TrackerConfig,
    TrelloConfig,
    TrelloListMapping,
    VCSConfig,
    WorkspacesConfig,
)
from integrations.llm.claude_code_adapter import API_KEY_VARS

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


def env_var_names(raw: Any) -> set[str]:
    """Every ${VAR} name in the raw (unresolved) YAML's string values, at any depth, not only the first of each
    string. Never a value."""
    if isinstance(raw, str):
        return set(ENV_VAR_PATTERN.findall(raw))
    values = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else ()
    return {name for value in values for name in env_var_names(value)}


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


def _parse_telegram_section(data: dict, file_path: str) -> TelegramConfig:
    """The `telegram:` section; its language, when set, must be one Cleave writes in."""
    telegram = _parse_section(data, "telegram", TelegramConfig, file_path)
    if telegram.language and telegram.language not in TELEGRAM_LANGUAGES:
        raise ConfigError(
            f"'telegram.language' must be one of {TELEGRAM_LANGUAGES} (got {telegram.language!r})",
            file_path=file_path,
            field="telegram.language",
        )
    return telegram


def _parse_dashboard_section(data: dict, file_path: str) -> DashboardConfig:
    """The `dashboard:` section; its allowed_networks must be a list of CIDR networks (an empty value would read as
    any peer, a bad one would fail every request) and its allowed_hosts a list of names."""
    dashboard = _parse_section(data, "dashboard", DashboardConfig, file_path)
    networks = dashboard.allowed_networks
    if not isinstance(networks, list) or not all(isinstance(n, str) for n in networks):
        raise ConfigError(
            f"'dashboard.allowed_networks' must be a list of CIDR networks (got {networks!r})",
            file_path=file_path, field="dashboard.allowed_networks",
        )
    for network in networks:
        try:
            ipaddress.ip_network(network)
        except ValueError as e:
            raise ConfigError(
                f"'dashboard.allowed_networks' has a bad network {network!r}: {e}",
                file_path=file_path, field="dashboard.allowed_networks",
            ) from e
    hosts = dashboard.allowed_hosts
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ConfigError(
            f"'dashboard.allowed_hosts' must be a list of host names (got {hosts!r})",
            file_path=file_path, field="dashboard.allowed_hosts",
        )
    return dashboard


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
    cap = reviewer.max_parallel_jobs
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ConfigError(
            f"'reviewer.max_parallel_jobs' must be a whole number of at least 1, not {cap!r}",
            file_path=file_path, field="reviewer.max_parallel_jobs",
        )
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
        telegram=_parse_telegram_section(data, file_str),
        claude=_parse_section(data, "claude", ClaudeConfig, file_str),
        workspaces=_parse_section(data, "workspaces", WorkspacesConfig, file_str),
        defaults=_parse_defaults_section(data, file_str),
        logging=_parse_section(data, "logging", LoggingConfig, file_str),
        heartbeat=_parse_section(data, "heartbeat", HeartbeatConfig, file_str),
        operator=_parse_section(data, "operator", OperatorProfile, file_str),
        failure_summarizer=_parse_section(data, "failure_summarizer", FailureSummarizerConfig, file_str),
        pipeline=_parse_section(data, "pipeline", PipelineConfig, file_str),
        intent_parser=_parse_section(data, "intent_parser", IntentParserConfig, file_str),
        dashboard=_parse_dashboard_section(data, file_str),
        reviewer=_parse_reviewer_global(data, file_str),
        env_refs=env_var_refs(raw_data),
        env_vars=env_var_names(raw_data),
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


def _load_yaml_file(path: Path) -> tuple[dict, dict[str, str], set[str]]:
    """Load a YAML file; return (data with env vars resolved, env refs, env var names). {} data if empty."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}", file_path=str(path))
    with open(path) as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}, {}, set()
    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping", file_path=str(path))
    return resolve_env_vars_recursive(raw), env_var_refs(raw), env_var_names(raw)


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

    data, env_refs, env_vars = _load_yaml_file(project_yaml)
    file_str = str(project_yaml)

    return ProjectConfig(
        project=_parse_section(data, "project", ProjectInfo, file_str),
        tracker=_parse_tracker_section(data, file_str),
        telegram=_parse_telegram_section(data, file_str),
        parallelism=_parse_section(data, "parallelism", ParallelismConfig, file_str),
        defaults=_parse_defaults_section(data, file_str),
        env_refs=env_refs,
        env_vars=env_vars,
    )


PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")
# Files a command may be given from an untrusted directory: data it reads.
_DATA_FILES = (".md", ".json", ".txt", ".diff", ".patch", ".log")
# Shells: each runs a script named by an argument (a launcher) and code given with -c. One list for both.
_SHELLS = "sh|bash|zsh|dash|ksh|mksh|ash|fish"
# Programs that run a script, a package or a directory named by an argument.
_LAUNCHER = re.compile(
    r"python|python3|python3\..+|node|deno|bun|ruby|perl|php|env|uv|uvx|npx|poetry|pipenv|pipx"
    rf"|\.|source|{_SHELLS}")
_SHELL = re.compile(_SHELLS)
_SHELL_CODE_OPTION = re.compile(r"-[A-Za-z]*c[A-Za-z]*")  # -c, -lc, -ec, -Cc
_CHANGE_DIR = re.compile(r"cd|pushd")
# Programs that run the rest of their words as a command, with their options that take the next word.
_WRAPPERS = {"command": "", "exec": "a", "nohup": "", "time": "", "nice": "n", "timeout": "ks", "xargs": "adEIJLnPRSs",
             "stdbuf": "eio", "caffeinate": "tw"}
# Their long options that take the next word as their value (`--opt=value` is one word).
_LONG_TAKES_VALUE = {"timeout": ("--signal", "--kill-after"), "nice": ("--adjustment",),
                     "stdbuf": ("--input", "--output", "--error"),
                     "xargs": ("--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars",
                               "--process-slot-var")}
_DURATION = re.compile(r"[0-9]+(\.[0-9]+)?[smhd]?")  # `timeout 30`, `timeout 1.5m`
_ENV_TAKES_VALUE = "CLPSUu"  # env's options with a value (GNU and BSD): -u NAME, -C DIR, -S STRING, -P PATH
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")  # NAME=value before a program, as `sh -c` code may set one
# An argument naming a file, such as the script a launcher runs: no option, no NAME=value.
_FILE_ARG = re.compile(r"(?!-|[A-Za-z_]\w*=)(\S*/\S*|\S*\.\w+)")
# A path from one of the job copy's placeholders to the end of its word.
_JOB_COPY_PATH = re.compile(r"\{(job_dir|report|plan)\}([^\s;&|<>()'\":]*)")


def _dir_pattern(directory: str) -> re.Pattern[str]:
    """A directory template as a pattern for the paths that name it, its placeholders matching any name: at
    any path boundary (an absolute path into the clone, or a word of `sh -c` code, too) and whatever the
    case (the host's APFS)."""
    pattern = "".join("[^/]*" if i % 2 else re.escape(piece)
                      for i, piece in enumerate(PLACEHOLDER_PATTERN.split(posixpath.normpath(directory))))
    return re.compile(rf"(?<![\w.-]){pattern}(?![\w.-])", re.IGNORECASE)


def _named_dirs(arg: str, patterns: dict[str, re.Pattern[str]]) -> list[tuple[str, bool]]:
    """The directories an argument names, each with whether it names a path inside the directory rather than
    the directory itself."""
    path = posixpath.normpath(arg)
    return [(name, path[m.end():m.end() + 1] == "/")
            for name, pattern in patterns.items() for m in pattern.finditer(path)]


def _shell_commands(code: str) -> list[list[str]]:
    """The simple commands of `sh -c` code: its words split at ;, &, | and parentheses; the target of a
    redirection stays an argument."""
    lexer = shlex.shlex(code, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # an unbalanced quote: sh refuses the code anyway
        words = code.split()
    commands: list[list[str]] = [[]]
    for word in words:
        if not set(word) <= set("();<>|&"):
            commands[-1].append(word)
        elif not set(word) & set("<>"):
            commands.append([])
    return [command for command in commands if command]


def _wrapped_start(command: list[str], takes_value: str) -> int:
    """Where the command a wrapper runs starts: after the wrapper, its options (with the value a short option in
    `takes_value`, or one of its long options in _LONG_TAKES_VALUE, takes) and its numbers or durations
    (`timeout -s KILL 30`, `timeout --signal KILL 5s`, `nice -n 10`). A number or a duration takes no value."""
    long_takes_value = _LONG_TAKES_VALUE.get(posixpath.basename(command[0]), ())
    i = 1
    while i < len(command) and (command[i].startswith("-") or _DURATION.fullmatch(command[i])):
        option = command[i]
        i += 1
        if option == "--":
            break
        if _DURATION.fullmatch(option):
            continue
        if (len(option) == 2 and option[1] in takes_value) or option in long_takes_value:
            i += 1
    return i


def _runs_shell_code(command: list[str]) -> bool:
    """Whether a command runs a shell's -c code, past the transparent wrappers and `env` (its options and
    NAME=value words): an argument appended to it becomes the code's $0, never a word of what the code runs."""
    i = 0
    while i < len(command):
        program = posixpath.basename(command[i])
        if program in _WRAPPERS:
            i += _wrapped_start(command[i:], _WRAPPERS[program])
        elif program == "env":
            i += _wrapped_start(command[i:], _ENV_TAKES_VALUE)
            while i < len(command) and _ASSIGNMENT.match(command[i]):
                i += 1
        else:
            break
    return (i < len(command) and bool(_SHELL.fullmatch(posixpath.basename(command[i])))
            and any(_SHELL_CODE_OPTION.fullmatch(arg) for arg in command[i + 1:]))


def _untrusted_use(command: list[str], patterns: dict[str, re.Pattern[str]],
                   cd_counts: bool = False) -> tuple[int, str] | None:
    """Where a command would run code from one of the directories: the argument's index and the directory's
    name, or None. Its program inside one; a launcher's operand (its first argument naming a file) inside one,
    whatever its extension; any other argument inside one, unless it names a data file; the directory itself
    given to a launcher before its operand (`python3 dir/` runs dir/__main__.py); a NAME=value before the
    program as `env NAME=value` (`PYTHONPATH=dir python3 x.py`); a wrapper (`nohup`, `timeout 30`) as the
    command it runs; with `cd_counts`, `cd` or `pushd` into one or to it (what follows runs from there); and
    the same for each command of `sh -c` code."""
    if not command:
        return None
    assignments = next((i for i, word in enumerate(command) if not _ASSIGNMENT.match(word)), len(command))
    for i, word in enumerate(command[:assignments]):
        data = word.lower().endswith(_DATA_FILES)
        for name, inside in _named_dirs(word, patterns):
            if not (inside and data):
                return i, name
    if assignments:
        if hit := _untrusted_use(command[assignments:], patterns, cd_counts):
            return hit[0] + assignments, hit[1]
        return None
    for name, inside in _named_dirs(command[0], patterns):
        if inside:
            return 0, name
    program = posixpath.basename(command[0])
    if program in _WRAPPERS:
        start = _wrapped_start(command, _WRAPPERS[program])
        for i, arg in enumerate(command[1:start], 1):   # its own options: arguments of a program
            for name, inside in _named_dirs(arg, patterns):
                if inside and not arg.lower().endswith(_DATA_FILES):
                    return i, name
        if hit := _untrusted_use(command[start:], patterns, cd_counts):
            return hit[0] + start, hit[1]
        return None
    if _SHELL.fullmatch(program) and any(_SHELL_CODE_OPTION.fullmatch(arg) for arg in command[1:]):
        for i, arg in enumerate(command[1:], 1):
            for inner in [] if arg.startswith("-") else _shell_commands(arg):
                if hit := _untrusted_use(inner, patterns, cd_counts):
                    return i, hit[1]
        return None
    launcher, file_seen = _LAUNCHER.fullmatch(program), False
    changes_dir = cd_counts and _CHANGE_DIR.fullmatch(program)
    for i, arg in enumerate(command[1:], 1):
        names_a_file = bool(_FILE_ARG.fullmatch(arg))
        # An interpreter ignores the extension: the file a launcher runs is never data.
        data = arg.lower().endswith(_DATA_FILES) and not (launcher and not file_seen and names_a_file)
        for name, inside in _named_dirs(arg, patterns):
            if changes_dir or (inside and not data) or (not inside and launcher and not file_seen):
                return i, name
        file_seen = file_seen or names_a_file
    return None


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
    for name, least in (("poll_interval_seconds", 60), ("quiet_minutes", 0), ("max_files_auto", 1)):
        value = getattr(reviewer, name)
        # bool is an int subclass; `true` is never a count.
        if not isinstance(value, int) or isinstance(value, bool) or value < least:
            field_name = f"reviewer.{name}"
            raise fail(f"{field_name} must be an integer of at least {least}", field_name)
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
    for name in ("doc", "run_dir", "replies_plan", "worktree_dir", "address_doc", "sharded_section"):
        if not isinstance(getattr(process, name), str):
            raise fail(f"reviewer.process.{name} must be a string", f"reviewer.process.{name}")
    count = process.sharded_min_files
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise fail("reviewer.process.sharded_min_files must be an integer of at least 1",
                   "reviewer.process.sharded_min_files")

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
    check_command(process.address_protected, "reviewer.process.address_protected")

    for name in ("doc", "run_dir", "replies_plan", "address_doc"):
        value = getattr(process, name)
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise fail(f"reviewer.process.{name} must be repo-relative", f"reviewer.process.{name}")
    # The review session may write the run directory and the replies plan's. At the repo root its Edit rule
    # would cover the whole clone, and the guard below would have no directory to look for.
    writable = {"run_dir": process.run_dir, "replies_plan": posixpath.dirname(posixpath.normpath(process.replies_plan))}
    if posixpath.normpath(writable["run_dir"]) == ".":
        raise fail("reviewer.process.run_dir must be a subdirectory of the repo, not its root: the review "
                   "session may write it", "reviewer.process.run_dir")
    if posixpath.normpath(writable["replies_plan"]) == ".":
        raise fail("reviewer.process.replies_plan must be in a subdirectory of the repo, not at its root: the "
                   "review session may write its directory", "reviewer.process.replies_plan")
    if posixpath.normpath(process.worktree_dir) == ".":
        raise fail("reviewer.process.worktree_dir must not be the repo root: it holds the PR's own code",
                   "reviewer.process.worktree_dir")

    texts = [(f, s) for f, cmd in commands.items() for s in cmd]
    texts += [(f"reviewer.process.{n}", getattr(process, n)) for n in ("run_dir", "replies_plan", "worktree_dir")]
    # What the runtime fills for each field.
    filled = {
        "reviewer.readiness.status_command": STATUS_COMMAND_PLACEHOLDERS,
        "reviewer.readiness.threads_command": THREADS_COMMAND_PLACEHOLDERS,
        "reviewer.poster.review_command": POSTER_PLACEHOLDERS,
        "reviewer.poster.replies_command": REPLIES_PLACEHOLDERS,
        **{f"reviewer.process.{n}": JOB_PATH_PLACEHOLDERS for n in ("run_dir", "replies_plan", "worktree_dir")},
        **{f"reviewer.process.prepare_steps.{i}": PREPARE_STEP_PLACEHOLDERS for i in range(len(process.prepare_steps))},
    }
    for field_name, text in texts:
        names = filled[field_name]
        for placeholder in PLACEHOLDER_PATTERN.findall(text):
            if placeholder not in REVIEWER_PLACEHOLDERS:
                raise fail(f"{field_name}: unknown placeholder '{{{placeholder}}}'", field_name)
            if placeholder not in names:
                takes = ", ".join(f"{{{n}}}" for n in sorted(names))
                raise fail(f"{field_name}: placeholder '{{{placeholder}}}' is never filled there; it takes {takes}",
                           field_name)

    # A script run from one of these directories would be the session's code or the PR author's, run on the
    # host: prepare steps with HOME, the readiness and poster commands with the forge token. The poster is
    # given the job's copy of the run directory, outside the clone: a sentinel path no repo names stands for it.
    # A `cd` into one counts only for the commands with the token: a prepare step may build the PR in its
    # worktree (the operator's choice). Limits: a directory given to a program that is not a launcher reads as
    # data, so `make -C {job_dir}` and `npm --prefix {job_dir} run post` still load; variables and command
    # substitutions inside `sh -c` are not followed (`D={job_dir}/x.json; python3 $D` loads).
    job_copy = "/cleave-job-copy"
    untrusted = {**writable, "worktree_dir": process.worktree_dir, "job_dir": job_copy}
    where = {**{n: f"reviewer.process.{n}, which the review session may write" for n in writable},
             "worktree_dir": "reviewer.process.worktree_dir, which holds the PR's own code",
             "job_dir": "{job_dir}, the job's copy of what the review session wrote"}
    patterns = {name: _dir_pattern(directory) for name, directory in untrusted.items()}
    in_untrusted = {"job_dir": job_copy, "report": f"{job_copy}/report_{{head9}}.md",
                    "plan": f"{job_copy}/posting/replies_{{head9}}.json", "worktree": process.worktree_dir}
    for field_name, command in commands.items():
        # Next to the job's copy are the other jobs' copies, and the sentinel would fold away under `..`.
        for arg in command:
            for m in _JOB_COPY_PATH.finditer(arg):
                if ".." in m[2].split("/")[1:]:
                    raise fail(f"{field_name} has '..' after {{{m[1]}}} in {arg!r}: it leaves the job's copy",
                               field_name)
        expanded = [PLACEHOLDER_PATTERN.sub(lambda m: in_untrusted.get(m[1], m[0]), arg) for arg in command]
        with_token = field_name.startswith(("reviewer.readiness.", "reviewer.poster."))
        if hit := _untrusted_use(expanded, patterns, cd_counts=with_token):
            i, name = hit
            raise fail(f"{field_name} runs {command[i]!r} from inside {where[name]}", field_name)

    # Cleave fills these on every post; the ledger is how a post never lands twice.
    for field_name, required in (("reviewer.poster.review_command", ("report", "ledger", "event")),
                                 ("reviewer.poster.replies_command", ("plan",))):
        passed = {p for arg in commands[field_name] for p in PLACEHOLDER_PATTERN.findall(arg)}
        for placeholder in required:
            if placeholder not in passed:
                raise fail(f"{field_name} must pass {{{placeholder}}}: Cleave fills it on every post", field_name)

    # Cleave appends one of the two flags to every poster run: after shell -c code it never reaches the tool,
    # and one flag for both, or a template passing one itself, would turn a dry run into a post.
    for field_name in ("reviewer.poster.review_command", "reviewer.poster.replies_command"):
        if _runs_shell_code(commands[field_name]):
            raise fail(f"{field_name} must be a direct command, not shell -c code: the dry-run flag Cleave "
                       "appends must reach the tool", field_name)
    flags = {"dry_run_flag": poster.dry_run_flag, "confirm_flag": poster.confirm_flag}
    for name, flag in flags.items():
        if not isinstance(flag, str):
            raise fail(f"reviewer.poster.{name} must be a non-empty string", f"reviewer.poster.{name}")
    if poster.confirm_flag == poster.dry_run_flag:
        raise fail("reviewer.poster.confirm_flag must differ from reviewer.poster.dry_run_flag: Cleave tells a "
                   "dry run from a post by them", "reviewer.poster.confirm_flag")
    for field_name in ("reviewer.poster.review_command", "reviewer.poster.replies_command"):
        for flag in flags.values():
            if any(word == flag or word.startswith(f"{flag}=") for word in _flag_words(commands[field_name])):
                raise fail(f"{field_name} passes {flag!r} itself: Cleave appends reviewer.poster.dry_run_flag "
                           "or confirm_flag to every run", field_name)
    return reviewer


def _flag_words(command: list[str]) -> list[str]:
    """The words a poster flag could be among: the command's arguments, and each word of the code a shell runs
    with -c (behind any program)."""
    words = list(command)
    for i, arg in enumerate(command):
        rest = command[i + 1:]
        if _SHELL.fullmatch(posixpath.basename(arg)) and any(_SHELL_CODE_OPTION.fullmatch(a) for a in rest):
            words += [word for code in rest if not code.startswith("-")
                      for inner in _shell_commands(code) for word in inner]
            break
    return words


_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:-]*")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_GATES = ("test", "lint", "build")


def _repo_relative(path: str) -> bool:
    return not Path(path).is_absolute() and ".." not in Path(path).parts


def _parse_skill_flow(
    data: dict, vcs: VCSConfig, config_vars: set[str], file_path: str, repo_enabled: bool,
) -> tuple[str, SkillFlowConfig]:
    """Parse the repo's `ticket_flow` and `skill_flow:`; the section is validated only when the flow is `both`
    and the repo is enabled. `config_vars`: the variables the global, project and repo configs read."""

    def fail(message: str, field_name: str) -> ConfigError:
        return ConfigError(message, file_path=file_path, field=field_name)

    ticket_flow = data.get("ticket_flow", "staged")
    if ticket_flow not in ("staged", "both"):
        raise fail(f"ticket_flow must be 'staged' or 'both' (got {ticket_flow!r})", "ticket_flow")
    raw = _mapping(data.get("skill_flow"), "skill_flow", file_path)
    try:
        skill_flow = SkillFlowConfig(**raw)
    except TypeError as e:
        raise fail(f"Invalid fields in 'skill_flow': {e}", "skill_flow") from e
    if ticket_flow != "both" or not repo_enabled:
        return ticket_flow, skill_flow

    if vcs.provider != "github":
        raise fail("ticket_flow 'both' supports only GitHub repos in this version", "ticket_flow")
    if not raw:
        raise fail("ticket_flow 'both' needs a skill_flow section", "skill_flow")
    for name in ("bug_skill", "task_skill"):
        value = getattr(skill_flow, name)
        if not isinstance(value, str) or not _SKILL_NAME.fullmatch(value):
            raise fail(f"skill_flow.{name} must be a skill name: a letter or digit, then letters, digits, '_', ':' "
                       f"or '-' (got {value!r})", f"skill_flow.{name}")
    # A bare string would be matched character by character.
    if not isinstance(skill_flow.bug_types, list) or not all(isinstance(t, str) for t in skill_flow.bug_types):
        raise fail("skill_flow.bug_types must be a list of ticket type names", "skill_flow.bug_types")
    for name in ("host_notes", "effort"):
        if not isinstance(getattr(skill_flow, name), str):
            raise fail(f"skill_flow.{name} must be a string", f"skill_flow.{name}")
    for name in ("notes_dir", "worktree_dir"):
        value = getattr(skill_flow, name)
        # Each run fills {key} with its ticket's key: without it two tickets would share one directory.
        if not (isinstance(value, str) and _repo_relative(value)
                and set(PLACEHOLDER_PATTERN.findall(value)) == {"key"}):
            raise fail(f"skill_flow.{name} must be a repo-relative path without '..' that holds {{key}} and no other "
                       f"placeholder (got {value!r})", f"skill_flow.{name}")
    # Apart, whatever the case (the host's APFS): the ticket copy in the worktree would be published, and a worktree
    # cannot be made where the notes already are.
    notes, worktree = (posixpath.normpath(d).casefold() for d in (skill_flow.notes_dir, skill_flow.worktree_dir))
    if posixpath.commonpath([notes, worktree]) in (notes, worktree):
        raise fail(f"skill_flow.notes_dir and skill_flow.worktree_dir must not be one directory or one inside the "
                   f"other (got {skill_flow.notes_dir!r} and {skill_flow.worktree_dir!r})", "skill_flow.notes_dir")
    files = skill_flow.required_files
    if not isinstance(files, list) or not all(isinstance(f, str) and _repo_relative(f) for f in files):
        raise fail("skill_flow.required_files must be a list of repo-relative paths without '..'",
                   "skill_flow.required_files")
    gates = skill_flow.gates
    if not isinstance(gates, dict) or not all(k in _GATES and isinstance(v, str) for k, v in gates.items()):
        raise fail(f"skill_flow.gates must map some of {', '.join(_GATES)} to a command string",
                   "skill_flow.gates")
    for name, least in (("gate_rounds", 0), ("timeout_seconds", 1)):
        value = getattr(skill_flow, name)
        # bool is an int subclass; `true` is never a count.
        if not isinstance(value, int) or isinstance(value, bool) or value < least:
            raise fail(f"skill_flow.{name} must be an integer of at least {least}", f"skill_flow.{name}")
    if not isinstance(skill_flow.build_env, list):
        raise fail("skill_flow.build_env must be a list of environment variable names", "skill_flow.build_env")
    for name in skill_flow.build_env:
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise fail(f"skill_flow.build_env: {name!r} is not an environment variable name", "skill_flow.build_env")
        # The session holds no secret: a variable a config level reads one from (the forge token in the repo's,
        # the tracker's in the project's, Telegram's in the global), or one that makes `claude` bill an API key
        # instead of the subscription.
        if name in config_vars:
            raise fail(f"skill_flow.build_env must not pass {name}: Cleave's config reads a secret from it",
                       "skill_flow.build_env")
        if name in API_KEY_VARS:
            raise fail(f"skill_flow.build_env must not pass {name}: with it `claude` bills an API key",
                       "skill_flow.build_env")
    return ticket_flow, skill_flow


def _load_repo_config(repo_path: Path, project_config: ProjectConfig, global_defaults_raw: dict, *,
                      global_env_vars: set[str] | None = None) -> RepoConfig:
    """Load a single repo yaml and merge with project config."""
    data, env_refs, env_vars = _load_yaml_file(repo_path)
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
    # Every name each level's strings read, not env_refs' first one per string: a clone URL can read a user and a
    # token.
    config_vars = {*(global_env_vars or set()), *project_config.env_vars, *env_vars}
    repo_cfg.ticket_flow, repo_cfg.skill_flow = _parse_skill_flow(
        data, repo_cfg.vcs, config_vars, file_str, repo_cfg.repo.enabled)

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

                repo_config = _load_repo_config(repo_file, project_config, global_defaults_raw,
                                                global_env_vars=global_config.env_vars)

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

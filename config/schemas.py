"""Configuration schema dataclasses for Sickle."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TelegramConfig:
    bot_token: str = ""
    default_chat_id: str = ""


@dataclass
class ClaudeConfig:
    api_key: str = ""
    model: str = "claude-sonnet-4-5"


@dataclass
class WorkspacesConfig:
    base_dir: str = "/workspaces"
    max_age_days: int = 7
    isolation: str = "directory"
    min_free_disk_gb: int = 5
    max_workspace_size_gb: int = 2


@dataclass
class DefaultsConfig:
    poll_interval_seconds: int = 900
    max_fix_iterations: int = 3
    max_scope_iterations: int = 3
    max_qa_iterations: int = 2
    max_parallel_tickets: int = 2


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "/var/log/sickle"


@dataclass
class HeartbeatConfig:
    enabled: bool = True
    interval_hours: int = 24
    send_at: str = ""


@dataclass
class OperatorProfile:
    role: str = ""
    stack: list[str] = field(default_factory=list)
    preferences: dict[str, str] = field(default_factory=dict)
    rules: list[str] = field(default_factory=list)


@dataclass
class GlobalConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    workspaces: WorkspacesConfig = field(default_factory=WorkspacesConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    operator: OperatorProfile = field(default_factory=OperatorProfile)


# --- Jira config (project-level) ---


@dataclass
class JiraStatusesConfig:
    todo: str = "To Do"
    in_progress: str = "In Progress"
    in_review: str = "In Review"
    done: str = "Done"


@dataclass
class JiraConfig:
    url: str = ""
    token: str = ""
    email: str = ""
    project_key: str = ""
    trigger_label: str = "ai-ready"
    ignore_labels: list[str] = field(default_factory=list)
    statuses: JiraStatusesConfig = field(default_factory=JiraStatusesConfig)


@dataclass
class ParallelismConfig:
    max_concurrent_tickets: int = 2


# --- GitHub / Git config (repo-level) ---


@dataclass
class GitHubConfig:
    token: str = ""
    owner: str = ""
    repo: str = ""
    default_branch: str = "main"
    branch_prefix: str = "feature"
    merge_method: str = "squash"


@dataclass
class GitConfig:
    clone_url: str = ""
    commit_author_name: str = "Sickle Pipeline"
    commit_author_email: str = "sickle@pipeline.local"
    depth: int = 0


@dataclass
class ArchitectureConfig:
    rules_file: str = ""


@dataclass
class LintConfig:
    tool: str = ""
    config_file: str = ""
    run_command: str = ""
    report_path: str = ""
    hard_gate: bool = True


@dataclass
class TestConfig:
    run_command: str = ""
    report_path: str = ""
    hard_gate: bool = True


@dataclass
class BuildConfig:
    check_command: str = ""
    hard_gate: bool = True


@dataclass
class CopilotConfig:
    enabled: bool = False
    wait_for_review_minutes: int = 15


@dataclass
class ExistingScriptsConfig:
    ticket_to_prompt: str = ""
    copilot_validator: str = ""


# --- Project config ---


@dataclass
class ProjectInfo:
    id: str = ""
    name: str = ""
    enabled: bool = True


@dataclass
class ProjectConfig:
    project: ProjectInfo = field(default_factory=ProjectInfo)
    jira: JiraConfig = field(default_factory=JiraConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    parallelism: ParallelismConfig = field(default_factory=ParallelismConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)


# --- Repo config ---


@dataclass
class RepoInfo:
    id: str = ""
    name: str = ""
    enabled: bool = True


@dataclass
class RepoConfig:
    repo: RepoInfo = field(default_factory=RepoInfo)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git: GitConfig = field(default_factory=GitConfig)
    architecture: ArchitectureConfig = field(default_factory=ArchitectureConfig)
    linting: LintConfig = field(default_factory=LintConfig)
    testing: TestConfig = field(default_factory=TestConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)
    existing_scripts: ExistingScriptsConfig = field(default_factory=ExistingScriptsConfig)
    jira_repo_label: str = ""
    pr_description_template: str = ""
    parallelism: ParallelismConfig = field(default_factory=ParallelismConfig)
    # Inherited from project/global — merged at load time
    jira: JiraConfig = field(default_factory=JiraConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)


# --- Loaded project with its repos ---


@dataclass
class LoadedProject:
    """A fully loaded project with its merged config and repos."""
    config: ProjectConfig = field(default_factory=ProjectConfig)
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    config_dir: str = ""

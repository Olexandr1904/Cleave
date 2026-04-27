"""Configuration schema dataclasses for Sickle (v2)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TelegramConfig:
    bot_token: str = ""
    default_chat_id: str = ""


@dataclass
class ClaudeConfig:
    api_key: str = ""


@dataclass
class WorkspacesConfig:
    base_dir: str = "/data"
    max_age_days: int = 7
    min_free_disk_gb: int = 5
    max_workspace_size_gb: int = 2


@dataclass
class MaxIterationsConfig:
    scope_guard: int = 3
    fix: int = 3
    qa: int = 2
    dev: int = 2


@dataclass
class DefaultsConfig:
    poll_interval_seconds: int = 60
    max_iterations: MaxIterationsConfig = field(default_factory=MaxIterationsConfig)
    max_parallel_tickets: int = 7
    pr_comment_fetch_delay_minutes: int = 30


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
class PipelineConfig:
    mode: str = "manual"  # "auto" or "manual"


@dataclass
class IntentParserConfig:
    timeout_seconds: int = 30


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    db_path: str = "data/events.db"
    terminal_command: str = "gnome-terminal -- bash -c"


@dataclass
class GlobalConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    workspaces: WorkspacesConfig = field(default_factory=WorkspacesConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    operator: OperatorProfile = field(default_factory=OperatorProfile)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    intent_parser: IntentParserConfig = field(default_factory=IntentParserConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


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
    trigger_labels: list[str] = field(default_factory=lambda: ["ai-pipeline"])
    ignore_labels: list[str] = field(default_factory=list)
    statuses: JiraStatusesConfig = field(default_factory=JiraStatusesConfig)


@dataclass
class ParallelismConfig:
    max_concurrent_tickets: int = 7


# --- VCS config (repo-level) ---


@dataclass
class GitHubConfig:
    token: str = ""
    owner: str = ""
    repo: str = ""
    default_branch: str = "develop"
    branch_prefix: str = "feature"
    merge_method: str = "squash"


@dataclass
class GitLabConfig:
    token: str = ""
    project_id: str = ""
    url: str = "https://gitlab.com"
    default_branch: str = "develop"
    branch_prefix: str = "feature"


@dataclass
class VCSConfig:
    """VCS provider selection. Only one sub-config is used based on provider."""
    provider: str = "github"  # "github" or "gitlab"
    github: GitHubConfig = field(default_factory=GitHubConfig)
    gitlab: GitLabConfig = field(default_factory=GitLabConfig)


# --- CI config (repo-level) ---


@dataclass
class JenkinsConfig:
    url: str = ""
    job_key: str = ""
    username: str = ""
    token: str = ""


@dataclass
class CIConfig:
    """CI provider selection. Only one sub-config is used based on provider."""
    provider: str = "github_actions"  # "github_actions" or "jenkins"
    jenkins: JenkinsConfig = field(default_factory=JenkinsConfig)


@dataclass
class GitConfig:
    clone_url: str = ""
    commit_author_name: str = "Sickle Bot"
    commit_author_email: str = "sickle@pipeline.local"
    depth: int = 0


@dataclass
class ArchitectureConfig:
    rules_file: str = ""
    protected_files: list[str] = field(default_factory=list)


@dataclass
class LintConfig:
    run_command: str = ""
    hard_gate: bool = True


@dataclass
class TestConfig:
    run_command: str = ""
    hard_gate: bool = True


@dataclass
class BuildConfig:
    check_command: str = ""
    hard_gate: bool = True


@dataclass
class HelpersConfig:
    """Paths to existing helper scripts (wrapped as subprocesses)."""
    fetch_pr_comments: str = ""
    resolve_pr_comments: str = ""
    fetch_ci_failure: str = ""
    fetch_jira_tickets: str = ""
    update_jira_status: str = ""
    # GitLab-specific helpers
    fetch_mr_comments: str = ""
    resolve_mr_comments: str = ""
    code_review: str = ""
    post_review_comments: str = ""
    fetch_jenkins: str = ""
    create_jira_ticket: str = ""


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
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)


# --- Repo config ---


@dataclass
class RepoInfo:
    id: str = ""
    name: str = ""
    enabled: bool = True


@dataclass
class RepoConfig:
    repo: RepoInfo = field(default_factory=RepoInfo)
    vcs: VCSConfig = field(default_factory=VCSConfig)
    ci: CIConfig = field(default_factory=CIConfig)
    git: GitConfig = field(default_factory=GitConfig)
    architecture: ArchitectureConfig = field(default_factory=ArchitectureConfig)
    linting: LintConfig = field(default_factory=LintConfig)
    testing: TestConfig = field(default_factory=TestConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    helpers: HelpersConfig = field(default_factory=HelpersConfig)
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

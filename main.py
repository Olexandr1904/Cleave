#!/usr/bin/env python3
"""Cleave — Autonomous AI Development Pipeline.

Entry point for the pipeline daemon. Parses CLI arguments
and starts the orchestrator.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Coroutine, Mapping
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

from config.schemas import GlobalConfig, LoadedProject, ProjectConfig, RepoConfig
from integrations.base.vcs import VCSInterface
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.service import RepoContext, _where
from orchestrator.skillflow.runner import SkillRepo


def _build_tracker_for_project(cfg, project_id, on_auth_failure=None):
    """Build a TrackerInterface from a TrackerConfig.

    Returns None if the provider's credentials are missing (lenient behavior —
    a project can be partially configured during setup).
    """
    if cfg.provider == "jira" and cfg.jira.url:
        from integrations.jira.jira_adapter import JiraAdapter
        j = cfg.jira
        return JiraAdapter(
            url=j.url, email=j.email, token=j.token,
            project_key=j.project_key,
            trigger_labels=j.trigger_labels,
            ignore_labels=j.ignore_labels,
            statuses={
                "todo": j.statuses.todo,
                "in_progress": j.statuses.in_progress,
                "in_review": j.statuses.in_review,
                "done": j.statuses.done,
            },
            on_auth_failure=on_auth_failure,
            transition_via=j.transition_via,
            transition_dates=j.transition_dates,
        )
    if cfg.provider == "trello" and cfg.trello.api_key and cfg.trello.token and cfg.trello.board_id:
        from integrations.trello.trello_adapter import TrelloAdapter
        t = cfg.trello
        return TrelloAdapter(
            api_key=t.api_key,
            token=t.token,
            board_id=t.board_id,
            trigger_labels=t.trigger_labels,
            ignore_labels=t.ignore_labels,
            list_mapping={
                "todo": t.lists.todo,
                "in_progress": t.lists.in_progress,
                "in_review": t.lists.in_review,
                "done": t.lists.done,
            },
            on_auth_failure=on_auth_failure,
        )
    # No matching provider configured or credentials missing — caller will
    # treat this project as "no tracker".
    logger.warning(
        "No tracker built for project %s (provider=%s); check credentials.",
        project_id, cfg.provider,
    )
    return None


def _build_vcs_adapter(
    repo_cfg: RepoConfig, on_auth_failure: Callable[[int], None] | None = None,
) -> VCSInterface | None:
    """Return the VCS adapter for this repo, or None if the provider is
    unsupported or required credentials are missing."""
    provider = repo_cfg.vcs.provider
    if provider == "github" and repo_cfg.vcs.github.token:
        from integrations.github.github_adapter import GitHubAdapter
        return GitHubAdapter(
            token=repo_cfg.vcs.github.token,
            owner=repo_cfg.vcs.github.owner,
            repo=repo_cfg.vcs.github.repo,
            on_auth_failure=on_auth_failure,
        )
    if provider == "gitlab" and repo_cfg.vcs.gitlab.token:
        from integrations.gitlab.gitlab_adapter import GitLabAdapter
        return GitLabAdapter(
            token=repo_cfg.vcs.gitlab.token,
            project_id=repo_cfg.vcs.gitlab.project_id,
            url=repo_cfg.vcs.gitlab.url or "https://gitlab.com",
            on_auth_failure=on_auth_failure,
        )
    return None


def _allowed_links(repo_cfg: RepoConfig, project_cfg: ProjectConfig) -> tuple[str, ...]:
    """The link prefixes a posted review body may use, lowercased as body_problems expects: the
    repo owner's on GitHub, plus the tracker's host (the Jira URL's scheme and host, or Trello's).
    A Jira URL without both adds none: links to it are refused."""
    links = [f"https://github.com/{repo_cfg.vcs.github.owner}/"]
    tracker = project_cfg.tracker
    jira = urlsplit(tracker.jira.url)
    if tracker.provider == "jira" and jira.scheme and jira.hostname:
        links.append(f"{jira.scheme}://{jira.hostname}/")
    elif tracker.provider == "trello":
        links.append("https://trello.com/")
    return tuple(link.lower() for link in links)


def _reviewed_scopes(contexts: list[RepoContext]) -> frozenset[str]:
    """The scope of every repo reviewer mode owns."""
    return frozenset(ctx.scope for ctx in contexts)


def _reviewer_contexts(
    global_config: GlobalConfig,
    projects: dict[str, LoadedProject],
    on_auth_failure: Callable[[str], Callable[[int], None]],
    *,
    dry_run: bool = False,
) -> list[RepoContext]:
    """One reviewer context per loaded repo with reviews enabled (GitHub only, checked at load).

    Each builds its own GitHub adapter, reporting a 401/403 through `on_auth_failure(key)` on the
    repo's `forge:<project>/<repo>` credential, and its poster with the forge token. A repo without
    a forge token is skipped with a warning. A dry run gets none: the reviewer would clone, send
    lines and write reviewer.db.
    """
    if dry_run:
        if any(r.reviewer.enabled for p in projects.values() for r in p.repos.values()):
            print("  Reviewer: off (dry-run)")
        return []

    from orchestrator.reviewer.clone import ReviewClone, github_clone_url
    from orchestrator.reviewer.db import reviewer_db_path
    from orchestrator.reviewer.forge_github import GitHubForgeReader
    from orchestrator.reviewer.poster import CommandPoster
    from orchestrator.reviewer.readiness import CommandReadiness

    clones_dir = reviewer_db_path(global_config.reviewer).parent / "repos"
    contexts: list[RepoContext] = []
    for project_id, project in projects.items():
        for repo_id, repo_cfg in project.repos.items():
            cfg = repo_cfg.reviewer
            if not cfg.enabled:
                continue
            adapter = _build_vcs_adapter(
                repo_cfg, on_auth_failure=on_auth_failure(f"forge:{project_id}/{repo_id}"),
            )
            if adapter is None:
                logger.warning(
                    "Reviews enabled for %s/%s, but it has no forge token; not reviewed",
                    project_id, repo_id,
                )
                continue
            github = repo_cfg.vcs.github
            contexts.append(RepoContext(
                project_id, repo_id, cfg,
                clone=ReviewClone(
                    clones_dir / project_id / repo_id, github_clone_url(github.owner, github.repo),
                    cfg.base_branch, github.token,
                ),
                readiness=CommandReadiness(
                    cfg.readiness, repo=f"{github.owner}/{github.repo}", token=github.token,
                ),
                forge=GitHubForgeReader(adapter),
                poster=CommandPoster(
                    cfg.poster, repo=f"{github.owner}/{github.repo}", token=github.token,
                ),
                allowed_links=_allowed_links(repo_cfg, project.config),
                commit_author=(repo_cfg.git.commit_author_name, repo_cfg.git.commit_author_email),
            ))
    return contexts


def _skill_chat_id(repo_cfg: RepoConfig, global_chat_id: str) -> str:
    """The chat a repo's skill runs write to: the repo's (its project's) Telegram chat, else the
    global one; "" when there is neither."""
    return repo_cfg.telegram.default_chat_id or global_chat_id


def _skill_repos(
    projects: dict[str, LoadedProject],
    vcs_adapters: dict[str, tuple[VCSInterface, RepoConfig]],
    skill_dir: Path,
    *,
    dry_run: bool,
    telegram: bool,
    global_chat_id: str = "",
) -> list[SkillRepo]:
    """One skill repo per loaded repo with ticket_flow both (GitHub only, checked at load), a forge
    adapter and a Telegram chat.

    Its checkout is at skill_dir/repos/<project>/<repo>, with the token its forge adapter was built
    from: the checkout's redact masks only its own token. A repo without a forge token is skipped
    with a warning; one without a chat (_skill_chat_id) with a warning and a line. A dry run gets
    none, and nor does a daemon without Telegram: a run nobody could be told about is never made; a
    line says the skill flow is off when a repo asked for it.
    """
    wanted = [(project_id, repo_id, repo_cfg) for project_id, project in projects.items()
              for repo_id, repo_cfg in project.repos.items() if repo_cfg.ticket_flow == "both"]
    if not wanted:
        return []
    if dry_run:
        print("  Skill flow: off (dry-run)")
        return []
    if not telegram:
        print("  Skill flow: off (no Telegram)")
        return []

    from orchestrator.reviewer.clone import github_clone_url
    from orchestrator.skillflow.checkout import SkillCheckout

    repos: list[SkillRepo] = []
    for project_id, repo_id, repo_cfg in wanted:
        if repo_id not in vcs_adapters:
            logger.warning(
                "Skill flow on for %s/%s, but it has no forge token; not run", project_id, repo_id,
            )
            continue
        adapter, adapter_cfg = vcs_adapters[repo_id]
        # Keyed by repo id alone: the same id in another project keeps that project's adapter, with
        # its token.
        if adapter_cfg is not repo_cfg:
            logger.warning(
                "Skill flow on for %s/%s, but another project has a repo %s too; not run",
                project_id, repo_id, repo_id,
            )
            continue
        if not _skill_chat_id(repo_cfg, global_chat_id):
            print(f"  Skill flow: off for {project_id}/{repo_id} (no Telegram chat)")
            logger.warning(
                "Skill flow on for %s/%s, but it has no Telegram chat; not run",
                project_id, repo_id,
            )
            continue
        github = repo_cfg.vcs.github
        checkout = SkillCheckout(
            skill_dir / "repos" / project_id / repo_id, github_clone_url(github.owner, github.repo),
            repo_cfg.vcs.default_branch, github.token,
        )
        repos.append(SkillRepo(project_id, repo_id, repo_cfg, checkout, adapter))
    return repos


def _reviewer_chat(
    notifier, global_config: GlobalConfig, projects: dict[str, LoadedProject], store=None, translator=None,
) -> ReviewerChat | None:
    """The reviewer chat, else the operator chat; a failed send raises. With `store`, every message it
    sends is a reply anchor in it; with `translator`, the model-written text of its messages is
    translated into the Telegram language.

    None when there is no target (no Telegram, or no chat id): then no line counts as delivered.
    """
    from integrations.telegram.resolve import operator_chat_id

    chat_id = global_config.reviewer.telegram_chat_id or operator_chat_id(global_config, projects)
    if notifier is None or not chat_id:
        return None
    return ReviewerChat(notifier, chat_id, store=store, translator=translator)


def _allowed_chat_ids(global_config: GlobalConfig, projects: dict[str, LoadedProject]) -> set[str]:
    """The chats the command handler takes messages from: telegram.default_chat_id in global.yaml
    and each project's. The reviewer reads replies and taps in these chats only, and in none when
    there is none (the ticket pipeline then takes every chat). The reviewer chat is not added: when
    it is none of these, replies there are ignored, and the startup says so."""
    allowed_chat_ids: set[str] = set()
    if global_config.telegram.default_chat_id:
        allowed_chat_ids.add(global_config.telegram.default_chat_id)
    for proj in projects.values():
        pcid = proj.config.telegram.default_chat_id
        if pcid:
            allowed_chat_ids.add(pcid)
    reviewer_chat_id = global_config.reviewer.telegram_chat_id
    if reviewer_chat_id and reviewer_chat_id not in allowed_chat_ids:
        print("  Reviewer: replies are ignored — reviewer.telegram_chat_id is not an allowed chat")
        logger.warning(
            "Reviewer replies are ignored: reviewer.telegram_chat_id is not an allowed chat",
        )
    return allowed_chat_ids


def _start_background(name: str, coro: Coroutine) -> asyncio.Task:
    """A background task that logs, once, if it ends on an exception: else it would stop silently.

    Logged with the exception's class and raising file:line only: its message can carry
    subprocess stderr, so a token. A cancelled task or a clean return is not logged.
    """
    def report(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("%s stopped unexpectedly: %s", name, _where(task.exception()))

    task = asyncio.create_task(coro)
    task.add_done_callback(report)
    return task


FIRST_ROUND_WAIT_SECONDS = 120.0


async def _when_ready(
    ready: asyncio.Event, stop: asyncio.Event,
    run: Callable[[asyncio.Event], Awaitable[None]], wait: float, *, name: str = "",
    start: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Await start() at once, when given; then run(stop) once `ready` is set or `wait` s have
    passed (a warning naming the part, `name`, says so); no run(stop) if `stop` is set first."""
    if start is not None:
        await start()
    waiters = {asyncio.create_task(ready.wait()), asyncio.create_task(stop.wait())}
    try:
        await asyncio.wait(waiters, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    if stop.is_set():
        return
    if not ready.is_set():
        logger.warning(
            "Credential health: first round not done after %.0f s; starting %s anyway",
            wait, name or "the reviewer",
        )
    await run(stop)


def _start_reviewer(
    reviewer, runner, small, ready: asyncio.Event | None = None, wait: float = FIRST_ROUND_WAIT_SECONDS,
) -> dict[str, tuple[asyncio.Task, asyncio.Event]]:
    """Start the reviewer's poll, its job runner and its edit and draft runner, each with its own
    stop event, for _stop_background. With `ready`, the poll and the two runners' loops start once
    it is set, `wait` s at most; the runners' start() (the jobs a dead process left running, failed)
    runs at once.

    Nothing without a repo to review: then none is built and all are None.
    """
    background = {}
    for name, part in (("Reviewer", reviewer), ("Review jobs", runner), ("Review edits", small)):
        if part is not None:
            stop = asyncio.Event()
            if ready is None:
                coro = part.run(stop)
            elif part is reviewer:   # the poll has nothing to recover: all of it waits
                coro = _when_ready(ready, stop, part.run, wait, name=name)
            else:
                coro = _when_ready(ready, stop, part.loop, wait, name=name, start=part.start)
            background[name] = (_start_background(name, coro), stop)
    return background


def _recover_posts(posting) -> None:
    """Without Telegram, whose replies' start() does this and announces each: the reviewer posts a restart left
    `posting`, marked interrupted and logged. Never raises (cancellation aside)."""
    try:
        posts = posting.recover()
    except Exception as e:
        logger.warning("Reviewer posts not recovered: %s", type(e).__name__)
        return
    for post in posts:
        logger.warning("Reviewer %s on %s#%d interrupted by a restart: marked interrupted",
                       post.kind, post.scope, post.number)


def _start_skill(
    runner, ready: asyncio.Event, wait: float = FIRST_ROUND_WAIT_SECONDS,
) -> dict[str, tuple[asyncio.Task, asyncio.Event]]:
    """Start the skill runner with its own stop event, for _stop_background: its start() (the runs
    a dead process left running, interrupted) at once, its loop once `ready` is set, `wait` s at
    most.

    Nothing without a runner: no repo runs the skill flow.
    """
    if runner is None:
        return {}
    stop = asyncio.Event()
    coro = _when_ready(ready, stop, runner.loop, wait, name="Skill runs", start=runner.start)
    return {"Skill runs": (_start_background("Skill runs", coro), stop)}


async def _stop_background(
    background: dict[str, tuple[asyncio.Task | None, asyncio.Event]], timeout: float,
) -> None:
    """Set every stop event, then wait for all the tasks under one budget; cancel the late ones.

    A None task is skipped. A task that raised never stops the rest: the callback of
    _start_background has logged it.
    """
    names = {task: name for name, (task, _) in background.items() if task is not None}
    for _, stop in background.values():
        stop.set()
    _, pending = await asyncio.wait(set(names), timeout=timeout)
    for task in pending:
        logger.warning("%s did not stop within %gs; cancelled", names[task], timeout)
        task.cancel()
    if pending:
        await asyncio.wait(pending)


def _stderr_level(handler: logging.Handler, environ: Mapping[str, str]) -> None:
    """CLEAVE_STDERR_LEVEL, a level name, as the stderr handler's level: under launchd stderr is a
    file nothing rotates, and the rotating log file has every line. Unset or not a level name:
    unchanged."""
    level = logging.getLevelName(environ.get("CLEAVE_STDERR_LEVEL", "").upper())
    if isinstance(level, int):
        handler.setLevel(level)


def _quiet_http_loggers() -> None:
    """httpx logs full request URLs at INFO; the Telegram Bot API URL carries the token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_version() -> str:
    """Read version from package metadata, falling back to pyproject.toml."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("cleave")
    except PackageNotFoundError:
        pass
    try:
        from pathlib import Path
        import re
        pyproject = Path(__file__).parent / "pyproject.toml"
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text())
        return match.group(1) if match else "unknown"
    except Exception:
        return "unknown"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="cleave",
        description="Cleave — Autonomous AI Development Pipeline",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the configuration directory containing global.yaml and projects/",
    )
    parser.add_argument(
        "--project",
        metavar="ID",
        default=None,
        help="Run only for the specified project (by project id)",
    )
    parser.add_argument(
        "--repo",
        metavar="ID",
        default=None,
        help="Run only for the specified repo (by repo id, requires --project)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Poll tickets and log actions without executing agents or creating workspaces",
    )

    args = parser.parse_args(argv)

    if args.repo and not args.project:
        parser.error("--repo requires --project to be specified")

    return args


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = parse_args(argv)

    version = get_version()
    print(f"Cleave v{version} starting with config: {args.config}")
    if args.project:
        print(f"  Project filter: {args.project}")
    if args.repo:
        print(f"  Repo filter: {args.repo}")
    if args.dry_run:
        print("  Mode: dry-run")

    # Load full config hierarchy
    from config.config_loader import ConfigError, load_config

    try:
        global_config, projects = load_config(
            args.config,
            project_filter=args.project,
            repo_filter=args.repo,
        )
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    print(f"  Config loaded: logging={global_config.logging.level}")
    print(f"  Projects discovered: {len(projects)}")
    for pid, proj in projects.items():
        print(f"    [{pid}] {proj.config.project.name} — {len(proj.repos)} repo(s)")
        for rid, repo in proj.repos.items():
            print(f"      [{rid}] {repo.repo.name}")

    # Discover BMAD-style resources
    from pathlib import Path

    from config.resource_registry import discover_resources, validate_dependencies

    project_root = str(Path(__file__).parent)
    registry = discover_resources(project_root)
    dep_warnings = validate_dependencies(registry)

    summary = registry.summary()
    print(f"  Resources discovered: "
          f"{summary['agents']} agents, "
          f"{summary['tasks']} tasks, "
          f"{summary['templates']} templates, "
          f"{summary['checklists']} checklists, "
          f"{summary['data']} data")

    if dep_warnings:
        for w in dep_warnings:
            print(f"  WARNING: {w}")

    if not projects:
        print("  No active projects configured — dashboard starting so you can create one via the wizard.")

    # Initialize orchestrator components
    import asyncio

    from integrations.llm.claude_adapter import ClaudeAdapter
    from orchestrator.agent_runtime import AgentRuntime
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.workflow_router import load_workflow
    from workspace.workspace_manager import WorkspaceManager

    # Configure logging — stderr (terminal) + rotating file (persistent).
    # Without the file handler, all logger.warning/error/info calls vanish
    # when the terminal running run.sh scrolls or closes, leaving us blind
    # for post-mortem analysis of agent/CLI failures.
    log_level = getattr(logging, global_config.logging.level.upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    stderr_handler = logging.StreamHandler()
    _stderr_level(stderr_handler, os.environ)
    logging.basicConfig(level=log_level, format=log_format, handlers=[stderr_handler])
    _quiet_http_loggers()

    from logging.handlers import RotatingFileHandler

    log_dir = Path(global_config.logging.dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Probe write access — Path.mkdir(exist_ok=True) succeeds even on
        # an existing dir we can't write to.
        (log_dir / ".write-probe").touch()
        (log_dir / ".write-probe").unlink()
    except (PermissionError, OSError):
        log_dir = Path(__file__).parent / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "cleave-daemon.log"
    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter(log_format))
    file_handler.setLevel(log_level)
    logging.getLogger().addHandler(file_handler)
    print(f"  Daemon logs: {log_file}")

    # Initialize event system
    from dashboard.events import EventBus
    from dashboard.event_store import EventStore

    event_bus = EventBus()
    db_path = global_config.dashboard.db_path
    if not Path(db_path).is_absolute():
        db_path = str(Path(__file__).parent / db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    event_store = EventStore(db_path)

    # Load workflow
    workflow_path = str(Path(__file__).parent / "workflows" / "default-workflow.yaml")
    workflow = load_workflow(workflow_path)

    # Initialize LLM adapter (model read fresh from runtime store on every dispatch).
    import sqlite3 as _sqlite3

    def _read_model() -> str:
        with _sqlite3.connect(db_path) as _conn:
            row = _conn.execute(
                "SELECT value FROM settings WHERE key='model'"
            ).fetchone()
        if row is None:
            from dashboard.settings_store import DEFAULT_MODEL
            return DEFAULT_MODEL
        return row[0]

    if global_config.claude.api_key:
        llm = ClaudeAdapter(
            api_key=global_config.claude.api_key,
            default_model_provider=_read_model,
        )
        print("  LLM: Anthropic API adapter")
    else:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        llm = ClaudeCodeAdapter(model_provider=_read_model)
        print("  LLM: Claude Code CLI adapter (using existing auth)")

    # Wire the agent failure summarizer. It uses a dedicated Sonnet 4.6
    # client (configured separately from the pipeline's LLM adapter) so
    # crash summaries are cheap and don't share the agents' model choice.
    from orchestrator.agent_failure_summarizer import configure_summarizer
    import anthropic
    _fs_cfg = global_config.failure_summarizer
    if _fs_cfg.enabled and global_config.claude.api_key:
        configure_summarizer(
            client=anthropic.AsyncAnthropic(api_key=global_config.claude.api_key),
            model=_fs_cfg.model,
            timeout_seconds=_fs_cfg.timeout_seconds,
            enabled=True,
        )
        print(f"  Failure summarizer: enabled (model={_fs_cfg.model})")
    else:
        configure_summarizer(client=None, model="", timeout_seconds=30, enabled=False)
        print("  Failure summarizer: disabled")

    # Cap the JVM heap the build toolchain may request, so concurrent/oversized
    # Android builds don't OOM the host (Kotlin daemon getting killed mid-compile).
    if global_config.defaults.cap_build_memory:
        from orchestrator.build_env import apply_build_memory_limits, explicit_gradle_user_home
        gradle_user_home = explicit_gradle_user_home(os.environ)
        if gradle_user_home is None:
            print("  Build memory caps: skipped (GRADLE_USER_HOME not set; ~/.gradle is shared, left alone)")
        else:
            try:
                props = apply_build_memory_limits(
                    gradle_user_home,
                    gradle_heap_mb=global_config.defaults.build_gradle_heap_mb,
                    kotlin_heap_mb=global_config.defaults.build_kotlin_heap_mb,
                )
                print(
                    f"  Build memory caps applied: {props} "
                    f"(gradle={global_config.defaults.build_gradle_heap_mb}MB, "
                    f"kotlin={global_config.defaults.build_kotlin_heap_mb}MB)"
                )
            except OSError as e:
                print(f"  WARNING: could not write build memory caps: {e}")

    # Initialize workspace manager
    workspace_manager = WorkspaceManager(base_dir=global_config.workspaces.base_dir)

    # Build operator profile string
    op = global_config.operator
    operator_profile = ""
    if op.role:
        operator_profile = f"Role: {op.role}\n"
        if op.stack:
            operator_profile += f"Stack: {', '.join(op.stack)}\n"
        if op.rules:
            operator_profile += "Rules:\n" + "\n".join(f"- {r}" for r in op.rules) + "\n"

    # Initialize agent runtime — propagate the configured per-agent budgets so
    # runaway loops trip on rounds, wall-clock, or total tokens (whichever
    # comes first) instead of silently chewing quota.
    agent_runtime = AgentRuntime(
        registry,
        llm,
        operator_profile=operator_profile,
        event_bus=event_bus,
        default_budget=global_config.defaults.agent_budget,
        budget_overrides=global_config.defaults.agent_budget_overrides,
    )

    # Initialize integration adapters
    vcs = None
    notifier = None

    # Credential health — built before the adapters so they can report a 401/403 to it.
    import shutil

    from health.credentials import CredentialHealth, build_credentials
    from integrations.telegram.resolve import daemon_bot_token, operator_chat_id

    claude_bin = shutil.which("claude") or "claude"

    async def _credential_line(text: str) -> None:
        """Send a credential-health line to the operator chat; nothing without Telegram."""
        if notifier is None:
            return
        chat_id = operator_chat_id(global_config, projects)
        if chat_id:
            await notifier.send_message(chat_id, text)

    from orchestrator.tg_i18n import chat_language, project_language

    def _language_of(project_id: str) -> str:
        """The Telegram language of a project ("": global.yaml's), read from the live config: the
        dashboard's Settings change it without a restart."""
        return project_language(global_config, projects, project_id)

    credential_health = CredentialHealth(
        lambda: build_credentials(global_config, projects, claude_bin),
        send_line=_credential_line,
        language=lambda: _language_of(""),
    )

    def _auth_reporter(key: str) -> Callable[[int], None]:
        """The adapter callback that reports a 401/403 on credential `key`."""
        return lambda status: credential_health.report_auth_failure(key, status)


    # Build a tracker per project. Provider is dispatched on tracker.provider.
    from integrations.base.tracker import TrackerInterface
    trackers: dict[str, TrackerInterface] = {}
    for project_id, project in projects.items():
        tracker = _build_tracker_for_project(
            project.config.tracker, project_id,
            on_auth_failure=_auth_reporter(f"tracker:{project_id}"),
        )
        if tracker is not None:
            trackers[project_id] = tracker
            print(f"  Tracker for {project_id}: {project.config.tracker.provider}")

    # VCS adapter — per-repo; first non-None adapter becomes the daemon
    # default (used as fallback when a workspace has no repo-scoped adapter).
    vcs_adapters: dict[str, tuple[VCSInterface, RepoConfig]] = {}
    for proj_id, proj in projects.items():
        for repo_id, repo_cfg in proj.repos.items():
            adapter = _build_vcs_adapter(
                repo_cfg, on_auth_failure=_auth_reporter(f"forge:{proj_id}/{repo_id}"),
            )
            if adapter is None:
                continue
            vcs_adapters[repo_id] = (adapter, repo_cfg)
            if vcs is None:
                vcs = adapter
            print(f"  {repo_cfg.vcs.provider} adapter for {repo_id}")

    # Telegram notifier — prefer the global token, else fall back to the first
    # project that configured one. The dashboard wizard writes Telegram creds
    # per-project, so global is usually empty; without this fallback the
    # notifier stays None and every escalation dead-ends at FAILED with
    # "No notifier configured for escalation".
    tg_token, _ = daemon_bot_token(global_config, projects)
    translator = None
    if tg_token:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from integrations.telegram.telegram_adapter import TelegramAdapter
        from orchestrator.reviewer.db import reviewer_db_path
        from orchestrator.tg_translate import Translator

        notifier = TelegramAdapter(bot_token=tg_token, event_bus=event_bus)
        # Model-written text in a message for a project whose telegram.language is not English: one
        # light, tool-less `claude -p` turn per message, on the claude.ai login like the reviewer's.
        translator = Translator(
            ClaudeCodeAdapter(), work_dir=reviewer_db_path(global_config.reviewer).parent / "translate",
        )
        notifier.set_translator(translator)
        print("  Telegram adapter initialized")

    def _build_repo_adapters(project_id, project, logger_):
        """Build + register VCS adapters for each repo in a single project."""
        for repo_id, repo_cfg in project.repos.items():
            adapter = _build_vcs_adapter(
                repo_cfg, on_auth_failure=_auth_reporter(f"forge:{project_id}/{repo_id}"),
            )
            if adapter is None:
                continue
            orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)
            logger_.info(
                "Hot-reload: registered %s adapter for %s",
                repo_cfg.vcs.provider, repo_id,
            )

    def on_project_added(project_id, new_project):
        log = logging.getLogger(__name__)
        _build_repo_adapters(project_id, new_project, log)

        if orchestrator.get_tracker_for_project(project_id) is None:
            new_tracker = _build_tracker_for_project(
                new_project.config.tracker, project_id,
                on_auth_failure=_auth_reporter(f"tracker:{project_id}"),
            )
            if new_tracker is not None:
                orchestrator.register_tracker(project_id, new_tracker)
                log.info(
                    "Tracker (%s) registered for project %s",
                    new_project.config.tracker.provider, project_id,
                )

        if command_handler is not None:
            pcid = new_project.config.telegram.default_chat_id
            if pcid:
                command_handler.add_allowed_chat_id(pcid)
            command_handler.set_trackers_resolver(lambda: dict(orchestrator._trackers))

        for rid, repo in new_project.repos.items():
            event_bus.emit(
                "project_loaded",
                f"Project {project_id}/{rid}: {repo.repo.name}",
                project_id=project_id,
                data={"repo_id": rid, "repo_name": repo.repo.name},
            )

        log.info("Project %s added and live (no restart required)", project_id)

    # Initialize orchestrator
    orchestrator = Orchestrator(
        global_config=global_config,
        projects=projects,
        registry=registry,
        workflow=workflow,
        workspace_manager=workspace_manager,
        agent_runtime=agent_runtime,
        default_model_provider=_read_model,
        trackers=trackers,
        vcs=vcs,
        notifier=notifier,
        dry_run=args.dry_run,
        event_bus=event_bus,
        config_dir=args.config,
        on_project_added=on_project_added,
    )

    # Register per-repo VCS adapters
    for repo_id, (adapter, repo_cfg) in vcs_adapters.items():
        orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)

    # Initialize mode handler (auto/manual pipeline mode)
    from integrations.telegram.handlers.mode import ModeHandler

    daemon_state_path = str(Path(args.config) / "daemon_state.json")
    mode_handler = ModeHandler(
        state_file_path=daemon_state_path,
        default_mode=global_config.pipeline.mode,
    )
    orchestrator.set_mode_handler(mode_handler)
    print(f"  Mode handler initialized (mode: {mode_handler.get_mode()})")

    # Running/paused switches (tickets, reviews), persisted in reviewer.db
    from orchestrator.reviewer.db import connect, reviewer_db_path
    from orchestrator.switches import Switches

    switches = Switches(connect(reviewer_db_path(global_config.reviewer)))
    orchestrator.set_switches(switches)

    def _on_switch_changed(mode: str, scope: str, state: str, by: str) -> None:
        event_bus.emit(
            "switch_changed", f"Switch {mode} {scope}: {state} (by {by})",
            data={"mode": mode, "scope": scope, "state": state, "by": by},
        )
        if mode == "tickets" and state == "running":
            orchestrator._wake_event.set()

    switches.add_listener(_on_switch_changed)
    print(f"  Switches: tickets={switches.get('tickets')}, reviews={switches.get('reviews')}")

    # The reviewer's and the skill flow's model sessions run on the claude.ai login (the CLI), never
    # the API key; they may read neither the repo's .env nor the config directory.
    secret_paths = [Path(__file__).parent / ".env", Path(args.config).absolute()]

    # Reviewer: polls each repo with reviewer.enabled, runs its review jobs one at a time and its
    # edit and draft jobs one at a time; the replies to its messages are wired with the command
    # handler. Repos added by hot reload wait for a restart.
    from orchestrator.reviewer.service import ReviewerService
    from orchestrator.reviewer.store import ReviewerStore

    reviewer_data = reviewer_db_path(global_config.reviewer).parent
    # The switches' database: with Telegram on, a reply to a reviewer message is answered from it
    # even with no repo to review.
    reviewer_store = ReviewerStore(connect(reviewer_db_path(global_config.reviewer)))
    reviewer_chat = _reviewer_chat(
        notifier, global_config, projects, store=reviewer_store, translator=translator,
    )
    # Build the chat-id allowlist from global + per-project configs so
    # the bot ignores commands from other chats. An empty set disables
    # the bot (fail-safe); None would disable the check entirely. The
    # reviewer reads replies in these chats only.
    allowed_chat_ids = _allowed_chat_ids(global_config, projects) if notifier is not None else set()
    reviewer = None
    review_jobs = None
    review_edits = None
    reply_parser = None
    reviewer_contexts = _reviewer_contexts(
        global_config, projects, _auth_reporter, dry_run=args.dry_run,
    )
    orchestrator.set_reviewed_repos(_reviewed_scopes(reviewer_contexts))
    if reviewer_contexts:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from orchestrator.reviewer.reply_llm import ReplyParser
        from orchestrator.reviewer.runner import JobRunner, RunnerConfig
        from orchestrator.reviewer.small_jobs import SmallJobRunner, SmallRunnerConfig

        def _pipeline_prs(project_id: str, repo_id: str) -> set[int]:
            """The PRs the ticket pipeline has open for this repo."""
            return {
                ws.state.pr_number for ws in orchestrator.get_active_workspaces()
                if ws.state.company_id == project_id and ws.state.repo_id == repo_id
                and ws.state.pr_number is not None
            }

        reviewer = ReviewerService(
            reviewer_contexts, reviewer_store, switches, credential_health.failing,
            reviewer_chat.send if reviewer_chat is not None else None, _pipeline_prs,
            event_bus=event_bus, language_of=_language_of,
            # Read on each poll: the comparison store is attached to the orchestrator further down.
            compared_tickets=orchestrator.compared_tickets,
        )
        jobs = global_config.reviewer.jobs
        review_jobs = JobRunner(
            reviewer_contexts, reviewer_store, switches,
            credential_health.failing, credential_health.report_auth_failure,
            reviewer_chat, ClaudeCodeAdapter(), trackers,
            RunnerConfig(
                reviewer_data / "jobs", secret_paths, jobs, ledgers_dir=reviewer_data / "ledgers",
                work_dir=reviewer_data / "work", max_parallel=global_config.reviewer.max_parallel_jobs,
                min_free_bytes=global_config.workspaces.min_free_disk_gb * 1024**3,
            ),
            event_bus=event_bus, language_of=_language_of,
        )
        # Edit and draft sessions read the review job's copy only; nothing is posted.
        review_edits = SmallJobRunner(
            reviewer_store, reviewer_chat, ClaudeCodeAdapter(),
            SmallRunnerConfig(reviewer_data / "jobs", secret_paths, jobs), event_bus=event_bus,
            language_of=_language_of,
        )
        # A reply the grammar does not know: the model's command runs only after a Yes.
        reply_parser = ReplyParser(
            ClaudeCodeAdapter(), jobs["command"], work_dir=reviewer_data / "command",
        )
        # Posting needs a reply read in the chat the reviewer's messages go to.
        replies_read = reviewer_chat is not None and reviewer_chat.chat_id in allowed_chat_ids
        from_telegram = ", posting from Telegram" if replies_read else ""
        print(f"  Reviewer: {len(reviewer_contexts)} repo(s), jobs on{from_telegram}")

    # Skill flow: with ticket_flow both, each new workspace's ticket also gets a skill run, worked
    # one at a time in the pipeline's build slot; the replies to its messages are wired with the
    # command handler. Repos added by hot reload wait for a restart.
    skill_dir = reviewer_data / "skill"
    global_chat_id = global_config.telegram.default_chat_id
    skill_repos = _skill_repos(
        projects, vcs_adapters, skill_dir, dry_run=args.dry_run, telegram=notifier is not None,
        global_chat_id=global_chat_id,
    )
    skill_runner = None
    skill_replies = None
    comparison_keeper = None
    comparison_board = None
    from orchestrator.skillflow.chat import SkillChat
    from orchestrator.skillflow.store import SkillStore

    def _scope_language(scope: str) -> str:
        """The Telegram language of a scope ("<project>/<repo>"): its project's."""
        return _language_of(scope.split("/", 1)[0])

    if skill_repos:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from orchestrator.skillflow.board import ComparisonBoard
        from orchestrator.skillflow.compare_store import ComparisonStore
        from orchestrator.skillflow.intake import SkillIntake
        from orchestrator.skillflow.keeper import ComparisonKeeper
        from orchestrator.skillflow.replies import SkillReplies
        from orchestrator.skillflow.runner import SkillRunner
        from orchestrator.skillflow.sides import Workspaces

        # One store and one chat for the runner, the intake and the replies: the compare-and-set
        # that keeps a reply and the runner apart is a lock inside the one store object. Likewise
        # one comparison store for the intake, the keeper, the replies and the board. One connection
        # for the skill runs and the comparisons: the skill store's and the comparison store's locks
        # each guard only their own writers.
        skill_conn = connect(reviewer_db_path(global_config.reviewer))
        skill_store = SkillStore(skill_conn)
        comparisons = ComparisonStore(skill_conn)
        skill_chat = SkillChat(notifier, skill_store, translator=translator)
        skill_runs = skill_dir / "runs"
        skill_runner = SkillRunner(
            skill_repos, skill_store, skill_chat, ClaudeCodeAdapter(),
            slot=orchestrator.build_slot, switches=switches,
            credential_failing=credential_health.failing, get_mode=mode_handler.get_mode,
            runs_dir=skill_runs, secret_paths=secret_paths, event_bus=event_bus,
            language_of=_scope_language,
        )
        # Only here: on a daemon that serves no repo on both, no workspace is compared.
        orchestrator.set_comparisons(comparisons)
        workspaces = Workspaces(orchestrator.get_active_workspaces)
        comparison_keeper = ComparisonKeeper(
            skill_repos, comparisons, skill_store, skill_chat, workspaces,
            tracker_for=orchestrator.get_tracker_for_project, language_of=_scope_language,
            event_bus=event_bus,
        )
        comparison_board = ComparisonBoard(comparisons, skill_store, workspaces)

        # Runs only for the repos the runner serves: a repo skipped above, or one added by hot
        # reload, would get runs no runner takes.
        served = frozenset(repo.scope for repo in skill_repos)
        orchestrator.set_skill_intake(SkillIntake(
            skill_store, skill_runs, lambda repo_cfg: _skill_chat_id(repo_cfg, global_chat_id),
            wake=skill_runner.wake, serves=served.__contains__, comparisons=comparisons,
        ))
        skill_replies = SkillReplies(
            skill_store, skill_chat, skill_runner.wake, comparisons=comparisons,
            wake_keeper=comparison_keeper.wake,
            # An answer about no known run or comparison is in the chat's language.
            language_of=lambda scope, chat_id: (
                _scope_language(scope) if scope else chat_language(global_config, projects, chat_id)
            ),
        )
        print(f"  Skill flow: {len(skill_repos)} repo(s)")
    elif notifier is not None:
        from orchestrator.skillflow.replies import SkillOff

        # A reply to a message an earlier daemon's skill runs or comparisons sent is answered
        # "the skill flow is off" rather than read as a command.
        off_store = SkillStore(connect(reviewer_db_path(global_config.reviewer)))
        if off_store.has_anchors():
            skill_replies = SkillOff(
                off_store, SkillChat(notifier, off_store),
                language_of=lambda chat_id: chat_language(global_config, projects, chat_id),
            )

    # The reviewer's commands, checked and run alike for a reply in Telegram and a click in the
    # dashboard: both share this one Posting, so one post or push runs at a time.
    from orchestrator.reviewer.actions import ReviewerActions
    from orchestrator.reviewer.posting import Posting

    reviewer_actions = ReviewerActions(
        reviewer_contexts, reviewer_store, reviewer,
        Posting(reviewer_store, ledgers_dir=reviewer_data / "ledgers", work_dir=reviewer_data / "work"),
        review_edits, reply_parser, credential_failing=credential_health.failing,
    )

    # Initialize command handler for Telegram free-text control
    command_handler = None
    reviewer_replies = None
    if notifier is not None:
        from datetime import datetime, timezone

        from integrations.telegram.command_handler import CommandHandler
        from integrations.telegram.intent_parser import IntentParser
        from integrations.telegram.telegram_adapter import TelegramAdapter
        from orchestrator.reviewer.replies import ReviewerReplies

        if isinstance(notifier, TelegramAdapter):
            # The replies to reviewer messages: the only way anything is posted. With no repo to
            # review they answer that reviewer mode is off.
            reviewer_replies = ReviewerReplies(
                reviewer_contexts, reviewer_store, reviewer, reviewer_chat, reviewer_actions.posting,
                review_edits, reply_parser, credential_failing=credential_health.failing,
                language_of=_language_of, actions=reviewer_actions,
            )
            intent_parser = IntentParser(
                llm_adapter=llm,
                intent_parser_config=global_config.intent_parser,
            )

            jira_base_url = ""
            for proj in projects.values():
                cfg = proj.config.tracker
                if cfg.provider == "jira" and cfg.jira.url:
                    jira_base_url = cfg.jira.url
                    break
            # Trello: card-specific shortUrl; no daemon-level base needed.

            command_handler = CommandHandler(
                intent_parser=intent_parser,
                notifier=notifier,
                mode_handler=mode_handler,
                active_workspaces_fn=orchestrator.get_active_workspaces,
                jira_base_url=jira_base_url,
                started_at=datetime.now(timezone.utc).isoformat(),
                get_trackers=lambda: dict(orchestrator._trackers),
                analyze_callback=orchestrator.analyze_ticket_ids,
                recent_completions_fn=orchestrator.get_recent_completions,
                # An empty allowlist locks the bot: no chat is admitted (docs/telegram.md, Allowlist).
                allowed_chat_ids=allowed_chat_ids,
                event_bus=event_bus,
                switches=switches,
                get_projects=lambda: projects,
                language_of=_language_of,
                chat_language=lambda chat_id: chat_language(global_config, projects, chat_id),
                reviewer=reviewer_replies,
                # Never None: with an empty allowlist the reviewer reads no chat at all.
                reviewer_chat_ids=allowed_chat_ids,
                skill=skill_replies,
            )
            command_handler._wake_fn = lambda: orchestrator._wake_event.set()
            orchestrator.set_poll_listener(command_handler.update_last_poll_time)
            notifier.set_command_handler(command_handler)
            print(
                f"  Telegram CommandHandler wired (allowlist: {len(allowed_chat_ids)} chat id(s))"
            )

    print("  Orchestrator initialized. Starting main loop...")

    # Run the main loop. Telegram polling runs via PTB's own background tasks
    # (start_polling returns after initialization), then the orchestrator loop
    # blocks until SIGINT/SIGTERM. On exit we stop the Telegram side cleanly.
    async def _run_all() -> None:
        from integrations.telegram.telegram_adapter import TelegramAdapter

        # Initialize persistent event store
        await event_store.initialize()
        event_bus.add_listener(lambda e: asyncio.ensure_future(event_store.insert(e)))

        # Credential health checks run in the background, with or without the dashboard.
        credential_stop = asyncio.Event()
        credential_task = _start_background(
            "Credential health", credential_health.run(credential_stop),
        )
        # No poll and no job before the first credential round: until then the forge and login
        # states are unknown.
        reviewer_tasks = _start_reviewer(
            reviewer, review_jobs, review_edits, ready=credential_health.first_round,
        )
        skill_tasks = _start_skill(skill_runner, credential_health.first_round)
        if comparison_keeper is not None:
            # No first-round wait: the keeper calls no CLI.
            keeper_stop = asyncio.Event()
            skill_tasks["Comparisons"] = (
                _start_background("Comparisons", comparison_keeper.loop(keeper_stop)), keeper_stop,
            )

        # Init runtime settings table (model picker, Telegram quiet hours).
        import aiosqlite as _aiosqlite
        from dashboard.settings_store import get_quiet_hours, init_settings
        async with _aiosqlite.connect(db_path) as _conn:
            await init_settings(_conn)
            # One object for the bot and the dashboard: a save in Settings applies to the next message.
            quiet_hours = await get_quiet_hours(_conn)
        if isinstance(notifier, TelegramAdapter):
            notifier.quiet_hours = quiet_hours

        if not isinstance(notifier, TelegramAdapter) and not args.dry_run:
            # The dashboard posts without Telegram too: a post a restart cut short is marked interrupted before it
            # serves (with Telegram, the replies' start() does it). A dry run writes nothing.
            _recover_posts(reviewer_actions.posting)

        # Start dashboard web server
        dash_config = global_config.dashboard
        web_server = None
        web_server_task: asyncio.Task | None = None
        if dash_config.enabled:
            from dashboard.web import create_app
            import uvicorn

            async def _production_atlas_fn(setup_ws, cfg_dir):
                """Run project-setup-agent against a dashboard-created setup workspace.

                Wraps the SetupWorkspace in a Workspace shim so AgentRuntime's
                prompt assembly and tool sandbox can reuse the ticket-agent path.
                """
                from workspace.workspace import Workspace, WorkspaceState

                state = WorkspaceState(
                    ticket_id=setup_ws.project_id,
                    company_id=setup_ws.project_id,
                    repo_id=setup_ws.repo_id,
                    workspace_root=str(setup_ws.setup_dir),
                )
                ws = Workspace(str(setup_ws.setup_dir), state=state)
                ws.source_dir.mkdir(parents=True, exist_ok=True)

                result = await agent_runtime.execute(
                    agent_id="project-setup-agent",
                    workspace=ws,
                    extra_context={"config_dir": str(Path(cfg_dir).resolve())},
                )
                if not result.success:
                    raise RuntimeError(
                        f"project-setup-agent failed: {result.error or 'unknown error'}"
                    )
                # Agent exited 0 but may have reported failure in its output
                output_lower = (result.output or "").lower()
                if "status: fail" in output_lower or "status:fail" in output_lower:
                    report_path = setup_ws.setup_dir / "reports" / "project-setup-agent-output.md"
                    report = report_path.read_text(encoding="utf-8") if report_path.exists() else result.output or ""
                    raise RuntimeError(
                        f"project-setup-agent reported failure:\n{report}"
                    )

                # The agent writes config files into source/ (sandbox boundary).
                # Copy them to the real config-live directory.
                import shutil
                src = ws.source_dir
                dest = Path(cfg_dir).resolve() / "projects" / setup_ws.project_id
                dest.mkdir(parents=True, exist_ok=True)
                # project.yaml — agent may write it at root or under a nested path
                for candidate in [
                    src / "project.yaml",
                    src / "config-live" / "projects" / setup_ws.project_id / "project.yaml",
                ]:
                    if candidate.exists():
                        shutil.copy2(candidate, dest / "project.yaml")
                        break
                # repos/*.yaml
                repos_dest = dest / "repos"
                repos_dest.mkdir(parents=True, exist_ok=True)
                for repos_src in [
                    src / "repos",
                    src / "config-live" / "projects" / setup_ws.project_id / "repos",
                ]:
                    if repos_src.is_dir():
                        for f in repos_src.glob("*.yaml"):
                            shutil.copy2(f, repos_dest / f.name)
                        break

                if not (dest / "project.yaml").exists():
                    raise RuntimeError(
                        f"Agent did not produce project.yaml (checked source/ and source/config-live/). "
                        f"Files in source/: {[p.name for p in src.iterdir()]}"
                    )

            ticket_announcer = None
            if notifier is not None:
                from orchestrator.ticket_announce import TicketAnnouncer
                ticket_announcer = TicketAnnouncer(notifier, orchestrator._get_chat_id, _language_of)

            app = create_app(
                event_bus, event_store,
                workspace_base_dir=global_config.workspaces.base_dir,
                orchestrator=orchestrator,
                mode_handler=mode_handler,
                global_config=global_config,
                projects=projects,
                config_dir=args.config,
                atlas_fn=_production_atlas_fn,
                switches=switches,
                credential_health=credential_health,
                reviewer=reviewer,
                comparisons=comparison_board,
                allowed_networks=global_config.dashboard.allowed_networks,
                allowed_hosts=global_config.dashboard.allowed_hosts,
                wake=lambda: orchestrator._wake_event.set(),
                ticket_announcer=ticket_announcer,
                # The layer the replies run their commands through: one Posting for both channels.
                reviewer_actions=reviewer_actions,
                reviewer_replies=reviewer_replies,
                quiet_hours=quiet_hours,
            )
            config = uvicorn.Config(
                app, host=dash_config.host, port=dash_config.port,
                log_level="warning",
            )
            web_server = uvicorn.Server(config)
            web_server_task = asyncio.create_task(web_server.serve())
            print(f"  Dashboard: http://{dash_config.host}:{dash_config.port}")

        event_bus.emit("daemon_started", f"Cleave v{version} started")

        # Emit events for configured projects so they appear in the dashboard
        for pid, proj in projects.items():
            for rid, repo in proj.repos.items():
                event_bus.emit(
                    "project_loaded",
                    f"Project {pid}/{rid}: {repo.repo.name}",
                    project_id=pid,
                    data={"repo_id": rid, "repo_name": repo.repo.name},
                )

        # Emit events for existing workspaces discovered on disk
        for ws in orchestrator.get_active_workspaces():
            event_bus.emit(
                "workspace_resumed",
                f"Resumed {ws.state.ticket_id} in state {ws.state.current_state}",
                project_id=ws.state.company_id,
                ticket_id=ws.state.ticket_id,
                data={"state": ws.state.current_state, "repo_id": ws.state.repo_id},
            )

        tg_active = isinstance(notifier, TelegramAdapter)
        if tg_active:
            # Before any reply can arrive: the posts a restart cut short are marked interrupted and
            # announced (the replies are built with the command handler, whenever Telegram is on).
            # Not in a dry run, which writes nothing and sends no line.
            if not args.dry_run:
                await reviewer_replies.start()
            await notifier.start_polling()
            print("  Telegram polling started")
        try:
            await orchestrator.run()
        finally:
            # First, while Telegram can still deliver a line from a check in flight. A review job,
            # or a skill run's turn or gate, still running is cancelled: its process group is
            # killed.
            await _stop_background({
                "Credential health": (credential_task, credential_stop),
                **reviewer_tasks,
                **skill_tasks,
            }, timeout=5.0)
            if tg_active:
                # A post cut short here stays `posting`: the next start announces it.
                await reviewer_replies.aclose()
                try:
                    await notifier.stop_polling()
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Error stopping Telegram polling: %s", e,
                    )
            if web_server:
                web_server.should_exit = True
            if web_server_task is not None:
                try:
                    await asyncio.wait_for(web_server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    logging.getLogger(__name__).warning(
                        "Dashboard server did not shut down within 5s; cancelling",
                    )
                    web_server_task.cancel()
                    try:
                        await web_server_task
                    except (asyncio.CancelledError, Exception):
                        pass
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Error during dashboard shutdown: %s", e,
                    )
            await event_store.close()

    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        print("\nShutdown requested.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

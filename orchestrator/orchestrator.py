"""Orchestrator — main daemon loop managing workspaces and agent dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from config.config_loader import ConfigError, load_config
from config.schemas import GlobalConfig, LoadedProject, RepoConfig
from config.resource_registry import ResourceRegistry
from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TrackerInterface
from integrations.base.vcs import VCSInterface
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.pipeline.actions.fetch_pr_comments import action_fetch_pr_comments
from orchestrator.pipeline.actions.finalize import action_finalize
from orchestrator.pipeline.actions.push_and_open_pr import action_push_and_open_pr
from orchestrator.runtime import Runtime
from orchestrator.ticket_prioritizer import PrioritizedTicket
from orchestrator.workflow_router import WorkflowDefinition
from workspace.workspace import Stage, Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class _RuntimeAttr:
    """Descriptor that forwards `_X` access to `self._runtime._X` when the
    runtime is wired up. Falls back to `__dict__` for tests that build
    Orchestrator via `__new__` and assign attributes directly.
    """

    def __init__(self, name: str, default_factory=None):
        self._name = name
        self._default = default_factory

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        runtime = instance.__dict__.get("_runtime")
        if runtime is not None:
            return getattr(runtime, self._name)
        if self._default is not None:
            return instance.__dict__.setdefault(self._name, self._default())
        return instance.__dict__[self._name]

    def __set__(self, instance, value):
        runtime = instance.__dict__.get("_runtime")
        if runtime is not None:
            setattr(runtime, self._name, value)
        else:
            instance.__dict__[self._name] = value


class Orchestrator:
    """Main daemon loop — poll for tickets, manage slots, advance workspaces."""

    # Approval gate stages in manual mode, mapped to the next stage that
    # represents the "happy path" past the gate. Gating should ONLY fire when
    # the workflow would move forward past the gate — not on failure loops
    # (QA fail → dev) or escalation (analysis unclear → escalate).
    # Source of truth lives in orchestrator.approval_gate; mirrored here as
    # class attributes for backward compat with any callers/tests that reach
    # in via Orchestrator._APPROVAL_GATE_STATES.
    _APPROVAL_GATE_STATES = {Stage.ANALYSIS, Stage.QA}
    _GATE_HAPPY_PATH_NEXT_STAGE = {
        Stage.ANALYSIS: "dev",
        Stage.QA: "push",
    }

    def __init__(
        self,
        global_config: GlobalConfig,
        projects: dict[str, LoadedProject],
        registry: ResourceRegistry,
        workflow: WorkflowDefinition,
        workspace_manager: WorkspaceManager,
        agent_runtime: AgentRuntime,
        default_model_provider: Callable[[], str],
        tracker: TrackerInterface | None = None,
        vcs: VCSInterface | None = None,
        notifier: NotifierInterface | None = None,
        dry_run: bool = False,
        event_bus: Any | None = None,
        config_dir: str | None = None,
        on_project_added: Callable[[str, LoadedProject], None] | None = None,
    ) -> None:
        self._global_config = global_config
        self._projects = projects
        self._registry = registry
        self._workflow = workflow
        self._workspace_manager = workspace_manager
        self._agent_runtime = agent_runtime
        self._default_model_provider = default_model_provider
        self._tracker = tracker
        self._vcs = vcs
        self._notifier = notifier
        self._dry_run = dry_run
        self._events = event_bus
        # Map repo_id -> (VCSInterface, RepoConfig) for per-repo VCS
        self._repo_vcs: dict[str, tuple[VCSInterface, RepoConfig]] = {}
        # Mode handler — initialized later via set_mode_handler or from config default
        self._mode_handler: ModeHandler | None = None
        # In-memory debounce for Claude CLI quota notifications.
        # Stores the retry_at of the first notification in the current window;
        # further quota hits while now < _quota_window_end are silenced.
        self._quota_window_end: datetime | None = None
        self._config_dir = config_dir
        self._on_project_added = on_project_added
        # Runtime owns the long-running daemon state (active workspaces,
        # recent completions, semaphore, shutdown/wake events). Orchestrator
        # delegates lifecycle methods to it via properties / shims below.
        # Callbacks resolve through self.* on each call so tests can patch
        # the bound methods (e.g. orch._rescan_projects_from_disk = AsyncMock())
        # after construction.
        self._runtime = Runtime(
            global_config=global_config,
            workspace_manager=workspace_manager,
            poll_callback=lambda: self._poll_and_create_workspaces(),
            advance_callback=lambda ws: self.advance_workspace(ws),
            rescan_callback=lambda: self._rescan_projects_from_disk(),
            sweep_quota_window_callback=self._sweep_quota_window,
            get_tracker=lambda: self._tracker,
            get_mode_handler=lambda: self._mode_handler,
            event_bus=event_bus,
            dry_run=dry_run,
        )

    def register_repo_vcs(
        self, repo_id: str, vcs: VCSInterface, repo_config: RepoConfig,
    ) -> None:
        """Register a VCS adapter for a specific repo."""
        self._repo_vcs[repo_id] = (vcs, repo_config)

    def set_tracker(self, tracker: TrackerInterface) -> None:
        """Attach a tracker after startup (used by wizard hot-reload)."""
        self._tracker = tracker

    async def rescan_projects(self) -> list[str]:
        """Re-read config from disk; add new projects; invoke hook for each.

        Returns the list of newly-added project ids. Public entry point called
        from the wizard route for instant kick.
        """
        added = await self._rescan_projects_from_disk()
        # Force an immediate ticket poll for newly added projects so the user
        # sees tickets right away, even in manual mode.
        if added and self._tracker:
            await self._poll_and_create_workspaces()
        return added

    async def _rescan_projects_from_disk(self) -> list[str]:
        """Internal: re-read config and merge new projects into _projects.

        Does NOT touch already-loaded projects (hot-reload of edits is out of
        scope). Swallows ConfigError (e.g., mid-edit YAML) and logs at WARNING.
        """
        if not self._config_dir:
            return []
        try:
            _, loaded = load_config(self._config_dir)
        except ConfigError as exc:
            logger.warning("Rescan: load_config failed: %s", exc)
            return []
        except Exception:
            logger.exception("Rescan: unexpected error reading %s", self._config_dir)
            return []

        added: list[str] = []
        for pid, proj in loaded.items():
            if pid in self._projects:
                continue
            self._projects[pid] = proj
            added.append(pid)
            if self._on_project_added is not None:
                try:
                    self._on_project_added(pid, proj)
                except Exception:
                    logger.exception("on_project_added hook failed for %s", pid)
        if added:
            logger.info("Rescan added projects: %s", added)
            self._runtime.wake()
        return added

    def set_mode_handler(self, handler: ModeHandler) -> None:
        """Register the mode handler for auto/manual switching."""
        self._mode_handler = handler

    def get_active_workspaces(self) -> list[Workspace]:
        """Return the current active workspace list (for CommandHandler status)."""
        return list(self._runtime.active_workspaces)

    def get_recent_completions(self) -> list[tuple[str, str, float]]:
        """Return recently-terminated workspaces (ticket_id, final_state, epoch).

        Used by /status to show DONE / FAILED tickets after they've been
        removed from the active list.
        """
        return self._runtime.recent_completions

    # Runtime-state attribute forwarders. Each delegates to the Runtime
    # instance when one is wired up; tests that build Orchestrator via
    # __new__ get fallback storage in __dict__.
    _active_workspaces = _RuntimeAttr("_active_workspaces", default_factory=list)
    _recent_completions = _RuntimeAttr(
        "_recent_completions", default_factory=lambda: deque(maxlen=20),
    )
    _shutdown_event = _RuntimeAttr("_shutdown_event")
    _wake_event = _RuntimeAttr("_wake_event")
    _agent_semaphore = _RuntimeAttr("_agent_semaphore")

    async def analyze_ticket_ids(self, ticket_ids: list[str]) -> dict[str, list[str]]:
        """Manually queue tickets for analysis (Telegram /analyze callback).

        Thin shim — see orchestrator/ingest.py for the implementation.
        """
        from orchestrator.ingest import analyze_ticket_ids as _impl
        return await _impl(
            ticket_ids,
            tracker=getattr(self, "_tracker", None),
            projects=self._projects,
            active_workspaces=self._active_workspaces,
            workspace_manager=self._workspace_manager,
            default_model_provider=getattr(self, "_default_model_provider", None),
            repo_vcs=getattr(self, "_repo_vcs", {}),
            dry_run=self._dry_run,
            notifier=getattr(self, "_notifier", None),
            create_workspace_fn=self._create_workspace,
        )

    def _get_repo_config(self, workspace: Workspace) -> RepoConfig | None:
        """Find the RepoConfig matching a workspace."""
        state = workspace.state
        for proj in self._projects.values():
            if state.repo_id in proj.repos:
                return proj.repos[state.repo_id]
        return None

    def _get_vcs_for_workspace(self, workspace: Workspace) -> tuple[VCSInterface | None, RepoConfig | None]:
        """Get the VCS adapter and config for a workspace."""
        repo_id = workspace.state.repo_id
        if repo_id in self._repo_vcs:
            return self._repo_vcs[repo_id]
        repo_config = self._get_repo_config(workspace)
        return self._vcs, repo_config

    def _get_chat_id(self, workspace: Workspace) -> str:
        """Get Telegram chat ID for a workspace (project or global)."""
        repo_config = self._get_repo_config(workspace)
        if repo_config and repo_config.telegram.default_chat_id:
            return repo_config.telegram.default_chat_id
        return self._global_config.telegram.default_chat_id

    async def run(self) -> None:
        """Main async loop — poll and advance until shutdown."""
        await self._runtime.run()

    async def poll_cycle(self) -> None:
        """Single poll + advance cycle."""
        await self._runtime.poll_cycle()

    async def _poll_and_create_workspaces(self) -> None:
        """Poll tracker for new tickets, create workspaces, append to active list."""
        from orchestrator.ingest import poll_and_create_workspaces
        new_workspaces = await poll_and_create_workspaces(
            tracker=self._tracker,
            projects=self._projects,
            active_workspaces=self._active_workspaces,
            global_config=self._global_config,
            workspace_manager=self._workspace_manager,
            default_model_provider=getattr(self, "_default_model_provider", None),
            repo_vcs=getattr(self, "_repo_vcs", {}),
            notifier=getattr(self, "_notifier", None),
            dry_run=self._dry_run,
            event_bus=getattr(self, "_events", None),
            create_workspace_fn=self._create_workspace,
        )
        self._active_workspaces.extend(new_workspaces)

    async def _create_workspace(
        self, pt: PrioritizedTicket, project_id: str, repo_config: RepoConfig,
    ) -> Workspace:
        """Forward to ingest.create_workspace_for_ticket with bound deps."""
        from orchestrator.ingest import create_workspace_for_ticket
        return await create_workspace_for_ticket(
            pt, project_id, repo_config,
            workspace_manager=self._workspace_manager,
            tracker=getattr(self, "_tracker", None),
            default_model_provider=getattr(self, "_default_model_provider", None),
            repo_vcs=getattr(self, "_repo_vcs", {}),
            notifier=getattr(self, "_notifier", None),
        )

    async def advance_workspace(
        self, workspace: Workspace, _resume_depth: int = 0,
    ) -> None:
        from orchestrator.escalation import handle_escalate
        from orchestrator.pipeline.driver import advance_workspace

        async def _escalate(ws, *, is_max_iterations: bool = False):
            notifier = getattr(self, "_notifier", None)
            chat_id = self._get_chat_id(ws) if notifier else ""
            await handle_escalate(
                ws, notifier, chat_id,
                workflow=getattr(self, "_workflow", None),
                event_bus=getattr(self, "_events", None),
                is_max_iterations=is_max_iterations,
            )

        await advance_workspace(
            workspace,
            workflow=self._workflow,
            mode_handler=getattr(self, "_mode_handler", None),
            handle_agent_stage_fn=self._handle_agent_stage,
            handle_action_stage_fn=self._handle_action_stage,
            handle_escalate_fn=_escalate,
            _resume_depth=_resume_depth,
        )

    async def _on_ticket_done(self, workspace: Workspace) -> None:
        """Resolved internally — kept as a small helper used by both stage handlers."""
        from orchestrator.pipeline.actions.finalize import on_ticket_done
        projects = getattr(self, "_projects", None) or {}
        project = projects.get(workspace.state.company_id)
        in_review_status = (
            project.config.tracker.jira.statuses.in_review if project else ""
        )
        await on_ticket_done(
            workspace,
            getattr(self, "_notifier", None),
            self._get_chat_id(workspace),
            getattr(self, "_tracker", None),
            in_review_status,
        )

    async def _handle_agent_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        from orchestrator.escalation import build_blocked_reason
        from orchestrator.notify import (
            notify_deferred,
            notify_failed,
            notify_verification_blocked,
        )
        from orchestrator.pipeline.agent_stage import handle_agent_stage
        from orchestrator.pipeline.driver import advance_to_stage, build_gate_summary
        repo_config = self._get_repo_config(workspace)
        event_bus = getattr(self, "_events", None)
        notifier = getattr(self, "_notifier", None)

        async def _deferred(ws, retry_at, reason=None):
            chat_id = self._get_chat_id(ws)
            self._quota_window_end = await notify_deferred(
                notifier, chat_id, ws, retry_at, reason, self._quota_window_end,
            )

        async def _failed(ws, error):
            if notifier is None:
                return
            await notify_failed(notifier, self._get_chat_id(ws), ws, error)

        async def _verification_blocked(ws, sid, verify_reason):
            if not notifier:
                return
            await notify_verification_blocked(
                notifier, self._get_chat_id(ws), ws, sid, verify_reason,
                build_blocked_reason,
            )

        await handle_agent_stage(
            workspace, stage_id, stage_def,
            workflow=getattr(self, "_workflow", None),
            agent_runtime=getattr(self, "_agent_runtime", None),
            repo_config=repo_config,
            notifier=notifier,
            mode_handler=getattr(self, "_mode_handler", None),
            get_chat_id=self._get_chat_id,
            dry_run=getattr(self, "_dry_run", False),
            event_bus=event_bus,
            advance_to_stage_fn=lambda ws, sid: advance_to_stage(ws, sid, event_bus=event_bus),
            on_ticket_done_fn=self._on_ticket_done,
            build_gate_summary_fn=build_gate_summary,
            notify_verification_blocked_fn=_verification_blocked,
            notify_deferred_fn=_deferred,
            notify_failed_fn=_failed,
        )

    def _reconcile_disk_workspaces(self) -> None:
        """Sync in-memory workspace state with disk (delegates to Runtime)."""
        self._ensure_runtime_for_tests().reconcile_disk_workspaces()

    async def _sweep_deferred(self) -> None:
        """Resume DEFERRED workspaces whose retry_at has passed (delegates)."""
        await self._ensure_runtime_for_tests().sweep_deferred()

    def _sweep_quota_window(self, now: datetime) -> None:
        """Clear in-memory quota debounce window once its retry_at has passed.

        Called by Runtime.sweep_deferred so the window state stays on the
        Orchestrator (alongside the rest of the quota debounce machinery).
        """
        if self._quota_window_end is not None and now >= self._quota_window_end:
            self._quota_window_end = None

    def _ensure_runtime_for_tests(self) -> Runtime:
        """Return the Runtime, lazily constructing a minimal one if missing.

        Tests sometimes build Orchestrator via __new__ and skip __init__.
        For those, build a Runtime on demand from whatever attrs the test
        set (workspace_manager, events). Production always goes through
        __init__ which constructs the full Runtime up front.
        """
        runtime = self.__dict__.get("_runtime")
        if runtime is not None:
            return runtime
        # Pull the workspace list / completions out of __dict__ so the
        # newly-built Runtime sees the same instances the test populated.
        existing_active = self.__dict__.pop("_active_workspaces", [])
        existing_recent = self.__dict__.pop("_recent_completions", None)
        global_config = getattr(self, "_global_config", None) or SimpleNamespace(
            defaults=SimpleNamespace(max_parallel_tickets=3, poll_interval_seconds=900),
            workspaces=SimpleNamespace(max_age_days=7),
        )
        runtime = Runtime(
            global_config=global_config,
            workspace_manager=getattr(self, "_workspace_manager", None),
            poll_callback=lambda: self._poll_and_create_workspaces(),
            advance_callback=lambda ws: self.advance_workspace(ws),
            rescan_callback=lambda: self._rescan_projects_from_disk(),
            sweep_quota_window_callback=self._sweep_quota_window,
            get_tracker=lambda: getattr(self, "_tracker", None),
            get_mode_handler=lambda: getattr(self, "_mode_handler", None),
            event_bus=getattr(self, "_events", None),
            dry_run=getattr(self, "_dry_run", False),
        )
        runtime._active_workspaces = list(existing_active)
        if existing_recent is not None:
            # Preserve any list/deque the test set up so identity-based
            # assertions keep working when possible.
            for entry in list(existing_recent):
                runtime._recent_completions.append(entry)
        self.__dict__["_runtime"] = runtime
        return runtime

    async def _handle_action_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        from orchestrator.pipeline.action_stage import handle_action_stage
        from orchestrator.pipeline.driver import advance_to_stage, build_gate_summary
        event_bus = getattr(self, "_events", None)
        notifier = getattr(self, "_notifier", None)
        tracker = getattr(self, "_tracker", None)

        async def _push(ws):
            vcs, repo_config = self._get_vcs_for_workspace(ws)
            return await action_push_and_open_pr(
                ws, vcs, repo_config, notifier, self._get_chat_id(ws),
                tracker, event_bus,
            )

        async def _fetch(ws, sd):
            return await action_fetch_pr_comments(
                ws, sd,
                get_vcs=lambda: self._get_vcs_for_workspace(ws),
                get_chat_id=lambda: self._get_chat_id(ws),
                tracker=tracker,
                notifier=notifier,
                agent_runtime=getattr(self, "_agent_runtime", None),
                event_bus=event_bus,
            )

        async def _finalize(ws):
            return await action_finalize(ws, notifier, self._get_chat_id(ws), tracker)

        await handle_action_stage(
            workspace, stage_id, stage_def,
            workflow=getattr(self, "_workflow", None),
            notifier=notifier,
            mode_handler=getattr(self, "_mode_handler", None),
            get_chat_id=lambda: self._get_chat_id(workspace),
            dry_run=getattr(self, "_dry_run", False),
            event_bus=event_bus,
            action_push_and_open_pr=_push,
            action_fetch_pr_comments=_fetch,
            action_finalize=_finalize,
            advance_to_stage_fn=lambda ws, sid: advance_to_stage(ws, sid, event_bus=event_bus),
            on_ticket_done_fn=self._on_ticket_done,
            build_gate_summary_fn=build_gate_summary,
        )

    def shutdown(self) -> None:
        """Trigger graceful shutdown (delegates to Runtime)."""
        runtime = self.__dict__.get("_runtime")
        if runtime is not None:
            runtime.shutdown()
        else:
            self._shutdown_event.set()

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self.shutdown()

    def _emit(self, event_type: str, message: str, **kwargs: Any) -> None:
        """Emit an event if the event bus is available (delegates)."""
        runtime = self.__dict__.get("_runtime")
        if runtime is not None:
            runtime.emit(event_type, message, **kwargs)
        elif getattr(self, "_events", None) is not None:
            self._events.emit(event_type, message, **kwargs)


# Backward-compat re-exports — moved to orchestrator.pipeline.agent_stage.
from orchestrator.pipeline.agent_stage import (  # noqa: E402
    _looks_like_fail,
    _looks_like_pass,
)

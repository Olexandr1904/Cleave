"""Orchestrator — main daemon loop managing workspaces and agent dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from config.config_loader import ConfigError, load_config

from config.schemas import GlobalConfig, LoadedProject, RepoConfig
from config.resource_registry import ResourceRegistry
from integrations.base.notifier import Button, NotifierInterface
from integrations.base.tracker import TicketData, TrackerInterface
from integrations.base.vcs import VCSInterface
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.constants import (
    REPORT_BA_QUESTIONS,
    STAGE_REPORT_FILE,
)
from orchestrator.gradle_remediation import (
    clear_gradle_transforms,
)
from orchestrator import stage_verifier
from orchestrator.stage_verifier import ActionResult
from orchestrator.ticket_prioritizer import PrioritizedTicket
from orchestrator.workflow_router import (
    WorkflowDefinition,
    load_workflow,
)
from workspace.workspace import Stage, Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


DEFAULT_QUOTA_RETRY_DELAY = timedelta(hours=1)


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
        self._active_workspaces: list[Workspace] = []
        self._shutdown_event = asyncio.Event()
        # Map repo_id -> (VCSInterface, RepoConfig) for per-repo VCS
        self._repo_vcs: dict[str, tuple[VCSInterface, RepoConfig]] = {}
        # Mode handler — initialized later via set_mode_handler or from config default
        self._mode_handler: ModeHandler | None = None
        # Ring buffer of recently-terminated workspaces for /status visibility.
        # Each entry is (ticket_id, final_state, timestamp_epoch).
        self._recent_completions: deque[tuple[str, str, float]] = deque(maxlen=20)
        # In-memory debounce for Claude CLI quota notifications.
        # Stores the retry_at of the first notification in the current window;
        # further quota hits while now < _quota_window_end are silenced.
        self._quota_window_end: datetime | None = None
        self._config_dir = config_dir
        self._on_project_added = on_project_added
        self._wake_event = asyncio.Event()
        # Limit concurrent agent executions to avoid quota exhaustion
        try:
            max_parallel = int(global_config.defaults.max_parallel_tickets)
        except (TypeError, ValueError, AttributeError):
            max_parallel = 3
        self._agent_semaphore = asyncio.Semaphore(max_parallel)

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
            self._wake_event.set()
        return added

    def set_mode_handler(self, handler: ModeHandler) -> None:
        """Register the mode handler for auto/manual switching."""
        self._mode_handler = handler

    def get_active_workspaces(self) -> list[Workspace]:
        """Return the current active workspace list (for CommandHandler status)."""
        return list(self._active_workspaces)

    def get_recent_completions(self) -> list[tuple[str, str, float]]:
        """Return recently-terminated workspaces (ticket_id, final_state, epoch).

        Used by /status to show DONE / FAILED tickets after they've been
        removed from the active list.
        """
        return list(self._recent_completions)

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
            create_workspace_fn=self._create_workspace_for_ticket,
        )

    def _route_manual_ticket(self, ticket: TicketData) -> PrioritizedTicket | None:
        """Find the project+repo that owns this ticket via tracker_label match.

        Thin shim — see orchestrator/ingest.py for the implementation.
        """
        from orchestrator.ingest import route_manual_ticket
        return route_manual_ticket(ticket, self._projects)

    def _should_approval_gate(
        self, completed_state: str, next_stage: str | None = None,
    ) -> bool:
        """Shim — see orchestrator.approval_gate.should_approval_gate."""
        from orchestrator.approval_gate import should_approval_gate
        return should_approval_gate(self._mode_handler, completed_state, next_stage)

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

    @staticmethod
    def _git_diff_files(workspace: Workspace, since_sha: str = "") -> set[str]:
        from orchestrator.git_ops import git_diff_files
        return git_diff_files(workspace, since_sha)

    @staticmethod
    def _git_head_sha(workspace: Workspace) -> str:
        from orchestrator.git_ops import git_head_sha
        return git_head_sha(workspace)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    async def run(self) -> None:
        """Main async loop — poll and advance until shutdown."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # Discover existing workspaces on startup
        self._active_workspaces = self._workspace_manager.discover_workspaces()
        if self._active_workspaces:
            logger.info(
                "Resumed %d active workspace(s) from disk",
                len(self._active_workspaces),
            )

        poll_interval = self._global_config.defaults.poll_interval_seconds
        logger.info(
            "Orchestrator started (poll_interval=%ds, dry_run=%s)",
            poll_interval, self._dry_run,
        )
        self._emit("daemon_started", f"Orchestrator started (mode={self._mode_handler.get_mode() if self._mode_handler else 'auto'}, dry_run={self._dry_run})")

        while not self._shutdown_event.is_set():
            try:
                await self.poll_cycle()
            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)

            try:
                self._wake_event.clear()
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(self._shutdown_event.wait()),
                        asyncio.create_task(self._wake_event.wait()),
                    ],
                    timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel and reap the loser(s) so they don't leak across cycles.
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                for t in done:
                    t.result()  # suppress unhandled-task warnings
            except asyncio.TimeoutError:
                pass

        logger.info("Orchestrator shutting down gracefully")

    async def poll_cycle(self) -> None:
        """Single poll + advance cycle."""
        # Pick up any projects added to config-live/ since last cycle (wizard or hand-edit).
        await self._rescan_projects_from_disk()
        self._emit("poll_cycle", "Poll cycle started")
        # 0. Re-adopt workspaces that exist on disk but fell out of the active list
        self._reconcile_disk_workspaces()
        # 0b. Resume any DEFERRED workspaces whose retry_at has passed
        await self._sweep_deferred()
        # 1. Poll for new tickets and create workspaces
        if self._tracker:
            await self._poll_and_create_workspaces()

        # 2. Advance active workspaces in parallel (bounded by semaphore)
        async def _safe_advance(ws: Workspace) -> None:
            async with self._agent_semaphore:
                try:
                    await self.advance_workspace(ws)
                except Exception as e:
                    logger.error(
                        "Workspace %s error: %s",
                        ws.state.ticket_id, e, exc_info=True,
                    )
                    try:
                        ws.transition(Stage.FAILED)
                        ws.update_state(error=str(e))
                    except Exception:
                        pass

        # Skip workspaces in terminal or clearly waiting states
        _SKIP = {Stage.DONE, Stage.ARCHIVED, Stage.BLOCKED,
                 Stage.MANUAL_CONTROL, Stage.DEFERRED, Stage.FAILED, Stage.PAUSED}
        active = [ws for ws in self._active_workspaces if ws.state.current_state not in _SKIP]
        if active:
            await asyncio.gather(*[_safe_advance(ws) for ws in active])

        # 3. Cleanup terminal workspaces from active list and record them for
        # /status to show recent completions even after they leave the list.
        terminal = {Stage.DONE, Stage.ARCHIVED}
        still_active: list[Workspace] = []
        now = time.time()
        for ws in self._active_workspaces:
            if ws.state.current_state in terminal:
                self._recent_completions.append(
                    (ws.state.ticket_id, ws.state.current_state, now),
                )
            else:
                still_active.append(ws)
        self._active_workspaces = still_active

        # 4. Workspace cleanup
        max_age = self._global_config.workspaces.max_age_days
        deleted = self._workspace_manager.cleanup_old_workspaces(max_age)
        if deleted:
            logger.info("Cleaned up %d old workspace(s)", len(deleted))

    async def _poll_and_create_workspaces(self) -> None:
        """Poll tracker for new tickets and create workspaces.

        Thin shim — see orchestrator/ingest.py for the implementation.
        """
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
            create_workspace_fn=self._create_workspace_for_ticket,
        )
        self._active_workspaces.extend(new_workspaces)

    async def _refetch_ticket_data(self, workspace: Workspace) -> None:
        """Refetch ticket data from tracker; write/append meta files.

        Thin shim — see orchestrator/ticket_sync.py for the implementation.
        """
        from orchestrator.ticket_sync import refetch_ticket_data
        await refetch_ticket_data(workspace, self._tracker)

    async def _create_workspace_for_ticket(
        self,
        pt: PrioritizedTicket,
        project_id: str,
        repo_config: RepoConfig,
    ) -> Workspace:
        """Create workspace, clone repo, write ticket data.

        Thin shim — see orchestrator/ingest.py for the implementation.
        """
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
        from orchestrator.pipeline.driver import advance_workspace
        await advance_workspace(
            workspace,
            workflow=self._workflow,
            mode_handler=getattr(self, "_mode_handler", None),
            handle_agent_stage_fn=self._handle_agent_stage,
            handle_action_stage_fn=self._handle_action_stage,
            handle_escalate_fn=self._handle_escalate,
            _resume_depth=_resume_depth,
        )

    async def _handle_agent_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        from orchestrator.pipeline.agent_stage import handle_agent_stage
        repo_config = self._get_repo_config(workspace)
        await handle_agent_stage(
            workspace, stage_id, stage_def,
            workflow=getattr(self, "_workflow", None),
            agent_runtime=getattr(self, "_agent_runtime", None),
            repo_config=repo_config,
            notifier=getattr(self, "_notifier", None),
            mode_handler=getattr(self, "_mode_handler", None),
            get_chat_id=self._get_chat_id,
            dry_run=getattr(self, "_dry_run", False),
            event_bus=getattr(self, "_events", None),
            advance_to_stage_fn=self._advance_to_stage,
            on_ticket_done_fn=self._on_ticket_done,
            build_gate_summary_fn=self._build_gate_summary,
            notify_verification_blocked_fn=self._notify_verification_blocked,
            notify_deferred_fn=self._notify_deferred,
            notify_failed_fn=self._notify_failed,
        )

    def _rollback_iteration(self, workspace: Workspace, stage_id: str) -> None:
        from orchestrator.pipeline.agent_stage import rollback_iteration
        rollback_iteration(workspace, stage_id)

    async def _notify_deferred(
        self, workspace: Workspace, retry_at: datetime,
        reason: str | None = None,
    ) -> None:
        from orchestrator.notify import notify_deferred
        chat_id = self._get_chat_id(workspace)
        self._quota_window_end = await notify_deferred(
            self._notifier, chat_id, workspace, retry_at, reason,
            self._quota_window_end,
        )

    async def _notify_failed(self, workspace: Workspace, error: str) -> None:
        if self._notifier is None:
            return
        from orchestrator.notify import notify_failed
        chat_id = self._get_chat_id(workspace)
        await notify_failed(self._notifier, chat_id, workspace, error)

    async def _notify_rerun(
        self, workspace: Workspace, branch: str, reason: str
    ) -> None:
        if self._notifier is None:
            return
        from orchestrator.notify import notify_rerun
        chat_id = self._get_chat_id(workspace)
        await notify_rerun(self._notifier, chat_id, workspace, branch, reason)

    def _reconcile_disk_workspaces(self) -> None:
        """Sync in-memory workspace state with disk.

        - Re-adopt workspaces on disk that fell out of the active list
        - Refresh state for active workspaces (picks up dashboard retries,
          manual edits, TG replies that wrote to state.json)
        """
        disk_workspaces = {ws.state.ticket_id: ws for ws in self._workspace_manager.discover_workspaces()}
        active_ids = {ws.state.ticket_id for ws in self._active_workspaces}

        # Re-adopt orphans
        for tid, ws in disk_workspaces.items():
            if tid not in active_ids:
                self._active_workspaces.append(ws)
                logger.warning(
                    "Re-adopted orphaned workspace: %s (state=%s)",
                    tid, ws.state.current_state,
                )

        # Refresh state from disk for all active workspaces
        for i, ws in enumerate(self._active_workspaces):
            disk_ws = disk_workspaces.get(ws.state.ticket_id)
            if disk_ws and disk_ws.state.current_state != ws.state.current_state:
                logger.info(
                    "Refreshed %s state from disk: %s -> %s",
                    ws.state.ticket_id, ws.state.current_state, disk_ws.state.current_state,
                )
                self._active_workspaces[i] = disk_ws

    async def _sweep_deferred(self) -> None:
        """Resume DEFERRED workspaces whose retry_at has passed.

        Called at the top of each poll cycle. Also clears the in-memory
        quota debounce window once its retry_at has passed.
        """
        now = datetime.now(timezone.utc)

        if self._quota_window_end is not None and now >= self._quota_window_end:
            self._quota_window_end = None

        for ws in list(self._active_workspaces):
            if ws.state.current_state != Stage.DEFERRED:
                continue
            retry_at_str = ws.state.retry_at
            if not retry_at_str:
                continue
            try:
                retry_at = datetime.fromisoformat(retry_at_str)
            except ValueError:
                logger.warning(
                    "Workspace %s has malformed retry_at: %s",
                    ws.state.ticket_id, retry_at_str,
                )
                continue
            if retry_at <= now:
                target = ws.state.previous_state or Stage.ANALYSIS
                ws.transition(target)
                self._emit(
                    "deferred_resumed",
                    f"Resumed {ws.state.ticket_id} from DEFERRED to {target}",
                    project_id=ws.state.company_id,
                    ticket_id=ws.state.ticket_id,
                    data={"target_state": target},
                )

    async def _handle_action_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        from orchestrator.pipeline.action_stage import handle_action_stage
        await handle_action_stage(
            workspace, stage_id, stage_def,
            workflow=getattr(self, "_workflow", None),
            notifier=getattr(self, "_notifier", None),
            mode_handler=getattr(self, "_mode_handler", None),
            get_chat_id=lambda: self._get_chat_id(workspace),
            dry_run=getattr(self, "_dry_run", False),
            event_bus=getattr(self, "_events", None),
            action_push_and_open_pr=self._action_push_and_open_pr,
            action_fetch_pr_comments=self._action_fetch_pr_comments,
            action_finalize=self._action_finalize,
            advance_to_stage_fn=self._advance_to_stage,
            on_ticket_done_fn=self._on_ticket_done,
            build_gate_summary_fn=self._build_gate_summary,
        )

    async def _action_push_and_open_pr(self, workspace: Workspace) -> ActionResult:
        from orchestrator.pipeline.actions.push_and_open_pr import action_push_and_open_pr
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        chat_id = self._get_chat_id(workspace)
        return await action_push_and_open_pr(
            workspace, vcs, repo_config, self._notifier, chat_id,
            self._tracker, self._events,
        )

    def _commit_pipeline_artifacts(
        self, workspace: Workspace, repo_config: RepoConfig,
    ) -> None:
        from orchestrator.pipeline.actions.push_and_open_pr import commit_pipeline_artifacts
        commit_pipeline_artifacts(workspace, repo_config)

    def _ensure_branch_has_commits(
        self, workspace: Workspace, repo_config: RepoConfig,
    ) -> None:
        from orchestrator.pipeline.actions.push_and_open_pr import ensure_branch_has_commits
        ensure_branch_has_commits(workspace, repo_config, event_bus=self._events)

    def _squash_feature_commits(
        self, workspace: Workspace, repo_config: RepoConfig | None = None,
    ) -> None:
        from orchestrator.pipeline.actions.push_and_open_pr import squash_feature_commits
        squash_feature_commits(workspace, repo_config)

    async def _reinvestigate_pending(self, workspace: Workspace) -> None:
        # All resolution is deferred: pre-refactor _reinvestigate_pending
        # only consulted chat_id deep inside a failure branch, so eager
        # lookups break test fixtures that mock Orchestrator without
        # _projects or _get_chat_id.
        from orchestrator.pipeline.actions.fetch_pr_comments import reinvestigate_pending
        await reinvestigate_pending(
            workspace,
            agent_runtime=getattr(self, "_agent_runtime", None),
            notifier=getattr(self, "_notifier", None),
            get_chat_id=lambda: self._get_chat_id(workspace),
            event_bus=getattr(self, "_events", None),
        )

    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> ActionResult:
        # Defer vcs/chat_id resolution: the action's first branches check
        # state.pr_number / pending lists before touching VCS, and some
        # call sites (e.g. test fixtures) mock the Orchestrator without
        # configuring `_get_vcs_for_workspace`. Use lazy thunks.
        from orchestrator.pipeline.actions.fetch_pr_comments import action_fetch_pr_comments
        return await action_fetch_pr_comments(
            workspace, stage_def,
            get_vcs=lambda: self._get_vcs_for_workspace(workspace),
            get_chat_id=lambda: self._get_chat_id(workspace),
            tracker=getattr(self, "_tracker", None),
            notifier=getattr(self, "_notifier", None),
            agent_runtime=getattr(self, "_agent_runtime", None),
            event_bus=getattr(self, "_events", None),
        )

    async def _send_escalated_comment_tg(self, workspace: Workspace, cc: Any, pr_number: int) -> int:
        from orchestrator.pipeline.actions.fetch_pr_comments import send_escalated_comment_tg
        return await send_escalated_comment_tg(
            workspace, cc, pr_number,
            notifier=getattr(self, "_notifier", None),
            get_chat_id=lambda: self._get_chat_id(workspace),
        )

    async def _execute_review_decisions(self, workspace: Workspace) -> ActionResult:
        from orchestrator.pipeline.actions.fetch_pr_comments import execute_review_decisions
        return await execute_review_decisions(
            workspace,
            get_vcs=lambda: self._get_vcs_for_workspace(workspace),
            get_chat_id=lambda: self._get_chat_id(workspace),
            notifier=getattr(self, "_notifier", None),
        )


    async def _on_ticket_done(self, workspace: Workspace) -> None:
        from orchestrator.pipeline.actions.finalize import on_ticket_done
        chat_id = self._get_chat_id(workspace)
        projects = getattr(self, "_projects", None) or {}
        project = projects.get(workspace.state.company_id)
        in_review_status = (
            project.config.jira.statuses.in_review if project else ""
        )
        await on_ticket_done(
            workspace,
            getattr(self, "_notifier", None),
            chat_id,
            getattr(self, "_tracker", None),
            in_review_status,
        )

    def _build_blocked_reason(self, workspace: Any, stage_id: str) -> str:
        from orchestrator.escalation import build_blocked_reason
        return build_blocked_reason(workspace, stage_id)

    @classmethod
    def _truncate_reason(cls, text: str) -> str:
        from orchestrator.escalation import truncate_reason
        return truncate_reason(text)

    async def _handle_escalate(
        self, workspace: Workspace, *, is_max_iterations: bool = False,
    ) -> None:
        from orchestrator.escalation import handle_escalate
        chat_id = self._get_chat_id(workspace) if self._notifier else ""
        await handle_escalate(
            workspace, self._notifier, chat_id,
            workflow=getattr(self, "_workflow", None),
            event_bus=self._events,
            is_max_iterations=is_max_iterations,
        )

    async def _notify_verification_blocked(
        self, workspace: Workspace, stage_id: str, verify_reason: str,
    ) -> None:
        if not self._notifier:
            return
        from orchestrator.notify import notify_verification_blocked
        from orchestrator.escalation import build_blocked_reason
        chat_id = self._get_chat_id(workspace)
        await notify_verification_blocked(
            self._notifier, chat_id, workspace, stage_id, verify_reason,
            build_blocked_reason,
        )

    async def _action_finalize(self, workspace: Workspace) -> ActionResult:
        from orchestrator.pipeline.actions.finalize import action_finalize
        chat_id = self._get_chat_id(workspace)
        return await action_finalize(
            workspace,
            getattr(self, "_notifier", None),
            chat_id,
            getattr(self, "_tracker", None),
        )

    @staticmethod
    def _log_pipeline(workspace: Workspace, entry: str) -> None:
        """Append a timestamped entry to ai_pipeline/<ticket>/pipeline-log.md."""
        log_path = workspace.reports_dir / "pipeline-log.md"
        timestamp = datetime.now(timezone.utc).strftime("%H:%M")
        line = f"- **{timestamp}** {entry}\n"
        if log_path.exists():
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        else:
            log_path.write_text(f"# Pipeline Log — {workspace.state.ticket_id}\n\n{line}", encoding="utf-8")

    def _advance_to_stage(self, workspace: Workspace, stage_id: str) -> None:
        from orchestrator.pipeline.driver import advance_to_stage
        advance_to_stage(workspace, stage_id, event_bus=getattr(self, "_events", None))

    def _build_gate_summary(
        self, workspace: Workspace, gate_state: str,
    ) -> tuple[str, list[Button]]:
        from orchestrator.pipeline.driver import build_gate_summary
        return build_gate_summary(workspace, gate_state)

    def _parse_agent_outcome(
        self, stage_id: str, output: str, workspace: Workspace,
    ) -> str:
        from orchestrator.pipeline.agent_stage import parse_agent_outcome
        return parse_agent_outcome(stage_id, output, workspace)

    def shutdown(self) -> None:
        """Trigger graceful shutdown."""
        self._shutdown_event.set()

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self.shutdown()

    def _emit(self, event_type: str, message: str, **kwargs: Any) -> None:
        """Emit an event if the event bus is available."""
        if self._events:
            self._events.emit(event_type, message, **kwargs)


# Backward-compat re-exports — moved to orchestrator.pipeline.agent_stage.
from orchestrator.pipeline.agent_stage import (  # noqa: E402
    _looks_like_fail,
    _looks_like_pass,
)

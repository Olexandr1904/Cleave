"""Orchestrator — main daemon loop managing workspaces and agent dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
from integrations.telegram.handlers.analyze import AnalyzeHandler
from integrations.telegram.handlers.approval import APPROVAL_NEXT_STATE
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.constants import (
    REPORT_BA,
    REPORT_BA_QUESTIONS,
    STAGE_REPORT_FILE,
    STAGE_RUNTIME_OUTPUT,
)
from orchestrator.model_resolver import resolve_ticket_model
from orchestrator.gradle_remediation import (
    clear_gradle_transforms,
)
from orchestrator.pr_creation import create_pr
from orchestrator import stage_verifier
from orchestrator import tg_format
from orchestrator.stage_verifier import ActionResult
from orchestrator.ticket_prioritizer import PrioritizedTicket, filter_tickets, prioritize_tickets, route_tickets
from orchestrator.workflow_router import (
    WorkflowDefinition,
    get_next_stage,
    load_workflow,
    should_escalate,
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

        Validates each ticket via AnalyzeHandler, skips duplicates, then
        creates a workspace for each valid one by matching its labels to a
        configured repo. Returns {"valid": [...], "invalid": [...]} where
        invalid entries are "TICKET: reason" strings.
        """
        result: dict[str, list[str]] = {"valid": [], "invalid": []}

        if not self._tracker:
            for tid in ticket_ids:
                result["invalid"].append(f"{tid}: no tracker configured")
            return result

        handler = AnalyzeHandler(self._tracker)
        validation = await handler.validate_tickets(ticket_ids)
        result["invalid"].extend(validation.invalid)

        for ticket in validation.valid:
            if handler.is_already_active(ticket.id, self._active_workspaces):
                result["invalid"].append(f"{ticket.id}: already active")
                continue

            pt = self._route_manual_ticket(ticket)
            if not pt:
                result["invalid"].append(
                    f"{ticket.id}: no matching repo label in any project",
                )
                continue

            project = self._projects.get(pt.project_id)
            if not project:
                result["invalid"].append(f"{ticket.id}: project {pt.project_id} not loaded")
                continue
            repo_config = project.repos.get(pt.repo_id)
            if not repo_config:
                result["invalid"].append(f"{ticket.id}: repo {pt.repo_id} not loaded")
                continue

            if self._dry_run:
                logger.info("[DRY RUN] Would create manual workspace for %s", ticket.id)
                result["valid"].append(ticket.id)
                continue

            try:
                ws = await self._create_workspace_for_ticket(
                    pt, pt.project_id, repo_config,
                )
                self._active_workspaces.append(ws)
                result["valid"].append(ticket.id)
                logger.info(
                    "Manually queued %s (%s/%s)",
                    ticket.id, pt.project_id, pt.repo_id,
                )
            except Exception as e:
                logger.error("Manual workspace creation failed for %s: %s", ticket.id, e)
                result["invalid"].append(f"{ticket.id}: {e}")

        return result

    def _route_manual_ticket(self, ticket: TicketData) -> PrioritizedTicket | None:
        """Find the project+repo that owns this ticket via tracker_label match."""
        for project_id, project in self._projects.items():
            for repo_id, repo_config in project.repos.items():
                if repo_config.tracker_label and repo_config.tracker_label in ticket.labels:
                    return PrioritizedTicket(
                        ticket=ticket, repo_id=repo_id, project_id=project_id,
                    )
        return None

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
        """Poll tracker for new tickets and create workspaces."""
        for project_id, project in self._projects.items():
            jira_config = project.config.jira
            if not jira_config.url:
                continue

            try:
                tickets = await self._tracker.poll_tickets()
            except Exception as e:
                logger.error("Failed to poll tickets for %s: %s", project_id, e)
                continue

            if not tickets:
                continue

            # Filter, route to repos, then prioritize
            filtered = filter_tickets(
                tickets,
                trigger_labels=jira_config.trigger_labels,
                ignore_labels=jira_config.ignore_labels,
            )
            routed = route_tickets(filtered, project)
            prioritized = prioritize_tickets(routed)
            max_parallel = project.config.parallelism.max_concurrent_tickets

            # Count active workspaces for this project
            active_count = sum(
                1 for ws in self._active_workspaces
                if ws.state.company_id == project_id
            )

            for pt in prioritized:
                if active_count >= max_parallel:
                    logger.info(
                        "Project %s at max capacity (%d/%d), skipping remaining",
                        project_id, active_count, max_parallel,
                    )
                    break

                # Check if workspace already exists (in memory or on disk)
                already_exists = any(
                    ws.state.ticket_id == pt.ticket.id
                    for ws in self._active_workspaces
                )
                if already_exists:
                    continue
                # Also check disk — workspace may be DONE/ARCHIVED but still on disk
                from pathlib import Path
                ws_dir = Path(self._global_config.workspaces.base_dir) / project_id / pt.repo_id / "tickets" / pt.ticket.id
                if ws_dir.exists():
                    logger.debug("Workspace on disk for %s — skipping", pt.ticket.id)
                    continue

                repo_config = project.repos.get(pt.repo_id)
                if not repo_config:
                    continue

                if self._dry_run:
                    logger.info(
                        "[DRY RUN] Would create workspace for %s -> %s/%s",
                        pt.ticket.id, project_id, pt.repo_id,
                    )
                    continue

                try:
                    ws = await self._create_workspace_for_ticket(
                        pt, project_id, repo_config,
                    )
                    self._active_workspaces.append(ws)
                    active_count += 1
                    logger.info(
                        "Created workspace for %s (%s/%s)",
                        pt.ticket.id, project_id, pt.repo_id,
                    )
                    self._emit("workspace_created", f"Created workspace for {pt.ticket.id}", project_id=project_id, ticket_id=pt.ticket.id, data={"repo_id": pt.repo_id})
                except Exception as e:
                    logger.error(
                        "Failed to create workspace for %s: %s",
                        pt.ticket.id, e,
                    )

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
        """Create workspace, clone repo, write ticket data."""
        ws = self._workspace_manager.create(
            company_id=project_id,
            repo_id=pt.repo_id,
            ticket_id=pt.ticket.id,
            clone_url=repo_config.git.clone_url,
            clone_depth=repo_config.git.depth,
            default_branch=repo_config.vcs.default_branch,
            branch_prefix=repo_config.vcs.branch_prefix,
            title=pt.ticket.summary,
        )

        # Per-ticket model snapshot — single source of truth for this workspace.
        # Resolves to a non-empty Claude model id at workspace creation and is
        # used by every agent dispatched against this ticket. See
        # docs/superpowers/specs/2026-04-30-per-ticket-model-label-design.md.
        resolution = resolve_ticket_model(pt.ticket.labels)
        ws.state.model = resolution.model or self._default_model_provider()
        ws.save_state()
        if resolution.warning and self._tracker is not None:
            try:
                await self._tracker.add_comment(pt.ticket.id, resolution.warning)
            except Exception as e:
                logger.warning(
                    "Failed to post model-label warning to %s: %s",
                    pt.ticket.id, e,
                )

        # Write ticket metadata — calls tracker for ticket description, comments, and history
        await self._refetch_ticket_data(ws)

        # Fetch and write parent ticket if linked
        if self._tracker and pt.ticket.linked_issues:
            for link in pt.ticket.linked_issues:
                parent_key = link.get("key", "")
                if parent_key and link.get("type", "").lower() in ("is child of", "parent"):
                    try:
                        from orchestrator.ticket_sync import ticket_to_markdown
                        parent = await self._tracker.get_ticket(parent_key)
                        parent_md = ticket_to_markdown(parent)
                        (ws.meta_dir / "parent.md").write_text(parent_md, encoding="utf-8")
                    except Exception as e:
                        logger.warning("Failed to fetch parent %s: %s", parent_key, e)
                    break

        # Transition Jira to In Progress
        if self._tracker:
            try:
                await self._tracker.transition_ticket(
                    pt.ticket.id, repo_config.jira.statuses.in_progress,
                )
            except Exception as e:
                logger.warning("Failed to transition %s: %s", pt.ticket.id, e)

        # Check if a PR already exists for this ticket's branch
        vcs_entry = self._repo_vcs.get(pt.repo_id)
        if vcs_entry:
            vcs_adapter, _ = vcs_entry
            branch = ws.state.branch
            if branch:
                try:
                    pr_info = await vcs_adapter.find_pr_by_branch(branch)
                    if pr_info:
                        pr_number, pr_url = pr_info
                        ws.update_state(pr_number=pr_number, pr_url=pr_url)
                        ws.transition(Stage.PR_REVIEW)
                        logger.info(
                            "Found existing PR #%d for %s — resuming from PR_REVIEW",
                            pr_number, pt.ticket.id,
                        )
                        return ws
                except Exception as e:
                    logger.warning("Failed to check for existing PR for %s: %s", pt.ticket.id, e)

        # No existing PR — start from ANALYSIS
        ws.transition(Stage.ANALYSIS)
        return ws

    async def advance_workspace(self, workspace: Workspace, _resume_depth: int = 0) -> None:
        """Advance a workspace through the pipeline.

        `_resume_depth` is internal: the auto-resume branch tail-calls back into
        this method so the resumed workspace doesn't wait a full poll cycle.
        Capped to prevent stack overflow if cascading gates ever chain.
        """
        state = workspace.state
        current = state.current_state

        if current == Stage.BLOCKED:
            return  # Waiting for human reply

        if current == Stage.MANUAL_CONTROL:
            return  # Under human control — skip entirely

        if current == Stage.AWAITING_APPROVAL:
            # Manual→Auto mid-flight: if the operator switched to auto while
            # this workspace was parked at a gate, auto-approve and resume.
            # Otherwise keep waiting for an explicit approve/reject.
            if not (self._mode_handler and self._mode_handler.get_mode() == "auto"):
                return  # Waiting for operator approval
            previous = state.previous_state
            next_state = APPROVAL_NEXT_STATE.get(previous)
            if not next_state:
                logger.warning(
                    "Cannot auto-resume %s: unknown previous gate %s",
                    state.ticket_id, previous,
                )
                return
            workspace.transition(next_state)
            logger.info(
                "Auto-resumed %s from AWAITING_APPROVAL (gate=%s) to %s "
                "after mode switch to auto",
                state.ticket_id, previous, next_state,
            )
            # Re-advance so the resumed workspace doesn't wait a full poll cycle.
            if _resume_depth >= 5:
                logger.warning(
                    "advance_workspace auto-resume cap hit for %s (depth=%d); "
                    "next poll cycle will pick it up",
                    state.ticket_id, _resume_depth,
                )
                return
            await self.advance_workspace(workspace, _resume_depth + 1)
            return

        if current in (Stage.DONE, Stage.ARCHIVED):
            return  # Terminal

        # Map pipeline state to workflow stage
        stage_id = _state_to_stage(current)
        if not stage_id:
            return

        stage_def = self._workflow.stages.get(stage_id)
        if not stage_def:
            logger.warning("No stage definition for '%s'", stage_id)
            return

        # Check iteration cap -> escalate. A workflow that loops back into an
        # earlier stage with a stale at-max counter must clear
        # state.stage_iterations[<stage_id>] explicitly at transition time —
        # otherwise it will escalate immediately on re-entry.
        if stage_def.max_iterations > 0:
            iterations = state.stage_iterations.get(stage_id, 0)
            if should_escalate(stage_id, self._workflow, iterations):
                next_stage = get_next_stage(stage_id, self._workflow, "max_iterations")
                if next_stage == "escalate":
                    await self._handle_escalate(workspace, is_max_iterations=True)
                return

        # Dispatch: agent stage or action stage
        if stage_def.agent:
            await self._handle_agent_stage(workspace, stage_id, stage_def)
        elif stage_def.action:
            await self._handle_action_stage(workspace, stage_id, stage_def)

    async def _handle_agent_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        """Execute an agent stage."""
        state = workspace.state

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would execute agent '%s' for %s",
                stage_def.agent, state.ticket_id,
            )
            next_stage = get_next_stage(stage_id, self._workflow)
            if next_stage:
                self._advance_to_stage(workspace, next_stage)
            return

        workspace.increment_iteration(stage_id)
        stage_start_commit = stage_verifier.capture_stage_start(workspace, stage_id)

        repo_config = self._get_repo_config(workspace)
        protected = repo_config.architecture.protected_files if repo_config else []

        iteration_now = workspace.state.stage_iterations.get(stage_id, 0)
        max_iter = stage_def.max_iterations
        start_sha_short = (stage_start_commit or "unknown")[:8]
        logger.info(
            "Stage entry: stage=%s ticket=%s agent=%s iteration=%d/%s "
            "model=%s start_sha=%s protected_files=%d",
            stage_id, state.ticket_id, stage_def.agent, iteration_now,
            max_iter if max_iter > 0 else "uncapped",
            getattr(workspace.state, "model", "unknown"),
            start_sha_short, len(protected),
        )

        self._emit("agent_dispatched", f"Dispatching {stage_def.agent} for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id})
        state_before = workspace.state.current_state
        result = await self._agent_runtime.execute(
            stage_def.agent, workspace, protected_files=protected,
        )
        logger.info(
            "Stage exit: stage=%s ticket=%s agent=%s success=%s "
            "duration=%.1fs in_tok=%d out_tok=%d tool_calls=%d "
            "tool_rounds=%d failure_kind=%s",
            stage_id, state.ticket_id, stage_def.agent,
            getattr(result, "success", False),
            getattr(result, "duration_seconds", 0.0),
            getattr(result, "input_tokens", 0),
            getattr(result, "output_tokens", 0),
            getattr(result, "tool_calls", 0),
            getattr(result, "tool_rounds", 0),
            getattr(result, "failure_kind", None),
        )

        # Operator intervened mid-flight (Pause / Take Control / etc.).
        # Discard the agent's transitions so we don't auto-advance out of the
        # operator-chosen state.
        state_after = workspace.state.current_state
        if state_after != state_before:
            logger.info(
                "State changed during %s for %s: %s -> %s, discarding transitions",
                stage_def.agent, state.ticket_id, state_before, state_after,
            )
            return

        if not result.success:
            self._emit(
                "agent_failed",
                f"{stage_def.agent} failed for {state.ticket_id}: {result.error}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                agent_id=stage_def.agent,
                data={"stage": stage_id, "error": result.error},
            )
            if result.failure_kind == "quota":
                self._rollback_iteration(workspace, stage_id)
                retry_at = result.retry_at or (
                    datetime.now(timezone.utc) + DEFAULT_QUOTA_RETRY_DELAY
                )
                workspace.transition(Stage.DEFERRED, retry_at=retry_at.isoformat())
                await self._notify_deferred(workspace, retry_at, reason=result.error)
            else:
                workspace.transition(Stage.FAILED)
                workspace.update_state(error=result.error)
                await self._notify_failed(workspace, result.error or "")
            return

        self._emit("agent_completed", f"{stage_def.agent} completed for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id, "duration": result.duration_seconds, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
        sha_info = ""
        if stage_id == "dev":
            sha = self._git_head_sha(workspace)
            if sha != "unknown":
                sha_info = f" Commit: {sha[:8]}."
        self._log_pipeline(workspace, f"{stage_id} ({stage_def.agent}) completed.{sha_info} Output: `ai_pipeline/{state.ticket_id}/{stage_def.agent}-output.md`")
        verify_result = stage_verifier.verify(
            stage_id, workspace, stage_start_commit,
            duration_seconds=result.duration_seconds,
        )
        if not verify_result.ok:
            agent_snippet = (result.output or "")[:200].replace("\n", " ")
            error_msg = f"{stage_id}: {verify_result.reason} (agent said: {agent_snippet})"
            workspace.transition(Stage.BLOCKED)
            workspace.update_state(error=error_msg)
            self._log_pipeline(workspace, f"BLOCKED — {stage_id} verification failed: {verify_result.reason}")
            self._emit(
                "stage_verification_failed",
                f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"stage": stage_id, "reason": verify_result.reason},
            )
            await self._notify_verification_blocked(workspace, stage_id, verify_result.reason)
            return
        # Determine outcome from agent output
        outcome = self._parse_agent_outcome(stage_id, result.output, workspace)

        # Warn if QA passed but couldn't compile/test
        if stage_id == "qa" and outcome == "pass" and self._notifier:
            output_lower = (result.output or "").lower()
            warnings = []
            if "sdk" in output_lower and "not found" in output_lower:
                warnings.append("Android SDK not installed — build not verified")
            if "java" in output_lower and ("not found" in output_lower or "command not found" in output_lower):
                warnings.append("JDK not installed — tests not run")
            if "hold" in output_lower or "could not" in output_lower:
                if not warnings:
                    warnings.append("Quality gates could not run locally")
            if warnings:
                chat_id = self._get_chat_id(workspace)
                if chat_id:
                    title = tg_format.read_ticket_title(workspace)
                    hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                    await self._notifier.send_message(chat_id, (
                        f"{hdr}\n"
                        f"QA passed but with warnings:\n"
                        + "\n".join(f"  • {w}" for w in warnings)
                        + f"\n\nCI on GitHub will be the authoritative gate."
                    ))

        next_stage = get_next_stage(stage_id, self._workflow, outcome)

        # Reset scope_check bounce counter on pass so max_iterations tracks
        # consecutive failures only, not lifetime runs.
        if stage_id == "scope_check" and outcome == "pass":
            workspace.state.stage_iterations.pop("scope_check", None)
            workspace.save_state()

        if next_stage:
            # Check for approval gate in manual mode. Only gate on happy-path
            # transitions — failure loops and escalations bypass the gate.
            current_state = workspace.state.current_state
            if self._should_approval_gate(current_state, next_stage):
                workspace.transition(Stage.AWAITING_APPROVAL)
                self._emit("approval_requested", f"Awaiting approval for {state.ticket_id} after {current_state}", project_id=state.company_id, ticket_id=state.ticket_id, data={"gate": current_state})
                if self._notifier:
                    chat_id = self._get_chat_id(workspace)
                    summary, buttons = self._build_gate_summary(workspace, current_state)
                    await self._notifier.send_message(chat_id, summary, buttons=buttons)
            elif next_stage == "escalate":
                await self._handle_escalate(workspace)
            else:
                self._advance_to_stage(workspace, next_stage)
        else:
            workspace.transition(Stage.DONE)
            self._log_pipeline(workspace, f"✅ DONE. PR: {workspace.state.pr_url or 'N/A'}")
            await self._on_ticket_done(workspace)

    def _rollback_iteration(self, workspace: Workspace, stage_id: str) -> None:
        """Undo the iteration counter increment for an aborted stage run.

        Used when a quota failure preempts the agent before it produced output —
        the stage should not consume one of its retry budget slots.
        """
        state = workspace.state
        current = state.stage_iterations.get(stage_id, 0)
        if current > 0:
            state.stage_iterations[stage_id] = current - 1
            workspace.save_state()

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
        """Execute an action stage with capture → execute → verify → transition."""
        action = stage_def.action
        state = workspace.state

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would execute action '%s' for %s",
                action, state.ticket_id,
            )
            next_stage = get_next_stage(stage_id, self._workflow)
            if next_stage:
                self._advance_to_stage(workspace, next_stage)
            return

        if action == "notify_human":
            await self._handle_escalate(workspace)
            return

        stage_start_commit = stage_verifier.capture_stage_start(workspace, stage_id)
        workspace.increment_iteration(stage_id)

        if action == "push_and_open_pr":
            result = await self._action_push_and_open_pr(workspace)
        elif action == "fetch_pr_comments":
            result = await self._action_fetch_pr_comments(workspace, stage_def)
        elif action == "finalize":
            result = await self._action_finalize(workspace)
        else:
            logger.warning("Unknown action: %s", action)
            return

        if result.skipped:
            self._rollback_iteration(workspace, stage_id)
            return

        if not result.success:
            self._emit(
                "action_failed",
                f"Action {action} failed for {state.ticket_id}: {result.error}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"stage": stage_id, "error": result.error},
            )
            if action == "push_and_open_pr":
                # PR creation failures need operator attention — escalate so a
                # Telegram message is sent and the ticket can be recovered.
                workspace.update_state(error=result.error)
                await self._handle_escalate(workspace)
            else:
                workspace.transition(Stage.FAILED)
                workspace.update_state(error=result.error)
            return

        verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
        if not verify_result.ok:
            error_msg = f"{stage_id}: {verify_result.reason}"
            workspace.transition(Stage.BLOCKED)
            workspace.update_state(error=error_msg)
            self._emit(
                "stage_verification_failed",
                f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"stage": stage_id, "reason": verify_result.reason},
            )
            return

        if result.metadata:
            workspace.update_state(**result.metadata)

        current_state = workspace.state.current_state
        # Only gate on forward transitions (DONE, PR_REVIEW, etc.), not fix loops back to DEV
        if result.next_state != Stage.DEV and self._should_approval_gate(current_state):
            workspace.transition(Stage.AWAITING_APPROVAL)
            self._emit(
                "approval_requested",
                f"Awaiting approval for {state.ticket_id} after {current_state}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"gate": current_state},
            )
            if self._notifier:
                chat_id = self._get_chat_id(workspace)
                summary, buttons = self._build_gate_summary(workspace, current_state)
                await self._notifier.send_message(chat_id, summary, buttons=buttons)
            return

        self._emit(
            "stage_transition",
            f"{state.ticket_id}: {current_state} -> {result.next_state}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            data={"from_state": current_state, "to_state": result.next_state},
        )
        workspace.transition(result.next_state)

        if result.next_state == Stage.DONE:
            await self._on_ticket_done(workspace)

        meta_summary = ""
        if result.metadata.get("pr_url"):
            meta_summary = f" PR: {result.metadata['pr_url']}"
        self._log_pipeline(workspace, f"{action} completed.{meta_summary} → {result.next_state}")
        self._emit(
            "action_completed",
            f"Action {action} completed for {state.ticket_id}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            data={"stage": stage_id, **result.metadata},
        )

        # Notify when PR is created — user needs to review and reply
        if action == "push_and_open_pr" and result.metadata.get("pr_url") and self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                pr_url = result.metadata["pr_url"]
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("🔗", state.company_id, state.ticket_id, title)
                msg = (
                    f"{hdr}\n"
                    f"PR opened: {pr_url}\n\n"
                    f"Review the diff and merge when ready. The pipeline will wait.\n\n"
                    f"If there are review comments, Cleave will escalate them one by one "
                    f"for your decision (Fix or Won't Fix). Reply to any escalation message "
                    f"to provide context.\n\n"
                    f"When done: tap Review Complete or reply to this message."
                )
                pr_buttons = [Button(label="Review Complete", action=f"reviewed:{state.ticket_id}")]
                msg_id = await self._notifier.send_message(chat_id, msg, buttons=pr_buttons)
                workspace.state.escalation_msg_id = msg_id
                workspace.state.escalation_chat_id = chat_id
                workspace.state.human_input_reply = None  # clear stale "reviewed" from any prior run
                workspace.save_state()

    async def _action_push_and_open_pr(self, workspace: Workspace) -> ActionResult:
        """Push branch and open PR. Returns ActionResult — caller transitions."""
        state = workspace.state

        # If PR already exists (from a previous cycle), just push new commits (no squash)
        if state.pr_number and state.pr_url:
            vcs, repo_config = self._get_vcs_for_workspace(workspace)
            if vcs and repo_config:
                self._commit_pipeline_artifacts(workspace, repo_config)
                try:
                    branch = state.branch
                    if branch:
                        skip_hooks = bool(getattr(repo_config.vcs, "skip_pre_push_hook", False))
                        await vcs.push(
                            str(workspace.source_dir), branch,
                            force=True, skip_hooks=skip_hooks,
                        )
                        logger.info("Pushed updates to existing PR #%d for %s", state.pr_number, state.ticket_id)
                except Exception as e:
                    logger.warning("Failed to push to existing PR: %s", e)

            # Verify PENDING entries in resolution report after push
            from orchestrator.resolution_report import read_entries, update_entry

            report_path = workspace.reports_dir / "pr-review-resolution.md"
            entries = read_entries(report_path)
            changed_files = self._git_diff_files(workspace)
            sha = self._git_head_sha(workspace)

            for cid, entry in entries.items():
                if entry.get("verified") != "PENDING":
                    continue
                file_path = entry.get("file", "")
                if file_path in changed_files:
                    # File was touched in this push — resolve
                    if vcs:
                        try:
                            await vcs.reply_to_comment(state.pr_number, cid, f"Fixed in commit {sha[:8]}")
                            await vcs.resolve_comment(state.pr_number, cid)
                            logger.info("Resolved comment %d after push (commit %s)", cid, sha[:8])
                        except Exception as e:
                            logger.warning("Failed to resolve comment %d: %s", cid, e)
                    update_entry(report_path, cid, {
                        "verified": "YES",
                        "fixed_in": sha[:8],
                        "verified_at": self._now(),
                    })
                else:
                    # File NOT in diff — increment fail count
                    fail_count = int(entry.get("fail_count", "0")) + 1
                    update_entry(report_path, cid, {
                        "verified": "FAILED",
                        "fail_count": str(fail_count),
                    })
                    if fail_count >= 2 and self._notifier:
                        chat_id = self._get_chat_id(workspace)
                        if chat_id:
                            title = tg_format.read_ticket_title(workspace)
                            hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                            await self._notifier.send_message(
                                chat_id,
                                f"{hdr}\n"
                                f"Dev-agent failed to apply the fix for comment #{cid} twice.\n\n"
                                f"File: {entry.get('file', '?')}:{entry.get('line', '?')}\n\n"
                                f"Options:\n"
                                f"- Reply \"fix\" to retry once more\n"
                                f"- Reply \"won't fix: <reason>\" to close the comment without fixing",
                            )

            return ActionResult(
                success=True, next_state=Stage.PR_REVIEW, error="",
                metadata={"pr_url": state.pr_url, "pr_number": state.pr_number},
            )

        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs or not repo_config:
            logger.error("No VCS configured for %s", state.repo_id)
            return ActionResult(
                success=False, next_state="", error="No VCS adapter configured",
                metadata={},
            )

        # Defensive: rescue branches that have staged dev work but zero
        # committed commits (typical aftermath of a partially-failed squash
        # before the ea8819c atomicity fix; also covers any future git
        # operation that leaves the branch in this state). Without this,
        # the next push opens an empty PR (GitHub 422 "No commits between
        # develop and feature/...") and operators have to commit by hand.
        self._ensure_branch_has_commits(workspace, repo_config)

        # Commit any pipeline artifacts the agents wrote after dev's commit
        # (scope-guard, qa) so they ride along on this push. The squash below
        # folds them into the single PR commit.
        self._commit_pipeline_artifacts(workspace, repo_config)

        # Squash commits into one clean commit before the first PR
        self._squash_feature_commits(workspace, repo_config)

        result = await create_pr(workspace, vcs, self._tracker, repo_config, event_bus=self._events)
        if result.success:
            return ActionResult(
                success=True, next_state=Stage.PR_REVIEW, error="",
                metadata={"pr_url": result.pr_url, "pr_number": result.pr_number},
            )
        return ActionResult(
            success=False, next_state="", error=result.error, metadata={},
        )

    def _commit_pipeline_artifacts(
        self, workspace: Workspace, repo_config: RepoConfig,
    ) -> None:
        """Stage and commit any uncommitted files under `ai_pipeline/<ticket>/`.

        Run before each push so agent reports written after the dev commit
        (scope-guard, qa, pr-review-*) ride along with the code on the PR.
        No-op when nothing is staged. On the first push the orchestrator
        squashes feature commits, so this commit gets folded into the PR's
        single squash commit. On subsequent pushes (PR-review cycle) it lands
        as its own `chore({ticket}): pipeline artifacts` commit.
        """
        import subprocess
        source = str(workspace.source_dir)
        state = workspace.state
        rel_dir = f"ai_pipeline/{state.ticket_id}"

        if not (workspace.source_dir / "ai_pipeline" / state.ticket_id).exists():
            return

        try:
            add_result = subprocess.run(
                ["git", "-C", source, "add", "--", rel_dir],
                capture_output=True, text=True, timeout=10,
            )
            if add_result.returncode != 0:
                logger.warning(
                    "Failed to git-add pipeline artifacts for %s: %s",
                    state.ticket_id, add_result.stderr.strip()[:200],
                )
                return

            staged = subprocess.run(
                ["git", "-C", source, "diff", "--cached", "--name-only", "--", rel_dir],
                capture_output=True, text=True, timeout=10,
            )
            files = [line for line in staged.stdout.splitlines() if line.strip()]
            if not files:
                return  # nothing to commit

            commit_msg = f"chore({state.ticket_id}): pipeline artifacts"
            commit_result = subprocess.run(
                [
                    "git", "-C", source,
                    "-c", f"user.email={repo_config.git.commit_author_email}",
                    "-c", f"user.name={repo_config.git.commit_author_name}",
                    "commit", "-m", commit_msg, "--", rel_dir,
                ],
                capture_output=True, text=True, timeout=10,
            )
            if commit_result.returncode != 0:
                logger.warning(
                    "Failed to commit pipeline artifacts for %s: %s",
                    state.ticket_id, commit_result.stderr.strip()[:200],
                )
                return

            logger.info(
                "Committed %d pipeline artifact(s) for %s",
                len(files), state.ticket_id,
            )
        except Exception as e:
            logger.warning("Pipeline-artifacts commit failed for %s: %s", state.ticket_id, e)

    def _ensure_branch_has_commits(
        self, workspace: Workspace, repo_config: RepoConfig,
    ) -> None:
        """If the feature branch has 0 commits ahead of remotes but the
        index has staged tracked changes, commit them so the upcoming push
        actually has something to send.

        Prevents the GitHub 422 "No commits between develop and feature/..."
        failure that occurs when an earlier git step (e.g. a non-atomic
        squash before ea8819c) reset the branch to base but never recorded
        the follow-up commit. Idempotent: a no-op when the branch already
        has commits or when there is no staged work to recover.

        Stays narrow on purpose:
          * commits ONLY what is already in the index (does not run
            `git add` — won't sweep up untracked clutter or files agents
            chose not to stage)
          * uses repo_config author (same as `_squash_feature_commits`)
          * emits `branch_recovered_from_orphan_state` so the recovery is
            visible in events.db rather than silent
        """
        import subprocess
        source = str(workspace.source_dir)
        state = workspace.state

        try:
            count_result = subprocess.run(
                ["git", "-C", source, "rev-list", "--count", "HEAD", "--not", "--remotes"],
                capture_output=True, text=True, timeout=10,
            )
            if count_result.returncode != 0:
                return
            if int(count_result.stdout.strip() or "0") > 0:
                return  # branch has commits — happy path

            # Zero commits ahead. Anything in the index?
            status = subprocess.run(
                ["git", "-C", source, "diff", "--cached", "--name-only"],
                capture_output=True, text=True, timeout=10,
            )
            staged = [line for line in status.stdout.splitlines() if line.strip()]
            if not staged:
                return  # truly nothing to recover

            commit_msg = f"feat({state.ticket_id}): recovered work after orphaned-branch state"
            commit_cmd = [
                "git", "-C", source,
                "-c", f"user.email={repo_config.git.commit_author_email}",
                "-c", f"user.name={repo_config.git.commit_author_name}",
                "commit", "-m", commit_msg,
            ]
            commit_result = subprocess.run(
                commit_cmd, capture_output=True, text=True, timeout=10,
            )
            if commit_result.returncode != 0:
                logger.error(
                    "Failed to recover orphaned branch for %s: %s",
                    state.ticket_id, commit_result.stderr.strip()[:500],
                )
                return

            logger.warning(
                "Recovered %d staged file(s) for %s — branch had 0 commits ahead",
                len(staged), state.ticket_id,
            )
            self._emit(
                "branch_recovered_from_orphan_state",
                f"Recovered {len(staged)} staged file(s) for {state.ticket_id}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"file_count": len(staged), "files": staged[:20]},
            )
        except Exception as e:
            logger.warning("Branch-recovery check failed for %s: %s", state.ticket_id, e)

    def _squash_feature_commits(
        self, workspace: Workspace, repo_config: RepoConfig | None = None,
    ) -> None:
        """Squash all commits on the feature branch into one clean commit.

        Keeps the first commit's message (the feat(...) one). This removes
        noise from scope-guard fix cycles and QA retry loops.

        Atomic: if the post-reset commit fails (e.g. global git config
        missing user.email so git refuses to record an author), the
        function rolls the branch back to its original HEAD with `reset
        --hard`. Without this rollback, a failed squash leaves the branch
        empty and the next push opens a 0-commit PR.
        """
        import subprocess
        source = str(workspace.source_dir)
        state = workspace.state

        try:
            # Count commits ahead of origin/develop (or whatever the base is)
            result = subprocess.run(
                ["git", "-C", source, "rev-list", "--count", "HEAD", "--not", "--remotes"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return
            count = int(result.stdout.strip() or "0")
            if count <= 1:
                return  # Nothing to squash

            # Capture HEAD so we can roll back if the squash commit fails.
            head_before = subprocess.run(
                ["git", "-C", source, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if head_before.returncode != 0:
                return
            old_head = head_before.stdout.strip()

            # Get the first (oldest) commit message on the branch
            result = subprocess.run(
                ["git", "-C", source, "log", "--reverse", "--format=%s", f"HEAD~{count}..HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return
            messages = result.stdout.strip().splitlines()
            commit_msg = messages[0] if messages else f"feat({state.ticket_id}): changes"

            # Build commit cmd with explicit author so git doesn't refuse
            # when the global gitconfig is missing user.email/user.name.
            commit_cmd = ["git", "-C", source]
            if repo_config is not None:
                commit_cmd += [
                    "-c", f"user.email={repo_config.git.commit_author_email}",
                    "-c", f"user.name={repo_config.git.commit_author_name}",
                ]
            commit_cmd += ["commit", "-m", commit_msg]

            # Soft reset to squash
            subprocess.run(
                ["git", "-C", source, "reset", "--soft", f"HEAD~{count}"],
                check=True, capture_output=True, timeout=10,
            )
            try:
                subprocess.run(
                    commit_cmd, check=True, capture_output=True, timeout=10,
                )
            except subprocess.CalledProcessError as commit_err:
                # Rollback: restore the original commit chain. Working tree
                # was preserved by --soft, but if commit refused to record
                # the squashed change we must restore HEAD or push opens an
                # empty PR.
                logger.error(
                    "Squash commit failed for %s; rolling back. stderr=%s",
                    state.ticket_id,
                    commit_err.stderr.decode(errors="replace")[:500] if commit_err.stderr else "",
                )
                subprocess.run(
                    ["git", "-C", source, "reset", "--hard", old_head],
                    check=False, capture_output=True, timeout=10,
                )
                return
            logger.info("Squashed %d commits into one for %s: %s", count, state.ticket_id, commit_msg)
        except Exception as e:
            logger.warning("Failed to squash commits for %s: %s", state.ticket_id, e)

    async def _reinvestigate_pending(self, workspace: Workspace) -> None:
        """Process any pending re-investigation requests on this workspace.

        For each entry with pending_reinvestigation=True (and not decided), call
        the classifier with operator_hint, update the entry in place, and re-send
        a fresh escalated TG message. Always re-escalates — never acts silently
        after a hint.
        """
        import sys
        _mod = sys.modules[__name__]
        classify_fn = getattr(_mod, "classify_comments", None)
        if classify_fn is None:
            from orchestrator.comment_classifier import classify_comments as classify_fn
        from orchestrator.resolution_report import update_entry

        state = workspace.state
        pending = state.pending_review_comments or []
        if not pending:
            return
        pr_number = state.pr_number
        if not pr_number:
            return

        for c in pending:
            if not c.get("pending_reinvestigation"):
                continue
            if c.get("decision") is not None:
                # Operator decided via button while this was queued — clear the flag
                c["pending_reinvestigation"] = False
                workspace.save_state()
                continue
            comment_stub = SimpleNamespace(
                id=c["comment_id"], author=c.get("author", ""),
                path=c.get("file", ""), line=c.get("line"),
                body=c.get("body", ""),
            )
            old_verdict = c.get("verdict")
            old_classification = "ESCALATE"
            try:
                classified = await classify_fn(
                    [comment_stub], workspace, self._agent_runtime,
                    operator_hint=c.get("last_hint") or "",
                )
            except Exception as e:
                logger.error("Re-investigation failed for comment %s: %s", c["comment_id"], e)
                retry_count = int(c.get("reinvestigation_retry_count", 0) or 0)
                if retry_count < 1:
                    # First failure — leave flag set so next tick retries
                    c["reinvestigation_retry_count"] = retry_count + 1
                    workspace.save_state()
                    continue
                # Second failure — surface and clear
                if self._notifier:
                    chat_id = self._get_chat_id(workspace)
                    if chat_id:
                        title = tg_format.read_ticket_title(workspace)
                        hdr = tg_format.tg_header("⚠️", workspace.state.company_id, workspace.state.ticket_id, title)
                        await self._notifier.send_message(
                            chat_id,
                            f"{hdr}\n"
                            f"Re-investigation failed for @{c.get('author','?')}'s comment "
                            f"on {c.get('file','?')}:{c.get('line','?')}\n\n"
                            f"The agent was unable to re-classify this comment after your hint. Options:\n"
                            f"- Reply \"fix\" to send the dev-agent in anyway\n"
                            f"- Reply \"won't fix: <reason>\" to close the comment on GitHub",
                        )
                c["pending_reinvestigation"] = False
                c["reinvestigation_retry_count"] = 0
                workspace.save_state()
                continue

            if not classified:
                logger.warning("Re-investigation returned no result for comment %s", c["comment_id"])
                c["pending_reinvestigation"] = False
                workspace.save_state()
                continue

            cc = classified[0]
            c["verdict"] = cc.verdict
            c["reason"] = cc.reason
            # Note: classification can change (e.g., ESCALATE → AUTO_FIX), but we
            # always re-escalate for human confirmation. We don't auto-act.
            c["hint_rounds"] = int(c.get("hint_rounds", 0) or 0) + 1
            c["reinvestigation_retry_count"] = 0
            c["pending_reinvestigation"] = False
            report_path = workspace.reports_dir / "pr-review-resolution.md"
            update_entry(report_path, c["comment_id"], {
                "verdict": cc.verdict,
                "reason": cc.reason,
                "hint_round": str(c["hint_rounds"]),
                "hint": (c.get("last_hint") or "")[:200],
            })
            workspace.save_state()

            new_msg_id = await self._send_escalated_comment_tg(workspace, cc, pr_number)
            if new_msg_id:
                c.setdefault("msg_ids", []).append(new_msg_id)
                workspace.save_state()

            if self._events is not None:
                self._emit(
                    "pr_comment_reinvestigation_completed",
                    f"{state.ticket_id}: re-checked comment {c['comment_id']}",
                    project_id=state.company_id, ticket_id=state.ticket_id,
                    data={
                        "comment_id": c["comment_id"],
                        "hint_round": c["hint_rounds"],
                        "old_verdict": old_verdict,
                        "new_verdict": cc.verdict,
                        "old_classification": old_classification,
                        "new_classification": cc.classification,
                    },
                )

    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> ActionResult:
        """PR review comment resolution flow.

        Uses resolution_report as the single source of truth for comment
        decisions and verification state.
        """
        from orchestrator.comment_classifier import classify_comments
        from orchestrator.resolution_report import read_entries, add_entry, update_entry

        state = workspace.state
        pr_number = state.pr_number
        report_path = workspace.reports_dir / "pr-review-resolution.md"

        if not pr_number:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        # Phase 1: Check PENDING verifications from previous cycle
        entries = read_entries(report_path)
        pending_verify = {
            cid: e for cid, e in entries.items()
            if e.get("verified") == "PENDING"
        }
        if pending_verify:
            sha = self._git_head_sha(workspace)
            last_verified = state.last_verified_sha or ""
            changed_files = self._git_diff_files(workspace, since_sha=last_verified)
            for cid, entry in pending_verify.items():
                file_path = entry.get("file", "")
                if file_path in changed_files:
                    vcs, _ = self._get_vcs_for_workspace(workspace)
                    if vcs:
                        try:
                            await vcs.reply_to_comment(pr_number, cid, f"Fixed in commit {sha[:8]}")
                            await vcs.resolve_comment(pr_number, cid)
                        except Exception as e:
                            logger.warning("Failed to resolve comment %d: %s", cid, e)
                    update_entry(report_path, cid, {
                        "verified": "YES",
                        "fixed_in": sha[:8],
                        "verified_at": self._now(),
                    })
                else:
                    fail_count = int(entry.get("fail_count", "0")) + 1
                    update_entry(report_path, cid, {
                        "verified": "FAILED",
                        "fail_count": str(fail_count),
                    })
                    if fail_count >= 2 and self._notifier:
                        chat_id = self._get_chat_id(workspace)
                        if chat_id:
                            title = tg_format.read_ticket_title(workspace)
                            hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                            await self._notifier.send_message(
                                chat_id,
                                f"{hdr}\n"
                                f"Dev-agent failed to apply the fix for comment #{cid} twice.\n\n"
                                f"File: {entry.get('file', '?')}:{entry.get('line', '?')}\n\n"
                                f"Options:\n"
                                f"- Reply \"fix\" to retry once more\n"
                                f"- Reply \"won't fix: <reason>\" to close the comment without fixing",
                            )
            # Advance the cursor so the next cycle diffs from here
            state.last_verified_sha = sha
            workspace.save_state()

        # Phase 1.5: Process any pending re-investigations from operator hints
        await self._reinvestigate_pending(workspace)

        # Phase 2: Check pending escalated decisions
        pending = state.pending_review_comments or []
        undecided = [c for c in pending if c.get("decision") is None]

        if pending and not undecided:
            return await self._execute_review_decisions(workspace)

        if undecided:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        # Phase 3: Wait for 'reviewed' signal
        reply = (state.human_input_reply or "").lower()
        if "reviewed" not in reply and "proceed" not in reply:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        state.human_input_reply = None
        state.review_cycle = (state.review_cycle or 0) + 1
        state.stage_iterations["pr_review"] = 0
        workspace.save_state()

        # Phase 4: Fetch comments, filter out already-decided (by comment ID in resolution report)
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        try:
            all_comments = await vcs.get_pr_comments(pr_number)
        except Exception as e:
            logger.error("Failed to fetch PR comments for %s: %s", state.ticket_id, e)
            return ActionResult(success=False, next_state="", error=f"Failed to fetch: {e}", metadata={})

        # Filter: only root comments not already in the resolution report
        decided_ids = set(entries.keys())
        replied_to_ids = set()
        for c in all_comments:
            if c.in_reply_to_id and c.body.strip().lower().startswith(("won't fix", "wont fix", "fixed")):
                replied_to_ids.add(c.in_reply_to_id)

        comments = [
            c for c in all_comments
            if not c.in_reply_to_id
            and c.id not in replied_to_ids
            and c.id not in decided_ids
        ]
        logger.info(
            "PR #%d: %d total, %d already decided, %d replied, %d new to process",
            pr_number, len(all_comments), len(decided_ids), len(replied_to_ids), len(comments),
        )

        if not comments:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        # Write comments for reference
        comment_md = "# PR Review Comments\n\n"
        for c in comments:
            comment_md += f"## Comment by {c.author}\n"
            if c.path:
                comment_md += f"File: `{c.path}`"
                if c.line:
                    comment_md += f" (line {c.line})"
                comment_md += "\n"
            comment_md += f"\n{c.body}\n\n---\n\n"
        (workspace.reports_dir / "pr-review-comments.md").write_text(comment_md, encoding="utf-8")

        # Phase 5: Classify new comments, write to resolution report
        classified = await classify_comments(comments, workspace, self._agent_runtime)

        auto_fixed, auto_rejected, escalated = [], [], []
        for cc in classified:
            if cc.classification == "AUTO_FIX":
                github_reply_status = "Posted (will fix)"
                if vcs:
                    try:
                        await vcs.reply_to_comment(
                            pr_number, cc.comment_id, f"Will fix: {cc.reason}",
                        )
                    except Exception as e:
                        logger.warning("Failed to post 'Will fix' on comment %d: %s", cc.comment_id, e)
                        github_reply_status = f"Failed: {str(e)[:120]}"
                add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                    "classification": "AUTO_FIX",
                    "verdict": cc.verdict,
                    "file": cc.file or "",
                    "line": str(cc.line or "?"),
                    "author": cc.author or "",
                    "reason": cc.reason or "",
                    "verified": "PENDING",
                    "github_reply": github_reply_status,
                    "fail_count": "0",
                    "cycle": str(state.review_cycle),
                })
                auto_fixed.append(cc)
            elif cc.classification == "AUTO_REJECT":
                # Phase 6: AUTO_REJECT replies + resolves immediately
                github_reply_status = "Posted"
                resolved_status = "YES"
                try:
                    await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
                except Exception as e:
                    logger.warning("Failed to reply on comment %d: %s", cc.comment_id, e)
                    github_reply_status = f"Failed: {str(e)[:120]}"
                    resolved_status = "NO"
                try:
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to resolve comment %d: %s", cc.comment_id, e)
                    resolved_status = "NO"
                add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                    "classification": "AUTO_REJECT",
                    "verdict": cc.verdict,
                    "file": cc.file or "",
                    "line": str(cc.line or "?"),
                    "author": cc.author or "",
                    "reason": cc.reason or "",
                    "verified": "N/A",
                    "github_reply": github_reply_status,
                    "resolved": resolved_status,
                    "cycle": str(state.review_cycle),
                })
                auto_rejected.append(cc)
            else:
                # ESCALATE goes to TG
                escalated.append(cc)

        # TG summary for auto-handled
        if (auto_fixed or auto_rejected) and self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                sep = "─" * 30
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("🤖", state.company_id, state.ticket_id, title)
                sep = "─" * 30
                lines = [hdr]
                lines.append(f"PR #{pr_number} — Auto-processed {len(auto_fixed) + len(auto_rejected)} comment(s):")
                lines.append(sep)
                for af in auto_fixed:
                    lines.append(f"✅ FIX: {af.reason} ({af.file}:{af.line or '?'})")
                for ar in auto_rejected:
                    lines.append(f"❌ REJECT: {ar.body[:60]} — {ar.reason}")
                lines.append(sep)
                summary_buttons = None
                if escalated:
                    lines.append(f"Waiting for your decisions on {len(escalated)} escalated comment(s).")
                    summary_buttons = [
                        Button(label=f"Show {len(escalated)} unanswered", action=f"unanswered:{state.ticket_id}"),
                    ]
                await self._notifier.send_message(chat_id, "\n".join(lines), buttons=summary_buttons)

        # Phase 7: Handle escalated or collect FIX items
        if not escalated:
            summary = f"PR review cycle {state.review_cycle}: {len(auto_fixed)} fix, {len(auto_rejected)} rejected"
            self._log_pipeline(workspace, f"{summary}. Report: `ai_pipeline/{state.ticket_id}/pr-review-resolution.md`")
            if auto_fixed:
                # Write fix instructions for the dev agent
                fix_md = "# PR Comment Fixes Required\n\n"
                for af in auto_fixed:
                    fix_md += f"## Fix: {af.file}:{af.line or '?'}\n"
                    fix_md += f"Comment by @{af.author}: {af.body[:200]}\n"
                    fix_md += f"What to do: {af.suggested_fix or af.reason}\n\n"
                (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")
                for stage in ("dev", "scope_check", "qa"):
                    state.stage_iterations.pop(stage, None)
                workspace.save_state()
                return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        # Store escalated with TG msg_ids
        pending_comments = []
        title = tg_format.read_ticket_title(workspace)
        for cc in escalated:
            msg_id = await self._send_escalated_comment_tg(workspace, cc, pr_number)
            pending_comments.append({
                "comment_id": cc.comment_id, "msg_ids": [msg_id], "decision": None,
                "author": cc.author, "file": cc.file, "line": cc.line,
                "body": cc.body, "reason": cc.reason,
                "verdict": cc.verdict,
                "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
                "ticket_title": title,
            })
            add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                "classification": "ESCALATE",
                "verdict": cc.verdict,
                "file": cc.file or "",
                "line": str(cc.line or "?"),
                "author": cc.author or "",
                "reason": cc.reason or "",
                "verified": "N/A",
                "decision": "PENDING_HUMAN",
                "cycle": str(state.review_cycle),
            })

        state.pending_review_comments = pending_comments
        workspace.save_state()
        return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

    async def _send_escalated_comment_tg(self, workspace: Workspace, cc: Any, pr_number: int) -> int:
        """Send a single escalated comment to TG. Returns the message ID."""
        from orchestrator.escalation_view import build_escalated_comment_message

        state = workspace.state
        title = tg_format.read_ticket_title(workspace)
        text, buttons = build_escalated_comment_message(
            state, cc, pr_number, ticket_title=title,
        )
        chat_id = self._get_chat_id(workspace)
        if chat_id and self._notifier:
            return await self._notifier.send_message(chat_id, text, buttons=buttons)
        return 0

    async def _execute_review_decisions(self, workspace: Workspace) -> ActionResult:
        """Execute all pending review decisions.

        Writes decisions to resolution report via add_entry/update_entry.
        WON'T_FIX replies + resolves immediately. FIX gets PENDING entry.
        SKIP gets recorded.
        """
        from orchestrator.resolution_report import update_entry

        state = workspace.state
        pending = state.pending_review_comments or []
        pr_number = state.pr_number
        report_path = workspace.reports_dir / "pr-review-resolution.md"

        vcs, _ = self._get_vcs_for_workspace(workspace)
        fixes_needed = []
        wont_fix = []
        skipped_comments = []

        def _is_fix(d: str) -> bool:
            """Match 'fix' with common typos."""
            d = d.lower().strip()
            return d in ("fix", "fxi", "fifx", "fixx", "fx", "yes", "fix it")

        for c in pending:
            decision = (c.get("decision") or "").lower().strip()
            cid = c["comment_id"]
            if _is_fix(decision):
                fixes_needed.append(c)
                github_reply_status = "Posted (will fix)"
                if vcs and pr_number:
                    try:
                        await vcs.reply_to_comment(
                            pr_number, cid, f"Will fix: {c.get('reason', 'operator decision')}",
                        )
                    except Exception as e:
                        logger.warning("Failed to post 'Will fix' on comment %d: %s", cid, e)
                        github_reply_status = f"Failed: {str(e)[:120]}"
                update_entry(report_path, cid, {
                    "verdict": c.get("verdict", "Unsure"),
                    "decision": "FIX",
                    "verified": "PENDING",
                    "github_reply": github_reply_status,
                    "fail_count": "0",
                    "decided_at": self._now(),
                })
            elif decision.startswith("won't fix") or decision.startswith("wont fix"):
                reason = decision.split(":", 1)[1].strip() if ":" in decision else "Operator decision"
                wont_fix.append({**c, "wont_fix_reason": reason})
                github_reply_status = "Posted"
                resolved_status = "YES"
                if vcs and pr_number:
                    try:
                        await vcs.reply_to_comment(pr_number, cid, f"Won't fix: {reason}")
                    except Exception as e:
                        logger.warning("Failed to reply on comment %d: %s", cid, e)
                        github_reply_status = f"Failed: {str(e)[:120]}"
                        resolved_status = "NO"
                    try:
                        await vcs.resolve_comment(pr_number, cid)
                    except Exception as e:
                        logger.warning("Failed to resolve comment %d: %s", cid, e)
                        resolved_status = "NO"
                update_entry(report_path, cid, {
                    "verdict": c.get("verdict", "Unsure"),
                    "decision": "WON'T_FIX",
                    "verified": "N/A",
                    "github_reply": github_reply_status,
                    "resolved": resolved_status,
                    "decided_at": self._now(),
                })
            else:
                # "Skip" (or any unrecognized free-text reply) means "drop from
                # pending, no GitHub action, don't nag me again". The PR
                # conversation stays open on GitHub — the operator can revisit
                # via the GitHub UI later — but the pipeline does not
                # re-escalate every 30 min for a comment the operator
                # already saw and chose to ignore.
                skipped_comments.append(c)
                update_entry(report_path, cid, {
                    "decision": "SKIP",
                    "resolved": "NO",
                    "decided_at": self._now(),
                })

        if fixes_needed:
            fix_md = "# PR Comment Fixes Required\n\n"
            for f in fixes_needed:
                fix_md += f"## Fix: {f['file']}:{f.get('line', '?')}\n"
                fix_md += f"Comment by @{f['author']}: {f['body'][:200]}\n"
                fix_md += f"Reason: {f['reason']}\n\n"
            (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")

        state.pending_review_comments = None
        if fixes_needed:
            for stage in ("dev", "scope_check", "qa"):
                state.stage_iterations.pop(stage, None)
        workspace.save_state()

        if fixes_needed:
            return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})

        # Skipped comments → AWAITING_APPROVAL, NOT silent DONE. Marking a
        # ticket DONE while review comments are still open on the PR hides
        # incomplete work. Instead we hand the decision back to the operator
        # explicitly: Approve to merge as-is (comments stay open on the PR
        # for manual follow-up) or Reject to send the workspace back so the
        # dev agent can re-engage. One TG message, then silence — no
        # 30-minute re-escalation loop.
        if skipped_comments:
            if self._notifier:
                chat_id = self._get_chat_id(workspace)
                if chat_id:
                    sep = "─" * 30
                    title = tg_format.read_ticket_title(workspace)
                    hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
                    lines = [hdr]
                    lines.append(f"{len(skipped_comments)} comment(s) marked Skip — still open on the PR:")
                    for sc in skipped_comments:
                        lines.append(f"  • @{sc.get('author','?')} on {sc.get('file','?')}:{sc.get('line','?')}")
                    lines.append(sep)
                    lines.append(
                        "Approve → mark DONE, leave comments open on GitHub for manual follow-up.\n"
                        "Reject → reopen for dev-agent to address the comments."
                    )
                    buttons = [
                        Button(label="Approve", action=f"approve:{state.ticket_id}"),
                        Button(label="Reject", action=f"reject:{state.ticket_id}"),
                    ]
                    try:
                        await self._notifier.send_message(
                            chat_id, "\n".join(lines), buttons=buttons,
                        )
                    except Exception as e:
                        logger.warning("Failed to send PR-review pause: %s", e)
            return ActionResult(
                success=True, next_state=Stage.AWAITING_APPROVAL, error="", metadata={},
            )

        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})


    async def _on_ticket_done(self, workspace: Workspace) -> None:
        """Handle ticket completion: TG notification + Jira status transition."""
        state = workspace.state

        # TG notification
        if self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("✅", state.company_id, state.ticket_id, title)
                await self._notifier.send_message(chat_id, (
                    f"{hdr}\n"
                    f"Pipeline complete.\n\n"
                    f"PR ready for merge: {state.pr_url or 'N/A'}\n\n"
                    f"Jira ticket moved to review status."
                ))

        # Transition tracker ticket to in-review status (Jira: "In Review",
        # Trello: a list-name match). Fuzzy keywords are pipeline policy and
        # stay on this side of the port.
        if self._tracker:
            project = self._projects.get(state.company_id)
            if project:
                target_status = project.config.jira.statuses.in_review
                if target_status:
                    try:
                        available = await self._tracker.list_transitions(
                            state.ticket_id,
                        )
                        matched = None
                        target_lower = target_status.lower()
                        for name in available:
                            if target_lower in name.lower():
                                matched = name
                                break
                        if matched is None:
                            for name in available:
                                if any(kw in name.lower() for kw in (
                                    "review", "qa", "verification", "ready for qa",
                                )):
                                    matched = name
                                    break
                        if matched is not None:
                            await self._tracker.transition_ticket(
                                state.ticket_id, matched,
                            )
                            logger.info(
                                "Transitioned %s to '%s'", state.ticket_id, matched,
                            )
                        else:
                            logger.warning(
                                "Cannot transition %s to '%s' — available: %s",
                                state.ticket_id, target_status, available,
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to transition %s on tracker: %s",
                            state.ticket_id, e,
                        )

    _BLOCKED_REASON_MAX_CHARS = 800

    _BOILERPLATE_LINE_PATTERNS = (
        re.compile(r"^-{3,}$"),
        re.compile(r"^={3,}$"),
        re.compile(r"^\*\*Attempt.*\*\*$"),
        re.compile(r"^## Decision:"),
    )

    def _build_blocked_reason(self, workspace: Any, stage_id: str) -> str:
        """Extract a human-readable reason for why a workspace is blocked.

        For analysis: prefer the BA agent's numbered questions
        (`ai_pipeline/<ticket>/ba-questions.md`). For other stages: prefer the
        stage-specific runtime output file (e.g. `qa-agent-output.md` for the
        qa stage) so that a different stage's more-recent output is never
        mistakenly shown as the block reason. Falls back to a generic message.
        """
        reports = workspace.reports_dir
        ticket_id = workspace.state.ticket_id
        artifact_path = f"ai_pipeline/{ticket_id}/"
        if not reports.exists():
            return f"Pipeline stuck at {stage_id}. Check {artifact_path} for details."

        if stage_id == "analysis":
            questions = reports / REPORT_BA_QUESTIONS
            if questions.exists():
                text = questions.read_text(encoding="utf-8").strip()
                if text:
                    return self._truncate_reason(text)

        # Prefer the runtime output file for this specific stage so we never
        # show a different stage's (e.g. scope-guard) output when QA is blocked.
        stage_output = None
        runtime_filename = STAGE_RUNTIME_OUTPUT.get(stage_id)
        if runtime_filename:
            candidate = reports / runtime_filename
            if candidate.exists():
                stage_output = candidate
            else:
                return f"{stage_id} agent produced no output (may have timed out or crashed). Check pipeline logs."

        if stage_output is None:
            # Stage has no known agent output — don't show an unrelated stage's file.
            return f"Pipeline stuck at {stage_id}. Check pipeline logs for details."

        raw = stage_output.read_text(encoding="utf-8")
        # Strip leading boilerplate and blank lines.
        lines = raw.splitlines()
        start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if any(p.match(stripped) for p in self._BOILERPLATE_LINE_PATTERNS):
                continue
            start = i
            break
        else:
            return f"Pipeline stuck at {stage_id}. Check {artifact_path} for details."

        body = "\n".join(lines[start:]).strip()
        if not body:
            return f"Pipeline stuck at {stage_id}. Check {artifact_path} for details."
        return self._truncate_reason(body)

    @classmethod
    def _truncate_reason(cls, text: str) -> str:
        if len(text) <= cls._BLOCKED_REASON_MAX_CHARS:
            return text
        return text[: cls._BLOCKED_REASON_MAX_CHARS] + "…"

    async def _handle_escalate(self, workspace: Workspace, *, is_max_iterations: bool = False) -> None:
        """Send escalation notification and block workspace."""
        state = workspace.state

        if not self._notifier:
            logger.warning("No notifier configured, cannot escalate %s", state.ticket_id)
            workspace.transition(Stage.FAILED)
            workspace.update_state(error="No notifier configured for escalation")
            return

        chat_id = self._get_chat_id(workspace)
        if not chat_id:
            logger.warning("No chat_id for escalation of %s", state.ticket_id)
            workspace.transition(Stage.FAILED)
            workspace.update_state(error="No Telegram chat_id configured")
            return

        stage = state.previous_state or state.current_state
        sep = "─" * 30
        title = tg_format.read_ticket_title(workspace)
        hdr = tg_format.tg_header("🔔", state.company_id, state.ticket_id, title)
        stage_id_str = stage.lower() if isinstance(stage, str) else str(stage).lower()
        is_agent_stage = stage_id_str in STAGE_RUNTIME_OUTPUT
        if is_max_iterations:
            stage_def = self._workflow.stages.get(stage_id_str)
            cap = stage_def.max_iterations if stage_def else "?"
            iterations = state.stage_iterations.get(stage_id_str, 0)
            header = f"{hdr}\nStage: {stage} — stuck after {iterations} attempts\n"
        else:
            header = f"{hdr}\nStage: {stage}\n"

        reason = tg_format.strip_markdown(self._build_blocked_reason(workspace, stage_id_str))
        if is_max_iterations:
            hint = (
                f"\n{sep}\n"
                f"The agent ran {iterations} times without completing this stage. "
                f"Last output is shown above — it may say PASS but something is still blocking progress.\n\n"
                f"Options:\n"
                f"  Reply with context — give the agent more information and resume\n"
                f"  Reply \"skip\" — advance past this stage to the next one\n"
                f"  Reply \"retry\" — re-run this stage\n"
                f"  Send \"retry {state.ticket_id} from {stage_id_str}\" — reset counter and run again\n"
                f"  Send \"retry {state.ticket_id} from dev\" — restart the dev agent from scratch"
            )
        elif is_agent_stage:
            hint = (
                f"\n{sep}\n"
                f"↩️ Reply with your answer or additional context, or:\n"
                f"  Reply \"skip\" — advance past this stage to the next one\n"
                f"  Reply \"retry\" — re-run this stage"
            )
        else:
            hint = (
                f"\n{sep}\n"
                f"Options:\n"
                f"  Reply \"skip\" — advance past this stage to the next one\n"
                f"  Reply \"retry\" — re-run this stage\n"
                f"  Send \"retry {state.ticket_id}\" to retry from this stage\n"
                f"  Send \"retry {state.ticket_id} from dev\" to restart from an earlier stage"
            )
        message = f"{header}\n{reason}{hint}"

        try:
            msg_id = await self._notifier.send_message(chat_id, message)
            workspace.transition(Stage.BLOCKED)
            workspace.update_state(human_input_question=reason)
            workspace.state.escalation_msg_id = msg_id
            workspace.state.escalation_chat_id = chat_id
            workspace.save_state()
            logger.info("Escalated %s via Telegram (msg_id=%d)", state.ticket_id, msg_id)
            self._emit(
                "escalation_sent",
                f"Escalated {workspace.state.ticket_id} to human",
                project_id=workspace.state.company_id,
                ticket_id=workspace.state.ticket_id,
                data={"reason": reason},
            )
        except Exception as e:
            logger.error("Telegram send failed for %s: %s", state.ticket_id, e)
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=f"Telegram notification failed: {e}")

    async def _notify_verification_blocked(
        self, workspace: Workspace, stage_id: str, verify_reason: str,
    ) -> None:
        if not self._notifier:
            return
        from orchestrator.notify import notify_verification_blocked
        chat_id = self._get_chat_id(workspace)
        await notify_verification_blocked(
            self._notifier, chat_id, workspace, stage_id, verify_reason,
            self._build_blocked_reason,
        )

    async def _action_finalize(self, workspace: Workspace) -> ActionResult:
        """Finalize a completed ticket. Returns ActionResult — caller transitions."""
        state = workspace.state

        if self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                pr_url = state.pr_url or "(no PR)"
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("✅", state.company_id, state.ticket_id, title)
                message = (
                    f"{hdr}\n"
                    f"PR ready for human merge: {pr_url}"
                )
                try:
                    await self._notifier.send_message(chat_id, message)
                except Exception as e:
                    logger.warning("Finalize notification failed: %s", e)

        if self._tracker:
            try:
                await self._tracker.add_comment(
                    state.ticket_id,
                    f"Pipeline complete. PR ready for merge: {state.pr_url or 'N/A'}",
                )
            except Exception as e:
                logger.warning("Finalize Jira comment failed: %s", e)

        return ActionResult(
            success=True, next_state=Stage.DONE, error="", metadata={},
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
        """Transition workspace to the state corresponding to a workflow stage."""
        state_name = _stage_to_state(stage_id)
        if state_name:
            self._log_pipeline(workspace, f"→ {state_name}")
            self._emit("stage_transition", f"{workspace.state.ticket_id}: {workspace.state.current_state} -> {state_name}", project_id=workspace.state.company_id, ticket_id=workspace.state.ticket_id, data={"from_state": workspace.state.current_state, "to_state": state_name})
            workspace.transition(state_name)
        else:
            logger.warning("Cannot map stage '%s' to state", stage_id)

    def _build_gate_summary(self, workspace: Workspace, gate_state: str) -> tuple[str, list[Button]]:
        """Build a summary message and buttons for an approval gate notification."""
        state = workspace.state
        tid = state.ticket_id
        title = tg_format.read_ticket_title(workspace)
        buttons = [
            Button(label="Approve", action=f"approve:{tid}"),
            Button(label="Reject", action=f"reject:{tid}"),
        ]

        if gate_state == Stage.PR_REVIEW:
            resolution_file = workspace.reports_dir / "pr-review-resolution.md"
            summary = ""
            if resolution_file.exists():
                content = resolution_file.read_text(encoding="utf-8")
                # Extract the last Resolution Summary line
                for line in reversed(content.splitlines()):
                    if line.startswith("Fixed:") or line.startswith("## Resolution Summary"):
                        summary = line
                        break
                if not summary:
                    summary = "Review complete."
            else:
                comments_file = workspace.reports_dir / "pr-review-comments.md"
                if comments_file.exists():
                    count = comments_file.read_text(encoding="utf-8").count("## Comment by")
                    summary = f"{count} comment(s) processed."
                else:
                    summary = "No PR comments found."
            hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
            text = (
                f"{hdr}\n"
                f"PR: {state.pr_url or 'N/A'}\n"
                f"{summary}"
            )
            return text, buttons

        if gate_state == Stage.ANALYSIS:
            # Include BA summary from ba.md
            ba_file = workspace.reports_dir / REPORT_BA
            summary = ""
            if ba_file.exists():
                content = ba_file.read_text(encoding="utf-8")
                # Extract first heading + summary paragraph
                lines = content.strip().splitlines()
                for line in lines:
                    if line.startswith("## Summary") or line.startswith("## Fix") or line.startswith("## Root"):
                        continue
                    if line.strip() and not line.startswith("#"):
                        summary = tg_format.strip_markdown(line.strip()[:200])
                        break
            gate_title = f"Analysis complete.\n{summary}" if summary else "Analysis complete."
            gate_title += "\n\nApprove = start coding. Reject = back to analysis."
        elif gate_state == Stage.QA:
            gate_title = "QA passed.\n\nApprove = push code & open PR. Reject = back to dev."
        else:
            gate_title = f"Awaiting approval at {gate_state}"

        hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
        text = (
            f"{hdr}\n"
            f"{gate_title}"
        )
        return text, buttons

    def _parse_agent_outcome(
        self, stage_id: str, output: str, workspace: Workspace,
    ) -> str:
        """Parse agent output to determine outcome for routing."""
        output_lower = output.lower()

        if stage_id == "analysis":
            ba_plan = workspace.reports_dir / REPORT_BA
            # If ba.md exists, analysis is done — proceed regardless of keywords
            if ba_plan.exists():
                return "default"
            # No ba.md — check if agent is asking questions
            if "unclear" in output_lower or "questions" in output_lower:
                return "unclear"
            logger.warning(
                "%s: BA completed but ba.md missing in ai_pipeline/ — treating as unclear",
                workspace.state.ticket_id,
            )
            return "unclear"

        if stage_id in ("scope_check", "qa"):
            runtime_name = STAGE_RUNTIME_OUTPUT.get(stage_id)
            if runtime_name:
                report = workspace.reports_dir / runtime_name
                if report.exists():
                    content = report.read_text().lower()
                    if _looks_like_pass(content):
                        return "pass"
                    if _looks_like_fail(content):
                        return "fail"
            if _looks_like_pass(output_lower):
                return "pass"
            return "fail"

        return "default"

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


# --- Mapping helpers ---

_STAGE_TO_STATE = {
    "analysis": Stage.ANALYSIS,
    "dev": Stage.DEV,
    "scope_check": Stage.SCOPE_CHECK,
    "qa": Stage.QA,
    "push": Stage.PUSHED,
    "pr_review": Stage.PR_REVIEW,
    "done": Stage.DONE,
    "escalate": Stage.BLOCKED,
}

_STATE_TO_STAGE = {v: k for k, v in _STAGE_TO_STATE.items()}


def _stage_to_state(stage_id: str) -> str | None:
    return _STAGE_TO_STATE.get(stage_id)


def _state_to_stage(state: str) -> str | None:
    return _STATE_TO_STAGE.get(state)


def _looks_like_pass(text: str) -> bool:
    """Check if agent output indicates a pass verdict."""
    return any(m in text for m in (
        "status: pass", "verdict: pass", "all gates passed",
        "qa pass", "qa complete", "verdict: hold",
        "scope audit complete. verdict: **pass",
        "scope audit complete. **status: pass",
        "advances to qa", "advances to",
        "merge as-is",
    ))


def _looks_like_fail(text: str) -> bool:
    """Check if agent output indicates a fail verdict."""
    return any(m in text for m in (
        "status: fail", "verdict: fail", "verdict: **fail",
        "status: blocked",
    ))

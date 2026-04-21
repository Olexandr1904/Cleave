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
from orchestrator.pr_creation import create_pr
from orchestrator import stage_verifier
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
        """Find the project+repo that owns this ticket via jira_repo_label match."""
        for project_id, project in self._projects.items():
            for repo_id, repo_config in project.repos.items():
                if repo_config.jira_repo_label and repo_config.jira_repo_label in ticket.labels:
                    return PrioritizedTicket(
                        ticket=ticket, repo_id=repo_id, project_id=project_id,
                    )
        return None

    def _should_approval_gate(
        self, completed_state: str, next_stage: str | None = None,
    ) -> bool:
        """Check if the workspace should pause for approval after this state.

        When next_stage is provided, the gate only fires on happy-path
        transitions (ANALYSIS→dev, QA→push, PR_REVIEW→done). Failure loops
        and escalations bypass the gate. When next_stage is None, the gate
        fires if the completed_state is in the gate set — used by callers
        (e.g., the PR_REVIEW "no comments" branch) that have already
        established they are on the happy path.
        """
        if not self._mode_handler or self._mode_handler.get_mode() != "manual":
            return False
        if completed_state not in self._APPROVAL_GATE_STATES:
            return False
        if next_stage is None:
            return True
        return next_stage == self._GATE_HAPPY_PATH_NEXT_STAGE.get(completed_state)

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
                done, _ = await asyncio.wait(
                    [
                        asyncio.create_task(self._shutdown_event.wait()),
                        asyncio.create_task(self._wake_event.wait()),
                    ],
                    timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
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
        # 0. Resume any DEFERRED workspaces whose retry_at has passed
        await self._sweep_deferred()
        # 1. Poll for new tickets and create workspaces
        if self._tracker:
            await self._poll_and_create_workspaces()

        # 2. Advance active workspaces
        for ws in list(self._active_workspaces):
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
            default_branch=repo_config.vcs.github.default_branch
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.default_branch,
            branch_prefix=repo_config.vcs.github.branch_prefix
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.branch_prefix,
        )

        # Write ticket data as markdown
        ticket_md = _ticket_to_markdown(pt.ticket)
        (ws.meta_dir / "ticket.md").write_text(ticket_md, encoding="utf-8")

        # Fetch and write parent ticket if linked
        if self._tracker and pt.ticket.linked_issues:
            for link in pt.ticket.linked_issues:
                parent_key = link.get("key", "")
                if parent_key and link.get("type", "").lower() in ("is child of", "parent"):
                    try:
                        parent = await self._tracker.get_ticket(parent_key)
                        parent_md = _ticket_to_markdown(parent)
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

        # Transition workspace to ANALYSIS
        ws.transition(Stage.ANALYSIS)
        return ws

    async def advance_workspace(self, workspace: Workspace) -> None:
        """Advance a workspace through the pipeline."""
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
            await self.advance_workspace(workspace)
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

        # Check iteration cap -> escalate
        if stage_def.max_iterations > 0:
            iterations = state.stage_iterations.get(stage_id, 0)
            if should_escalate(stage_id, self._workflow, iterations):
                next_stage = get_next_stage(stage_id, self._workflow, "max_iterations")
                if next_stage == "escalate":
                    await self._handle_escalate(workspace)
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

        self._emit("agent_dispatched", f"Dispatching {stage_def.agent} for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id})
        result = await self._agent_runtime.execute(
            stage_def.agent, workspace, protected_files=protected,
        )

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
                await self._notify_deferred(workspace, retry_at)
            else:
                workspace.transition(Stage.FAILED)
                workspace.update_state(error=result.error)
                await self._notify_failed(workspace, result.error or "")
            return

        self._emit("agent_completed", f"{stage_def.agent} completed for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id, "duration": result.duration_seconds, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
        verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
        if not verify_result.ok:
            agent_snippet = (result.output or "")[:200].replace("\n", " ")
            error_msg = f"{stage_id}: {verify_result.reason} (agent said: {agent_snippet})"
            workspace.transition(Stage.BLOCKED)
            workspace.update_state(error=error_msg)
            self._emit(
                "stage_verification_failed",
                f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"stage": stage_id, "reason": verify_result.reason},
            )
            return
        # Determine outcome from agent output
        outcome = self._parse_agent_outcome(stage_id, result.output, workspace)
        next_stage = get_next_stage(stage_id, self._workflow, outcome)

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
    ) -> None:
        """Send a one-shot Telegram notification for quota deferral (debounced).

        On send failure, `_quota_window_end` is left unchanged so the next quota
        hit will retry the notification instead of silencing for the full window.
        """
        now = datetime.now(timezone.utc)
        if self._quota_window_end is not None and now < self._quota_window_end:
            return  # still inside the already-announced quota window

        if self._notifier is None:
            self._quota_window_end = retry_at
            return

        state = workspace.state
        chat_id = self._get_chat_id(workspace)
        msg = (
            f"\u23f1 [{state.company_id}/{state.repo_id}] Quota exhausted. "
            f"{state.ticket_id} (at {state.previous_state or '?'}) deferred, "
            f"will retry at {retry_at.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"Other tickets hitting the same quota will defer silently until then."
        )
        buttons = [Button(label="Retry Now", action=f"retry:{state.ticket_id}")]
        try:
            await self._notifier.send_message(chat_id, msg, buttons=buttons)
            self._quota_window_end = retry_at
        except Exception as e:
            logger.warning("Failed to send deferred notification: %s", e)

    async def _notify_failed(self, workspace: Workspace, error: str) -> None:
        """Send a one-shot Telegram notification for a permanent failure."""
        if self._notifier is None:
            return
        state = workspace.state
        chat_id = self._get_chat_id(workspace)
        first_line = (error or "").splitlines()[0] if error else ""
        msg = (
            f"\u274c [{state.company_id}/{state.repo_id}] {state.ticket_id} "
            f"FAILED at {state.previous_state or '?'}. Error: {first_line}. "
            f"Reply 'retry {state.ticket_id}' or use the dashboard."
        )
        buttons = [Button(label="Retry", action=f"retry:{state.ticket_id}")]
        try:
            await self._notifier.send_message(chat_id, msg, buttons=buttons)
        except Exception as e:
            logger.warning("Failed to send failure notification: %s", e)

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
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=result.error)
            self._emit(
                "action_failed",
                f"Action {action} failed for {state.ticket_id}: {result.error}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"stage": stage_id, "error": result.error},
            )
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
                sep = "─" * 30
                pr_url = result.metadata["pr_url"]
                msg = (
                    f"🔗 [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                    f"{sep}\n"
                    f"PR created: {pr_url}\n\n"
                    f"Please review the code. When the review is complete,\n"
                    f"reply to THIS message with 'reviewed'.\n"
                    f"{sep}"
                )
                msg_id = await self._notifier.send_message(chat_id, msg)
                workspace.state.escalation_msg_id = msg_id
                workspace.state.escalation_chat_id = chat_id
                workspace.save_state()

    async def _action_push_and_open_pr(self, workspace: Workspace) -> ActionResult:
        """Push branch and open PR. Returns ActionResult — caller transitions."""
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs or not repo_config:
            logger.error("No VCS configured for %s", workspace.state.repo_id)
            return ActionResult(
                success=False, next_state="", error="No VCS adapter configured",
                metadata={},
            )

        result = await create_pr(workspace, vcs, self._tracker, repo_config)
        if result.success:
            return ActionResult(
                success=True, next_state=Stage.PR_REVIEW, error="",
                metadata={"pr_url": result.pr_url, "pr_number": result.pr_number},
            )
        return ActionResult(
            success=False, next_state="", error=result.error, metadata={},
        )

    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> ActionResult:
        """PR review comment resolution flow."""
        from orchestrator.comment_classifier import classify_comments

        state = workspace.state
        pr_number = state.pr_number

        if not pr_number:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        # Phase 1: Check pending escalated decisions
        pending = state.pending_review_comments or []
        undecided = [c for c in pending if c.get("decision") is None]

        if pending and not undecided:
            return await self._execute_review_decisions(workspace)

        if undecided:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        # Phase 2: Wait for 'reviewed' signal
        reply = (state.human_input_reply or "").lower()
        if "reviewed" not in reply and "proceed" not in reply:
            return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

        state.human_input_reply = None
        state.review_cycle = (state.review_cycle or 0) + 1
        state.stage_iterations["pr_review"] = 0
        workspace.save_state()

        # Phase 3: Fetch comments
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs:
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        try:
            comments = await vcs.get_pr_comments(pr_number)
        except Exception as e:
            logger.error("Failed to fetch PR comments for %s: %s", state.ticket_id, e)
            return ActionResult(success=False, next_state="", error=f"Failed to fetch: {e}", metadata={})

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

        # Phase 4: Classify
        classified = await classify_comments(comments, workspace, self._agent_runtime)

        # Phase 5: Auto-handle
        auto_fixed, auto_rejected, escalated = [], [], []
        for cc in classified:
            if cc.classification == "AUTO_FIX":
                auto_fixed.append(cc)
                try:
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to resolve comment %d: %s", cc.comment_id, e)
            elif cc.classification == "AUTO_REJECT":
                try:
                    await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to reply/resolve comment %d: %s", cc.comment_id, e)
                auto_rejected.append(cc)
            else:
                escalated.append(cc)

        # Phase 6: TG summary for auto-handled
        if (auto_fixed or auto_rejected) and self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                sep = "─" * 30
                lines = [f"🤖 [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR #{pr_number}"]
                lines.append(f"Auto-processed {len(auto_fixed) + len(auto_rejected)} comment(s):")
                lines.append(sep)
                for af in auto_fixed:
                    lines.append(f"✅ FIX: {af.reason} ({af.file}:{af.line or '?'})")
                for ar in auto_rejected:
                    lines.append(f"❌ REJECT: {ar.body[:60]} — {ar.reason}")
                lines.append(sep)
                if escalated:
                    lines.append(f"Waiting for your decisions on {len(escalated)} escalated comment(s).")
                await self._notifier.send_message(chat_id, "\n".join(lines))

        # Phase 7: Handle escalated or finish
        if not escalated:
            _write_resolution_report(workspace, auto_fixed, auto_rejected, [], state.review_cycle)
            if auto_fixed:
                return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})
            return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

        # Store escalated with TG msg_ids
        pending_comments = []
        for cc in escalated:
            msg_id = await self._send_escalated_comment_tg(workspace, cc, pr_number)
            pending_comments.append({
                "comment_id": cc.comment_id, "msg_id": msg_id, "decision": None,
                "author": cc.author, "file": cc.file, "line": cc.line,
                "body": cc.body, "reason": cc.reason,
            })

        state.pending_review_comments = pending_comments
        workspace.save_state()
        return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

    async def _send_escalated_comment_tg(self, workspace: Workspace, cc: Any, pr_number: int) -> int:
        """Send a single escalated comment to TG. Returns the message ID."""
        state = workspace.state
        sep = "─" * 30
        msg = (
            f"💬 [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR #{pr_number}\n"
            f"Comment by @{cc.author} on {cc.file}:{cc.line or '?'}\n"
            f"{sep}\n"
            f"Suggestion:\n  {cc.body[:300]}\n\n"
            f"Agent assessment:\n  {cc.reason}\n"
            f"{sep}\n"
            f"↩️ Reply: fix / skip / won't fix [reason]"
        )
        chat_id = self._get_chat_id(workspace)
        if chat_id and self._notifier:
            return await self._notifier.send_message(chat_id, msg)
        return 0

    async def _execute_review_decisions(self, workspace: Workspace) -> ActionResult:
        """Execute all pending review decisions."""
        state = workspace.state
        pending = state.pending_review_comments or []
        pr_number = state.pr_number

        vcs, _ = self._get_vcs_for_workspace(workspace)
        fixes_needed = []
        wont_fix = []
        skipped_comments = []

        for c in pending:
            decision = (c.get("decision") or "").lower().strip()
            if decision == "fix":
                fixes_needed.append(c)
            elif decision.startswith("won't fix") or decision.startswith("wont fix"):
                reason = decision.split(":", 1)[1].strip() if ":" in decision else "Operator decision"
                wont_fix.append({**c, "wont_fix_reason": reason})
                if vcs and pr_number:
                    try:
                        await vcs.reply_to_comment(pr_number, c["comment_id"], f"Won't fix: {reason}")
                        await vcs.resolve_comment(pr_number, c["comment_id"])
                    except Exception as e:
                        logger.warning("Failed to reply/resolve %d: %s", c["comment_id"], e)
            else:
                skipped_comments.append(c)

        if fixes_needed:
            fix_md = "# PR Comment Fixes Required\n\n"
            for f in fixes_needed:
                fix_md += f"## Fix: {f['file']}:{f.get('line', '?')}\n"
                fix_md += f"Comment by @{f['author']}: {f['body'][:200]}\n"
                fix_md += f"Reason: {f['reason']}\n\n"
            (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")

        _write_resolution_report(workspace, [], wont_fix, pending, state.review_cycle)

        state.pending_review_comments = None
        workspace.save_state()

        if fixes_needed:
            return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})
        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})


    async def _on_ticket_done(self, workspace: Workspace) -> None:
        """Handle ticket completion: TG notification + Jira status transition."""
        state = workspace.state

        # TG notification
        if self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                sep = "─" * 30
                await self._notifier.send_message(chat_id, (
                    f"✅ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                    f"{sep}\n"
                    f"Pipeline complete. PR ready for merge:\n"
                    f"{state.pr_url or 'N/A'}\n"
                    f"{sep}"
                ))

        # Transition Jira ticket to "In Review" (or project-specific equivalent)
        if self._tracker:
            project = self._projects.get(state.company_id)
            if project:
                target_status = project.config.jira.statuses.in_review
                if target_status:
                    try:
                        await self._tracker.transition_ticket(state.ticket_id, target_status)
                        logger.info("Transitioned %s to '%s' on Jira", state.ticket_id, target_status)
                    except Exception as e:
                        logger.warning("Failed to transition %s on Jira: %s", state.ticket_id, e)

    async def _handle_escalate(self, workspace: Workspace) -> None:
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

        # Build a human-readable escalation message from the latest agent output
        stage = state.previous_state or state.current_state
        header = f"🔔 [{state.company_id}/{state.repo_id}] {state.ticket_id}\nStage: {stage}\n{'─' * 30}\n\n"

        # Find the most recent agent output report
        report_content = None
        if workspace.reports_dir.exists():
            outputs = sorted(
                workspace.reports_dir.glob("*-output.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if outputs:
                report_content = outputs[0].read_text(encoding="utf-8").strip()

        hint = f"\n\n{'─' * 30}\n↩️ Reply to THIS message to answer for {state.ticket_id}."
        if report_content:
            budget = 4000 - len(header) - len(hint)
            if len(report_content) > budget:
                report_content = report_content[:budget] + "\n..."
            message = header + report_content + hint
        else:
            message = header + "Pipeline needs input. Check the workspace for details." + hint

        try:
            msg_id = await self._notifier.send_message(chat_id, message)
            workspace.transition(Stage.BLOCKED)
            workspace.update_state(
                human_input_question=message,
            )
            workspace.state.escalation_msg_id = msg_id
            workspace.state.escalation_chat_id = chat_id
            workspace.save_state()
            logger.info("Escalated %s via Telegram (msg_id=%d)", state.ticket_id, msg_id)
            self._emit("escalation_sent", f"Escalated {workspace.state.ticket_id} to human", project_id=workspace.state.company_id, ticket_id=workspace.state.ticket_id, data={"reason": workspace.state.human_input_question or "unknown"})
        except Exception as e:
            logger.error("Telegram send failed for %s: %s", state.ticket_id, e)
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=f"Telegram notification failed: {e}")

    async def _action_finalize(self, workspace: Workspace) -> ActionResult:
        """Finalize a completed ticket. Returns ActionResult — caller transitions."""
        state = workspace.state

        if self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                pr_url = state.pr_url or "(no PR)"
                message = (
                    f"[{state.company_id}/{state.repo_id}] {state.ticket_id}\n\n"
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

    def _advance_to_stage(self, workspace: Workspace, stage_id: str) -> None:
        """Transition workspace to the state corresponding to a workflow stage."""
        state_name = _stage_to_state(stage_id)
        if state_name:
            self._emit("stage_transition", f"{workspace.state.ticket_id}: {workspace.state.current_state} -> {state_name}", project_id=workspace.state.company_id, ticket_id=workspace.state.ticket_id, data={"from_state": workspace.state.current_state, "to_state": state_name})
            workspace.transition(state_name)
        else:
            logger.warning("Cannot map stage '%s' to state", stage_id)

    def _build_gate_summary(self, workspace: Workspace, gate_state: str) -> tuple[str, list[Button]]:
        """Build a summary message and buttons for an approval gate notification."""
        state = workspace.state
        tid = state.ticket_id
        sep = "─" * 30
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
            text = (
                f"⏸ [{state.company_id}/{state.repo_id}] {tid}\n"
                f"{sep}\n"
                f"PR: {state.pr_url or 'N/A'}\n"
                f"{summary}\n"
                f"{sep}\n"
                f"↩️ Reply: proceed = finalize, reject = back to dev"
            )
            return text, buttons

        actions = {
            Stage.ANALYSIS: ("Analysis done → ready for development", "proceed = start coding, reject = back to analysis"),
            Stage.QA: ("QA passed → ready to push & open PR", "proceed = push code, reject = back to dev"),
        }
        title, options = actions.get(gate_state, (f"Awaiting approval at {gate_state}", "proceed or reject"))

        text = (
            f"⏸ [{state.company_id}/{state.repo_id}] {tid}\n"
            f"{sep}\n"
            f"{title}\n"
            f"{sep}\n"
            f"↩️ Reply: {options}"
        )
        return text, buttons

    def _parse_agent_outcome(
        self, stage_id: str, output: str, workspace: Workspace,
    ) -> str:
        """Parse agent output to determine outcome for routing."""
        output_lower = output.lower()

        if stage_id == "analysis":
            ba_plan = workspace.reports_dir / "ba.md"
            # If ba.md exists, analysis is done — proceed regardless of keywords
            if ba_plan.exists():
                return "default"
            # No ba.md — check if agent is asking questions
            if "unclear" in output_lower or "questions" in output_lower:
                return "unclear"
            logger.warning(
                "%s: BA completed but reports/ba.md missing — treating as unclear",
                workspace.state.ticket_id,
            )
            return "unclear"

        if stage_id == "scope_check":
            # Check for scope guard report
            report = workspace.reports_dir / "scope-guard-agent-output.md"
            if report.exists():
                content = report.read_text().lower()
                if "status: pass" in content:
                    return "pass"
                if "status: fail" in content:
                    return "fail"
            if "pass" in output_lower and "fail" not in output_lower:
                return "pass"
            return "fail"

        if stage_id == "qa":
            report = workspace.reports_dir / "qa-agent-output.md"
            if report.exists():
                content = report.read_text().lower()
                if "all gates passed" in content or "status: pass" in content:
                    return "pass"
            if "pass" in output_lower and "fail" not in output_lower:
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


def _ticket_to_markdown(ticket: TicketData) -> str:
    """Convert TicketData to a markdown document."""
    lines = [
        f"# {ticket.id}: {ticket.summary}",
        "",
        f"**URL:** {ticket.url}",
        f"**Priority:** {ticket.priority}",
        f"**Reporter:** {ticket.reporter}",
    ]
    if ticket.assignee:
        lines.append(f"**Assignee:** {ticket.assignee}")
    if ticket.sprint:
        lines.append(f"**Sprint:** {ticket.sprint}")
    if ticket.labels:
        lines.append(f"**Labels:** {', '.join(ticket.labels)}")

    lines.extend(["", "## Description", "", ticket.description])

    if ticket.acceptance_criteria:
        lines.extend(["", "## Acceptance Criteria", "", ticket.acceptance_criteria])

    if ticket.linked_issues:
        lines.extend(["", "## Linked Issues", ""])
        for link in ticket.linked_issues:
            lines.append(f"- {link.get('type', 'related')}: {link.get('key', '')}")

    return "\n".join(lines)


def _write_resolution_report(
    workspace: Any, auto_fixed: list, auto_rejected: list,
    escalated_decisions: list, cycle: int,
) -> None:
    """Write or append to reports/pr-review-resolution.md."""
    report_path = workspace.reports_dir / "pr-review-resolution.md"
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""

    lines: list[str] = []
    if not existing:
        lines.append(f"# PR Review Resolution — {workspace.state.ticket_id}\n")
        lines.append(f"PR: #{workspace.state.pr_number}\n")

    total = len(auto_fixed) + len(auto_rejected) + len(escalated_decisions)
    lines.append(f"\n## Review Cycle {cycle}")
    lines.append(f"Comments this cycle: {total}\n")

    for af in auto_fixed:
        cid = af.comment_id if hasattr(af, "comment_id") else af.get("comment_id", "?")
        reason = af.reason if hasattr(af, "reason") else af.get("reason", "")
        lines.append(f"### Comment #{cid} — AUTO_FIX")
        lines.append(f"Reason: {reason}")
        lines.append("Status: FIXED\nMark as Resolved: YES\n")

    for ar in auto_rejected:
        cid = ar.comment_id if hasattr(ar, "comment_id") else ar.get("comment_id", "?")
        reason = ar.reason if hasattr(ar, "reason") else ar.get("reason", "")
        lines.append(f"### Comment #{cid} — AUTO_REJECT")
        lines.append(f"Reason: {reason}")
        lines.append("Status: WON'T_FIX\nGitHub reply: Posted\nMark as Resolved: YES\n")

    for ed in escalated_decisions:
        decision = (ed.get("decision") or "skip").lower()
        status = "FIXED" if decision == "fix" else "WON'T_FIX" if "won't fix" in decision else "SKIPPED"
        lines.append(f"### Comment #{ed.get('comment_id', '?')} — ESCALATED")
        lines.append(f"By: @{ed.get('author', '?')} on {ed.get('file', '?')}:{ed.get('line', '?')}")
        lines.append(f"Decision: {ed.get('decision', 'skip')}")
        lines.append(f"Status: {status}")
        commented = "YES" if "won't fix" in decision else "NO"
        lines.append(f"GitHub reply: {'Posted' if commented == 'YES' else 'N/A'}")
        lines.append(f"Mark as Resolved: {'YES' if status != 'SKIPPED' else 'NO'}\n")

    fixed = len(auto_fixed) + sum(1 for e in escalated_decisions if (e.get("decision") or "").lower() == "fix")
    wf = len(auto_rejected) + sum(1 for e in escalated_decisions if "won't fix" in (e.get("decision") or "").lower())
    skip = total - fixed - wf
    lines.append(f"## Resolution Summary — Cycle {cycle}")
    lines.append(f"Fixed: {fixed} | Won't Fix: {wf} | Commented: {wf} | Skipped: {skip}\n")

    report_path.write_text(existing + "\n".join(lines), encoding="utf-8")

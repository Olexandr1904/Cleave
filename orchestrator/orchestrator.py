"""Orchestrator — main daemon loop managing workspaces and agent dispatch."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from config.config_loader import load_config
from config.resource_registry import ResourceRegistry, discover_resources, validate_dependencies
from config.schemas import GlobalConfig, LoadedProject
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.safeguards import check_protected_files, get_changed_files
from orchestrator.workflow_router import WorkflowDefinition, load_workflow, get_next_stage, should_escalate
from workspace.workspace import Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main daemon loop — poll for tickets, manage slots, advance workspaces."""

    def __init__(
        self,
        global_config: GlobalConfig,
        projects: dict[str, LoadedProject],
        registry: ResourceRegistry,
        workflow: WorkflowDefinition,
        workspace_manager: WorkspaceManager,
        agent_runtime: AgentRuntime,
        dry_run: bool = False,
    ) -> None:
        self._global_config = global_config
        self._projects = projects
        self._registry = registry
        self._workflow = workflow
        self._workspace_manager = workspace_manager
        self._agent_runtime = agent_runtime
        self._dry_run = dry_run
        self._active_workspaces: list[Workspace] = []
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        """Main async loop — poll and advance until shutdown."""
        # Install signal handlers
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

        while not self._shutdown_event.is_set():
            try:
                await self.poll_cycle()
            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)

            # Wait for next cycle or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=poll_interval,
                )
            except asyncio.TimeoutError:
                pass  # Normal — poll interval elapsed

        logger.info("Orchestrator shutting down gracefully")

    async def poll_cycle(self) -> None:
        """Single poll + advance cycle."""
        # 1. Advance active workspaces
        for ws in list(self._active_workspaces):
            try:
                await self.advance_workspace(ws)
            except Exception as e:
                logger.error(
                    "Workspace %s error: %s",
                    ws.state.ticket_id, e, exc_info=True,
                )
                # Workspace-level failure doesn't crash daemon
                ws.update_state(status="failed", error=str(e))

        # 2. Cleanup completed/failed workspaces from active list
        self._active_workspaces = [
            ws for ws in self._active_workspaces
            if ws.state.status in ("pending", "running", "waiting_for_human")
        ]

        # 3. Workspace cleanup
        max_age = self._global_config.workspaces.max_age_days
        deleted = self._workspace_manager.cleanup_old_workspaces(max_age)
        if deleted:
            logger.info("Cleaned up %d old workspace(s)", len(deleted))

    async def advance_workspace(self, workspace: Workspace) -> None:
        """Advance a workspace by invoking its next agent."""
        state = workspace.state

        if state.status == "waiting_for_human":
            return  # Skip — waiting for reply

        if state.status in ("completed", "failed"):
            return  # Terminal state

        current_stage = state.current_stage

        # Check if we should escalate
        stage_def = self._workflow.stages.get(current_stage)
        if stage_def and stage_def.agent:
            iterations = state.stage_iterations.get(current_stage, 0)
            if should_escalate(current_stage, self._workflow, iterations):
                next_stage = get_next_stage(current_stage, self._workflow, "max_iterations")
                if next_stage:
                    workspace.update_state(
                        current_stage=next_stage,
                        status="waiting_for_human",
                        human_input_pending=True,
                    )
                    logger.warning(
                        "Workspace %s: stage '%s' exceeded max iterations, escalating",
                        state.ticket_id, current_stage,
                    )
                return

        # Execute the current stage's agent
        if stage_def and stage_def.agent:
            if state.status == "pending":
                workspace.transition_status("running")

            if self._dry_run:
                logger.info(
                    "[DRY RUN] Would execute agent '%s' for %s",
                    stage_def.agent, state.ticket_id,
                )
                # In dry run, advance to next stage
                next_stage = get_next_stage(current_stage, self._workflow)
                if next_stage:
                    workspace.update_state(current_stage=next_stage)
                return

            workspace.increment_iteration(current_stage)
            result = await self._agent_runtime.execute(
                stage_def.agent, workspace,
            )

            if result.success:
                # AC1 (7.2): Post-execution file write monitor
                changed = get_changed_files(str(workspace.repo_dir))
                violations = check_protected_files(str(workspace.repo_dir), changed)
                if violations:
                    violation_msg = "; ".join(str(v) for v in violations)
                    logger.error(
                        "Workspace %s: PROTECTED FILE VIOLATION by agent '%s': %s",
                        state.ticket_id, stage_def.agent, violation_msg,
                    )
                    workspace.update_state(
                        status="failed",
                        error=f"Protected file violation: {violation_msg}",
                    )
                    return

                next_stage = get_next_stage(current_stage, self._workflow)
                if next_stage:
                    workspace.update_state(current_stage=next_stage)
                else:
                    workspace.update_state(status="completed")
            else:
                workspace.update_state(
                    status="failed",
                    error=result.error,
                )

    def shutdown(self) -> None:
        """Trigger graceful shutdown."""
        self._shutdown_event.set()

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self.shutdown()

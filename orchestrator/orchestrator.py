"""Orchestrator — main daemon loop managing workspaces and agent dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any

from config.schemas import GlobalConfig, LoadedProject, RepoConfig
from config.resource_registry import ResourceRegistry
from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TicketData, TrackerInterface
from integrations.base.vcs import VCSInterface
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.pr_creation import create_pr
from orchestrator.ticket_prioritizer import PrioritizedTicket, prioritize_tickets
from orchestrator.workflow_router import (
    WorkflowDefinition,
    get_next_stage,
    load_workflow,
    should_escalate,
)
from workspace.workspace import Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main daemon loop — poll for tickets, manage slots, advance workspaces."""

    # Approval gate stages in manual mode, mapped to the next stage that
    # represents the "happy path" past the gate. Gating should ONLY fire when
    # the workflow would move forward past the gate — not on failure loops
    # (QA fail → dev) or escalation (analysis unclear → escalate).
    _APPROVAL_GATE_STATES = {"ANALYSIS", "QA", "PR_REVIEW"}
    _GATE_HAPPY_PATH_NEXT_STAGE = {
        "ANALYSIS": "dev",
        "QA": "push",
        "PR_REVIEW": "done",
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
        self._active_workspaces: list[Workspace] = []
        self._shutdown_event = asyncio.Event()
        # Map repo_id -> (VCSInterface, RepoConfig) for per-repo VCS
        self._repo_vcs: dict[str, tuple[VCSInterface, RepoConfig]] = {}
        # Mode handler — initialized later via set_mode_handler or from config default
        self._mode_handler: ModeHandler | None = None

    def register_repo_vcs(
        self, repo_id: str, vcs: VCSInterface, repo_config: RepoConfig,
    ) -> None:
        """Register a VCS adapter for a specific repo."""
        self._repo_vcs[repo_id] = (vcs, repo_config)

    def set_mode_handler(self, handler: ModeHandler) -> None:
        """Register the mode handler for auto/manual switching."""
        self._mode_handler = handler

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

        while not self._shutdown_event.is_set():
            try:
                await self.poll_cycle()
            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=poll_interval,
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Orchestrator shutting down gracefully")

    async def poll_cycle(self) -> None:
        """Single poll + advance cycle."""
        # 1. Poll for new tickets and create workspaces (skip in manual mode)
        is_manual = bool(
            self._mode_handler and self._mode_handler.get_mode() == "manual"
        )
        if self._tracker and not is_manual:
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
                    ws.transition("FAILED")
                    ws.update_state(error=str(e))
                except Exception:
                    pass

        # 3. Cleanup terminal workspaces from active list
        terminal = {"DONE", "FAILED", "ARCHIVED"}
        self._active_workspaces = [
            ws for ws in self._active_workspaces
            if ws.state.current_state not in terminal
        ]

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

            # Prioritize and route tickets to repos
            prioritized = prioritize_tickets(tickets, project)
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

                # Check if workspace already exists for this ticket
                already_exists = any(
                    ws.state.ticket_id == pt.ticket.id
                    for ws in self._active_workspaces
                )
                if already_exists:
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
            branch_prefix=repo_config.vcs.github.branch_prefix
            if repo_config.vcs.provider == "github"
            else repo_config.vcs.gitlab.branch_prefix,
            depth=repo_config.git.depth,
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
        ws.transition("ANALYSIS")
        return ws

    async def advance_workspace(self, workspace: Workspace) -> None:
        """Advance a workspace through the pipeline."""
        state = workspace.state
        current = state.current_state

        if current == "BLOCKED":
            return  # Waiting for human reply

        if current == "AWAITING_APPROVAL":
            return  # Waiting for operator approval

        if current in ("DONE", "FAILED", "ARCHIVED"):
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

        repo_config = self._get_repo_config(workspace)
        protected = repo_config.architecture.protected_files if repo_config else []

        result = await self._agent_runtime.execute(
            stage_def.agent, workspace, protected_files=protected,
        )

        if not result.success:
            workspace.transition("FAILED")
            workspace.update_state(error=result.error)
            return

        # Determine outcome from agent output
        outcome = self._parse_agent_outcome(stage_id, result.output, workspace)
        next_stage = get_next_stage(stage_id, self._workflow, outcome)

        if next_stage:
            # Check for approval gate in manual mode. Only gate on happy-path
            # transitions — failure loops and escalations bypass the gate.
            current_state = workspace.state.current_state
            if self._should_approval_gate(current_state, next_stage):
                workspace.transition("AWAITING_APPROVAL")
                if self._notifier:
                    chat_id = self._get_chat_id(workspace)
                    summary = self._build_gate_summary(workspace, current_state)
                    await self._notifier.send_message(chat_id, summary)
            else:
                self._advance_to_stage(workspace, next_stage)
        else:
            workspace.transition("DONE")

    async def _handle_action_stage(
        self, workspace: Workspace, stage_id: str, stage_def: Any,
    ) -> None:
        """Execute an action stage (non-agent)."""
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

        if action == "push_and_open_pr":
            await self._action_push_and_open_pr(workspace)
        elif action == "fetch_pr_comments":
            await self._action_fetch_pr_comments(workspace, stage_def)
        elif action == "notify_human":
            await self._handle_escalate(workspace)
        elif action == "finalize":
            await self._action_finalize(workspace)
        else:
            logger.warning("Unknown action: %s", action)

    async def _action_push_and_open_pr(self, workspace: Workspace) -> None:
        """Push branch and open PR."""
        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs or not repo_config:
            logger.error("No VCS configured for %s", workspace.state.repo_id)
            workspace.transition("FAILED")
            workspace.update_state(error="No VCS adapter configured")
            return

        result = await create_pr(workspace, vcs, self._tracker, repo_config)
        if result.success:
            workspace.transition("PR_REVIEW")
        else:
            workspace.transition("FAILED")
            workspace.update_state(error=result.error)

    async def _action_fetch_pr_comments(
        self, workspace: Workspace, stage_def: Any,
    ) -> None:
        """Fetch PR comments and decide if fixes are needed."""
        state = workspace.state
        pr_number = state.pr_number

        if not pr_number:
            workspace.transition("DONE")
            return

        # Check delay
        delay_minutes = stage_def.delay_minutes
        if delay_minutes > 0:
            last_updated = state.last_updated_at
            if last_updated:
                from datetime import datetime, timezone
                try:
                    updated_time = datetime.fromisoformat(last_updated)
                    elapsed = (datetime.now(timezone.utc) - updated_time).total_seconds() / 60
                    if elapsed < delay_minutes:
                        logger.debug(
                            "%s: PR review delay not met (%.0f/%.0f min)",
                            state.ticket_id, elapsed, delay_minutes,
                        )
                        return  # Wait longer
                except (ValueError, TypeError):
                    pass

        workspace.increment_iteration("pr_review")

        vcs, repo_config = self._get_vcs_for_workspace(workspace)
        if not vcs:
            workspace.transition("DONE")
            return

        try:
            comments = await vcs.get_pr_comments(pr_number)
        except Exception as e:
            logger.error("Failed to fetch PR comments for %s: %s", state.ticket_id, e)
            return

        if not comments:
            # No comments -> done (or gate for approval in manual mode)
            if self._should_approval_gate("PR_REVIEW"):
                workspace.transition("AWAITING_APPROVAL")
                if self._notifier:
                    chat_id = self._get_chat_id(workspace)
                    summary = self._build_gate_summary(workspace, "PR_REVIEW")
                    await self._notifier.send_message(chat_id, summary)
            else:
                workspace.transition("DONE")
            return

        # Write comments to reports for PR Comment Responder agent
        comment_md = "# PR Review Comments\n\n"
        for c in comments:
            comment_md += f"## Comment by {c.author}\n"
            if c.path:
                comment_md += f"File: `{c.path}`"
                if c.line:
                    comment_md += f" (line {c.line})"
                comment_md += "\n"
            comment_md += f"\n{c.body}\n\n---\n\n"
        (workspace.reports_dir / "pr-review-comments.md").write_text(
            comment_md, encoding="utf-8",
        )

        # Run PR Comment Responder agent if available
        pr_agent = self._registry.get_agent("pr-comment-responder-agent")
        if pr_agent:
            result = await self._agent_runtime.execute(
                "pr-comment-responder-agent", workspace,
            )
            if result.success and "fix_required" in result.output.lower():
                self._advance_to_stage(workspace, "dev")
                return

        # Default: if there are comments, go back to dev
        self._advance_to_stage(workspace, "dev")

    async def _handle_escalate(self, workspace: Workspace) -> None:
        """Send escalation notification and block workspace."""
        state = workspace.state

        if not self._notifier:
            logger.warning("No notifier configured, cannot escalate %s", state.ticket_id)
            workspace.transition("FAILED")
            workspace.update_state(error="No notifier configured for escalation")
            return

        chat_id = self._get_chat_id(workspace)
        if not chat_id:
            logger.warning("No chat_id for escalation of %s", state.ticket_id)
            workspace.transition("FAILED")
            workspace.update_state(error="No Telegram chat_id configured")
            return

        message = (
            f"[{state.company_id}/{state.repo_id}] {state.ticket_id}\n\n"
            f"Pipeline needs human input.\n"
            f"Current state: {state.current_state}\n"
        )

        # Check for questions from BA
        questions_file = workspace.reports_dir / "ba-questions.md"
        if questions_file.exists():
            message += f"\n{questions_file.read_text(encoding='utf-8')}"

        try:
            msg_id = await self._notifier.send_message(chat_id, message)
            workspace.transition("BLOCKED")
            workspace.update_state(
                human_input_question=message,
            )
            logger.info("Escalated %s via Telegram (msg_id=%d)", state.ticket_id, msg_id)
        except Exception as e:
            logger.error("Telegram send failed for %s: %s", state.ticket_id, e)
            workspace.transition("FAILED")
            workspace.update_state(error=f"Telegram notification failed: {e}")

    async def _action_finalize(self, workspace: Workspace) -> None:
        """Finalize a completed ticket."""
        state = workspace.state

        # Send completion notification
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

        # Add Jira comment
        if self._tracker:
            try:
                await self._tracker.add_comment(
                    state.ticket_id,
                    f"Pipeline complete. PR ready for merge: {state.pr_url or 'N/A'}",
                )
            except Exception as e:
                logger.warning("Finalize Jira comment failed: %s", e)

        workspace.transition("DONE")

    def _advance_to_stage(self, workspace: Workspace, stage_id: str) -> None:
        """Transition workspace to the state corresponding to a workflow stage."""
        state_name = _stage_to_state(stage_id)
        if state_name:
            workspace.transition(state_name)
        else:
            logger.warning("Cannot map stage '%s' to state", stage_id)

    def _build_gate_summary(self, workspace: Workspace, gate_state: str) -> str:
        """Build a summary message for an approval gate notification."""
        state = workspace.state
        ticket_id = state.ticket_id

        if gate_state == "ANALYSIS":
            ba_report = workspace.reports_dir / "ba-agent-output.md"
            summary = ""
            if ba_report.exists():
                content = ba_report.read_text(encoding="utf-8")
                summary = content[:500]
            return (
                f"[{state.company_id}/{state.repo_id}] {ticket_id}\n\n"
                f"Analysis complete. Here's the plan:\n{summary}\n\n"
                f"Proceed to development?"
            )

        if gate_state == "QA":
            qa_report = workspace.reports_dir / "qa-agent-output.md"
            summary = ""
            if qa_report.exists():
                content = qa_report.read_text(encoding="utf-8")
                summary = content[:500]
            return (
                f"[{state.company_id}/{state.repo_id}] {ticket_id}\n\n"
                f"Tests pass.\n{summary}\n\n"
                f"Push and open PR?"
            )

        if gate_state == "PR_REVIEW":
            return (
                f"[{state.company_id}/{state.repo_id}] {ticket_id}\n\n"
                f"PR review complete. PR: {state.pr_url or 'N/A'}\n\n"
                f"Finalize and merge?"
            )

        return f"{ticket_id}: Awaiting approval at {gate_state}."

    def _parse_agent_outcome(
        self, stage_id: str, output: str, workspace: Workspace,
    ) -> str:
        """Parse agent output to determine outcome for routing."""
        output_lower = output.lower()

        if stage_id == "analysis":
            if "unclear" in output_lower or "questions" in output_lower:
                return "unclear"
            return "default"

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


# --- Mapping helpers ---

_STAGE_TO_STATE = {
    "analysis": "ANALYSIS",
    "dev": "DEV",
    "scope_check": "SCOPE_CHECK",
    "qa": "QA",
    "push": "PUSHED",
    "pr_review": "PR_REVIEW",
    "done": "DONE",
    "escalate": "BLOCKED",
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

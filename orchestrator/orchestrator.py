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
from orchestrator.gradle_remediation import (
    ARCH_MISMATCH_HELP,
    clear_gradle_transforms,
    looks_like_aapt2_arch_mismatch,
    looks_like_gradle_cache_corruption,
)
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

    @staticmethod
    def _git_diff_files(workspace: Workspace) -> set[str]:
        """Get set of files changed in the latest commit."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.source_dir), "diff", "HEAD~1", "--name-only"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return set(result.stdout.strip().splitlines())
        except Exception:
            pass
        return set()

    @staticmethod
    def _git_head_sha(workspace: Workspace) -> str:
        """Get current HEAD sha."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.source_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _get_ticket_title(workspace: Workspace) -> str:
        """Read ticket summary from meta/ticket.json, or return empty string."""
        ticket_file = workspace.meta_dir / "ticket.json"
        if ticket_file.exists():
            try:
                data = json.loads(ticket_file.read_text(encoding="utf-8"))
                return data.get("summary", "")
            except (json.JSONDecodeError, KeyError):
                pass
        return ""

    @staticmethod
    def _tg_header(emoji: str, state: Any, title: str) -> str:
        """Build a standard TG message header line, including ticket title if available."""
        header = f"{emoji} [{state.company_id}/{state.repo_id}] {state.ticket_id}"
        if title:
            header += f"\n{title}"
        return header

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
            title=pt.ticket.summary,
        )

        # Write ticket data as markdown
        ticket_md = _ticket_to_markdown(pt.ticket)
        (ws.meta_dir / "ticket.md").write_text(ticket_md, encoding="utf-8")

        # Fetch Jira comments and status history for agent context
        if self._tracker and hasattr(self._tracker, '_request'):
            try:
                data = await self._tracker._request(
                    "GET", f"/issue/{pt.ticket.id}?expand=changelog&fields=comment",
                )
                # Comments
                comments = data.get("fields", {}).get("comment", {}).get("comments", [])
                if comments:
                    lines = ["# Jira Comments\n"]
                    for c in comments:
                        author = c.get("author", {}).get("displayName", "?")
                        created = c.get("created", "")[:10]
                        body = c.get("body", "")
                        if isinstance(body, dict):
                            from integrations.jira.jira_adapter import _extract_adf_text
                            body = _extract_adf_text(body)
                        lines.append(f"## {author} ({created})\n\n{body}\n")
                    (ws.meta_dir / "comments.md").write_text("\n".join(lines), encoding="utf-8")

                # Status history
                changelog = data.get("changelog", {}).get("histories", [])
                status_changes = []
                for h in changelog:
                    for item in h.get("items", []):
                        if item.get("field") == "status":
                            status_changes.append(
                                f"- {h.get('created','')[:10]}: "
                                f"{item.get('fromString','?')} → {item.get('toString','?')} "
                                f"by {h.get('author',{}).get('displayName','?')}"
                            )
                if status_changes:
                    history = "# Status History\n\n" + "\n".join(status_changes) + "\n"
                    (ws.meta_dir / "history.md").write_text(history, encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to fetch comments/history for %s: %s", pt.ticket.id, e)

        # Download ticket attachments (screenshots, images)
        if pt.ticket.attachments:
            attachments_dir = ws.meta_dir / "attachments"
            attachments_dir.mkdir(exist_ok=True)
            for att in pt.ticket.attachments:
                mime = att.get("mime_type", "")
                if not mime.startswith("image/"):
                    continue  # Only download images
                filename = att.get("filename", "attachment")
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=30) as client:
                        # Jira requires auth for attachment downloads
                        auth = None
                        if self._tracker and hasattr(self._tracker, '_email') and hasattr(self._tracker, '_token'):
                            import base64
                            creds = base64.b64encode(f"{self._tracker._email}:{self._tracker._token}".encode()).decode()
                            headers = {"Authorization": f"Basic {creds}"}
                        else:
                            headers = {}
                        resp = await client.get(att["url"], headers=headers, follow_redirects=True)
                        if resp.status_code == 200:
                            (attachments_dir / filename).write_bytes(resp.content)
                            logger.info("Downloaded attachment %s for %s", filename, pt.ticket.id)
                        else:
                            logger.warning("Failed to download %s: HTTP %d", filename, resp.status_code)
                except Exception as e:
                    logger.warning("Failed to download attachment %s: %s", filename, e)

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

        # If the stage was previously completed (ticket looped back from a later stage),
        # reset iteration counter so it gets a fresh budget. Detect by checking if the
        # counter already equals or exceeds max — that means it ran in a prior cycle.
        max_iter = stage_def.max_iterations
        if max_iter > 0 and state.stage_iterations.get(stage_id, 0) >= max_iter:
            state.stage_iterations[stage_id] = 0
            workspace.save_state()
            logger.info("Reset %s iteration counter for %s (was at max %d)", stage_id, state.ticket_id, max_iter)

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
        state_before = workspace.state.current_state
        result = await self._agent_runtime.execute(
            stage_def.agent, workspace, protected_files=protected,
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
        self._log_pipeline(workspace, f"{stage_id} ({stage_def.agent}) completed.{sha_info} Output: `reports/{stage_def.agent}-output.md`")
        verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
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
                    sep = "─" * 30
                    title = self._get_ticket_title(workspace)
                    hdr = self._tg_header("⚠️", state, title)
                    await self._notifier.send_message(chat_id, (
                        f"{hdr}\n"
                        f"{sep}\n"
                        f"QA passed but with warnings:\n"
                        + "\n".join(f"  • {w}" for w in warnings)
                        + f"\n\nCI on GitHub will be the authoritative gate.\n"
                        f"{sep}"
                    ))

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
        """Send a one-shot Telegram notification for transient agent failure
        deferrals.

        Picks a headline from the actual cause \u2014 not all DEFERREDs are quota
        hits, even though the pipeline reuses the quota retry path for any
        transient CLI failure. Hard-coding "Quota exhausted" misled operators
        when the real cause was, e.g., the agent hitting `max_turns`.

        Quota-window debouncing only applies when the cause IS a real
        usage-limit hit; other transient failures skip the silence window so
        each ticket's distinct reason still surfaces.
        """
        state = workspace.state
        is_real_quota = bool(
            reason and (
                "usage limit" in reason.lower()
                or "api_error_status\":429" in reason
                or '"api_error_status": 429' in reason
            )
        )

        # Window debouncing is for the case where one quota hit cascades
        # across many tickets \u2014 silence the same announcement.
        now = datetime.now(timezone.utc)
        if (
            is_real_quota
            and self._quota_window_end is not None
            and now < self._quota_window_end
        ):
            return

        if self._notifier is None:
            if is_real_quota:
                self._quota_window_end = retry_at
            return

        chat_id = self._get_chat_id(workspace)
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("\u23f1", state, title)

        # Pick a headline that names the actual cause.
        if is_real_quota:
            headline = (
                f"Quota exhausted at {state.previous_state or '?'}, deferred "
                f"until {retry_at.strftime('%Y-%m-%d %H:%M')} UTC. "
                f"Other tickets hitting the same quota will defer silently."
            )
        elif reason and "error_max_turns" in reason:
            headline = (
                f"Agent hit max-turns limit at {state.previous_state or '?'}, "
                f"deferred until {retry_at.strftime('%H:%M')} UTC for retry. "
                f"If this recurs the agent task may be too complex \u2014 consider "
                f"raising `max_turns` or splitting the work."
            )
        else:
            short = (reason or "").splitlines()[0][:200] if reason else "no detail captured"
            headline = (
                f"Transient agent failure at {state.previous_state or '?'}, "
                f"deferred until {retry_at.strftime('%H:%M')} UTC. "
                f"Reason: {short}"
            )

        msg = f"{hdr}\n{headline}"
        buttons = [Button(label="Retry Now", action=f"retry:{state.ticket_id}")]
        try:
            await self._notifier.send_message(chat_id, msg, buttons=buttons)
            if is_real_quota:
                self._quota_window_end = retry_at
        except Exception as e:
            logger.warning("Failed to send deferred notification: %s", e)

    async def _notify_failed(self, workspace: Workspace, error: str) -> None:
        """Send a one-shot Telegram notification for a permanent failure."""
        if self._notifier is None:
            return
        state = workspace.state
        chat_id = self._get_chat_id(workspace)
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("\u274c", state, title)
        first_line = (error or "").splitlines()[0] if error else ""
        buttons = [Button(label="Retry", action=f"retry:{state.ticket_id}")]
        # Architecture mismatch (x86-64 aapt2 on non-x86 host) is a host-setup
        # issue \u2014 the pipeline cannot apt-install or rewrite gradle.properties.
        # Surface a distinct message with concrete fix options and NO clear-
        # cache button (which loops forever on this failure).
        if looks_like_aapt2_arch_mismatch(error):
            sep = "\u2500" * 30
            msg = (
                f"{hdr}\n"
                f"FAILED at {state.previous_state or '?'}.\n"
                f"\u26a0\ufe0f Architecture mismatch (x86-64 aapt2 on non-x86 host).\n"
                f"{sep}\n"
                f"{ARCH_MISMATCH_HELP}"
            )
        elif looks_like_gradle_cache_corruption(error):
            msg = (
                f"{hdr}\n"
                f"FAILED at {state.previous_state or '?'}. Error: {first_line}.\n"
                f"Detected Gradle cache corruption \u2014 tap below to clear it."
            )
            buttons.insert(
                0,
                Button(label="🧹 Clear cache & retry", action=f"clear_gradle:{state.ticket_id}"),
            )
        else:
            msg = (
                f"{hdr}\n"
                f"FAILED at {state.previous_state or '?'}. Error: {first_line}."
            )
        try:
            await self._notifier.send_message(chat_id, msg, buttons=buttons)
        except Exception as e:
            logger.warning("Failed to send failure notification: %s", e)

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
                sep = "─" * 30
                pr_url = result.metadata["pr_url"]
                title = self._get_ticket_title(workspace)
                hdr = self._tg_header("🔗", state, title)
                msg = (
                    f"{hdr}\n"
                    f"{sep}\n"
                    f"PR created: {pr_url}\n\n"
                    f"Please review the code."
                )
                pr_buttons = [Button(label="Review Complete", action=f"reviewed:{state.ticket_id}")]
                msg_id = await self._notifier.send_message(chat_id, msg, buttons=pr_buttons)
                workspace.state.escalation_msg_id = msg_id
                workspace.state.escalation_chat_id = chat_id
                workspace.save_state()

    async def _action_push_and_open_pr(self, workspace: Workspace) -> ActionResult:
        """Push branch and open PR. Returns ActionResult — caller transitions."""
        state = workspace.state

        # If PR already exists (from a previous cycle), just push new commits (no squash)
        if state.pr_number and state.pr_url:
            vcs, repo_config = self._get_vcs_for_workspace(workspace)
            if vcs:
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
                            await self._notifier.send_message(
                                chat_id,
                                f"⚠️ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                                f"Dev-agent failed to fix comment #{cid} twice "
                                f"({entry.get('file', '?')}:{entry.get('line', '?')})",
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
            changed_files = self._git_diff_files(workspace)
            sha = self._git_head_sha(workspace)
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
                            await self._notifier.send_message(
                                chat_id,
                                f"⚠️ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                                f"Dev-agent failed to fix comment #{cid} twice "
                                f"({entry.get('file', '?')}:{entry.get('line', '?')})",
                            )

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
                add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                    "classification": "AUTO_FIX",
                    "file": cc.file or "",
                    "line": str(cc.line or "?"),
                    "author": cc.author or "",
                    "reason": cc.reason or "",
                    "verified": "PENDING",
                    "fail_count": "0",
                    "cycle": str(state.review_cycle),
                })
                auto_fixed.append(cc)
            elif cc.classification == "AUTO_REJECT":
                # Phase 6: AUTO_REJECT replies + resolves immediately
                try:
                    await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
                    await vcs.resolve_comment(pr_number, cc.comment_id)
                except Exception as e:
                    logger.warning("Failed to reply/resolve comment %d: %s", cc.comment_id, e)
                add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                    "classification": "AUTO_REJECT",
                    "file": cc.file or "",
                    "line": str(cc.line or "?"),
                    "author": cc.author or "",
                    "reason": cc.reason or "",
                    "verified": "N/A",
                    "github_reply": "Posted",
                    "resolved": "YES",
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

        # Phase 7: Handle escalated or collect FIX items
        if not escalated:
            summary = f"PR review cycle {state.review_cycle}: {len(auto_fixed)} fix, {len(auto_rejected)} rejected"
            self._log_pipeline(workspace, f"{summary}. Report: `reports/pr-review-resolution.md`")
            if auto_fixed:
                # Write fix instructions for the dev agent
                fix_md = "# PR Comment Fixes Required\n\n"
                for af in auto_fixed:
                    fix_md += f"## Fix: {af.file}:{af.line or '?'}\n"
                    fix_md += f"Comment by @{af.author}: {af.body[:200]}\n"
                    fix_md += f"What to do: {af.suggested_fix or af.reason}\n\n"
                (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")
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
            add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                "classification": "ESCALATE",
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
        state = workspace.state
        sep = "─" * 30
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("💬", state, title)
        msg = (
            f"{hdr} — PR #{pr_number}\n"
            f"Comment by @{cc.author} on {cc.file}:{cc.line or '?'}\n"
            f"{sep}\n"
            f"Suggestion:\n  {cc.body[:300]}\n\n"
            f"Agent assessment:\n  {cc.reason}\n"
            f"{sep}\n"
            "Tap a button below, or reply to this message with:\n"
            "  • `fix` — re-engage dev-agent\n"
            "  • `won't fix: <reason>` — post the reason on GitHub and resolve\n"
            "Free-text reply lets you add context the buttons can't."
        )
        # Use a unique action key so the callback handler can match this comment.
        # Skip button is intentionally omitted — operators interpreted it as
        # "I'm done with this comment", but the previous semantic was "ask me
        # again every 30 min", which trapped them in a nag loop. If the
        # operator wants to drop a comment, they reply with `won't fix:`
        # explaining why, which is more honest about the PR state.
        comment_key = f"{state.ticket_id}:{cc.comment_id}"
        buttons = [
            Button(label="Fix", action=f"pr_fix:{comment_key}"),
            Button(label="Won't Fix", action=f"pr_wontfix:{comment_key}"),
        ]
        chat_id = self._get_chat_id(workspace)
        if chat_id and self._notifier:
            return await self._notifier.send_message(chat_id, msg, buttons=buttons)
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
                update_entry(report_path, cid, {
                    "decision": "FIX",
                    "verified": "PENDING",
                    "fail_count": "0",
                    "decided_at": self._now(),
                })
            elif decision.startswith("won't fix") or decision.startswith("wont fix"):
                reason = decision.split(":", 1)[1].strip() if ":" in decision else "Operator decision"
                wont_fix.append({**c, "wont_fix_reason": reason})
                if vcs and pr_number:
                    try:
                        await vcs.reply_to_comment(pr_number, cid, f"Won't fix: {reason}")
                        await vcs.resolve_comment(pr_number, cid)
                    except Exception as e:
                        logger.warning("Failed to reply/resolve %d: %s", cid, e)
                update_entry(report_path, cid, {
                    "decision": "WON'T_FIX",
                    "verified": "N/A",
                    "github_reply": "Posted",
                    "resolved": "YES",
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
                    lines = [f"⏸ [{state.company_id}/{state.repo_id}] {state.ticket_id} — PR review pause"]
                    lines.append(sep)
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
                sep = "─" * 30
                await self._notifier.send_message(chat_id, (
                    f"✅ [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                    f"{sep}\n"
                    f"Pipeline complete. PR ready for merge:\n"
                    f"{state.pr_url or 'N/A'}\n"
                    f"{sep}"
                ))

        # Transition Jira ticket to review status
        if self._tracker:
            project = self._projects.get(state.company_id)
            if project:
                target_status = project.config.jira.statuses.in_review
                if target_status:
                    try:
                        # First check available transitions
                        trans_data = await self._tracker._request("GET", f"/issue/{state.ticket_id}/transitions")
                        available = trans_data.get("transitions", [])
                        avail_names = [t.get("to", {}).get("name", t.get("name", "")) for t in available]

                        # Try exact match first, then fuzzy match on "review"/"qa"
                        matched = False
                        for t in available:
                            to_name = t.get("to", {}).get("name", "").lower()
                            t_name = t.get("name", "").lower()
                            if target_status.lower() in (to_name, t_name):
                                await self._tracker._request(
                                    "POST", f"/issue/{state.ticket_id}/transitions",
                                    json={"transition": {"id": t["id"]}},
                                )
                                logger.info("Transitioned %s to '%s' on Jira", state.ticket_id, target_status)
                                matched = True
                                break

                        if not matched:
                            # Try fuzzy: any transition with "review", "qa", "verification" in name
                            for t in available:
                                to_name = t.get("to", {}).get("name", "").lower()
                                t_name = t.get("name", "").lower()
                                if any(kw in to_name or kw in t_name for kw in ("review", "qa", "verification", "ready for qa")):
                                    await self._tracker._request(
                                        "POST", f"/issue/{state.ticket_id}/transitions",
                                        json={"transition": {"id": t["id"]}},
                                    )
                                    actual = t.get("to", {}).get("name", t.get("name", "?"))
                                    logger.info("Transitioned %s to '%s' (fuzzy match for '%s')", state.ticket_id, actual, target_status)
                                    matched = True
                                    break

                        if not matched:
                            logger.warning(
                                "Cannot transition %s to '%s' — available: %s",
                                state.ticket_id, target_status, avail_names,
                            )
                    except Exception as e:
                        logger.warning("Failed to transition %s on Jira: %s", state.ticket_id, e)

    _BLOCKED_REASON_MAX_CHARS = 800

    _BOILERPLATE_LINE_PATTERNS = (
        re.compile(r"^-{3,}$"),
        re.compile(r"^={3,}$"),
        re.compile(r"^\*\*Attempt.*\*\*$"),
        re.compile(r"^## Decision:"),
    )

    def _build_blocked_reason(self, workspace: Any, stage_id: str) -> str:
        """Extract a human-readable reason for why a workspace is blocked.

        For analysis: prefer reports/ba-questions.md (the BA agent's numbered
        questions). For other stages (or if ba-questions.md is absent): read the
        latest reports/*-output.md by mtime and strip header boilerplate.
        Falls back to a generic message if nothing useful is found.
        """
        reports = workspace.reports_dir
        if not reports.exists():
            return f"Pipeline stuck at {stage_id}. Check reports/ for details."

        if stage_id == "analysis":
            questions = reports / "ba-questions.md"
            if questions.exists():
                text = questions.read_text(encoding="utf-8").strip()
                if text:
                    return self._truncate_reason(text)

        outputs = sorted(
            reports.glob("*-output.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not outputs:
            return f"Pipeline stuck at {stage_id}. Check reports/ for details."

        raw = outputs[0].read_text(encoding="utf-8")
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
            return f"Pipeline stuck at {stage_id}. Check reports/ for details."

        body = "\n".join(lines[start:]).strip()
        if not body:
            return f"Pipeline stuck at {stage_id}. Check reports/ for details."
        return self._truncate_reason(body)

    @classmethod
    def _truncate_reason(cls, text: str) -> str:
        if len(text) <= cls._BLOCKED_REASON_MAX_CHARS:
            return text
        return text[: cls._BLOCKED_REASON_MAX_CHARS] + "…"

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

        stage = state.previous_state or state.current_state
        sep = "─" * 30
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("🔔", state, title)
        header = f"{hdr}\nStage: {stage}\n{sep}\n"

        reason = self._build_blocked_reason(workspace, stage.lower() if isinstance(stage, str) else stage)
        hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
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
        """Send a TG notification for a stage that just failed verification.

        Mirrors _handle_escalate semantics (populates escalation_msg_id so the
        reply flow in command_handler.handle_reply can unblock), but uses a
        distinct header to flag that this is a mechanical verification failure
        rather than an agent-requested escalation.
        """
        if not self._notifier:
            return
        chat_id = self._get_chat_id(workspace)
        if not chat_id:
            return

        sep = "─" * 30
        title = self._get_ticket_title(workspace)
        hdr = self._tg_header("⚠️", workspace.state, title)
        header = f"{hdr}\nStage: {stage_id} — verification failed\n{sep}\n"

        agent_reason = self._build_blocked_reason(workspace, stage_id)
        combined = f"Verification failed: {verify_reason}\n\n{agent_reason}"
        hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
        message = f"{header}\n{combined}{hint}"

        try:
            msg_id = await self._notifier.send_message(chat_id, message)
            workspace.update_state(human_input_question=combined)
            workspace.state.escalation_msg_id = msg_id
            workspace.state.escalation_chat_id = chat_id
            workspace.save_state()
            logger.info(
                "Verification-blocked %s via Telegram (msg_id=%d)",
                workspace.state.ticket_id, msg_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to send verification-blocked notification for %s: %s",
                workspace.state.ticket_id, e,
            )

    async def _action_finalize(self, workspace: Workspace) -> ActionResult:
        """Finalize a completed ticket. Returns ActionResult — caller transitions."""
        state = workspace.state

        if self._notifier:
            chat_id = self._get_chat_id(workspace)
            if chat_id:
                pr_url = state.pr_url or "(no PR)"
                title = self._get_ticket_title(workspace)
                hdr = self._tg_header("✅", state, title)
                message = (
                    f"{hdr}\n\n"
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
        """Append a timestamped entry to reports/pipeline-log.md."""
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
        sep = "─" * 30
        title = self._get_ticket_title(workspace)
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
            hdr = self._tg_header("⏸", state, title)
            text = (
                f"{hdr}\n"
                f"{sep}\n"
                f"PR: {state.pr_url or 'N/A'}\n"
                f"{summary}"
            )
            return text, buttons

        if gate_state == Stage.ANALYSIS:
            # Include BA summary from ba.md
            ba_file = workspace.reports_dir / "ba.md"
            summary = ""
            if ba_file.exists():
                content = ba_file.read_text(encoding="utf-8")
                # Extract first heading + summary paragraph
                lines = content.strip().splitlines()
                for line in lines:
                    if line.startswith("## Summary") or line.startswith("## Fix") or line.startswith("## Root"):
                        continue
                    if line.strip() and not line.startswith("#"):
                        summary = line.strip()[:200]
                        break
            gate_title = f"Analysis complete.\n{summary}" if summary else "Analysis complete."
            gate_title += "\n\nApprove = start coding. Reject = back to analysis."
        elif gate_state == Stage.QA:
            gate_title = "QA passed.\n\nApprove = push code & open PR. Reject = back to dev."
        else:
            gate_title = f"Awaiting approval at {gate_state}"

        hdr = self._tg_header("⏸", state, title)
        text = (
            f"{hdr}\n"
            f"{sep}\n"
            f"{gate_title}"
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
            report = workspace.reports_dir / "scope-guard-agent-output.md"
            if report.exists():
                content = report.read_text().lower()
                if _looks_like_pass(content):
                    return "pass"
                if _looks_like_fail(content):
                    return "fail"
            if _looks_like_pass(output_lower):
                return "pass"
            return "fail"

        if stage_id == "qa":
            report = workspace.reports_dir / "qa-agent-output.md"
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

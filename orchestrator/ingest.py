"""Ticket ingest: poll -> filter -> route -> create workspace.

Provider-neutral; tracker is injected via TrackerInterface. Extracted from
orchestrator.Orchestrator in Phase E.4 of the layer refactor — see
docs/features/orchestrator.md.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config.schemas import GlobalConfig, LoadedProject, RepoConfig
from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TicketData, TrackerInterface
from integrations.base.vcs import VCSInterface
from integrations.telegram.handlers.analyze import AnalyzeHandler
from orchestrator.model_resolver import resolve_ticket_model
from orchestrator.ticket_prioritizer import (
    PrioritizedTicket,
    filter_tickets,
    prioritize_tickets,
    route_tickets,
)
from orchestrator.ticket_sync import refetch_ticket_data, ticket_to_markdown
from workspace.workspace import Stage, Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


def _emit(event_bus: Any | None, event_type: str, message: str, **kwargs: Any) -> None:
    if event_bus is not None:
        event_bus.emit(event_type, message, **kwargs)


def route_manual_ticket(
    ticket: TicketData,
    projects: dict[str, LoadedProject],
) -> PrioritizedTicket | None:
    """Find the project+repo that owns this ticket via tracker_label match."""
    for project_id, project in projects.items():
        for repo_id, repo_config in project.repos.items():
            if repo_config.tracker_label and repo_config.tracker_label in ticket.labels:
                return PrioritizedTicket(
                    ticket=ticket, repo_id=repo_id, project_id=project_id,
                )
    return None


async def create_workspace_for_ticket(
    pt: PrioritizedTicket,
    project_id: str,
    repo_config: RepoConfig,
    *,
    workspace_manager: WorkspaceManager,
    tracker: TrackerInterface | None,
    default_model_provider: Any,
    repo_vcs: dict[str, tuple[VCSInterface, RepoConfig]],
    notifier: NotifierInterface | None = None,
) -> Workspace:
    """Create workspace, clone repo, write ticket data."""
    ws = workspace_manager.create(
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
    ws.state.model = resolution.model or default_model_provider()
    ws.save_state()
    if resolution.warning and tracker is not None:
        try:
            await tracker.add_comment(pt.ticket.id, resolution.warning)
        except Exception as e:
            logger.warning(
                "Failed to post model-label warning to %s: %s",
                pt.ticket.id, e,
            )

    # Write ticket metadata — calls tracker for ticket description, comments, and history
    await refetch_ticket_data(ws, tracker)

    # Fetch and write parent ticket if linked
    if tracker and pt.ticket.linked_issues:
        for link in pt.ticket.linked_issues:
            parent_key = link.get("key", "")
            if parent_key and link.get("type", "").lower() in ("is child of", "parent"):
                try:
                    parent = await tracker.get_ticket(parent_key)
                    parent_md = ticket_to_markdown(parent)
                    (ws.meta_dir / "parent.md").write_text(parent_md, encoding="utf-8")
                except Exception as e:
                    logger.warning("Failed to fetch parent %s: %s", parent_key, e)
                break

    # Transition Jira to In Progress
    if tracker:
        try:
            await tracker.transition_ticket(
                pt.ticket.id, repo_config.tracker.jira.statuses.in_progress,
            )
        except Exception as e:
            logger.warning("Failed to transition %s: %s", pt.ticket.id, e)

    # Check if a PR already exists for this ticket's branch
    vcs_entry = repo_vcs.get(pt.repo_id)
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


async def poll_and_create_workspaces(
    *,
    trackers: dict[str, TrackerInterface],
    projects: dict[str, LoadedProject],
    active_workspaces: list[Workspace],
    global_config: GlobalConfig,
    workspace_manager: WorkspaceManager,
    default_model_provider: Any,
    repo_vcs: dict[str, tuple[VCSInterface, RepoConfig]],
    notifier: NotifierInterface | None,
    dry_run: bool,
    event_bus: Any | None,
    create_workspace_fn: Any = None,
) -> list[Workspace]:
    """Poll each project's tracker for new tickets and create workspaces.

    Returns the newly created workspaces (caller appends to active list).

    `create_workspace_fn`: optional callable used to create a workspace for a
    routed ticket. Defaults to the module-level `create_workspace_for_ticket`.
    Tests inject a mock here via the Orchestrator shim so existing
    `_create_workspace_for_ticket` patches keep working.
    """
    new_workspaces: list[Workspace] = []

    for project_id, project in projects.items():
        tracker = trackers.get(project_id)
        if tracker is None:
            continue

        tracker_cfg = project.config.tracker
        if tracker_cfg.provider == "jira":
            trigger_labels = tracker_cfg.jira.trigger_labels
            ignore_labels = tracker_cfg.jira.ignore_labels
        else:  # trello (and any future provider — fall through)
            trigger_labels = tracker_cfg.trello.trigger_labels
            ignore_labels = tracker_cfg.trello.ignore_labels

        try:
            tickets = await tracker.poll_tickets()
        except Exception as e:
            logger.error(
                "Failed to poll tickets for %s (%s): %s",
                project_id, tracker_cfg.provider, e,
            )
            continue

        if not tickets:
            continue

        # Filter, route to repos, then prioritize
        filtered = filter_tickets(
            tickets,
            trigger_labels=trigger_labels,
            ignore_labels=ignore_labels,
        )
        routed = route_tickets(filtered, project)
        prioritized = prioritize_tickets(routed)
        max_parallel = project.config.parallelism.max_concurrent_tickets

        # Count active workspaces for this project (existing + newly created in this cycle)
        active_count = sum(
            1 for ws in active_workspaces
            if ws.state.company_id == project_id
        ) + sum(
            1 for ws in new_workspaces
            if ws.state.company_id == project_id
        )

        # Per-project create-workspace function captures the project's tracker
        if create_workspace_fn is None:
            async def _create(pt, pid, repo_cfg, _t=tracker):
                return await create_workspace_for_ticket(
                    pt, pid, repo_cfg,
                    workspace_manager=workspace_manager,
                    tracker=_t,
                    default_model_provider=default_model_provider,
                    repo_vcs=repo_vcs,
                    notifier=notifier,
                )
            _project_create = _create
        else:
            _project_create = create_workspace_fn

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
                for ws in active_workspaces
            ) or any(
                ws.state.ticket_id == pt.ticket.id
                for ws in new_workspaces
            )
            if already_exists:
                continue
            # Also check disk — workspace may be DONE/ARCHIVED but still on disk
            ws_dir = Path(global_config.workspaces.base_dir) / project_id / pt.repo_id / "tickets" / pt.ticket.id
            if ws_dir.exists():
                logger.debug("Workspace on disk for %s — skipping", pt.ticket.id)
                continue

            repo_config = project.repos.get(pt.repo_id)
            if not repo_config:
                continue

            if dry_run:
                logger.info(
                    "[DRY RUN] Would create workspace for %s -> %s/%s",
                    pt.ticket.id, project_id, pt.repo_id,
                )
                continue

            try:
                ws = await _project_create(pt, project_id, repo_config)
                new_workspaces.append(ws)
                active_count += 1
                logger.info(
                    "Created workspace for %s (%s/%s)",
                    pt.ticket.id, project_id, pt.repo_id,
                )
                _emit(
                    event_bus, "workspace_created",
                    f"Created workspace for {pt.ticket.id}",
                    project_id=project_id, ticket_id=pt.ticket.id,
                    data={"repo_id": pt.repo_id},
                )
            except Exception as e:
                logger.error(
                    "Failed to create workspace for %s: %s",
                    pt.ticket.id, e,
                )

    return new_workspaces


async def analyze_ticket_ids(
    ticket_ids: list[str],
    *,
    trackers: dict[str, TrackerInterface],
    projects: dict[str, LoadedProject],
    active_workspaces: list[Workspace],
    workspace_manager: WorkspaceManager,
    default_model_provider: Any,
    repo_vcs: dict[str, tuple[VCSInterface, RepoConfig]],
    dry_run: bool,
    notifier: NotifierInterface | None = None,
    create_workspace_fn: Any = None,
) -> dict[str, list[str]]:
    """Manually queue tickets for analysis (Telegram /analyze callback).

    Validates each ticket via AnalyzeHandler, skips duplicates, then
    creates a workspace for each valid one by matching its labels to a
    configured repo. Returns {"valid": [...], "invalid": [...]} where
    invalid entries are "TICKET: reason" strings.

    Mutates `active_workspaces` by appending newly created workspaces.
    """
    result: dict[str, list[str]] = {"valid": [], "invalid": []}

    if not trackers:
        for tid in ticket_ids:
            result["invalid"].append(f"{tid}: no tracker configured")
        return result

    for tid in ticket_ids:
        # Try each tracker; the first one to return a ticket wins.
        found_ticket = None
        found_tracker = None
        found_project_id = None
        for project_id, tracker in trackers.items():
            try:
                found_ticket = await tracker.get_ticket(tid)
                found_tracker = tracker
                found_project_id = project_id
                break
            except Exception:
                continue
        if found_ticket is None:
            result["invalid"].append(f"{tid}: not found in any tracker")
            continue

        handler = AnalyzeHandler(found_tracker)
        if handler.is_already_active(found_ticket.id, active_workspaces):
            result["invalid"].append(f"{found_ticket.id}: already active")
            continue

        pt = route_manual_ticket(found_ticket, projects)
        if not pt:
            result["invalid"].append(
                f"{found_ticket.id}: no matching repo label in any project",
            )
            continue

        project = projects.get(pt.project_id)
        if not project:
            result["invalid"].append(f"{found_ticket.id}: project {pt.project_id} not loaded")
            continue
        repo_config = project.repos.get(pt.repo_id)
        if not repo_config:
            result["invalid"].append(f"{found_ticket.id}: repo {pt.repo_id} not loaded")
            continue

        if create_workspace_fn is None:
            async def _create_ws(pt, project_id, repo_config, _t=found_tracker):  # type: ignore[no-redef]
                return await create_workspace_for_ticket(
                    pt, project_id, repo_config,
                    workspace_manager=workspace_manager,
                    tracker=_t,
                    default_model_provider=default_model_provider,
                    repo_vcs=repo_vcs,
                    notifier=notifier,
                )
            _ticket_create = _create_ws
        else:
            _ticket_create = create_workspace_fn

        if dry_run:
            logger.info("[DRY RUN] Would create manual workspace for %s", found_ticket.id)
            result["valid"].append(found_ticket.id)
            continue

        try:
            ws = await _ticket_create(pt, pt.project_id, repo_config)
            active_workspaces.append(ws)
            result["valid"].append(found_ticket.id)
            logger.info(
                "Manually queued %s (%s/%s)",
                found_ticket.id, pt.project_id, pt.repo_id,
            )
        except Exception as e:
            logger.error("Manual workspace creation failed for %s: %s", found_ticket.id, e)
            result["invalid"].append(f"{found_ticket.id}: {e}")

    return result

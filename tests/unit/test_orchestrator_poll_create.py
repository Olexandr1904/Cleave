"""Characterization tests for _poll_and_create_workspaces and _create_workspace_for_ticket.

Pin down: tracker polling, label-based routing, dedupe (memory + disk),
per-project parallelism cap, dry-run no-op.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.base.tracker import TicketData
from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _make_orchestrator(
    *,
    tracker=None,
    projects=None,
    active=None,
    workspaces_base: Path,
    dry_run: bool = False,
):
    """Build a minimally-wired Orchestrator with __new__ to skip __init__ deps."""
    orc = Orchestrator.__new__(Orchestrator)
    orc._tracker = tracker
    orc._projects = projects or {}
    orc._active_workspaces = active or []
    orc._dry_run = dry_run
    orc._global_config = SimpleNamespace(
        workspaces=SimpleNamespace(base_dir=str(workspaces_base)),
        defaults=SimpleNamespace(max_parallel_tickets=3),
        telegram=SimpleNamespace(default_chat_id=""),
    )
    orc._workspace_manager = MagicMock()
    orc._on_project_added = None
    orc._events = None
    orc._registry = MagicMock()
    return orc


def _ticket(ticket_id: str, labels: list[str]) -> TicketData:
    return TicketData(
        id=ticket_id, url=f"https://j/{ticket_id}",
        summary=f"summary {ticket_id}", description="", labels=labels,
    )


def _project(repo_id: str, repo_label: str, max_parallel: int = 5):
    repo = SimpleNamespace(
        repo=SimpleNamespace(id=repo_id),
        jira_repo_label=repo_label,
        git=SimpleNamespace(clone_url="https://x/y.git", depth=0),
        vcs=SimpleNamespace(
            provider="github",
            default_branch="develop",
            branch_prefix="feature",
            github=SimpleNamespace(default_branch="develop", branch_prefix="feature"),
            gitlab=SimpleNamespace(default_branch="develop", branch_prefix="feature"),
        ),
    )
    return SimpleNamespace(
        config=SimpleNamespace(
            project=SimpleNamespace(id="acme"),
            jira=SimpleNamespace(
                url="https://x", trigger_labels=["ai-pipeline"], ignore_labels=[],
            ),
            parallelism=SimpleNamespace(max_concurrent_tickets=max_parallel),
        ),
        repos={repo_id: repo},
    )


@pytest.mark.asyncio
async def test_poll_routes_by_repo_label(tmp_path: Path) -> None:
    """A ticket whose labels include repo_label gets a workspace under that repo."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [
        _ticket("PROJ-1", labels=["ai-pipeline", "android"]),
    ]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        workspaces_base=tmp_path,
    )
    orc._create_workspace_for_ticket = AsyncMock(
        return_value=SimpleNamespace(state=SimpleNamespace(
            ticket_id="PROJ-1", company_id="acme", current_state=Stage.ANALYSIS,
        )),
    )

    await orc._poll_and_create_workspaces()

    tracker.poll_tickets.assert_awaited_once()
    orc._create_workspace_for_ticket.assert_called_once()
    pt_arg = orc._create_workspace_for_ticket.call_args.args[0]
    assert pt_arg.ticket.id == "PROJ-1"
    assert pt_arg.repo_id == "android"
    args = orc._create_workspace_for_ticket.call_args.args
    assert args[1] == "acme", f"project_id should be 'acme', got {args[1]!r}"
    # repo_config identity — same object the project's repos dict holds
    project = orc._projects["acme"]
    assert args[2] is project.repos["android"], "repo_config arg must be the routed repo"


@pytest.mark.asyncio
async def test_poll_dedupes_in_memory(tmp_path: Path) -> None:
    """A ticket already in _active_workspaces is skipped."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("PROJ-1", ["ai-pipeline", "android"])]
    existing = SimpleNamespace(state=SimpleNamespace(
        ticket_id="PROJ-1", company_id="acme", current_state=Stage.DEV,
    ))
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        active=[existing], workspaces_base=tmp_path,
    )
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    tracker.poll_tickets.assert_awaited_once()
    orc._create_workspace_for_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_poll_dedupes_on_disk(tmp_path: Path) -> None:
    """A ticket whose workspace exists on disk is skipped even if not in memory."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("PROJ-1", ["ai-pipeline", "android"])]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        workspaces_base=tmp_path,
    )
    # Pre-create the workspace dir on disk
    (tmp_path / "acme" / "android" / "tickets" / "PROJ-1").mkdir(parents=True)
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    tracker.poll_tickets.assert_awaited_once()
    orc._create_workspace_for_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_poll_respects_parallel_cap(tmp_path: Path) -> None:
    """When active count >= max_concurrent_tickets, remaining tickets are skipped."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [
        _ticket(f"PROJ-{i}", ["ai-pipeline", "android"]) for i in range(5)
    ]
    active = [
        SimpleNamespace(state=SimpleNamespace(
            ticket_id=f"OLD-{i}", company_id="acme", current_state=Stage.DEV,
        )) for i in range(2)
    ]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android", max_parallel=2)},
        active=list(active), workspaces_base=tmp_path,
    )
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    tracker.poll_tickets.assert_awaited_once()
    # Already at cap (2/2) — no new workspaces
    orc._create_workspace_for_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_poll_dry_run_no_create(tmp_path: Path) -> None:
    """dry_run=True logs but does not call _create_workspace_for_ticket."""
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("PROJ-1", ["ai-pipeline", "android"])]
    orc = _make_orchestrator(
        tracker=tracker,
        projects={"acme": _project("android", "android")},
        workspaces_base=tmp_path, dry_run=True,
    )
    orc._create_workspace_for_ticket = AsyncMock()

    await orc._poll_and_create_workspaces()

    tracker.poll_tickets.assert_awaited_once()
    orc._create_workspace_for_ticket.assert_not_called()

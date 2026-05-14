"""Tests for orchestrator.ingest — multi-tracker behaviour."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_loaded_project(project_id: str):
    """Build a minimal LoadedProject for ingest tests.
    Mirrors the helper in tests/unit/test_health_runner.py:_make_project."""
    from config.schemas import (
        JiraConfig, LoadedProject, ProjectConfig, ProjectInfo,
        RepoConfig, RepoInfo, TrackerConfig, ParallelismConfig,
    )
    cfg = ProjectConfig(
        project=ProjectInfo(id=project_id, name=project_id, enabled=True),
        tracker=TrackerConfig(
            provider="jira",
            jira=JiraConfig(
                url=f"https://{project_id}.atlassian.net",
                token="t", email="bot@x.com", project_key=project_id.upper(),
                trigger_labels=["ai-pipeline"],
            ),
        ),
        parallelism=ParallelismConfig(max_concurrent_tickets=3),
    )
    repo = RepoConfig(repo=RepoInfo(id="main", name="main"), tracker_label="ai-pipeline")
    return LoadedProject(config=cfg, repos={"main": repo}, config_dir="")


@pytest.mark.asyncio
async def test_poll_isolates_per_tracker_failure(tmp_path):
    """One tracker raising must not block other trackers from being polled."""
    from config.schemas import GlobalConfig, WorkspacesConfig

    tracker_ok = AsyncMock()
    tracker_ok.poll_tickets.return_value = []  # empty — successful, no tickets
    tracker_bad = AsyncMock()
    tracker_bad.poll_tickets.side_effect = RuntimeError("boom")

    proj_a = _make_loaded_project("proj-a")
    proj_b = _make_loaded_project("proj-b")

    from orchestrator.ingest import poll_and_create_workspaces
    workspaces = await poll_and_create_workspaces(
        trackers={"proj-a": tracker_ok, "proj-b": tracker_bad},
        projects={"proj-a": proj_a, "proj-b": proj_b},
        active_workspaces=[],
        global_config=GlobalConfig(workspaces=WorkspacesConfig(base_dir=str(tmp_path))),
        workspace_manager=MagicMock(),
        default_model_provider=lambda: "model",
        repo_vcs={},
        notifier=None,
        dry_run=True,
        event_bus=None,
    )

    tracker_ok.poll_tickets.assert_awaited_once()
    tracker_bad.poll_tickets.assert_awaited_once()
    assert workspaces == []


@pytest.mark.asyncio
async def test_create_workspace_uses_schema_key_for_trello_transition():
    """Regression for C1: a Trello tracker must receive 'in_progress' (schema key),
    not a Jira-style label like 'In Progress'."""
    from unittest.mock import patch
    from config.schemas import (
        LoadedProject, ProjectConfig, ProjectInfo,
        RepoConfig, RepoInfo, TrackerConfig, TrelloConfig, ParallelismConfig,
    )
    from integrations.base.tracker import TicketData
    from orchestrator.ingest import create_workspace_for_ticket
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker.transition_ticket = AsyncMock()
    tracker.add_comment = AsyncMock()

    ticket = TicketData(
        id="Sl1", url="https://trello.com/c/Sl1", summary="Card",
        description="body", labels=["ai-pipeline"], attachments=[],
    )
    pt = PrioritizedTicket(ticket=ticket, project_id="marketing", repo_id="main")

    repo = RepoConfig(
        repo=RepoInfo(id="main", name="main"),
        tracker_label="ai-pipeline",
        tracker=TrackerConfig(
            provider="trello",
            trello=TrelloConfig(api_key="k", token="t", board_id="b"),
        ),
    )

    ws_mock = MagicMock()
    ws_mock.state.ticket_id = "Sl1"
    ws_mock.state.model = ""
    ws_mock.state.branch = None
    ws_mock.meta_dir = MagicMock()
    ws_mock.meta_dir.__truediv__ = lambda self, other: MagicMock()
    wsm = MagicMock()
    wsm.create.return_value = ws_mock

    with patch("orchestrator.ingest.refetch_ticket_data", new=AsyncMock()):
        with patch("orchestrator.ingest.resolve_ticket_model") as mock_resolve:
            mock_resolve.return_value = MagicMock(model="claude-sonnet", warning=None)
            await create_workspace_for_ticket(
                pt, "marketing", repo,
                workspace_manager=wsm,
                tracker=tracker,
                default_model_provider=lambda: "model",
                repo_vcs={},
                notifier=None,
            )

    # Critical assertion: transition_ticket must have been called with the
    # schema key "in_progress", never a Jira-style label.
    assert tracker.transition_ticket.await_count >= 1, (
        "transition_ticket was never called — fix not exercised"
    )
    call_args = tracker.transition_ticket.await_args
    status_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("status")
    forbidden = {"To Do", "In Progress", "In Review", "Done"}
    assert status_arg not in forbidden, (
        f"Trello adapter received Jira-style status label {status_arg!r}; "
        "should be schema key like 'in_progress'."
    )
    assert status_arg == "in_progress", (
        f"Expected 'in_progress' for Trello but got {status_arg!r}"
    )

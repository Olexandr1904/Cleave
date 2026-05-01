"""Tests for Orchestrator._refetch_ticket_data()."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Workspace, WorkspaceState


def _make_orch(tracker=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch._tracker = tracker
    return orch


def _make_ws(tmp_path: Path, ticket_id: str = "T-1") -> Workspace:
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    meta = ws_root / "meta"
    meta.mkdir()
    state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="co",
        repo_id="repo",
        workspace_root=str(ws_root),
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


def _make_tracker(ticket_summary="Summary", comments=None, changelog=None):
    tracker = MagicMock()
    ticket = SimpleNamespace(
        id="T-1",
        summary=ticket_summary,
        description="Description",
        status="In Progress",
        labels=[],
        attachments=[],
        linked_issues=[],
        assignee=None,
        reporter=None,
        priority=None,
        created=None,
        updated=None,
        url="https://jira.example.com/browse/T-1",
        sprint=None,
        acceptance_criteria=None,
    )
    tracker.get_ticket = AsyncMock(return_value=ticket)
    tracker._request = AsyncMock(return_value={
        "fields": {
            "comment": {"comments": comments or []},
        },
        "changelog": {"histories": changelog or []},
    })
    return tracker


@pytest.mark.asyncio
async def test_writes_fresh_ticket_md_on_first_run(tmp_path):
    tracker = _make_tracker(ticket_summary="Login screen flickers")
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "ticket.md").read_text()
    assert "Login screen flickers" in content


@pytest.mark.asyncio
async def test_appends_refresh_block_on_rerun(tmp_path):
    tracker = _make_tracker(ticket_summary="Updated description")
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)
    (ws.meta_dir / "ticket.md").write_text("# T-1\n\nOriginal description\n")

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "ticket.md").read_text()
    assert "Original description" in content
    assert "## Refresh" in content
    assert "Updated description" in content


@pytest.mark.asyncio
async def test_appends_only_new_comments(tmp_path):
    existing_comment = {
        "id": "100",
        "author": {"displayName": "Alice"},
        "created": "2026-04-01T10:00:00Z",
        "body": "First comment",
    }
    new_comment = {
        "id": "101",
        "author": {"displayName": "Bob"},
        "created": "2026-05-01T10:00:00Z",
        "body": "New comment after rerun",
    }
    tracker = _make_tracker(comments=[existing_comment, new_comment])
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)
    # Simulate existing comments.md with comment 100 already written
    (ws.meta_dir / "comments.md").write_text(
        "# Jira Comments\n\n<!-- comment:100 -->\n## Alice (2026-04-01)\n\nFirst comment\n"
    )

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "comments.md").read_text()
    assert "First comment" in content
    assert content.count("First comment") == 1  # not duplicated
    assert "New comment after rerun" in content
    assert "<!-- comment:101 -->" in content


@pytest.mark.asyncio
async def test_appends_only_new_history_entries(tmp_path):
    histories = [
        {
            "created": "2026-04-01T09:00:00Z",
            "author": {"displayName": "PM"},
            "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
        },
        {
            "created": "2026-05-01T09:00:00Z",
            "author": {"displayName": "QA"},
            "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}],
        },
    ]
    tracker = _make_tracker(changelog=histories)
    orch = _make_orch(tracker)
    ws = _make_ws(tmp_path)
    # Existing history has first entry only
    (ws.meta_dir / "history.md").write_text(
        "# Status History\n\n- 2026-04-01: To Do → In Progress by PM\n"
    )

    await orch._refetch_ticket_data(ws)

    content = (ws.meta_dir / "history.md").read_text()
    assert "To Do → In Progress" in content
    assert content.count("To Do → In Progress") == 1  # not duplicated
    assert "In Progress → Done" in content


@pytest.mark.asyncio
async def test_no_op_when_no_tracker(tmp_path):
    orch = _make_orch(tracker=None)
    ws = _make_ws(tmp_path)

    # Should not raise
    await orch._refetch_ticket_data(ws)

    assert not (ws.meta_dir / "ticket.md").exists()

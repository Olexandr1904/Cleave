"""Tests for orchestrator.ticket_sync.refetch_ticket_data()."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.base.tracker import StatusChange, TicketComment
from orchestrator.ticket_sync import refetch_ticket_data
from workspace.workspace import Workspace, WorkspaceState


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


def _make_tracker(ticket_summary="Summary", comments=None, history=None):
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
    tracker.get_comments = AsyncMock(return_value=comments or [])
    tracker.get_status_history = AsyncMock(return_value=history or [])
    return tracker


@pytest.mark.asyncio
async def test_writes_fresh_ticket_md_on_first_run(tmp_path):
    tracker = _make_tracker(ticket_summary="Login screen flickers")
    ws = _make_ws(tmp_path)

    await refetch_ticket_data(ws, tracker)

    content = (ws.meta_dir / "ticket.md").read_text()
    assert "Login screen flickers" in content


@pytest.mark.asyncio
async def test_appends_refresh_block_on_rerun(tmp_path):
    tracker = _make_tracker(ticket_summary="Updated description")
    ws = _make_ws(tmp_path)
    (ws.meta_dir / "ticket.md").write_text("# T-1\n\nOriginal description\n")

    await refetch_ticket_data(ws, tracker)

    content = (ws.meta_dir / "ticket.md").read_text()
    assert "Original description" in content
    assert "## Refresh" in content
    assert "Updated description" in content


@pytest.mark.asyncio
async def test_appends_only_new_comments(tmp_path):
    existing_comment = TicketComment(
        id="100",
        author="Alice",
        created="2026-04-01",
        body="First comment",
    )
    new_comment = TicketComment(
        id="101",
        author="Bob",
        created="2026-05-01",
        body="New comment after rerun",
    )
    tracker = _make_tracker(comments=[existing_comment, new_comment])
    ws = _make_ws(tmp_path)
    # Simulate existing comments.md with comment 100 already written
    (ws.meta_dir / "comments.md").write_text(
        "# Ticket Comments\n\n<!-- comment:100 -->\n## Alice (2026-04-01)\n\nFirst comment\n"
    )

    await refetch_ticket_data(ws, tracker)

    content = (ws.meta_dir / "comments.md").read_text()
    assert "First comment" in content
    assert content.count("First comment") == 1  # not duplicated
    assert "New comment after rerun" in content
    assert "<!-- comment:101 -->" in content


@pytest.mark.asyncio
async def test_appends_only_new_history_entries(tmp_path):
    history = [
        StatusChange(
            created="2026-04-01",
            from_status="To Do",
            to_status="In Progress",
            author="PM",
        ),
        StatusChange(
            created="2026-05-01",
            from_status="In Progress",
            to_status="Done",
            author="QA",
        ),
    ]
    tracker = _make_tracker(history=history)
    ws = _make_ws(tmp_path)
    # Existing history has first entry only
    (ws.meta_dir / "history.md").write_text(
        "# Status History\n\n- 2026-04-01: To Do → In Progress by PM\n"
    )

    await refetch_ticket_data(ws, tracker)

    content = (ws.meta_dir / "history.md").read_text()
    assert "To Do → In Progress" in content
    assert content.count("To Do → In Progress") == 1  # not duplicated
    assert "In Progress → Done" in content


@pytest.mark.asyncio
async def test_no_op_when_no_tracker(tmp_path):
    ws = _make_ws(tmp_path)

    # Should not raise
    await refetch_ticket_data(ws, None)

    assert not (ws.meta_dir / "ticket.md").exists()


class TestAttachmentFilter:
    """attachment_is_keepable governs which Jira attachments we download."""

    @pytest.mark.parametrize("filename,mime", [
        ("crash.txt", "text/plain"),
        ("crash.log", "application/octet-stream"),  # Jira often serves logs as octet-stream
        ("payload.json", "application/json"),
        ("screenshot.png", "image/png"),
        ("Foo.kt", ""),  # extension fallback when MIME missing
        ("stack.stacktrace", ""),
    ])
    def test_keeps_text_and_image_attachments(self, filename, mime):
        from orchestrator.ticket_sync import attachment_is_keepable
        assert attachment_is_keepable(filename, mime) is True

    @pytest.mark.parametrize("filename,mime", [
        ("repro.mp4", "video/mp4"),
        ("voice.m4a", "audio/mp4"),
        ("blob.bin", "application/octet-stream"),  # unknown ext, generic mime → skip
        ("archive.zip", "application/zip"),
    ])
    def test_skips_video_audio_and_unknown_binary(self, filename, mime):
        from orchestrator.ticket_sync import attachment_is_keepable
        assert attachment_is_keepable(filename, mime) is False


def test_ticket_md_lists_attachments():
    """ticket.md surfaces the attachment list so agents know what's available."""
    from orchestrator.ticket_sync import ticket_to_markdown
    from integrations.base.tracker import TicketData

    ticket = TicketData(
        id="T-1",
        url="https://jira/browse/T-1",
        summary="Crash on launch",
        description="App crashes",
        acceptance_criteria="",
        labels=[],
        priority="High",
        sprint=None,
        linked_issues=[],
        assignee=None,
        reporter="QA",
        created="",
        attachments=[
            {"filename": "crash.txt", "url": "u1", "mime_type": "text/plain"},
            {"filename": "repro.mp4", "url": "u2", "mime_type": "video/mp4"},
        ],
    )
    md = ticket_to_markdown(ticket)
    assert "## Attachments" in md
    assert "crash.txt" in md
    # Video is listed but flagged as skipped so agents don't expect content.
    assert "repro.mp4" in md
    assert "skipped" in md

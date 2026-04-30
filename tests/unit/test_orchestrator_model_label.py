"""Tests for per-ticket model label wiring in _create_workspace_for_ticket.

These tests focus on the resolver call + state persistence + comment-post
behavior. They do not exercise the full polling pipeline — that's covered
by other orchestrator tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.model_resolver import SHORT_NAME_TO_MODEL
from workspace.workspace import Workspace, WorkspaceState


@pytest.fixture
def fake_ws(tmp_path):
    """Build a minimal Workspace on disk with the directories the orchestrator writes to."""
    root = tmp_path / "fake-ws"
    root.mkdir()
    (root / "meta").mkdir()  # _create_workspace_for_ticket writes ticket.md here
    state = WorkspaceState(
        ticket_id="TEST-1",
        company_id="p",
        repo_id="r",
        workspace_root=str(root),
    )
    ws = Workspace(str(root), state)
    ws.save_state()
    return ws


def _make_ticket(labels: list[str]):
    """Build a minimal TicketData with the given labels."""
    from integrations.base.tracker import TicketData
    return TicketData(
        id="TEST-1",
        url="https://jira.example.com/browse/TEST-1",
        summary="t",
        description="d",
        labels=labels,
    )


def _make_repo_config() -> MagicMock:
    """Build a MagicMock RepoConfig with the fields _create_workspace_for_ticket reads."""
    cfg = MagicMock()
    cfg.git.clone_url = "x"
    cfg.git.depth = 1
    cfg.vcs.provider = "github"
    cfg.vcs.github.default_branch = "main"
    cfg.vcs.github.branch_prefix = "ai/"
    return cfg


def _make_orchestrator(fake_ws, tracker_mock, default_model="claude-sonnet-4-6"):
    """Build a real Orchestrator with the heavy deps stubbed.

    Bypasses __init__ and injects the attributes _create_workspace_for_ticket
    accesses. The stubs are minimal — we only need the method to reach the
    end without crashing so we can inspect state.model and add_comment calls.
    """
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._workspace_manager = MagicMock()
    orch._workspace_manager.create.return_value = fake_ws
    orch._tracker = tracker_mock
    orch._global_config = MagicMock()
    orch._emit = MagicMock()
    orch._repo_vcs = {}  # empty -> no existing-PR check
    orch._mode_handler = None
    orch._default_model_provider = lambda: default_model
    return orch


@pytest.mark.asyncio
async def test_valid_label_persists_model_and_no_comment(fake_ws):
    """Single valid label -> state.model is set, no comment posted."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock()

    orch = _make_orchestrator(fake_ws, tracker)
    pt = PrioritizedTicket(
        ticket=_make_ticket(["model-opus"]), repo_id="r", project_id="p",
    )

    await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())

    reloaded = Workspace(str(fake_ws.root))
    assert reloaded.state.model == SHORT_NAME_TO_MODEL["opus"]
    tracker.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_no_label_snapshots_global_default(fake_ws):
    """No model-* label -> state.model is set to the global default snapshot."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock()

    orch = _make_orchestrator(fake_ws, tracker, default_model="claude-haiku-4-5-20251001")
    pt = PrioritizedTicket(
        ticket=_make_ticket(["ai-pipeline"]), repo_id="r", project_id="p",
    )

    await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())

    reloaded = Workspace(str(fake_ws.root))
    assert reloaded.state.model == "claude-haiku-4-5-20251001"
    tracker.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_conflicting_labels_snapshot_default_and_post_comment(fake_ws):
    """Two model-* labels -> state.model = global default + comment posted once."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock()

    orch = _make_orchestrator(fake_ws, tracker, default_model="claude-sonnet-4-6")
    pt = PrioritizedTicket(
        ticket=_make_ticket(["model-opus", "model-haiku"]),
        repo_id="r", project_id="p",
    )

    await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())

    reloaded = Workspace(str(fake_ws.root))
    assert reloaded.state.model == "claude-sonnet-4-6"
    tracker.add_comment.assert_called_once()
    call_args = tracker.add_comment.call_args
    assert call_args.args[0] == "TEST-1"
    body = call_args.args[1]
    assert "model-opus" in body
    assert "model-haiku" in body


@pytest.mark.asyncio
async def test_comment_post_failure_does_not_abort_workspace_creation(fake_ws):
    """If add_comment raises, workspace creation still completes."""
    from orchestrator.ticket_prioritizer import PrioritizedTicket

    tracker = AsyncMock()
    tracker._request = AsyncMock(side_effect=Exception("skip jira fetch"))
    tracker.add_comment = AsyncMock(side_effect=RuntimeError("Jira down"))

    orch = _make_orchestrator(fake_ws, tracker)
    pt = PrioritizedTicket(
        ticket=_make_ticket(["model-llama"]),
        repo_id="r", project_id="p",
    )

    ws = await orch._create_workspace_for_ticket(pt, "p", _make_repo_config())
    assert ws is fake_ws
    tracker.add_comment.assert_called_once()

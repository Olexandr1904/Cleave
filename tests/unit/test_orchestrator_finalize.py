"""Characterization tests for _on_ticket_done fuzzy tracker transition.

The matching logic uses tracker.list_transitions() to fetch available
transition names and tracker.transition_ticket() to apply the first match
(exact, then fuzzy on "review"/"qa"/"verification" keywords).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _orc(tracker, project, monkeypatch):
    orc = Orchestrator.__new__(Orchestrator)
    orc._tracker = tracker
    orc._notifier = AsyncMock()
    orc._projects = {"acme": project}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None
    orc._workflow = SimpleNamespace(stages={})
    orc._repo_vcs = {}
    # Bypass repo config lookup; just return the global chat id.
    orc._get_chat_id = lambda ws: "chat-1"
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "Sample title",
    )
    return orc


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state=Stage.DONE,
        pr_url="https://g/pr/42",
    )
    ws.meta_dir = MagicMock()
    return ws


def _project(in_review="In Review"):
    return SimpleNamespace(
        config=SimpleNamespace(
            jira=SimpleNamespace(
                statuses=SimpleNamespace(in_review=in_review),
            ),
        ),
        repos={},
    )


@pytest.mark.asyncio
async def test_exact_match_transition_fires(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["In Review"]
    orc = _orc(tracker, _project("In Review"), monkeypatch)
    await orc._on_ticket_done(_ws())
    tracker.transition_ticket.assert_awaited_once()
    assert tracker.transition_ticket.await_args.args[1] == "In Review"


@pytest.mark.asyncio
async def test_fuzzy_match_on_review_keyword(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["Reviewing"]
    orc = _orc(tracker, _project("Nonexistent Status"), monkeypatch)
    await orc._on_ticket_done(_ws())
    tracker.transition_ticket.assert_awaited_once()
    assert tracker.transition_ticket.await_args.args[1] == "Reviewing"


@pytest.mark.asyncio
async def test_no_matching_transition_does_nothing_fatal(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["Closed"]
    orc = _orc(tracker, _project("In Review"), monkeypatch)
    await orc._on_ticket_done(_ws())
    tracker.transition_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_sends_completion_message(monkeypatch) -> None:
    """_on_ticket_done sends a TG message containing pipeline-complete copy."""
    tracker = AsyncMock()
    project = SimpleNamespace(
        config=SimpleNamespace(
            jira=SimpleNamespace(statuses=SimpleNamespace(in_review="")),
        ),
        repos={},
    )
    orc = _orc(tracker, project, monkeypatch)
    await orc._on_ticket_done(_ws())

    assert orc._notifier.send_message.await_count == 1
    chat_id, message = orc._notifier.send_message.await_args.args[:2]
    assert chat_id == "chat-1"
    assert "Pipeline complete" in message
    assert "https://g/pr/42" in message
    # With empty target_status, transition lookups are skipped entirely.
    tracker.list_transitions.assert_not_awaited()
    tracker.transition_ticket.assert_not_awaited()

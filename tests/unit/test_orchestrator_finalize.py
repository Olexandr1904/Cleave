"""Characterization tests for _on_ticket_done fuzzy Jira transition.

The matching logic walks tracker._request('GET', '/issue/.../transitions')
output and POSTs the first matching transition. After refactor this same
logic must work via tracker.list_transitions() + tracker.transition_ticket().
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
    tracker._request.side_effect = [
        # GET /issue/T-1/transitions
        {"transitions": [
            {"id": "21", "name": "Start Review", "to": {"name": "In Review"}},
        ]},
        # POST /issue/T-1/transitions
        {},
    ]
    orc = _orc(tracker, _project("In Review"), monkeypatch)
    await orc._on_ticket_done(_ws())
    # GET then POST = 2 requests
    assert tracker._request.await_count == 2
    post_call = tracker._request.await_args_list[1]
    assert post_call.args[0] == "POST"


@pytest.mark.asyncio
async def test_fuzzy_match_on_review_keyword(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker._request.side_effect = [
        {"transitions": [
            {"id": "31", "name": "Ready for Review", "to": {"name": "Reviewing"}},
        ]},
        {},
    ]
    orc = _orc(tracker, _project("Nonexistent Status"), monkeypatch)
    await orc._on_ticket_done(_ws())
    assert tracker._request.await_count == 2


@pytest.mark.asyncio
async def test_no_matching_transition_does_nothing_fatal(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker._request.return_value = {"transitions": [
        {"id": "11", "name": "Close as Won't Do", "to": {"name": "Closed"}},
    ]}
    orc = _orc(tracker, _project("In Review"), monkeypatch)
    # Should not raise
    await orc._on_ticket_done(_ws())
    # GET happened; POST did not
    assert tracker._request.await_count == 1


@pytest.mark.asyncio
async def test_done_sends_completion_message(monkeypatch) -> None:
    """_on_ticket_done sends a TG message containing pipeline-complete copy."""
    tracker = AsyncMock()
    tracker._request.return_value = {"transitions": []}
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

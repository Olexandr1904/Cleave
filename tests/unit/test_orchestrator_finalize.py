"""Characterization tests for on_ticket_done fuzzy tracker transition.

The matching logic uses tracker.list_transitions() to fetch available
transition names and tracker.transition_ticket() to apply the first match
(exact, then fuzzy on "review"/"qa"/"verification" keywords).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.pipeline.actions.finalize import on_ticket_done
from workspace.workspace import Stage


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state=Stage.DONE,
        pr_url="https://g/pr/42",
    )
    ws.meta_dir = MagicMock()
    return ws


@pytest.mark.asyncio
async def test_exact_match_transition_fires(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["In Review"]
    notifier = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "Sample title",
    )
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "In Review")
    tracker.transition_ticket.assert_awaited_once()
    assert tracker.transition_ticket.await_args.args[1] == "In Review"


@pytest.mark.asyncio
async def test_fuzzy_match_on_review_keyword(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["Reviewing"]
    notifier = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "Sample title",
    )
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "Nonexistent Status")
    tracker.transition_ticket.assert_awaited_once()
    assert tracker.transition_ticket.await_args.args[1] == "Reviewing"


@pytest.mark.asyncio
async def test_no_matching_transition_does_nothing_fatal(monkeypatch) -> None:
    tracker = AsyncMock()
    tracker.list_transitions.return_value = ["Closed"]
    notifier = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "Sample title",
    )
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "In Review")
    tracker.transition_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_sends_completion_message(monkeypatch) -> None:
    """on_ticket_done sends a TG message containing pipeline-complete copy."""
    tracker = AsyncMock()
    notifier = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "Sample title",
    )
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "")

    assert notifier.send_message.await_count == 1
    chat_id, message = notifier.send_message.await_args.args[:2]
    assert chat_id == "chat-1"
    assert "Pipeline complete" in message
    assert "https://g/pr/42" in message
    # With empty target_status, transition lookups are skipped entirely.
    tracker.list_transitions.assert_not_awaited()
    tracker.transition_ticket.assert_not_awaited()

"""on_ticket_done: the tracker move to the configured review status, and a done message that says what happened.

The move is the tracker's transition_ticket (Jira steps only through the statuses it is told to); no keyword
matching on the transitions offered, which once picked a "verification" status that means "merged and on stage".
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


@pytest.fixture(autouse=True)
def _title(monkeypatch):
    monkeypatch.setattr("orchestrator.tg_format.read_ticket_title", lambda w: "Sample title")


@pytest.mark.asyncio
async def test_moves_the_ticket_to_the_configured_status_and_says_so() -> None:
    tracker, notifier = AsyncMock(), AsyncMock()
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "Ready for Build")
    tracker.transition_ticket.assert_awaited_once_with("T-1", "Ready for Build")
    tracker.list_transitions.assert_not_awaited()
    assert notifier.send_message.await_args.args[1].endswith("Tracker: moved to Ready for Build.")


@pytest.mark.asyncio
async def test_a_move_that_fails_is_reported_with_its_reason() -> None:
    tracker, notifier = AsyncMock(), AsyncMock()
    tracker.transition_ticket.side_effect = ValueError(
        "Cannot transition T-1 to 'In Review'. Available transitions: ['Resolved', 'On Hold']")
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "In Review")
    text = notifier.send_message.await_args.args[1]
    assert "Ticket moved" not in text and "Tracker: moved" not in text
    assert text.endswith("Tracker: not moved to In Review: Cannot transition T-1 to 'In Review'. Available "
                         "transitions: ['Resolved', 'On Hold'].")


@pytest.mark.asyncio
async def test_no_review_status_means_no_move_and_no_tracker_line() -> None:
    tracker, notifier = AsyncMock(), AsyncMock()
    await on_ticket_done(_ws(), notifier, "chat-1", tracker, "")
    assert notifier.send_message.await_count == 1
    chat_id, message = notifier.send_message.await_args.args[:2]
    assert chat_id == "chat-1"
    assert message.endswith("Pipeline complete.\n\nPR ready for merge: https://g/pr/42")
    tracker.transition_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_handed_over_text() -> None:
    notifier = AsyncMock()
    await on_ticket_done(_ws(), notifier, "chat-1", AsyncMock(), "In Review", handed_over=True)
    text = notifier.send_message.await_args.args[1]
    assert text.endswith(
        "PR opened: https://g/pr/42\n\nReviewer mode takes it from here: its turns and review threads come to "
        "the reviewer chat.\n\nTracker: moved to In Review.")
    assert "ready for merge" not in text


@pytest.mark.asyncio
async def test_no_tracker_means_no_tracker_line() -> None:
    notifier = AsyncMock()
    await on_ticket_done(_ws(), notifier, "chat-1", None, "In Review")
    assert notifier.send_message.await_args.args[1].endswith("PR ready for merge: https://g/pr/42")

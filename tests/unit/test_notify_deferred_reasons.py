"""Tests for orchestrator.notify.notify_deferred — accurate reason in TG message.

Hard-coding "Quota exhausted" misled operators when the real cause was, e.g.,
the agent hitting max_turns; matching "usage limit" in the reason missed the
CLI's "You've hit your 5-hour limit". The caller says whether it is a quota
hit (AgentResult.failure_kind), and a quota hit gets one message per limit
window and chat, which later tickets edit to add themselves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.notify import QuotaWindow, notify_deferred


def _make_workspace(ticket_id: str = "T-1", stage: str = "QA"):
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id=ticket_id,
        company_id="acme",
        repo_id="app",
        current_state="DEFERRED",
        previous_state=stage,
    )
    return ws


def _notifier(message_id: int = 7) -> MagicMock:
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=message_id)
    notifier.edit_message = AsyncMock()
    return notifier


def _future(hours: int = 1) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0)


@pytest.mark.asyncio
async def test_quota_hit_says_usage_limit_whatever_the_cli_wrote():
    notifier = _notifier()
    retry_at = _future()

    window = await notify_deferred(
        notifier, "chat-1", _make_workspace(), retry_at, "You've hit your 5-hour limit · resets 3pm",
        quota=True, window=None,
    )

    msg = notifier.send_message.call_args.args[1]
    assert "Usage limit until" in msg
    assert retry_at.strftime("%H:%M") in msg
    assert "T-1 (QA)" in msg
    assert window == QuotaWindow(end=retry_at, message_id=7, tickets=[("T-1", "QA")])


@pytest.mark.asyncio
async def test_quota_message_has_no_retry_now_button():
    """Retry Now during a usage limit only fails again and adds two messages."""
    notifier = _notifier()

    await notify_deferred(notifier, "chat-1", _make_workspace(), _future(), "limit", quota=True, window=None)

    assert not notifier.send_message.call_args.kwargs.get("buttons")


@pytest.mark.asyncio
async def test_a_second_ticket_in_the_window_edits_the_message_instead_of_sending():
    notifier = _notifier()
    first_end = _future(1)
    window = QuotaWindow(end=first_end, message_id=7, tickets=[("T-1", "QA")])
    later = first_end + timedelta(minutes=10)

    window = await notify_deferred(
        notifier, "chat-1", _make_workspace("T-2", "DEV"), later, "limit", quota=True, window=window,
    )

    notifier.send_message.assert_not_awaited()
    chat_id, message_id, text = notifier.edit_message.call_args.args
    assert (chat_id, message_id) == ("chat-1", 7)
    assert "T-1 (QA), T-2 (DEV)" in text
    assert later.strftime("%H:%M") in text  # the window ends when its last ticket may resume
    assert window == QuotaWindow(end=later, message_id=7, tickets=[("T-1", "QA"), ("T-2", "DEV")])


@pytest.mark.asyncio
async def test_a_ticket_already_in_the_window_is_not_listed_twice():
    notifier = _notifier()
    end = _future()
    window = QuotaWindow(end=end, message_id=7, tickets=[("T-1", "QA")])

    window = await notify_deferred(
        notifier, "chat-1", _make_workspace("T-1", "QA"), end, "limit", quota=True, window=window,
    )

    notifier.send_message.assert_not_awaited()
    notifier.edit_message.assert_not_awaited()
    assert window.tickets == [("T-1", "QA")]


@pytest.mark.asyncio
async def test_a_window_that_has_ended_opens_a_new_message():
    notifier = _notifier(message_id=9)
    old = QuotaWindow(end=datetime.now(timezone.utc) - timedelta(minutes=1), message_id=7, tickets=[("T-1", "QA")])
    retry_at = _future()

    window = await notify_deferred(
        notifier, "chat-1", _make_workspace("T-2", "DEV"), retry_at, "limit", quota=True, window=old,
    )

    notifier.send_message.assert_awaited_once()
    assert window == QuotaWindow(end=retry_at, message_id=9, tickets=[("T-2", "DEV")])


@pytest.mark.asyncio
async def test_a_failed_send_opens_no_window():
    """The next quota hit then sends the message again instead of editing one nobody saw."""
    notifier = _notifier()
    notifier.send_message = AsyncMock(side_effect=RuntimeError("telegram flake"))

    window = await notify_deferred(notifier, "chat-1", _make_workspace(), _future(), "limit", quota=True, window=None)

    assert window is None


@pytest.mark.asyncio
async def test_a_failed_edit_keeps_the_ticket_in_the_window():
    notifier = _notifier()
    notifier.edit_message = AsyncMock(side_effect=RuntimeError("telegram flake"))
    end = _future()
    window = QuotaWindow(end=end, message_id=7, tickets=[("T-1", "QA")])

    window = await notify_deferred(
        notifier, "chat-1", _make_workspace("T-2", "DEV"), end, "limit", quota=True, window=window,
    )

    assert window.tickets == [("T-1", "QA"), ("T-2", "DEV")]


@pytest.mark.asyncio
async def test_usage_limit_words_in_a_transient_failure_are_not_a_quota_hit():
    notifier = _notifier()

    window = await notify_deferred(
        notifier, "chat-1", _make_workspace(), _future(), "Claude Code CLI exited: usage limit reached?",
        quota=False, window=None,
    )

    msg = notifier.send_message.call_args.args[1]
    assert "Usage limit until" not in msg
    assert "Transient agent failure" in msg
    assert window is None


@pytest.mark.asyncio
async def test_max_turns_says_max_turns_not_quota():
    """The original bug that triggered this fix: error_max_turns surfaced
    as 'Quota exhausted' even though the user had quota."""
    notifier = _notifier()

    max_turns_reason = (
        "Claude Code CLI exited with code 1. stderr='' "
        "stdout='{\"type\":\"result\",\"subtype\":\"error_max_turns\","
        "\"is_error\":true,\"num_turns\":51,...}'"
    )
    window = await notify_deferred(
        notifier, "chat-1", _make_workspace(), _future(), max_turns_reason, quota=False, window=None,
    )

    msg = notifier.send_message.call_args.args[1]
    assert "max-turns" in msg.lower() or "max turns" in msg.lower()
    assert "Usage limit" not in msg
    assert window is None


@pytest.mark.asyncio
async def test_generic_transient_includes_reason_excerpt_and_retry_now():
    notifier = _notifier()

    reason = "Claude Code CLI exited with code 1. stderr='ECONNRESET' stdout=''"
    await notify_deferred(notifier, "chat-1", _make_workspace(), _future(), reason, quota=False, window=None)

    msg = notifier.send_message.call_args.args[1]
    assert "ECONNRESET" in msg
    assert [b.action for b in notifier.send_message.call_args.kwargs["buttons"]] == ["retry:T-1"]


@pytest.mark.asyncio
async def test_no_reason_does_not_lie_about_quota():
    notifier = _notifier()

    await notify_deferred(notifier, "chat-1", _make_workspace(), _future(), None, quota=False, window=None)

    msg = notifier.send_message.call_args.args[1]
    assert "Usage limit" not in msg


@pytest.mark.asyncio
async def test_an_open_window_does_not_silence_a_transient_failure():
    """A usage-limit window must NOT silence a max_turns notification —
    they are distinct events and the operator should hear about each."""
    notifier = _notifier()
    window = QuotaWindow(end=_future(), message_id=7, tickets=[("T-1", "QA")])

    reason = "Claude Code CLI exited. stdout='{\"subtype\":\"error_max_turns\",...}'"
    kept = await notify_deferred(
        notifier, "chat-1", _make_workspace("T-2"), _future(), reason, quota=False, window=window,
    )

    notifier.send_message.assert_awaited_once()
    notifier.edit_message.assert_not_awaited()
    assert kept is window

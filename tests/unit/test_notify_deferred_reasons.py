"""Tests for orchestrator.notify.notify_deferred — accurate reason in TG message.

Hard-coding "Quota exhausted" misled operators when the real cause was, e.g.,
the agent hitting max_turns. These tests pin the contract that each known
transient-failure reason gets its own headline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.notify import notify_deferred


def _make_workspace():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="app",
        current_state="DEFERRED",
        previous_state="QA",
    )
    return ws


@pytest.mark.asyncio
async def test_real_quota_hit_says_quota_exhausted():
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    retry_at = datetime(2026, 4, 29, 17, 50, tzinfo=timezone.utc)

    quota_reason = (
        "Claude Code CLI exited with code 1. stdout='{...,"
        "\"api_error_status\":429,\"result\":\"You\\'ve hit your limit\",...}'"
    )
    new_window = await notify_deferred(
        notifier, "chat-1", _make_workspace(), retry_at, quota_reason, None,
    )

    msg = notifier.send_message.call_args.args[1]
    assert "Quota exhausted" in msg
    # Quota window debounce engaged so the same window doesn't re-notify
    assert new_window == retry_at


@pytest.mark.asyncio
async def test_max_turns_says_max_turns_not_quota():
    """The original bug that triggered this fix: error_max_turns surfaced
    as 'Quota exhausted' even though the user had quota."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    retry_at = datetime(2026, 4, 29, 17, 50, tzinfo=timezone.utc)

    max_turns_reason = (
        "Claude Code CLI exited with code 1. stderr='' "
        "stdout='{\"type\":\"result\",\"subtype\":\"error_max_turns\","
        "\"is_error\":true,\"num_turns\":51,...}'"
    )
    new_window = await notify_deferred(
        notifier, "chat-1", _make_workspace(), retry_at, max_turns_reason, None,
    )

    msg = notifier.send_message.call_args.args[1]
    assert "max-turns" in msg.lower() or "max turns" in msg.lower()
    assert "Quota exhausted" not in msg
    # Don't engage quota debounce — this isn't a quota event
    assert new_window is None


@pytest.mark.asyncio
async def test_generic_transient_includes_reason_excerpt():
    """For unrecognized transient failures, surface the actual error
    excerpt rather than pretending it was a quota hit."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    retry_at = datetime(2026, 4, 29, 17, 50, tzinfo=timezone.utc)

    reason = "Claude Code CLI exited with code 1. stderr='ECONNRESET' stdout=''"
    await notify_deferred(notifier, "chat-1", _make_workspace(), retry_at, reason, None)

    msg = notifier.send_message.call_args.args[1]
    assert "Quota exhausted" not in msg
    assert "ECONNRESET" in msg or "transient" in msg.lower()


@pytest.mark.asyncio
async def test_no_reason_does_not_lie_about_quota():
    """If the caller doesn't pass a reason, the message must say 'transient',
    not invent a quota story."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    retry_at = datetime(2026, 4, 29, 17, 50, tzinfo=timezone.utc)

    await notify_deferred(notifier, "chat-1", _make_workspace(), retry_at, None, None)

    msg = notifier.send_message.call_args.args[1]
    assert "Quota exhausted" not in msg


@pytest.mark.asyncio
async def test_quota_debounce_only_silences_real_quota():
    """A pre-existing quota window must NOT silence a max_turns notification —
    they are distinct events and the operator should hear about each."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    # Simulate prior real quota hit recorded a window
    existing_window = datetime(2026, 4, 29, 18, 0, tzinfo=timezone.utc)

    retry_at = datetime(2026, 4, 29, 17, 50, tzinfo=timezone.utc)
    reason = "Claude Code CLI exited. stdout='{\"subtype\":\"error_max_turns\",...}'"
    await notify_deferred(
        notifier, "chat-1", _make_workspace(), retry_at, reason, existing_window,
    )

    notifier.send_message.assert_awaited_once()
    msg = notifier.send_message.call_args.args[1]
    assert "max-turns" in msg.lower() or "max turns" in msg.lower()

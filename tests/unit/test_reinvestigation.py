"""Tests for the free-text → re-investigation flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram.command_handler import CommandHandler


def _handler():
    h = CommandHandler.__new__(CommandHandler)
    h._allowed_chat_ids = None
    h._notifier = MagicMock()
    h._notifier.send_message = AsyncMock()
    h._events = MagicMock()
    h._events.emit = MagicMock()
    h._wake_fn = MagicMock()
    return h


def _entry():
    return {
        "comment_id": 1, "msg_ids": [100], "decision": None,
        "author": "Copilot", "file": "x.kt", "line": 10,
        "body": "b", "reason": "r", "verdict": "Valid",
        "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
    }


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        pending_review_comments=[_entry()],
    )
    return ws


class TestStageReinvestigation:
    @pytest.mark.asyncio
    async def test_first_hint_stages_flag(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        result = await h._stage_reinvestigation(c, ws, "look at other repos", "chat-1")
        assert result is True
        assert c["pending_reinvestigation"] is True
        assert c["last_hint"] == "look at other repos"
        ws.save_state.assert_called()
        h._wake_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_hint_sends_recognition_ack(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        await h._stage_reinvestigation(c, ws, "hint", "chat-1")
        sent = h._notifier.send_message.call_args.args[1]
        assert "Recognized as hint" in sent
        assert "round 1/3" in sent
        assert "Re-checking" in sent

    @pytest.mark.asyncio
    async def test_third_round_still_allowed(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        c["hint_rounds"] = 2
        await h._stage_reinvestigation(c, ws, "third hint", "chat-1")
        assert c["pending_reinvestigation"] is True
        sent = h._notifier.send_message.call_args.args[1]
        assert "round 3/3" in sent

    @pytest.mark.asyncio
    async def test_fourth_round_rejected(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        c["hint_rounds"] = 3
        await h._stage_reinvestigation(c, ws, "fourth hint", "chat-1")
        assert c["pending_reinvestigation"] is False
        assert c["last_hint"] is None  # not stored
        sent = h._notifier.send_message.call_args.args[1]
        assert "Hint loop exceeded" in sent
        h._wake_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_staged_event(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        await h._stage_reinvestigation(c, ws, "hint", "chat-1")
        h._events.emit.assert_called()
        event_name = h._events.emit.call_args.args[0]
        assert event_name == "pr_comment_reinvestigation_staged"

    @pytest.mark.asyncio
    async def test_emits_exhausted_event_on_cap(self):
        h = _handler()
        ws = _ws()
        c = ws.state.pending_review_comments[0]
        c["hint_rounds"] = 3
        await h._stage_reinvestigation(c, ws, "fourth hint", "chat-1")
        h._events.emit.assert_called()
        event_name = h._events.emit.call_args.args[0]
        assert event_name == "pr_comment_hint_exhausted"

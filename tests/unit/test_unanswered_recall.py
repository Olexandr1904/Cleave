"""Tests for /unanswered recall + 'Show unanswered' button + reply matching."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import ParsedIntent


def _handler():
    h = CommandHandler.__new__(CommandHandler)
    h._allowed_chat_ids = None
    h._notifier = MagicMock()
    h._notifier.send_message = AsyncMock(return_value=200)
    h._events = MagicMock()
    h._events.emit = MagicMock()
    h._wake_fn = MagicMock()
    return h


def _ws_with_pending(comments):
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1",
        company_id="acme", repo_id="app", pr_number=42,
        pending_review_comments=comments,
    )
    return ws


def _entry(comment_id, decision=None, msg_ids=None):
    return {
        "comment_id": comment_id,
        "msg_ids": msg_ids or [100 + comment_id],
        "decision": decision,
        "author": "Copilot", "file": f"x{comment_id}.kt", "line": comment_id,
        "body": "b", "reason": "r", "verdict": "Valid",
        "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
    }


class TestHandleUnanswered:
    @pytest.mark.asyncio
    async def test_no_pending_comments_replies_with_empty_message(self):
        h = _handler()
        h._active_workspaces_fn = lambda: []
        intent = ParsedIntent(intent="unanswered", params={"ticket_id": ""}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)
        sent = h._notifier.send_message.call_args.args[1]
        assert "No tickets" in sent

    @pytest.mark.asyncio
    async def test_resends_only_undecided_comments(self):
        h = _handler()
        ws = _ws_with_pending([
            _entry(1),
            _entry(2, decision="fix"),
            _entry(3),
        ])
        h._active_workspaces_fn = lambda: [ws]
        intent = ParsedIntent(intent="unanswered", params={"ticket_id": ""}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)
        # 2 undecided comments + 1 summary = 3 sends
        assert h._notifier.send_message.call_count == 3
        # Comment ids 1 and 3 should have new msg_ids appended
        c1 = ws.state.pending_review_comments[0]
        c3 = ws.state.pending_review_comments[2]
        assert len(c1["msg_ids"]) == 2
        assert len(c3["msg_ids"]) == 2

    @pytest.mark.asyncio
    async def test_filters_to_specified_ticket(self):
        h = _handler()
        ws1 = _ws_with_pending([_entry(1)])
        ws1.state.ticket_id = "T-1"
        ws2 = _ws_with_pending([_entry(2)])
        ws2.state.ticket_id = "T-2"
        h._active_workspaces_fn = lambda: [ws1, ws2]

        intent = ParsedIntent(intent="unanswered", params={"ticket_id": "T-1"}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)

        # Only T-1 entry should get a new msg_id
        assert len(ws1.state.pending_review_comments[0]["msg_ids"]) == 2
        assert len(ws2.state.pending_review_comments[0]["msg_ids"]) == 1

    @pytest.mark.asyncio
    async def test_emits_recall_event(self):
        h = _handler()
        ws = _ws_with_pending([_entry(1)])
        h._active_workspaces_fn = lambda: [ws]
        intent = ParsedIntent(intent="unanswered", params={"ticket_id": ""}, reply="")
        await h._handle_unanswered(intent, "chat-1", processing_msg_id=None)
        h._events.emit.assert_called()
        names = [call.args[0] for call in h._events.emit.call_args_list]
        assert "pr_comments_unanswered_recalled" in names

    @pytest.mark.asyncio
    async def test_reply_to_recall_message_resolves_comment(self):
        """Reply to either original or recall msg_id matches."""
        h = _handler()
        c = _entry(1, msg_ids=[100, 200])  # 100 = original, 200 = recall
        ws = _ws_with_pending([c])
        ws.save_state = MagicMock()
        h._active_workspaces_fn = lambda: [ws]

        # Reply to the recall message
        await h.handle_reply(reply_to_msg_id=200, text="fix", chat_id="chat-1")
        assert ws.state.pending_review_comments[0]["decision"] == "fix"

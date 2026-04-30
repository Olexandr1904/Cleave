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


class TestOrchestratorReinvestigation:
    """Tests _action_fetch_pr_comments re-investigation phase."""

    @pytest.mark.asyncio
    async def test_pending_reinvestigation_calls_classifier_with_hint(self, tmp_path, monkeypatch):
        from orchestrator.orchestrator import Orchestrator
        from orchestrator.comment_classifier import ClassifiedComment

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(return_value=999)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._get_ticket_title = MagicMock(return_value="Ticket")
        orch._tg_header = MagicMock(return_value="hdr")
        orch._agent_runtime = MagicMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            current_state="PR_REVIEW", ticket_id="T-1",
            company_id="acme", repo_id="app",
            pr_number=42, review_cycle=1,
            human_input_reply=None, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": None,
                 "author": "C", "file": "x.kt", "line": 1, "body": "b",
                 "reason": "old reason", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": "look at repo X",
                 "pending_reinvestigation": True},
            ],
        )
        ws.save_state = MagicMock()

        async def fake_classify(comments, workspace, runtime, *, operator_hint=""):
            assert operator_hint == "look at repo X"
            return [ClassifiedComment(
                comment_id=1, classification="ESCALATE", verdict="Not valid",
                reason="new reason after re-check",
                author="C", file="x.kt", line=1, body="b",
            )]

        import orchestrator.orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "classify_comments", fake_classify, raising=False)

        await orch._reinvestigate_pending(ws)

        c = ws.state.pending_review_comments[0]
        assert c["verdict"] == "Not valid"
        assert c["reason"] == "new reason after re-check"
        assert c["hint_rounds"] == 1
        assert c["pending_reinvestigation"] is False
        # New escalation message sent → msg_id appended
        assert 999 in c["msg_ids"]
        assert len(c["msg_ids"]) == 2

    @pytest.mark.asyncio
    async def test_skips_decided_comments_during_reinvestigation(self, tmp_path):
        from orchestrator.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._events = None

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            pr_number=42,
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": "fix",
                 "pending_reinvestigation": True, "hint_rounds": 0,
                 "last_hint": "x", "verdict": "Valid", "reason": "r",
                 "author": "C", "file": "x.kt", "line": 1, "body": "b"},
            ],
        )
        ws.save_state = MagicMock()
        # Should be a no-op — comment is already decided
        await orch._reinvestigate_pending(ws)
        c = ws.state.pending_review_comments[0]
        assert c["hint_rounds"] == 0
        assert c["pending_reinvestigation"] is True  # not cleared since decided

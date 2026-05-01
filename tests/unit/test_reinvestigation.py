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
    async def test_clears_flag_on_decided_comments_during_reinvestigation(self, tmp_path):
        """When operator decided via button while re-investigation was queued,
        the pending_reinvestigation flag must be cleared (not left as True)."""
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
        # Operator already decided — flag must be cleared, classifier not called
        await orch._reinvestigate_pending(ws)
        c = ws.state.pending_review_comments[0]
        assert c["hint_rounds"] == 0
        assert c["pending_reinvestigation"] is False  # cleared since operator already decided
        ws.save_state.assert_called()

    @pytest.mark.asyncio
    async def test_reescalated_message_includes_new_verdict(self, tmp_path, monkeypatch):
        """When a comment is re-investigated and the verdict changes, the
        re-escalated TG message must contain the NEW verdict + reason."""
        from orchestrator.orchestrator import Orchestrator
        from orchestrator.comment_classifier import ClassifiedComment
        import orchestrator.orchestrator as orch_mod

        orch = Orchestrator.__new__(Orchestrator)
        sent_messages = []
        async def capture_send(chat_id, text, buttons=None):
            sent_messages.append(text)
            return 999
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock(side_effect=capture_send)
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._agent_runtime = MagicMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            current_state="PR_REVIEW", ticket_id="T-1",
            company_id="acme", repo_id="app", pr_number=42, review_cycle=1,
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
            return [ClassifiedComment(
                comment_id=1, classification="ESCALATE", verdict="Not valid",
                reason="reviewer was wrong, file follows existing pattern.",
                author="C", file="x.kt", line=1, body="b",
            )]

        monkeypatch.setattr(orch_mod, "classify_comments", fake_classify, raising=False)

        await orch._reinvestigate_pending(ws)

        assert any("Not valid — reviewer was wrong" in m for m in sent_messages)

    @pytest.mark.asyncio
    async def test_reinvestigation_first_failure_retries_silently(self, tmp_path, monkeypatch):
        """First re-investigation failure leaves flag set for retry — no TG surface."""
        from orchestrator.orchestrator import Orchestrator
        import orchestrator.orchestrator as orch_mod

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock()
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._agent_runtime = MagicMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, review_cycle=1, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": None,
                 "author": "C", "file": "x.kt", "line": 1, "body": "b",
                 "reason": "r", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": "x",
                 "pending_reinvestigation": True},
            ],
        )
        ws.save_state = MagicMock()

        async def fake_classify_fail(comments, workspace, runtime, *, operator_hint=""):
            raise RuntimeError("agent crash")
        monkeypatch.setattr(orch_mod, "classify_comments", fake_classify_fail, raising=False)

        await orch._reinvestigate_pending(ws)

        c = ws.state.pending_review_comments[0]
        assert c["pending_reinvestigation"] is True  # left for retry
        assert c.get("reinvestigation_retry_count") == 1
        # Did NOT surface to TG yet
        orch._notifier.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_button_decision_after_hint_clears_flag_on_next_orchestrator_tick(self, tmp_path, monkeypatch):
        """If the operator taps Fix while a hint is in flight, the next
        orchestrator tick should NOT re-investigate, AND should clear
        the pending_reinvestigation flag so it doesn't linger forever."""
        from orchestrator.orchestrator import Orchestrator
        import orchestrator.orchestrator as orch_mod

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock()
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._agent_runtime = MagicMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, review_cycle=1, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": "fix",  # decided!
                 "author": "C", "file": "x.kt", "line": 1, "body": "b",
                 "reason": "r", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": "x",
                 "pending_reinvestigation": True},  # but hint was queued before decision
            ],
        )
        ws.save_state = MagicMock()

        classify_called = False
        async def fake_classify(comments, workspace, runtime, *, operator_hint=""):
            nonlocal classify_called
            classify_called = True
            return []
        monkeypatch.setattr(orch_mod, "classify_comments", fake_classify, raising=False)

        await orch._reinvestigate_pending(ws)

        c = ws.state.pending_review_comments[0]
        assert c["pending_reinvestigation"] is False  # cleared
        assert classify_called is False  # never invoked classifier

    @pytest.mark.asyncio
    async def test_reinvestigation_second_failure_surfaces_and_clears(self, tmp_path, monkeypatch):
        """Second failure surfaces to TG and clears the flag."""
        from orchestrator.orchestrator import Orchestrator
        import orchestrator.orchestrator as orch_mod

        orch = Orchestrator.__new__(Orchestrator)
        orch._notifier = MagicMock()
        orch._notifier.send_message = AsyncMock()
        orch._events = None
        orch._get_chat_id = MagicMock(return_value="chat-1")
        orch._agent_runtime = MagicMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, review_cycle=1, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 1, "msg_ids": [100], "decision": None,
                 "author": "C", "file": "x.kt", "line": 1, "body": "b",
                 "reason": "r", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": "x",
                 "pending_reinvestigation": True,
                 "reinvestigation_retry_count": 1},  # already retried once
            ],
        )
        ws.save_state = MagicMock()

        async def fake_classify_fail(comments, workspace, runtime, *, operator_hint=""):
            raise RuntimeError("agent crash again")
        monkeypatch.setattr(orch_mod, "classify_comments", fake_classify_fail, raising=False)

        await orch._reinvestigate_pending(ws)

        c = ws.state.pending_review_comments[0]
        assert c["pending_reinvestigation"] is False  # cleared
        assert c.get("reinvestigation_retry_count") == 0  # reset
        orch._notifier.send_message.assert_awaited()
        sent = orch._notifier.send_message.call_args.args[1]
        assert "Re-investigation failed" in sent

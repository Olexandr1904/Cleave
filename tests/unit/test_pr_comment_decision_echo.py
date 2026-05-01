"""Tests for PR-comment decision UX:

1. The escalation message must NOT include a Skip button (the design hole).
2. The footer must explain the free-text reply path.
3. Button presses and free-text replies must echo the recorded decision back
   to TG so the operator can verify the bot interpreted them correctly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from integrations.telegram.command_handler import CommandHandler, _classify_reply
from orchestrator.orchestrator import Orchestrator


def _make_orch_for_send(notifier):
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = notifier
    orch._events = None
    orch._get_chat_id = MagicMock(return_value="chat-1")
    return orch


def _fake_workspace_for_send():
    ws = MagicMock()
    ws.state = SimpleNamespace(ticket_id="T-1", company_id="acme", repo_id="app")
    return ws


@pytest.mark.asyncio
async def test_escalation_message_renders_verdict():
    """The escalated TG message must include the one-word verdict."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    orch = _make_orch_for_send(notifier)

    cc = SimpleNamespace(
        comment_id=99, author="Copilot", file="x.kt", line=10,
        body="Suggestion", reason="Real issue.", verdict="Valid",
    )
    await orch._send_escalated_comment_tg(_fake_workspace_for_send(), cc, pr_number=1234)

    body = notifier.send_message.call_args.args[1]
    assert "Valid — Real issue." in body


@pytest.mark.asyncio
async def test_escalation_message_has_no_skip_button():
    """Skip button removed — operators interpreted it as 'I'm done with this'
    but the old semantic was 'remind me again every 30 min', which trapped
    them in a nag loop. Drop button = kill the trap."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    orch = _make_orch_for_send(notifier)

    cc = SimpleNamespace(
        comment_id=99, author="Copilot", file="x.kt", line=10,
        body="Suggestion text", reason="Real issue.",
    )
    await orch._send_escalated_comment_tg(_fake_workspace_for_send(), cc, pr_number=1234)

    notifier.send_message.assert_awaited_once()
    buttons = notifier.send_message.call_args.kwargs.get("buttons") or []
    labels = [b.label for b in buttons]
    assert "Skip" not in labels, "Skip button must be removed (UX trap)"
    assert labels == ["Fix", "Won't Fix"]


@pytest.mark.asyncio
async def test_escalation_message_includes_reply_instructions_footer():
    """Operators need to know the free-text reply path AND that they can add
    context. The buttons alone don't communicate this."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    orch = _make_orch_for_send(notifier)

    cc = SimpleNamespace(
        comment_id=99, author="Copilot", file="x.kt", line=10,
        body="Suggestion", reason="Real issue.",
    )
    await orch._send_escalated_comment_tg(_fake_workspace_for_send(), cc, pr_number=1234)

    body = notifier.send_message.call_args.args[1]
    # Must explain both the button path and the reply path
    assert "reply" in body.lower()
    assert "fix" in body.lower()
    assert "won't fix" in body.lower()
    # Must hint that free-text adds context
    assert "context" in body.lower() or "free-text" in body.lower() or "free text" in body.lower()


# --- Decision echo on button press ---


def _make_workspace_with_pending(ticket_id="T-1", state="PR_REVIEW", pending=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.pending_review_comments = pending or []
    type(ws).state = PropertyMock(return_value=ws_state)
    ws.save_state = MagicMock()
    return ws


@pytest.mark.asyncio
async def test_button_fix_press_echoes_decision_with_file_line():
    """The confirmation must show: which decision was recorded, for whose
    comment, and where. The user wanted explicit feedback after pressing
    a button so they can verify the bot didn't register the wrong choice."""
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    ws = _make_workspace_with_pending(pending=[
        {"comment_id": 99, "decision": None, "author": "Copilot",
         "file": "Frag.kt", "line": 96},
    ])
    handler = CommandHandler(
        intent_parser=AsyncMock(),
        notifier=notifier,
        mode_handler=MagicMock(get_mode=MagicMock(return_value="auto")),
        active_workspaces_fn=lambda: [ws],
    )

    await handler.handle_callback("pr_fix", "T-1:99", "12345", 42)

    sent = notifier.send_message.call_args
    msg = sent.args[1] if len(sent.args) > 1 else sent.kwargs.get("message", "")
    assert "FIX" in msg.upper()
    assert "Frag.kt:96" in msg
    assert sent.kwargs.get("reply_to_message_id") == 42


@pytest.mark.asyncio
async def test_button_wontfix_press_echoes_distinct_label():
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    ws = _make_workspace_with_pending(pending=[
        {"comment_id": 99, "decision": None, "author": "Copilot",
         "file": "Frag.kt", "line": 96},
    ])
    handler = CommandHandler(
        intent_parser=AsyncMock(),
        notifier=notifier,
        mode_handler=MagicMock(get_mode=MagicMock(return_value="auto")),
        active_workspaces_fn=lambda: [ws],
    )

    await handler.handle_callback("pr_wontfix", "T-1:99", "12345", 42)

    msg = notifier.send_message.call_args.args[1]
    assert "WON'T FIX" in msg.upper()
    assert "github" in msg.lower()


@pytest.mark.asyncio
async def test_text_reply_fix_echoes_decision():
    """Same echo for the text-reply path — operators must see what got
    interpreted, especially since we accept typos like 'fxi'."""
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    ws = _make_workspace_with_pending(pending=[
        {"comment_id": 99, "msg_id": 591, "decision": None, "author": "Copilot",
         "file": "Frag.kt", "line": 96},
    ])
    handler = CommandHandler(
        intent_parser=AsyncMock(),
        notifier=notifier,
        mode_handler=MagicMock(get_mode=MagicMock(return_value="auto")),
        active_workspaces_fn=lambda: [ws],
    )

    handled = await handler.handle_reply(591, "fxi", "12345")

    assert handled is True
    msg = notifier.send_message.call_args.args[1]
    assert "FIX" in msg.upper()
    assert "Frag.kt:96" in msg


@pytest.mark.asyncio
async def test_text_reply_wontfix_with_reason_echoes_reason():
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    ws = _make_workspace_with_pending(pending=[
        {"comment_id": 99, "msg_id": 591, "decision": None, "author": "Copilot",
         "file": "x.kt", "line": 1},
    ])
    handler = CommandHandler(
        intent_parser=AsyncMock(),
        notifier=notifier,
        mode_handler=MagicMock(get_mode=MagicMock(return_value="auto")),
        active_workspaces_fn=lambda: [ws],
    )

    await handler.handle_reply(591, "won't fix: out of scope", "12345")

    msg = notifier.send_message.call_args.args[1]
    assert "WON'T FIX" in msg.upper()
    assert "out of scope" in msg


@pytest.mark.asyncio
async def test_text_reply_freeform_routes_to_reinvestigation():
    """Free-text that doesn't match fix or won't-fix now routes to
    _stage_reinvestigation (Task 8). The old SKIP echo is gone; the stub
    returns True without sending a message."""
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    ws = _make_workspace_with_pending(pending=[
        {"comment_id": 99, "msg_id": 591, "decision": None, "author": "Copilot",
         "file": "x.kt", "line": 1},
    ])
    handler = CommandHandler(
        intent_parser=AsyncMock(),
        notifier=notifier,
        mode_handler=MagicMock(get_mode=MagicMock(return_value="auto")),
        active_workspaces_fn=lambda: [ws],
    )

    handled = await handler.handle_reply(591, "looks fine to me", "12345")

    assert handled is True
    # The stub _stage_reinvestigation doesn't send a message; confirm no SKIP echo
    for call in notifier.send_message.call_args_list:
        msg = call.args[1] if len(call.args) > 1 else call.kwargs.get("message", "")
        assert "SKIP" not in msg.upper()


@pytest.mark.asyncio
async def test_reply_matches_against_msg_ids_list():
    """Reply matching must work whether comment has 'msg_id' (old) or 'msg_ids' (new)."""
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None

    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1", company_id="acme",
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100, 200], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
             "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    matched = await handler.handle_reply(reply_to_msg_id=200, text="fix", chat_id="c-1")
    assert matched is True
    assert ws.state.pending_review_comments[0]["decision"] == "fix"


class TestClassifyReply:
    @pytest.mark.parametrize("text,expected_token", [
        ("fix", "fix"), ("FIX", "fix"), ("yes", "yes"), ("fxi", "fxi"),
        ("fixx", "fixx"), ("Fix it", "fix it"),
    ])
    def test_fix_synonyms(self, text, expected_token):
        decision, token, _ = _classify_reply(text)
        assert decision == "fix"
        assert token == expected_token

    @pytest.mark.parametrize("text,expected_token", [
        ("won't fix", "won't fix"),
        ("wont fix", "wont fix"),
        ("don't fix", "don't fix"),
        ("dont fix", "dont fix"),
        ("do not fix", "do not fix"),
        ("not fix", "not fix"),
        ("no fix", "no fix"),
        ("WON'T FIX: out of scope", "won't fix"),
    ])
    def test_wont_fix_synonyms(self, text, expected_token):
        decision, token, _reason = _classify_reply(text)
        assert decision == "wont_fix"
        assert token == expected_token

    def test_wont_fix_extracts_reason(self):
        _, _, reason = _classify_reply("don't fix: this is intentional")
        assert reason == "this is intentional"

    def test_wont_fix_no_reason(self):
        _, _, reason = _classify_reply("won't fix")
        assert reason == ""

    def test_free_text_is_reinvestigate(self):
        decision, token, _ = _classify_reply("check other repos, we use this pattern")
        assert decision == "reinvestigate"
        assert token == ""

    def test_skip_is_no_longer_recognized(self):
        """Skip semantic was removed — should now be free-text."""
        decision, _, _ = _classify_reply("skip")
        assert decision == "reinvestigate"

    def test_wont_fix_reason_preserves_case(self):
        _, _, reason = _classify_reply("Won't fix: Out Of Scope For This Ticket")
        assert reason == "Out Of Scope For This Ticket"

    def test_wont_fix_reason_preserves_case_with_dont_synonym(self):
        _, _, reason = _classify_reply("Don't fix: Already Handled Upstream")
        assert reason == "Already Handled Upstream"


@pytest.mark.asyncio
async def test_echo_includes_matched_token_for_fix():
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None
    handler._wake_fn = None
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1", company_id="acme",
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
             "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    await handler.handle_reply(reply_to_msg_id=100, text="yes", chat_id="c-1")

    sent = handler._notifier.send_message.call_args.args[1]
    assert "Recognized as FIX" in sent
    assert "matched: 'yes'" in sent


@pytest.mark.asyncio
async def test_echo_includes_matched_token_for_wont_fix():
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = None
    handler._wake_fn = None
    ws = MagicMock()
    ws.state = SimpleNamespace(
        current_state="PR_REVIEW", ticket_id="T-1", company_id="acme",
        pending_review_comments=[
            {"comment_id": 1, "msg_ids": [100], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b", "reason": "r",
             "verdict": "Valid", "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    await handler.handle_reply(reply_to_msg_id=100, text="don't fix this is intentional", chat_id="c-1")

    sent = handler._notifier.send_message.call_args.args[1]
    assert "Recognized as WON'T FIX" in sent
    # Accept either escaping style for the apostrophe
    assert "matched: 'don\\'t fix'" in sent or "matched: \"don't fix\"" in sent


@pytest.mark.asyncio
async def test_button_press_echo_includes_matched_token():
    handler = CommandHandler.__new__(CommandHandler)
    handler._allowed_chat_ids = None
    handler._notifier = MagicMock()
    handler._notifier.send_message = AsyncMock()
    handler._events = MagicMock()
    handler._events.emit = MagicMock()
    handler._wake_fn = None

    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme",
        pending_review_comments=[
            {"comment_id": 7, "msg_ids": [10], "decision": None,
             "author": "C", "file": "x.kt", "line": 1, "body": "b",
             "reason": "r", "verdict": "Valid",
             "hint_rounds": 0, "last_hint": None,
             "pending_reinvestigation": False},
        ],
    )
    handler._active_workspaces_fn = lambda: [ws]

    await handler.handle_callback(
        action="pr_fix", ticket_id="T-1:7", chat_id="chat-1", message_id=10,
    )

    sent = handler._notifier.send_message.call_args.args[1]
    assert "Recognized as FIX" in sent
    assert "matched: 'Fix' button" in sent

    # Event payload includes matched_token + via=button
    event_data = handler._events.emit.call_args.kwargs.get("data") or {}
    assert event_data.get("matched_token") == "button:fix"
    assert event_data.get("via") == "button"

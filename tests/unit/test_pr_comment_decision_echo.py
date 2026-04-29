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

from integrations.telegram.command_handler import CommandHandler
from orchestrator.orchestrator import Orchestrator


def _make_orch_for_send(notifier):
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = notifier
    orch._events = None
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._get_ticket_title = MagicMock(return_value="A ticket")
    orch._tg_header = MagicMock(return_value="💬 [acme/app] T-1")
    return orch


def _fake_workspace_for_send():
    ws = MagicMock()
    ws.state = SimpleNamespace(ticket_id="T-1", company_id="acme", repo_id="app")
    return ws


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
    assert "@Copilot" in msg
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
async def test_text_reply_freeform_shows_dropped_label():
    """Free-text that doesn't match any prefix gets stored verbatim and
    treated as skip downstream. The echo must be honest about that —
    operators shouldn't think "looks fine to me" was registered as fix."""
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

    await handler.handle_reply(591, "looks fine to me", "12345")

    msg = notifier.send_message.call_args.args[1]
    assert "SKIP" in msg.upper()
    assert "looks fine to me" in msg.lower()  # echoed so user can reconsider

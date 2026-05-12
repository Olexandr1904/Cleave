"""Characterization tests for notify_rerun and notify_verification_blocked.

These methods produce operator-visible TG messages. Pin the chat_id, message
shape (key phrases), and that buttons (when present) survive.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.escalation import build_blocked_reason
from orchestrator.notify import notify_rerun, notify_verification_blocked


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state="dev",
    )
    # build_blocked_reason inspects workspace.reports_dir.exists(); when False,
    # the function returns a short fallback string with no file IO.
    reports_dir = MagicMock()
    reports_dir.exists.return_value = False
    ws.reports_dir = reports_dir
    return ws


@pytest.mark.asyncio
async def test_notify_rerun_sends_message(monkeypatch) -> None:
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "title",
    )
    await notify_rerun(notifier, "chat-1", _ws(), "feature/T-1", "manual rerun")
    assert notifier.send_message.await_count == 1
    msg = notifier.send_message.await_args.args[1]
    assert "Rerun" in msg
    assert "manual rerun" in msg
    assert "feature/T-1" in msg


@pytest.mark.asyncio
async def test_notify_verification_blocked_sends_message(monkeypatch) -> None:
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "title",
    )
    await notify_verification_blocked(
        notifier, "chat-1", _ws(), "qa", "no new commits",
        build_blocked_reason,
    )
    assert notifier.send_message.await_count == 1
    msg = notifier.send_message.await_args.args[1]
    assert "no new commits" in msg
    assert "qa" in msg
    assert "verification failed" in msg.lower()

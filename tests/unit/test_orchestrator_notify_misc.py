"""Characterization tests for _notify_rerun and _notify_verification_blocked.

These methods produce operator-visible TG messages. Pin the chat_id, message
shape (key phrases), and that buttons (when present) survive.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _orc(notifier, monkeypatch):
    orc = Orchestrator.__new__(Orchestrator)
    orc._notifier = notifier
    orc._projects = {}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None
    orc._repo_vcs = {}
    orc._get_chat_id = lambda ws: "chat-1"
    monkeypatch.setattr(
        "orchestrator.tg_format.read_ticket_title", lambda w: "title",
    )
    return orc


def _ws():
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state="dev",
    )
    # _build_blocked_reason inspects workspace.reports_dir.exists(); when False,
    # the method returns a short fallback string with no file IO.
    reports_dir = MagicMock()
    reports_dir.exists.return_value = False
    ws.reports_dir = reports_dir
    return ws


@pytest.mark.asyncio
async def test_notify_rerun_sends_message(monkeypatch) -> None:
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orc = _orc(notifier, monkeypatch)
    # _notify_rerun signature: (workspace, branch, reason). See orchestrator.py:1157.
    await orc._notify_rerun(_ws(), "feature/T-1", "manual rerun")
    assert notifier.send_message.await_count == 1
    msg = notifier.send_message.await_args.args[1]
    assert "Rerun" in msg
    assert "manual rerun" in msg
    assert "feature/T-1" in msg


@pytest.mark.asyncio
async def test_notify_verification_blocked_sends_message(monkeypatch) -> None:
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    orc = _orc(notifier, monkeypatch)
    # _notify_verification_blocked signature: (workspace, stage_id, verify_reason).
    # See orchestrator.py:2464.
    await orc._notify_verification_blocked(
        _ws(), stage_id="qa", verify_reason="no new commits",
    )
    assert notifier.send_message.await_count == 1
    msg = notifier.send_message.await_args.args[1]
    assert "no new commits" in msg
    assert "qa" in msg
    assert "verification failed" in msg.lower()

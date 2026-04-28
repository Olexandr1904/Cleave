"""Tests for Orchestrator._notify_failed — Gradle cache button surfacing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_workspace() -> MagicMock:
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="FAILED",
        previous_state="PUSHED",
        error=None,
    )
    return ws


def _make_orch(notifier) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = notifier
    orch._events = None
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._get_ticket_title = MagicMock(return_value="A ticket")
    orch._tg_header = MagicMock(return_value="❌ [acme/acme-app] T-1")
    return orch


@pytest.mark.asyncio
async def test_notify_failed_default_only_retry_button():
    """Generic failure: only the existing Retry button is sent."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    await orch._notify_failed(_make_workspace(), "Some unrelated build error")

    notifier.send_message.assert_awaited_once()
    buttons = notifier.send_message.call_args.kwargs.get("buttons")
    assert buttons is not None
    labels = [b.label for b in buttons]
    assert labels == ["Retry"]


@pytest.mark.asyncio
async def test_notify_failed_aapt2_corruption_adds_clear_cache_button():
    """AAPT2 cache corruption: Clear-cache button is added before Retry."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    error_msg = (
        "Git command failed: git push -u origin feature/X\n"
        "AAPT2 aapt2-8.6.1-linux Daemon #2: Daemon startup failed\n"
        "FAILURE: Build failed with an exception."
    )
    await orch._notify_failed(_make_workspace(), error_msg)

    notifier.send_message.assert_awaited_once()
    args, kwargs = notifier.send_message.call_args
    buttons = kwargs.get("buttons")
    assert buttons is not None
    labels = [b.label for b in buttons]
    # Clear-cache button must come first so it's the more obvious choice
    assert labels[0] == "🧹 Clear cache & retry"
    assert labels[1] == "Retry"
    actions = [b.action for b in buttons]
    assert actions[0] == "clear_gradle:T-1"
    assert actions[1] == "retry:T-1"
    # Message should mention the detected cache corruption
    assert "AAPT2" in args[1] or "cache" in args[1].lower()


@pytest.mark.asyncio
async def test_notify_failed_no_notifier_returns_silently():
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = None
    # Should not raise
    await orch._notify_failed(_make_workspace(), "anything")

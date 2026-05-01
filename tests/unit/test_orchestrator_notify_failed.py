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
async def test_notify_failed_arch_mismatch_uses_help_message_no_clear_button():
    """x86-64 aapt2 on non-x86 host: no clear-cache button, message shows
    suggested host-level fixes."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    error_msg = (
        "Git command failed: git push -u origin feature/X\n"
        "AAPT2 aapt2-8.6.1-11315950-linux Daemon #0: Unexpected error output: "
        "/home/admin0/.gradle/caches/8.14.1/transforms/abc/transformed/"
        "aapt2-8.6.1-11315950-linux/aapt2: 2: Syntax error: \"(\" unexpected"
    )
    await orch._notify_failed(_make_workspace(), error_msg)

    notifier.send_message.assert_awaited_once()
    args, kwargs = notifier.send_message.call_args
    buttons = kwargs.get("buttons") or []
    labels = [b.label for b in buttons]
    # No clear-cache button — wiping won't help
    assert "🧹 Clear cache & retry" not in labels
    assert labels == ["Retry"]
    body = args[1]
    # Message must call out the host issue and offer concrete remediation paths
    assert "Architecture mismatch" in body
    assert "qemu-user-static" in body  # the install hint
    assert "aarch64" in body  # the gradle.properties hint
    assert "Retry" in body or "Click Retry" in body or "After fixing" in body


@pytest.mark.asyncio
async def test_notify_failed_arch_mismatch_takes_precedence_over_cache_corruption():
    """The error contains both the AAPT2 corruption substring AND the
    architecture-mismatch path. The arch path is the real diagnosis; the
    cache-corruption branch must NOT fire (else operator gets a useless
    🧹 button on a host-level problem)."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    error_msg = (
        "AAPT2 aapt2-8.6.1-11315950-linux Daemon #0: Daemon startup failed\n"
        "AAPT2 aapt2-8.6.1-11315950-linux Daemon #1: Unexpected error output: "
        "/home/admin0/.gradle/caches/8.14.1/transforms/abc/transformed/"
        "aapt2-8.6.1-11315950-linux/aapt2: 2: Syntax error: \"(\" unexpected"
    )
    await orch._notify_failed(_make_workspace(), error_msg)

    buttons = notifier.send_message.call_args.kwargs.get("buttons") or []
    labels = [b.label for b in buttons]
    assert "🧹 Clear cache & retry" not in labels
    assert "Architecture mismatch" in notifier.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_notify_failed_no_notifier_returns_silently():
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = None
    # Should not raise
    await orch._notify_failed(_make_workspace(), "anything")

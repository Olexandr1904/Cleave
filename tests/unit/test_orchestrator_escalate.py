from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.constants import REPORT_BA_QUESTIONS
from orchestrator.escalation import handle_escalate
from workspace.workspace import Stage


def _make_workspace(tmp_path: Path) -> MagicMock:
    ws = MagicMock()
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="ANALYSIS",
        previous_state="ANALYSIS",
        escalation_msg_id=None,
        escalation_chat_id=None,
        human_input_question=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.save_state = MagicMock()
    return ws


@pytest.mark.asyncio
async def test_escalate_sends_message_without_buttons(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws.reports_dir / REPORT_BA_QUESTIONS).write_text(
        "## Questions for Human Review\n\n1. [AC2] What errors?\n"
    )
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)

    await handle_escalate(ws, notifier, "chat-1")

    notifier.send_message.assert_awaited_once()
    args, kwargs = notifier.send_message.call_args
    # Signature: send_message(chat_id, message, buttons=None, reply_to_message_id=None)
    assert kwargs.get("buttons") is None
    assert "buttons" not in kwargs or kwargs["buttons"] is None
    message = args[1] if len(args) >= 2 else kwargs.get("message", "")
    assert "[AC2]" in message
    assert "Questions for Human Review" in message


@pytest.mark.asyncio
async def test_escalate_stores_reason_only_in_human_input_question(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws.reports_dir / REPORT_BA_QUESTIONS).write_text("Only the questions.\n")
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=99)

    await handle_escalate(ws, notifier, "chat-1")

    # update_state called with human_input_question=<reason only, no header, no hint>
    found = [c for c in ws.update_state.call_args_list if "human_input_question" in c.kwargs]
    assert found, "update_state must set human_input_question"
    stored = found[-1].kwargs["human_input_question"]
    assert "Only the questions." in stored
    assert "🔔" not in stored  # No header
    assert "↩️" not in stored  # No reply hint


@pytest.mark.asyncio
async def test_escalate_transitions_to_blocked_and_records_msg_id(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws.reports_dir / REPORT_BA_QUESTIONS).write_text("Q.\n")
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=123)

    await handle_escalate(ws, notifier, "chat-1")

    ws.transition.assert_called_once_with(Stage.BLOCKED)
    assert ws.state.escalation_msg_id == 123
    assert ws.state.escalation_chat_id == "chat-1"


@pytest.mark.asyncio
async def test_escalate_no_notifier_transitions_to_failed(tmp_path):
    ws = _make_workspace(tmp_path)

    await handle_escalate(ws, None, "")

    ws.transition.assert_called_once_with(Stage.FAILED)


@pytest.mark.asyncio
async def test_escalate_no_chat_id_transitions_to_failed(tmp_path):
    ws = _make_workspace(tmp_path)
    notifier = MagicMock()
    notifier.send_message = AsyncMock()

    await handle_escalate(ws, notifier, "")

    ws.transition.assert_called_once_with(Stage.FAILED)
    notifier.send_message.assert_not_called()

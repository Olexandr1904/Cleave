"""Tests for orchestrator/ticket_announce.py: the Telegram line for a dashboard action."""

from __future__ import annotations

import logging

from orchestrator.tg_i18n import tr
from orchestrator.ticket_announce import TicketAnnouncer
from workspace.workspace import Workspace, WorkspaceState


class FakeNotifier:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str, int | None]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((chat_id, text, reply_to_message_id))


def _ws(tmp_path) -> Workspace:
    return Workspace(str(tmp_path), WorkspaceState(
        ticket_id="ACME-123", company_id="acme", repo_id="app", workspace_root=str(tmp_path),
        current_state="DEV"))


async def test_announcement_is_in_the_project_language_and_replies_to_the_escalation(tmp_path):
    notifier = FakeNotifier()
    await TicketAnnouncer(notifier, lambda ws: "42", lambda p: "uk").announce(
        _ws(tmp_path), lambda: tr("approved → {state}", state="DEV"), reply_to=77)
    assert notifier.sent == [("42", "🖥 Дашборд: схвалено → DEV", 77)]


async def test_without_a_reply_the_line_follows_the_ticket_header(tmp_path):
    notifier = FakeNotifier()
    await TicketAnnouncer(notifier, lambda ws: "42", lambda p: "en").announce(
        _ws(tmp_path), lambda: tr("approved → {state}", state="DEV"))
    chat, text, reply = notifier.sent[0]
    assert (chat, reply) == ("42", None)
    assert text.startswith("🖥 [acme] ACME-123\n") and text.endswith("\nDashboard: approved → DEV")


async def test_a_failing_send_is_logged_not_raised(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        await TicketAnnouncer(FakeNotifier(fail=True), lambda ws: "42", lambda p: "en").announce(
            _ws(tmp_path), lambda: "x")
    assert "ACME-123" in caplog.text

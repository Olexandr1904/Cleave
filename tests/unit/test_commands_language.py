"""The Telegram command layer and the credential lines in the configured language: a chat's answers in the chat's
language, a ticket's in its project's, credential lines in the global one while the dashboard stays English."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from config.schemas import LoadedProject, RepoConfig, ReviewerConfig
from health.credentials import Credential, CredentialHealth
from health.validators import ValidatorResult
from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import IntentParser, ParsedIntent
from orchestrator.reviewer.db import connect
from orchestrator.switches import Switches
from orchestrator.tg_i18n import marked_texts, telegram_language
from workspace.workspace import Stage

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
CHATS = {"1": "uk", "2": "en"}  # chat id -> its projects' language
PROJECTS = {"acme": "uk", "beta": "en"}  # project id -> language


def _workspace(ticket_id: str, state: str, previous_state: str | None = None, project: str = "acme"):
    ws = MagicMock()
    s = MagicMock()
    s.ticket_id, s.current_state, s.previous_state = ticket_id, state, previous_state
    s.company_id, s.repo_id = project, "app"
    s.pending_review_comments, s.escalation_msg_id = [], None
    s.stage_iterations, s.error = {}, None
    type(ws).state = PropertyMock(return_value=s)
    return ws


def _handler(tmp_path, workspaces=(), parsed: ParsedIntent | None = None):
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    notifier.edit_message = AsyncMock()
    parser = AsyncMock()
    parser.parse = AsyncMock(return_value=parsed or ParsedIntent(intent="status", params={}))
    mode = MagicMock()
    mode.get_mode.return_value = "manual"
    projects = {"acme": LoadedProject(repos={"app": RepoConfig(reviewer=ReviewerConfig(enabled=True))})}
    handler = CommandHandler(
        intent_parser=parser, notifier=notifier, mode_handler=mode,
        active_workspaces_fn=lambda: list(workspaces), allowed_chat_ids={"1", "2"},
        switches=Switches(connect(tmp_path / "r.db")), get_projects=lambda: projects,
        language_of=lambda project_id: PROJECTS.get(project_id, "en"),
        chat_language=lambda chat_id: CHATS.get(chat_id, "en"),
    )
    return handler, notifier


def _sent(notifier) -> str:
    return notifier.send_message.await_args.args[1]


async def test_switch_change_and_status_in_the_chats_language(tmp_path):
    handler, notifier = _handler(tmp_path)
    await handler.handle_message("ревью пауза", "1")
    assert _sent(notifier).startswith("Тікети: на паузі\n· acme/app — на паузі (глобально)\nРев'ю: на паузі")
    await handler.handle_message("status", "1")
    assert "Стан Cleave" in _sent(notifier) and "Режим: manual" in _sent(notifier)
    await handler.handle_message("status", "2")  # the other chat is English
    assert _sent(notifier).startswith("Tickets: paused")


async def test_unknown_repo_keeps_the_repo_quoted(tmp_path):
    handler, notifier = _handler(tmp_path)
    await handler.handle_message("тикеты старт nope", "1")
    assert _sent(notifier) == 'Невідомий репозиторій "nope". Відомі: acme/app'


async def test_approve_answers_in_the_chats_language(tmp_path):
    ws = _workspace("ACME-1", Stage.AWAITING_APPROVAL, previous_state="QA")
    handler, notifier = _handler(tmp_path, [ws], ParsedIntent(intent="approve", params={"ticket_id": "ACME-1"}))
    await handler.handle_message("approve ACME-1", "1")
    assert notifier.send_message.await_args_list[0].args[1].startswith("⚙️ Обробляю... (approve ACME-1)")
    assert notifier.edit_message.await_args.args[2] == "ACME-1 схвалено. Переходжу до PUSHED."

    handler, notifier = _handler(tmp_path, [], ParsedIntent(intent="approve", params={}))
    await handler.handle_message("approve", "1")
    assert notifier.edit_message.await_args.args[2] == "Немає воркспейсів, що чекають на схвалення."
    await handler.handle_message("approve", "2")
    assert notifier.edit_message.await_args.args[2] == "No workspaces awaiting approval."


async def test_reply_to_an_escalation_answers_in_the_tickets_project_language(tmp_path):
    ws = _workspace("ACME-2", Stage.BLOCKED, previous_state="DEV", project="acme")
    ws.state.escalation_msg_id = 100
    handler, notifier = _handler(tmp_path, [ws])
    assert await handler.handle_reply(100, "retry", "2") is True  # an English chat
    assert _sent(notifier).endswith("\nПерезапускаю DEV.")

    other = _workspace("BETA-1", Stage.BLOCKED, previous_state="QA", project="beta")
    other.state.escalation_msg_id = 200
    handler, notifier = _handler(tmp_path, [other])
    assert await handler.handle_reply(200, "retry", "1") is True  # a Ukrainian chat, an English project
    assert _sent(notifier).endswith("\nRetrying QA.")


async def test_a_tap_answers_in_the_tickets_project_language(tmp_path):
    ws = _workspace("ACME-3", "AWAITING_APPROVAL", previous_state="PLAN_REVIEW")
    handler, notifier = _handler(tmp_path, [ws])
    await handler.handle_callback("approve", "ACME-3", "2", 42)
    assert _sent(notifier).endswith("\nСхвалено. Переходжу до DEV.")
    await handler.handle_callback("approve", "GONE-1", "1", 43)  # no workspace: the chat's language
    assert _sent(notifier) == "Для GONE-1 немає воркспейсу, що чекає на схвалення."
    await handler.handle_callback("approve", "GONE-1", "2", 44)
    assert _sent(notifier) == "No workspace awaiting approval for GONE-1."


async def test_a_pr_comment_tap_keeps_the_github_text_literal(tmp_path):
    ws = _workspace("ACME-4", "PR_REVIEW")
    ws.state.pending_review_comments = [
        {"comment_id": 7, "file": "app/Foo.kt", "line": 3, "decision": None, "msg_ids": [5]},
        {"comment_id": 8, "file": "app/Bar.kt", "line": 9, "decision": None, "msg_ids": [6]},
    ]
    handler, notifier = _handler(tmp_path, [ws])
    await handler.handle_callback("pr_wontfix", "ACME-4:7", "2", 5)
    text = _sent(notifier)
    assert 'Публікую на GitHub "operator decision" для app/Foo.kt:3.' in text
    assert "Залишилось коментарів: 1." in text
    assert notifier.send_message.await_args.kwargs["buttons"][0].label == "Показати без відповіді (1)"
    assert notifier.send_message.await_args.kwargs["buttons"][0].action == "unanswered:ACME-4"
    assert ws.state.pending_review_comments[0]["decision"] == "won't fix: operator decision"


async def test_the_status_drill_down_marks_the_agents_question(tmp_path):
    ws = _workspace("ACME-5", "BLOCKED")
    ws.state.human_input_question = "Which screen should scroll?"
    ws.state.branch, ws.state.pr_url = "feature/ACME-5", None
    handler, notifier = _handler(tmp_path, [ws], ParsedIntent(intent="status", params={"ticket_id": "ACME-5"}))
    await handler.handle_message("status ACME-5", "1")
    text = notifier.edit_message.await_args.args[2]
    assert "Етап: BLOCKED" in text and "Гілка: feature/ACME-5" in text
    assert marked_texts(text) == ["Which screen should scroll?"]  # the adapter translates it when it sends


def _cred(check):
    return Credential("forge:acme/app", "forge", "github", "GitHub acme-org/acme-app", "acme-org/acme-app",
                      "GH_T", "vcs.github.token", check)


async def test_credential_line_in_the_global_language_dashboard_fix_in_english():
    lines: list[str] = []

    async def send(text):
        lines.append(text)

    async def failing():
        return ValidatorResult(False, "forge", "t", "HTTP 401", "", extra={"status": 401})

    health = CredentialHealth(lambda: [_cred(failing)], send, now=lambda: NOW, tz=timezone.utc,
                              language=lambda: "uk")
    with telegram_language("en"):
        await health.check_all()
    assert lines == [("⚠️ GitHub acme-org/acme-app: недійсний або прострочений. "
                      "Створіть новий токен, запишіть його в .env як GH_T і перезапустіть демон.")]
    [status] = health.snapshot()
    assert status.fix == "Create a new token, put it in .env as GH_T, restart the daemon."


async def test_a_check_started_in_a_ukrainian_scope_keeps_the_dashboard_english():
    async def failing():
        return ValidatorResult(False, "forge", "t", "HTTP 403", "", extra={"status": 403})

    health = CredentialHealth(lambda: [_cred(failing)], None, now=lambda: NOW, tz=timezone.utc)
    with telegram_language("uk"):  # e.g. an adapter's 401/403 report from a Ukrainian ticket's task
        await health.check_one("forge:acme/app")
    [status] = health.snapshot()
    assert status.fix.startswith("Check the token's access to acme-org/acme-app;")


async def test_login_warning_in_ukrainian():
    lines: list[str] = []

    async def send(text):
        lines.append(text)

    async def expiring():
        return ValidatorResult(True, "claude_login", "host", "", "",
                               extra={"logged_in": True, "expires_at": "2026-11-22T12:00:00Z", "status": None})

    login = Credential("claude_login", "claude_login", "claude", "claude login", "host", "", "", expiring)
    health = CredentialHealth(lambda: [login], send, now=lambda: NOW, tz=timezone.utc, language=lambda: "uk")
    await health.check_all()
    assert lines == ["⏳ Вхід у claude на хості спливає 2026-11-22 12:00. Увійдіть знову на хості в GUI-терміналі."]


@pytest.mark.parametrize("language, named", [("uk", True), ("en", False)])
async def test_intent_prompt_asks_for_the_reply_language(language, named):
    llm = AsyncMock()
    llm.quick_query = AsyncMock(return_value='{"intent": "status", "params": {}, "reply": "ok"}')
    parser = IntentParser(llm)
    with telegram_language(language):
        await parser.parse("що там?", {})
    system = llm.quick_query.await_args.kwargs["system"]
    assert ("Ukrainian" in system) is named
    if not named:
        assert system.endswith('The "reply" field is a natural language confirmation message for the user.')

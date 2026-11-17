"""The ticket pipeline's Telegram messages in the project's language (telegram.language): Cleave's own texts from
the Ukrainian catalogue, model-written text marked for translation at send time, stored state left English."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from config.schemas import TelegramConfig
from orchestrator.constants import REPORT_BA_QUESTIONS
from orchestrator.escalation import handle_escalate
from orchestrator.escalation_view import build_escalated_comment_message
from orchestrator.notify import notify_failed, notify_verification_blocked
from orchestrator.orchestrator import Orchestrator
from orchestrator.pipeline.driver import build_gate_summary
from orchestrator.tg_i18n import current_language, fill, marked_texts, telegram_language
from workspace.workspace import Stage

QUESTION = "1. [AC2] Which error should the login screen show?"


def _workspace(tmp_path: Path) -> MagicMock:
    ws = MagicMock()
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="ACME-7", company_id="acme", repo_id="acme-app", current_state="ANALYSIS",
        previous_state="ANALYSIS", escalation_msg_id=None, escalation_chat_id=None, human_input_question=None,
        stage_iterations={}, pr_url=None,
    )
    return ws


def _notifier() -> MagicMock:
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)
    return notifier


async def test_escalation_in_ukrainian_keeps_commands_and_marks_the_reason(tmp_path):
    ws = _workspace(tmp_path)
    (ws.reports_dir / REPORT_BA_QUESTIONS).write_text(QUESTION + "\n")
    notifier = _notifier()

    with telegram_language("uk"):
        await handle_escalate(ws, notifier, "chat-1")

    text = notifier.send_message.call_args.args[1]
    assert "Етап: ANALYSIS" in text
    assert "↩️ Дайте відповідь на це повідомлення" in text
    assert 'Відповідь "skip"' in text and 'Відповідь "retry"' in text
    assert marked_texts(text) == [QUESTION]
    # The agent reads the question back on resume: stored as written, unmarked.
    ws.update_state.assert_called_with(human_input_question=QUESTION)


async def test_escalation_of_an_action_stage_in_ukrainian(tmp_path):
    ws = _workspace(tmp_path)
    ws.state.previous_state = "PUSHED"
    notifier = _notifier()

    with telegram_language("uk"):
        await handle_escalate(ws, notifier, "chat-1")

    text = notifier.send_message.call_args.args[1]
    assert "Що можна зробити:" in text
    assert 'Надішліть "retry ACME-7", щоб повторити з цього етапу' in text
    assert '"retry ACME-7 from dev"' in text


async def test_escalation_in_english_is_unchanged(tmp_path):
    ws = _workspace(tmp_path)
    (ws.reports_dir / REPORT_BA_QUESTIONS).write_text(QUESTION + "\n")
    notifier = _notifier()

    await handle_escalate(ws, notifier, "chat-1")

    text = notifier.send_message.call_args.args[1]
    assert text == (
        "🔔 [acme] ACME-7\n" + "_" * 30 + "\nStage: ANALYSIS\n\n" + QUESTION + "\n" + "─" * 30 + "\n"
        "↩️ Reply with your answer or additional context, or:\n"
        '  Reply "skip" — advance past this stage to the next one\n'
        '  Reply "retry" — re-run this stage'
    )


async def test_notify_failed_in_ukrainian(tmp_path):
    ws = _workspace(tmp_path)
    ws.state.previous_state = "QA"
    notifier = _notifier()

    with telegram_language("uk"):
        await notify_failed(notifier, "chat-1", ws, "gradle exited with 1\nstack trace")

    text = notifier.send_message.call_args.args[1]
    assert "ЗБІЙ на етапі QA." in text
    assert '"retry ACME-7 from dev"' in text
    assert marked_texts(text) == ["gradle exited with 1"]
    [button] = notifier.send_message.call_args.kwargs["buttons"]
    assert (button.label, button.action) == ("Повторити", "retry:ACME-7")


async def test_verification_blocked_shows_ukrainian_and_stores_english(tmp_path):
    ws = _workspace(tmp_path)
    notifier = _notifier()

    async def reason(_ws, _stage):
        return "**The agent** left no commit."

    with telegram_language("uk"):
        await notify_verification_blocked(notifier, "chat-1", ws, "dev", "no commits since stage start", reason)

    text = notifier.send_message.call_args.args[1]
    assert "Етап: dev — перевірка не пройшла" in text
    assert marked_texts(text) == ["no commits since stage start", "The agent left no commit."]
    assert fill(text, {}).count("Перевірка не пройшла: no commits since stage start") == 1
    ws.update_state.assert_called_with(
        human_input_question="Verification failed: no commits since stage start\n\nThe agent left no commit.")


def test_gate_summary_buttons_in_ukrainian(tmp_path):
    ws = _workspace(tmp_path)
    with telegram_language("uk"):
        text, buttons = build_gate_summary(ws, Stage.QA)
    assert [(b.label, b.action) for b in buttons] == [("Схвалити", "approve:ACME-7"), ("Відхилити", "reject:ACME-7")]
    assert "QA пройдено." in text


def test_escalated_comment_in_ukrainian_keeps_the_comment_body():
    state = SimpleNamespace(ticket_id="ACME-7", company_id="acme")
    cc = {"comment_id": 5, "author": "dev", "file": "app/Foo.kt", "line": 41, "body": "Please null-check this.",
          "reason": "The value can be null after logout.", "verdict": "Valid"}

    with telegram_language("uk"):
        text, buttons = build_escalated_comment_message(state, cc, 12, recall=True)

    assert text.startswith("🔁 (досі чекає) 💬 [acme] ACME-7")
    assert "PR #12 — коментар @dev до app/Foo.kt:41" in text
    assert "Please null-check this." in text  # the author's words, not translated
    assert "Слушне — " in text and marked_texts(text) == ["The value can be null after logout."]
    assert "won't fix: <причина>" in text
    assert [(b.label, b.action) for b in buttons] == [("Виправити", "pr_fix:ACME-7:5"),
                                                      ("Не виправляти", "pr_wontfix:ACME-7:5")]


def _orchestrator(project_language: str, global_language: str = "") -> Orchestrator:
    orc = Orchestrator.__new__(Orchestrator)
    orc._workflow = SimpleNamespace(stages={})
    orc._mode_handler = None
    orc._notifier = None
    orc._events = None
    orc._global_config = SimpleNamespace(telegram=TelegramConfig(language=global_language))
    project = SimpleNamespace(config=SimpleNamespace(telegram=TelegramConfig(language=project_language)), repos={})
    orc._projects = {"acme": project}
    return orc


async def test_advance_workspace_runs_in_the_projects_language(monkeypatch, tmp_path):
    seen = []

    async def fake_advance(workspace, **kwargs):
        seen.append(current_language())

    monkeypatch.setattr("orchestrator.pipeline.driver.advance_workspace", fake_advance)
    ws = _workspace(tmp_path)

    await _orchestrator("uk").advance_workspace(ws)
    await _orchestrator("", global_language="uk").advance_workspace(ws)
    await _orchestrator("en", global_language="uk").advance_workspace(ws)
    ws.state.company_id = "other"
    await _orchestrator("uk").advance_workspace(ws)

    assert seen == ["uk", "uk", "en", "en"]
    assert current_language() == "en"

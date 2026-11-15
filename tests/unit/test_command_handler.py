"""Tests for integrations/telegram/command_handler.py."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.handlers.approval import ApprovalHandler
from integrations.telegram.intent_parser import ParsedIntent
from workspace.workspace import Stage


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = None
    ws_state.pr_number = None
    ws_state.started_at = "2026-04-08T00:00:00Z"
    ws_state.last_updated_at = "2026-04-08T01:00:00Z"
    ws_state.stage_iterations = {}
    ws_state.error = None
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


@pytest.fixture
def mock_intent_parser():
    parser = AsyncMock()
    parser.parse = AsyncMock(return_value=ParsedIntent(
        intent="status", params={}, reply="Here is the status",
    ))
    return parser


@pytest.fixture
def mock_notifier():
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    notifier.edit_message = AsyncMock()
    notifier.delete_message = AsyncMock()
    return notifier


@pytest.fixture
def mock_mode_handler():
    handler = MagicMock()
    handler.get_mode.return_value = "auto"
    return handler


class TestCommandHandler:
    @pytest.fixture
    def command_handler(self, mock_intent_parser, mock_notifier, mock_mode_handler):
        return CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            jira_base_url="https://acme.atlassian.net",
            started_at="2026-04-08T00:00:00Z",
        )

    async def test_handle_status_sends_message(self, command_handler, mock_notifier):
        await command_handler.handle_message("what's going on", "12345")
        # Processing indicator sent, then response edited into it
        mock_notifier.send_message.assert_called_once()
        assert "Processing" in mock_notifier.send_message.call_args[0][1]
        mock_notifier.edit_message.assert_called_once()
        assert mock_notifier.edit_message.call_args[0][0] == "12345"

    async def test_handle_set_mode(self, command_handler, mock_intent_parser, mock_notifier, mock_mode_handler):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="set_mode", params={"mode": "manual"}, reply="Switched to manual",
        ))
        await command_handler.handle_message("switch to manual", "12345")
        mock_mode_handler.set_mode.assert_called_once_with("manual")

    async def test_handle_unknown_sends_help(self, command_handler, mock_intent_parser, mock_notifier):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="unknown", params={}, reply="I didn't understand that.",
        ))
        await command_handler.handle_message("gibberish", "12345")
        mock_notifier.send_message.assert_called_once()
        mock_notifier.edit_message.assert_called_once()

    async def test_handle_error_sends_error_message(self, command_handler, mock_intent_parser, mock_notifier):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="error", params={}, reply="I'm having trouble.",
        ))
        await command_handler.handle_message("hello", "12345")
        mock_notifier.edit_message.assert_called_once()
        call_text = mock_notifier.edit_message.call_args[0][2]
        assert "trouble" in call_text.lower()

    async def test_unauthorized_chat_id_is_dropped(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            allowed_chat_ids={"99999"},
        )
        await handler.handle_message("status", "12345")
        mock_intent_parser.parse.assert_not_called()
        mock_notifier.send_message.assert_not_called()

    async def test_authorized_chat_id_is_accepted(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            allowed_chat_ids={"12345"},
        )
        await handler.handle_message("status", "12345")
        mock_intent_parser.parse.assert_called_once()

    async def test_reject_disambiguates_multiple_awaiting(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        ws_a = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        ws_b = _make_workspace("T-2", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="reject", params={}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws_a, ws_b],
        )
        await handler.handle_message("reject it", "12345")
        # Should ask operator to disambiguate, not mark any as FAILED
        ws_a.transition.assert_not_called()
        ws_b.transition.assert_not_called()
        msg = mock_notifier.edit_message.call_args[0][2]
        assert "T-1" in msg and "T-2" in msg

    async def test_analyze_dispatches_callback_and_reports(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="analyze", params={"ticket_ids": ["T-1", "T-2"]}, reply="",
        ))
        callback = AsyncMock(return_value={"valid": ["T-1"], "invalid": ["T-2: not found"]})
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            analyze_callback=callback,
        )
        await handler.handle_message("analyze T-1 and T-2", "12345")
        callback.assert_awaited_once_with(["T-1", "T-2"])
        msg = mock_notifier.edit_message.call_args[0][2]
        assert "T-1" in msg
        assert "T-2: not found" in msg

    async def test_analyze_without_callback_sends_error(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="analyze", params={"ticket_ids": ["T-1"]}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
        )
        await handler.handle_message("analyze T-1", "12345")
        msg = mock_notifier.edit_message.call_args[0][2]
        assert "not available" in msg.lower()

    async def test_status_includes_recent_completions(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            recent_completions_fn=lambda: [("T-DONE", "DONE", 1_700_000_000.0)],
        )
        await handler.handle_message("status", "12345")
        msg = mock_notifier.edit_message.call_args[0][2]
        assert "T-DONE" in msg

    async def test_processing_indicator_includes_original_text(
        self, command_handler, mock_notifier,
    ):
        await command_handler.handle_message("check status please", "12345")
        processing_text = mock_notifier.send_message.call_args[0][1]
        assert "Processing" in processing_text
        assert "check status please" in processing_text

    async def test_processing_indicator_truncates_long_messages(
        self, command_handler, mock_notifier,
    ):
        long_msg = "x" * 200
        await command_handler.handle_message(long_msg, "12345")
        processing_text = mock_notifier.send_message.call_args[0][1]
        # Original text should be truncated to 100 chars
        assert len(processing_text) < 200


class TestRetryDeferred:
    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        notifier.edit_message = AsyncMock()
        return notifier

    @pytest.fixture
    def mock_mode_handler(self):
        handler = MagicMock()
        handler.get_mode.return_value = "auto"
        return handler

    async def test_retry_deferred_ticket(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-D", "DEFERRED", previous_state="QA")
        mock_intent_parser = AsyncMock()
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="retry", params={"ticket_id": "T-D"}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_message("resume T-D", "12345")
        ws.transition.assert_called_once_with("QA")

    async def test_smart_retry_with_only_a_plan_goes_through_plan_review(
        self, mock_notifier, mock_mode_handler, tmp_path,
    ):
        """ba.md but no dev output means the plan was never (or not
        successfully) reviewed — retry into the gate, not past it."""
        from orchestrator.constants import REPORT_BA
        ws = _make_workspace("T-R", "FAILED", previous_state="DEV")
        ws.reports_dir = tmp_path
        (tmp_path / REPORT_BA).write_text("# plan")
        mock_intent_parser = AsyncMock()
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="retry", params={"ticket_id": "T-R"}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_message("retry T-R", "12345")
        ws.transition.assert_called_once_with(Stage.PLAN_REVIEW)
        assert ws.state.stage_iterations["plan_review"] == 0

    async def test_retry_deferred_ticket_discriminates_branch(
        self, mock_notifier, mock_mode_handler,
    ):
        # Use a previous_state whose lowercase form IS a key in _VALID_RETRY_STATES
        # but maps to a DIFFERENT value, so the two code paths diverge:
        #   - DEFERRED branch:  target = ws.state.previous_state → "push"
        #   - fallback path:    target = _VALID_RETRY_STATES.get("push") → "PUSHED"
        # This proves the DEFERRED branch in the (BLOCKED, FAILED, DEFERRED) tuple
        # is actually taken; removing DEFERRED from the tuple will fail this test.
        ws = _make_workspace("T-D", "DEFERRED", previous_state="push")
        mock_intent_parser = AsyncMock()
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="retry", params={"ticket_id": "T-D"}, reply="",
        ))
        handler = CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_message("resume T-D", "12345")
        ws.transition.assert_called_once_with("push")


def test_set_tracker_attaches_after_init():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        jira_base_url="",
        started_at="2026-04-16T00:00:00Z",
        tracker=None,
        analyze_callback=MagicMock(),
        recent_completions_fn=lambda: [],
        allowed_chat_ids=None,
        event_bus=None,
    )
    assert handler._get_trackers() == {}

    new_tracker = MagicMock()
    handler.set_tracker(new_tracker)
    # Resolver should return the tracker wrapped under _legacy key
    assert handler._get_trackers() == {"_legacy": new_tracker}


def test_set_trackers_resolver():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    t1 = MagicMock()
    t2 = MagicMock()
    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
    )
    handler.set_trackers_resolver(lambda: {"p1": t1, "p2": t2})
    assert handler._get_trackers() == {"p1": t1, "p2": t2}


def test_get_trackers_kwarg():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    t1 = MagicMock()
    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        get_trackers=lambda: {"proj": t1},
    )
    assert handler._get_trackers() == {"proj": t1}


def test_add_allowed_chat_id_admits_new_chat():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        jira_base_url="",
        started_at="2026-04-16T00:00:00Z",
        tracker=MagicMock(),
        analyze_callback=MagicMock(),
        recent_completions_fn=lambda: [],
        allowed_chat_ids={"1001"},
        event_bus=None,
    )
    handler.add_allowed_chat_id("2002")
    assert handler._allowed_chat_ids == {"1001", "2002"}


def test_add_allowed_chat_id_noop_when_allowlist_is_none():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        jira_base_url="",
        started_at="2026-04-16T00:00:00Z",
        tracker=MagicMock(),
        analyze_callback=MagicMock(),
        recent_completions_fn=lambda: [],
        allowed_chat_ids=None,
        event_bus=None,
    )
    handler.add_allowed_chat_id("2002")
    assert handler._allowed_chat_ids is None


class TestHandleCallback:
    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        notifier.edit_message = AsyncMock()
        return notifier

    @pytest.fixture
    def mock_mode_handler(self):
        handler = MagicMock()
        handler.get_mode.return_value = "manual"
        return handler

    async def test_callback_approve(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="PLAN_REVIEW")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("approve", "T-1", "12345", 42)
        ws.transition.assert_called_once_with("DEV")
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_reject(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("reject", "T-1", "12345", 42)
        ws.transition.assert_called_once_with("FAILED")
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_reject_at_plan_review_gate_goes_to_analysis(
        self, mock_notifier, mock_mode_handler,
    ):
        """The same gate message that says "Reject = back to analysis" is the
        one this button is attached to — marking the ticket FAILED makes it a
        lie."""
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="PLAN_REVIEW")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("reject", "T-1", "12345", 42)
        ws.transition.assert_called_once_with("ANALYSIS")
        assert "FAILED" not in mock_notifier.send_message.call_args.args[1]

    async def test_callback_reviewed(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "PR_REVIEW")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        handler._wake_fn = MagicMock()
        await handler.handle_callback("reviewed", "T-1", "12345", 42)
        assert ws.state.human_input_reply == "reviewed"
        ws.save_state.assert_called_once()
        handler._wake_fn.assert_called_once()
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_retry(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "FAILED", previous_state="QA")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("retry", "T-1", "12345", 42)
        ws.transition.assert_called_once()
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_clear_gradle_clears_and_retries(self, mock_notifier, mock_mode_handler, tmp_path, monkeypatch):
        # Set up a fake Gradle home with a transforms dir we'll wipe
        fake_home = tmp_path / "gradle-home"
        transforms = fake_home / "caches" / "8.14.1" / "transforms" / "abc"
        transforms.mkdir(parents=True)
        (transforms / "binary").write_bytes(b"\x00" * 4096)
        monkeypatch.setenv("GRADLE_USER_HOME", str(fake_home))

        ws = _make_workspace("T-1", "FAILED", previous_state="PUSHED")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )

        await handler.handle_callback("clear_gradle", "T-1", "12345", 42)

        # Cache wiped
        assert not (fake_home / "caches" / "8.14.1" / "transforms").exists()
        # Workspace transitioned back to previous_state for retry
        ws.transition.assert_called_once_with(Stage.PUSHED)
        # Confirmation sent via reply
        sent = mock_notifier.send_message.call_args
        assert "Cleared" in sent[0][1]
        assert "T-1" in sent[0][1]
        assert sent.kwargs.get("reply_to_message_id") == 42

    async def test_callback_clear_gradle_unknown_ticket(self, mock_notifier, mock_mode_handler):
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
        )
        await handler.handle_callback("clear_gradle", "MISSING-1", "12345", 42)
        sent = mock_notifier.send_message.call_args[0]
        assert "No active workspace" in sent[1]

    async def test_callback_unknown_action_sends_error(self, mock_notifier, mock_mode_handler):
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
        )
        await handler.handle_callback("bogus", "T-1", "12345", 42)
        call_args = mock_notifier.send_message.call_args[0]
        assert "Unknown" in call_args[1] or "unknown" in call_args[1]


from config.schemas import LoadedProject, RepoConfig, ReviewerConfig
from orchestrator.reviewer.db import connect
from orchestrator.switches import Switches


def _switch_handler(tmp_path, parser, notifier, mode_handler):
    projects = {"acme": LoadedProject(repos={"app": RepoConfig(reviewer=ReviewerConfig(enabled=True)),
                                             "web": RepoConfig()})}
    sw = Switches(connect(tmp_path / "r.db"))
    h = CommandHandler(intent_parser=parser, notifier=notifier, mode_handler=mode_handler,
                       active_workspaces_fn=lambda: [], allowed_chat_ids={"1"},
                       switches=sw, get_projects=lambda: projects)
    return h, sw


class TestSwitchCommands:
    async def test_pause_reviews_skips_llm(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("ревью пауза", "1")
        assert sw.get("reviews") == "paused"
        mock_intent_parser.parse.assert_not_awaited()
        assert "Reviews: paused" in mock_notifier.send_message.await_args.args[1]

    async def test_repo_scoped_and_not_enabled(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("тикеты старт web", "1")
        assert sw.get("tickets", "acme/web") == "running"
        await h.handle_message("ревью пауза web", "1")
        assert sw.get("reviews", "acme/web") == "running"
        assert mock_notifier.send_message.await_args.args[1] == (
            "Reviews are not enabled for acme/web (reviewer.enabled in YAML)")

    async def test_status_shows_switches_and_summary(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, _ = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("статус", "1")
        text = mock_notifier.send_message.await_args.args[1]
        assert text.startswith("Tickets: paused") and "Reviews: running" in text
        assert text.split("\n\n", 1)[1]  # the existing summary follows
        mock_intent_parser.parse.assert_not_awaited()

    async def test_status_intent_shows_switches_first(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, _ = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("what's running", "1")
        mock_intent_parser.parse.assert_awaited_once()  # the LLM `status` intent, not the fixed command
        text = mock_notifier.edit_message.await_args.args[2]
        assert text.startswith("Tickets: paused\n· acme/app — paused (global)")
        assert text.split("\n\n", 1)[1].startswith("Cleave Status")  # then the existing summary

    async def test_status_intent_with_ticket_id_has_no_switches(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, _ = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        mock_intent_parser.parse.return_value = ParsedIntent(intent="status", params={"ticket_id": "ACME-1"})
        await h.handle_message("status of ACME-1", "1")
        assert mock_notifier.edit_message.await_args.args[2] == "No active workspace found for ACME-1."

    async def test_disallowed_chat_ignored(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("ревью пауза", "999")
        assert sw.get("reviews") == "running"
        mock_notifier.send_message.assert_not_awaited()

    async def test_mixed_case_repo_id(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        h._get_projects = lambda: {"acme": LoadedProject(repos={"App": RepoConfig()})}
        await h.handle_message("тикеты старт App", "1")
        assert sw.get("tickets", "acme/App") == "running"


NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)  # about when the operator replies


def _reviewer(anchors=(("1", 100),), repos=True):
    """Task 14's ReviewerReplies as the command handler sees it: (chat id, message id) anchors, whether a repo is in
    reviewer mode; calls recorded."""
    reviewer = MagicMock()
    reviewer.has_repos.return_value = repos
    reviewer.is_anchor.side_effect = lambda chat_id, message_id: (chat_id, message_id) in anchors
    reviewer.handle_reply = AsyncMock()
    reviewer.handle_callback = AsyncMock()
    reviewer.anchor_answer = MagicMock()
    return reviewer


def _reply_update(text, reply_to=100, chat=1, sent_at=NOW - timedelta(minutes=1)):
    update = MagicMock()
    update.message.reply_to_message.message_id = reply_to
    update.message.text = text
    update.message.chat.id = chat
    update.message.date = sent_at
    return update


def _tap_update(data, message_id=55, chat=1, sent_at=NOW - timedelta(minutes=1)):
    update = MagicMock()
    update.callback_query.data = data
    update.callback_query.message.chat.id = chat
    update.callback_query.message.message_id = message_id
    update.callback_query.message.date = sent_at  # the button's message's (the echo's), not the tap's
    update.callback_query.answer = AsyncMock()
    return update


class TestReviewerReplies:
    """A reply to a reviewer message (a reply anchor), and a tap on an rv_ button, go to the reviewer before anything
    else, and never to the ticket pipeline: its intent parser would read a review message's own hint
    ("post 1 2 4 approve") as an approval."""

    NOT_HANDLED = "Reply not handled: the reviewer's store could not be read; nothing was done."
    OFF = "Reviewer mode is off on this daemon: nothing was done."

    def _handler(self, parser, notifier, mode_handler, workspaces=(), reviewer=None, allowed=frozenset({"1"}),
                 reviewer_chats=frozenset({"1"})):
        handler = CommandHandler(
            intent_parser=parser, notifier=notifier, mode_handler=mode_handler,
            active_workspaces_fn=MagicMock(return_value=list(workspaces)),
            allowed_chat_ids=None if allowed is None else set(allowed), reviewer_chat_ids=set(reviewer_chats),
            reviewer=reviewer,
        )
        handler._approval_handler = MagicMock()
        handler._wake_fn = MagicMock()
        return handler

    def _adapter(self, handler):
        from integrations.telegram.telegram_adapter import TelegramAdapter
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
        adapter.set_command_handler(handler)
        return adapter

    async def test_review_reply_never_reaches_intent_parser(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        reviewer = _reviewer()
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer)
        await self._adapter(handler)._handle_incoming(_reply_update("approve"), None)
        # with the reply's Telegram date: the reviewer applies the age limit
        reviewer.handle_reply.assert_awaited_once_with("1", 100, "approve", sent_at=NOW - timedelta(minutes=1))
        mock_intent_parser.parse.assert_not_awaited()
        assert handler._approval_handler.mock_calls == [] and handler._wake_fn.mock_calls == []
        handler._active_workspaces_fn.assert_not_called()
        mock_notifier.send_message.assert_not_awaited()   # the reviewer answers, through its own chat

    async def test_lookup_error_with_ticket_match_goes_to_pipeline(
        self, mock_intent_parser, mock_notifier, mock_mode_handler, caplog,
    ):
        blocked = _make_workspace("ACME-1", Stage.BLOCKED, previous_state=Stage.DEV)
        blocked.state.escalation_msg_id = 100
        blocked.meta_dir = "/nonexistent"
        reviewer = _reviewer()
        reviewer.is_anchor.side_effect = sqlite3.OperationalError("database is locked")
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, [blocked], reviewer)
        with caplog.at_level(logging.WARNING, logger="integrations.telegram.command_handler"):
            assert await handler.handle_reply(100, "retry", "1") is True
        blocked.transition.assert_called_once_with(Stage.DEV)
        assert "Retrying" in mock_notifier.send_message.await_args.args[1]
        reviewer.handle_reply.assert_not_awaited()
        assert "Reply anchor not looked up: OperationalError" in caplog.text
        assert "database is locked" not in caplog.text

    async def test_lookup_error_without_ticket_match_is_swallowed(
        self, mock_intent_parser, mock_notifier, mock_mode_handler, caplog,
    ):
        reviewer = _reviewer()
        reviewer.is_anchor.side_effect = sqlite3.OperationalError("database is locked")
        # the answer is anchored best effort: the store may be what failed
        reviewer.anchor_answer.side_effect = [None, sqlite3.OperationalError("database is locked")]
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer)
        await self._adapter(handler)._handle_incoming(_reply_update("post 1 2 4 approve"), None)
        with caplog.at_level(logging.WARNING, logger="integrations.telegram.command_handler"):
            assert await handler.handle_reply(100, "post 1 2 4 approve", "1") is True
        assert mock_notifier.send_message.await_args_list == [call("1", self.NOT_HANDLED)] * 2
        assert reviewer.anchor_answer.call_args_list == [call("1", 1)] * 2   # the id send_message returned
        assert "Reply-not-handled answer not anchored: OperationalError" in caplog.text
        assert "database is locked" not in caplog.text
        mock_intent_parser.parse.assert_not_awaited()
        reviewer.handle_reply.assert_not_awaited()
        assert handler._approval_handler.mock_calls == [] and handler._wake_fn.mock_calls == []

    async def test_reply_to_another_message_behaves_as_before(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        blocked = _make_workspace("ACME-1", Stage.BLOCKED, previous_state=Stage.DEV)
        blocked.state.escalation_msg_id = 200
        blocked.meta_dir = "/nonexistent"
        reviewer = _reviewer()
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, [blocked], reviewer)
        assert await handler.handle_reply(200, "retry", "1") is True
        blocked.transition.assert_called_once_with(Stage.DEV)
        assert "Retrying" in mock_notifier.send_message.await_args.args[1]
        assert await handler.handle_reply(300, "approve", "1") is False   # left to handle_message
        assert await handler.handle_reply(100, "approve", "2") is False   # not an allowlisted chat
        mock_intent_parser.parse.assert_not_awaited()
        reviewer.handle_reply.assert_not_awaited()

    async def test_without_a_reviewer_replies_are_as_before(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(intent_parser=mock_intent_parser, notifier=mock_notifier,
                                 mode_handler=mock_mode_handler, active_workspaces_fn=lambda: [])
        assert await handler.handle_reply(100, "approve", "1") is False
        mock_notifier.send_message.assert_not_awaited()

    async def test_a_disallowed_chats_reply_is_ignored(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        reviewer = _reviewer()
        reviewer.is_anchor.side_effect = lambda chat_id, message_id: True   # an anchor in any chat
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer)
        assert await handler.handle_reply(100, "post 1 2 4 approve", "2") is False
        mock_notifier.send_message.assert_not_awaited()
        reviewer.is_anchor.assert_not_called()
        reviewer.handle_reply.assert_not_awaited()

    async def test_rv_callbacks_routed(self, mock_intent_parser, mock_notifier, mock_mode_handler):
        reviewer = _reviewer()
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer)
        await handler.handle_callback("rv_ok", "abc123", "1", 55)
        await handler.handle_callback("rv_no", "abc123", "1", 55)
        assert reviewer.handle_callback.await_args_list == [call("rv_ok", "abc123", "1", 55),
                                                            call("rv_no", "abc123", "1", 55)]
        handler._active_workspaces_fn.assert_not_called()
        mock_notifier.send_message.assert_not_awaited()

        await handler.handle_callback("rv_ok", "abc123", "2", 55)   # a disallowed chat: nothing
        assert reviewer.handle_callback.await_count == 2
        mock_notifier.send_message.assert_not_awaited()

        off = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=None)
        await off.handle_callback("rv_ok", "abc123", "1", 55)
        mock_notifier.send_message.assert_awaited_once_with("1", self.OFF, reply_to_message_id=55)
        off._active_workspaces_fn.assert_not_called()
        await off.handle_callback("rv_ok", "abc123", "2", 55)
        assert mock_notifier.send_message.await_count == 1

    async def test_reviewer_path_only_in_reviewer_chats(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """An empty allowlist leaves the ticket pipeline open to every chat (None), but never the reviewer: its
        replies and taps are read only in the configured chats, and with none configured, in none. A reply to one
        of its messages elsewhere is dropped; any other reply takes the ticket path, as before."""
        reviewer = _reviewer(anchors=(("42", 100),))
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer,
                                allowed=None, reviewer_chats=())
        assert await handler.handle_reply(100, "post 1", "42") is True   # the reviewer's message: dropped
        assert await handler.handle_reply(300, "approve", "42") is False   # left to the ticket pipeline
        await handler.handle_callback("rv_ok", "abc123", "42", 55)
        assert reviewer.is_anchor.call_args_list == [call("42", 100), call("42", 300)]
        reviewer.handle_reply.assert_not_awaited()
        reviewer.handle_callback.assert_not_awaited()
        reviewer.anchor_answer.assert_not_called()
        mock_notifier.send_message.assert_not_awaited()

        off = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=None,
                            allowed=None, reviewer_chats=())
        await off.handle_callback("rv_ok", "abc123", "42", 55)   # not even the "off" answer
        mock_notifier.send_message.assert_not_awaited()

    async def test_a_reply_to_a_reviewer_message_in_another_chat_is_dropped(
        self, mock_intent_parser, mock_notifier, mock_mode_handler, caplog,
    ):
        """In a chat the reviewer does not read (an empty allowlist admits it for tickets), a reply to a reviewer
        message reaches neither the reviewer nor the ticket pipeline's handle_message: its intent parser would read
        "post 1 2 4 approve" as an approval. A failed anchor lookup drops the reply too (fails closed)."""
        reviewer = _reviewer(anchors=(("42", 100),))
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer,
                                allowed=None, reviewer_chats=())
        handler.handle_message = AsyncMock()
        adapter = self._adapter(handler)
        with caplog.at_level(logging.WARNING, logger="integrations.telegram.command_handler"):
            await adapter._handle_incoming(_reply_update("post 1 2 4 approve", chat=42), None)
            reviewer.is_anchor.side_effect = sqlite3.OperationalError("database is locked")
            await adapter._handle_incoming(_reply_update("post 1 2 4 approve", chat=42), None)
        handler.handle_message.assert_not_awaited()
        mock_intent_parser.parse.assert_not_awaited()
        reviewer.handle_reply.assert_not_awaited()
        reviewer.anchor_answer.assert_not_called()
        handler._active_workspaces_fn.assert_not_called()
        mock_notifier.send_message.assert_not_awaited()   # no answer either
        assert caplog.text.count("Reply to reviewer message 100 dropped") == 2
        assert "post 1 2 4" not in caplog.text and "database is locked" not in caplog.text   # never the text

        reviewer.is_anchor.side_effect = lambda chat_id, message_id: False   # not the reviewer's message
        await adapter._handle_incoming(_reply_update("approve", reply_to=300, chat=42), None)
        handler.handle_message.assert_awaited_once_with("approve", "42")   # the ticket path, as before

    async def test_a_chat_added_live_takes_reviewer_replies(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """A project added by hot reload extends the reviewer's chats like the allowlist, which an empty startup
        allowlist (None: every chat) does not need."""
        for allowed in ({"1"}, None):
            reviewer = _reviewer(anchors=(("7", 100),))
            handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=reviewer,
                                    allowed=allowed, reviewer_chats=() if allowed is None else allowed)
            handler.add_allowed_chat_id("7")
            assert await handler.handle_reply(100, "post 1", "7") is True
            reviewer.handle_reply.assert_awaited_once_with("7", 100, "post 1", sent_at=None)

    async def test_a_reply_to_the_age_line_is_the_reviewers(
        self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """The age line is anchored like every reviewer answer: a reply to it ("send it again") is the reviewer's
        too, never the intent parser's."""
        from tests.unit.test_reviewer_replies import T1, Rig
        rig = Rig(tmp_path)   # the real ReviewerReplies; its review message anchored at ("1", 100)
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer=rig.replies)
        adapter = self._adapter(handler)
        await adapter._handle_incoming(_reply_update("post 1 2", sent_at=T1 - timedelta(minutes=16)), None)
        await rig.replies.wait_idle()
        assert rig.texts() == ["This reply is from 11:44 UTC; send it again to act on it. Nothing was done."]
        assert rig.posting.calls == []

        update = _reply_update("post 1 2 4 approve", reply_to=rig.last_id(), sent_at=T1 - timedelta(minutes=1))
        await adapter._handle_incoming(update, None)
        await rig.replies.wait_idle()
        assert [(name, job_id) for name, _, job_id, _ in rig.posting.calls] == [("post_review", rig.job.id)]
        mock_intent_parser.parse.assert_not_awaited()
        mock_notifier.send_message.assert_not_awaited()

    async def test_the_age_limit_is_for_reviewer_replies_only(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """Ticket replies and buttons have none; an rv_ tap has none here either: its message is the echo, whose
        confirmation expires on its own."""
        blocked = _make_workspace("ACME-1", Stage.BLOCKED, previous_state=Stage.DEV)
        blocked.state.escalation_msg_id = 200
        blocked.meta_dir = "/nonexistent"
        awaiting = _make_workspace("ACME-2", "AWAITING_APPROVAL", previous_state="PLAN_REVIEW")
        reviewer = _reviewer()
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, [blocked, awaiting], reviewer)
        adapter = self._adapter(handler)
        old = NOW - timedelta(hours=1)
        await adapter._handle_incoming(_reply_update("retry", reply_to=200, sent_at=old), None)
        blocked.transition.assert_called_once_with(Stage.DEV)
        await adapter._handle_callback(_tap_update("approve:ACME-2", sent_at=old), None)
        awaiting.transition.assert_called_once()
        await adapter._handle_callback(_tap_update("rv_ok:abc123", sent_at=old), None)
        reviewer.handle_callback.assert_awaited_once_with("rv_ok", "abc123", "1", 55)
        reviewer.handle_reply.assert_not_awaited()

    UNANCHORED = "Reply to the reviewer's message to act on it; nothing was done."

    def _awaiting(self, parser, notifier, mode_handler, reviewer, **chats):
        """One ticket waiting for approval, the real ApprovalHandler, and an intent parser that reads anything as
        an approval of it."""
        awaiting = _make_workspace("ACME-2", "AWAITING_APPROVAL", previous_state="PLAN_REVIEW")
        handler = self._handler(parser, notifier, mode_handler, [awaiting], reviewer, **chats)
        handler._approval_handler = ApprovalHandler()
        parser.parse.return_value = ParsedIntent(intent="approve", params={})
        return awaiting, handler

    async def test_a_reviewer_command_that_is_not_a_reply_approves_no_ticket(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """A reviewer command sent without Telegram's Reply, or as a reply to a message that is no anchor (its
        anchor write failed, say), falls through to handle_message, whose intent parser would read "post 1 2 4
        approve" as an approval of the only ticket waiting. In the reviewer's chats it is answered instead."""
        awaiting, handler = self._awaiting(mock_intent_parser, mock_notifier, mock_mode_handler, _reviewer())
        adapter = self._adapter(handler)
        plain = _reply_update("post 1 2 4 approve")
        plain.message.reply_to_message = None   # a new message, not a reply
        await adapter._handle_incoming(plain, None)
        await adapter._handle_incoming(_reply_update("post 1 2 4 approve", reply_to=300), None)   # no anchor
        for text in ("post all confirmed approve", "text 2", "edit 2: shorter", "draft 7", "post replies 1 3",
                     "review again", "skip"):
            await handler.handle_message(text, "1")
        mock_intent_parser.parse.assert_not_awaited()
        awaiting.transition.assert_not_called()
        assert mock_notifier.send_message.await_args_list == [call("1", self.UNANCHORED)] * 9
        mock_notifier.edit_message.assert_not_awaited()

    async def test_a_bare_approve_stays_the_ticket_pipelines(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """An event-only post names no number of a review: "approve" (or "апрув") as a new message is the ticket
        pipeline's, as before; and outside the reviewer's chats nothing changes."""
        for text in ("approve", "апрув"):
            awaiting, handler = self._awaiting(mock_intent_parser, mock_notifier, mock_mode_handler, _reviewer())
            plain = _reply_update(text)
            plain.message.reply_to_message = None
            await self._adapter(handler)._handle_incoming(plain, None)
            mock_intent_parser.parse.assert_awaited_once()
            assert mock_intent_parser.parse.await_args.args[0] == text
            awaiting.transition.assert_called_once_with("DEV")
            mock_intent_parser.parse.reset_mock()

        # No reviewer, or no repo in reviewer mode: nothing changes (/code-review C8).
        for reviewer in (None, _reviewer(repos=False)):
            awaiting, handler = self._awaiting(mock_intent_parser, mock_notifier, mock_mode_handler, reviewer)
            await handler.handle_message("post 1 2 4 approve", "1")
            mock_intent_parser.parse.assert_awaited_once()
            awaiting.transition.assert_called_once_with("DEV")
            mock_intent_parser.parse.reset_mock()
        assert call("1", self.UNANCHORED) not in mock_notifier.send_message.await_args_list

    async def test_the_reviewer_guard_needs_a_reviewed_repo(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        """The guard is the reviewer's only while a repo is in reviewer mode (/code-review C8). With none, every
        message reaches the ticket flow as before: a bare approve, the ticket words, the reviewer's own forms. With
        one, it applies in every chat the ticket pipeline admits, an empty allowlist's included: the numbered forms
        are never ticket words, and a bare approve and the ticket words still pass."""
        ticket_words = ("approve", "апрув", "approve ACME-2", "status", "retry ACME-1", "reject ACME-2: wrong",
                        "fix it")
        reviewer_forms = ("review again", "skip", "post 1 2 4 approve", "text 2", "post replies 1 3")
        for text in (*ticket_words, *reviewer_forms):
            awaiting, handler = self._awaiting(mock_intent_parser, mock_notifier, mock_mode_handler,
                                               _reviewer(repos=False))
            await handler.handle_message(text, "1")
            assert mock_intent_parser.parse.await_args.args[0] == text
            mock_intent_parser.parse.reset_mock()
        assert call("1", self.UNANCHORED) not in mock_notifier.send_message.await_args_list

        # A repo reviewed, the allowlist empty: every chat is admitted for tickets, and the reviewer reads none.
        for text in ticket_words:
            awaiting, handler = self._awaiting(mock_intent_parser, mock_notifier, mock_mode_handler, _reviewer(),
                                               allowed=None, reviewer_chats=())
            await handler.handle_message(text, "42")
            assert mock_intent_parser.parse.await_args.args[0] == text
            mock_intent_parser.parse.reset_mock()
        for text in reviewer_forms:
            awaiting, handler = self._awaiting(mock_intent_parser, mock_notifier, mock_mode_handler, _reviewer(),
                                               allowed=None, reviewer_chats=())
            await handler.handle_message(text, "42")
            mock_intent_parser.parse.assert_not_awaited()
            awaiting.transition.assert_not_called()
        assert mock_notifier.send_message.await_args_list[-len(reviewer_forms):] == [
            call("42", self.UNANCHORED)] * len(reviewer_forms)

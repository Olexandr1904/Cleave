"""Tests for integrations/telegram/command_handler.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from integrations.telegram.command_handler import CommandHandler
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


class TestReviewerReplies:
    """A reply to a reviewer message (a reply anchor) never reaches the ticket pipeline: its intent parser
    would read the message's own hint ("post 1 2 4 approve") as an approval."""

    ANSWER = "Replies to review messages are handled from the next version on; nothing was done."

    def _handler(self, parser, notifier, mode_handler, workspaces=()):
        handler = CommandHandler(
            intent_parser=parser, notifier=notifier, mode_handler=mode_handler,
            active_workspaces_fn=lambda: list(workspaces), allowed_chat_ids={"1"},
            review_anchor=lambda chat_id, message_id: (chat_id, message_id) == ("1", 100),
        )
        handler._approval_handler = MagicMock()
        handler._wake_fn = MagicMock()
        return handler

    async def test_reply_to_a_review_message_is_answered_and_does_nothing(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        blocked = _make_workspace("ACME-1", Stage.BLOCKED, previous_state=Stage.DEV)
        blocked.state.escalation_msg_id = 100   # even a ticket anchor at the same id is not reached
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, [blocked])
        assert await handler.handle_reply(100, "post 1 2 4 approve", "1") is True
        mock_notifier.send_message.assert_awaited_once_with("1", self.ANSWER)
        mock_intent_parser.parse.assert_not_awaited()
        assert handler._approval_handler.mock_calls == [] and handler._wake_fn.mock_calls == []
        blocked.transition.assert_not_called()
        blocked.save_state.assert_not_called()

    async def test_reply_to_a_review_message_never_reaches_the_intent_parser(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        from integrations.telegram.telegram_adapter import TelegramAdapter
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler)
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
        adapter.set_command_handler(handler)
        update = MagicMock()
        update.message.reply_to_message.message_id = 100
        update.message.text = "post 1 2 4 approve"
        update.message.chat.id = 1
        await adapter._handle_incoming(update, None)
        mock_intent_parser.parse.assert_not_awaited()
        assert handler._approval_handler.mock_calls == []
        mock_notifier.send_message.assert_awaited_once_with("1", self.ANSWER)

    async def test_reply_to_another_message_behaves_as_before(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        blocked = _make_workspace("ACME-1", Stage.BLOCKED, previous_state=Stage.DEV)
        blocked.state.escalation_msg_id = 200
        blocked.meta_dir = "/nonexistent"
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, [blocked])
        assert await handler.handle_reply(200, "retry", "1") is True
        blocked.transition.assert_called_once_with(Stage.DEV)
        assert "Retrying" in mock_notifier.send_message.await_args.args[1]
        assert await handler.handle_reply(300, "approve", "1") is False   # left to handle_message
        assert await handler.handle_reply(100, "approve", "2") is False   # not the reviewer's chat
        mock_intent_parser.parse.assert_not_awaited()

    async def test_without_a_reviewer_replies_are_as_before(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = CommandHandler(intent_parser=mock_intent_parser, notifier=mock_notifier,
                                 mode_handler=mock_mode_handler, active_workspaces_fn=lambda: [])
        assert await handler.handle_reply(100, "approve", "1") is False
        mock_notifier.send_message.assert_not_awaited()

    async def test_a_disallowed_chats_reply_to_an_anchor_gets_no_answer(
        self, mock_intent_parser, mock_notifier, mock_mode_handler,
    ):
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler)
        handler._review_anchor = lambda chat_id, message_id: True   # an anchor in any chat
        assert await handler.handle_reply(100, "post 1 2 4 approve", "2") is False
        mock_notifier.send_message.assert_not_awaited()

    async def test_an_anchor_lookup_error_leaves_the_reply_to_the_pipeline(
        self, mock_intent_parser, mock_notifier, mock_mode_handler, caplog,
    ):
        import logging
        import sqlite3
        blocked = _make_workspace("ACME-1", Stage.BLOCKED, previous_state=Stage.DEV)
        blocked.state.escalation_msg_id = 100
        blocked.meta_dir = "/nonexistent"
        handler = self._handler(mock_intent_parser, mock_notifier, mock_mode_handler, [blocked])
        def broken(chat_id, message_id):
            raise sqlite3.OperationalError("database is locked")
        handler._review_anchor = broken
        with caplog.at_level(logging.WARNING, logger="integrations.telegram.command_handler"):
            assert await handler.handle_reply(100, "retry", "1") is True
        blocked.transition.assert_called_once_with(Stage.DEV)
        assert "Reply anchor not looked up: OperationalError" in caplog.text
        assert "database is locked" not in caplog.text

"""Tests for integrations/telegram/intent_parser.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from integrations.telegram.intent_parser import IntentParser, ParsedIntent, parse_fixed_intent


class TestParsedIntent:
    def test_from_valid_json(self):
        raw = '{"intent": "status", "params": {}, "reply": "Here is the status"}'
        intent = ParsedIntent.from_json(raw)
        assert intent.intent == "status"
        assert intent.params == {}
        assert intent.reply == "Here is the status"

    def test_from_invalid_json(self):
        intent = ParsedIntent.from_json("not json at all")
        assert intent.intent == "unknown"
        assert intent.reply != ""

    def test_from_missing_fields(self):
        raw = '{"intent": "status"}'
        intent = ParsedIntent.from_json(raw)
        assert intent.intent == "status"
        assert intent.params == {}
        assert intent.reply == ""

    def test_intent_parser_recognizes_unanswered(self):
        raw = '{"intent": "unanswered", "params": {"ticket_id": ""}, "reply": "Showing pending comments"}'
        parsed = ParsedIntent.from_json(raw)
        assert parsed.intent == "unanswered"
        assert parsed.params.get("ticket_id") == ""

    def test_intent_parser_recognizes_unanswered_with_ticket(self):
        raw = '{"intent": "unanswered", "params": {"ticket_id": "T-1"}, "reply": "ok"}'
        parsed = ParsedIntent.from_json(raw)
        assert parsed.intent == "unanswered"
        assert parsed.params.get("ticket_id") == "T-1"

    def test_intent_parser_prompt_lists_unanswered(self):
        """Sanity check that the LLM prompt mentions 'unanswered' as a valid intent."""
        from integrations.telegram import intent_parser
        import inspect
        src = inspect.getsource(intent_parser)
        assert "unanswered" in src


class TestIntentParser:
    @pytest.fixture
    def mock_adapter(self):
        adapter = AsyncMock()
        adapter.quick_query = AsyncMock(return_value=json.dumps({
            "intent": "status",
            "params": {},
            "reply": "Here is the pipeline status",
        }))
        return adapter

    @pytest.fixture
    def parser(self, mock_adapter):
        return IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)

    async def test_parse_returns_parsed_intent(self, parser):
        result = await parser.parse("what's going on", pipeline_context={})
        assert result.intent == "status"

    async def test_parse_passes_context_in_system_prompt(self, parser, mock_adapter):
        context = {
            "mode": "manual",
            "awaiting_approval": ["ACME-123 (post_analysis)"],
            "active_workspaces": ["ACME-456 — DEV"],
        }
        await parser.parse("yes", pipeline_context=context)

        # The test name promises context is wired into the system prompt.
        # Inspect what the adapter actually received instead of just trusting
        # `.called` — otherwise a renamed parameter or skipped substitution
        # would slip past the assertion silently.
        system_prompt = mock_adapter.quick_query.call_args.kwargs.get("system") \
            or mock_adapter.quick_query.call_args[0][1]
        assert "manual" in system_prompt
        assert "ACME-123" in system_prompt
        assert "ACME-456" in system_prompt

    async def test_parse_handles_adapter_error(self, mock_adapter):
        mock_adapter.quick_query = AsyncMock(side_effect=RuntimeError("CLI unavailable"))
        parser = IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)
        result = await parser.parse("hello", pipeline_context={})
        assert result.intent == "error"
        assert "trouble" in result.reply.lower()

    async def test_parse_handles_malformed_response(self, mock_adapter):
        mock_adapter.quick_query = AsyncMock(return_value="not json")
        parser = IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)
        result = await parser.parse("hello", pipeline_context={})
        assert result.intent == "unknown"


class TestDeferredContext:
    @pytest.fixture
    def mock_adapter(self):
        adapter = AsyncMock()
        adapter.quick_query = AsyncMock(return_value=json.dumps({
            "intent": "retry",
            "params": {"ticket_id": "T-D"},
            "reply": "Retrying T-D",
        }))
        return adapter

    @pytest.fixture
    def parser(self, mock_adapter):
        return IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)

    async def test_deferred_workspaces_included_in_system_prompt(self, parser, mock_adapter):
        context = {
            "mode": "auto",
            "awaiting_approval": [],
            "active_workspaces": [],
            "blocked_workspaces": [],
            "deferred_workspaces": ["T-D (QA, retry at 20:00)"],
        }
        await parser.parse("resume T-D", pipeline_context=context)
        assert mock_adapter.quick_query.called
        system_prompt = mock_adapter.quick_query.call_args.kwargs.get("system") \
            or mock_adapter.quick_query.call_args[0][1]
        # The ticket ID and retry-time details only appear in the prompt if
        # deferred_workspaces was actually substituted into the template. The
        # bare word "deferred" already exists in the template, so we must check
        # for content unique to the context value.
        assert "T-D" in system_prompt
        assert "retry at 20:00" in system_prompt


class TestFixedIntent:
    """The commands with a fixed shape are read without the LLM (it took seconds of a model turn each)."""

    @pytest.mark.parametrize("text", ["/status", "status", "Status", "статус", "/status@CleaveBot", "/status."])
    def test_status(self, text):
        assert parse_fixed_intent(text) == ParsedIntent("status", {})

    @pytest.mark.parametrize("text", ["/status ACME-12", "status acme-12", "статус ACME-12"])
    def test_status_of_a_ticket(self, text):
        assert parse_fixed_intent(text) == ParsedIntent("status", {"ticket_id": text.split()[1]})

    def test_status_of_a_number(self):
        assert parse_fixed_intent("status 12") == ParsedIntent("status", {"ticket_id": "12"})

    @pytest.mark.parametrize("text", ["retry ACME-12", "/retry ACME-12", "Retry ACME-12"])
    def test_retry(self, text):
        assert parse_fixed_intent(text) == ParsedIntent("retry", {"ticket_id": "ACME-12"})

    @pytest.mark.parametrize("text, stage", [
        ("retry ACME-12 from qa", "qa"),
        ("retry ACME-12 from SCOPE_CHECK", "scope_check"),
        ("retry ACME-12 from scope check", "scope_check"),
        ("retry ACME-12 from pushed", "push"),
        ("retry ACME-12 from push", "push"),
        ("retry 12 from plan_review", "plan_review"),
    ])
    def test_retry_from_a_stage(self, text, stage):
        assert parse_fixed_intent(text).params == {"ticket_id": text.split()[1], "from_stage": stage}

    def test_an_unknown_stage_is_passed_on_for_the_handler_to_refuse(self):
        assert parse_fixed_intent("retry ACME-12 from nowhere").params["from_stage"] == "nowhere"

    @pytest.mark.parametrize("text", [
        "retry", "status please", "what's going on", "retry ACME-12 now", "retry ACME-12 from",
        "the status is weird", "approve ACME-12",
    ])
    def test_anything_else_is_the_llm_s(self, text):
        assert parse_fixed_intent(text) is None

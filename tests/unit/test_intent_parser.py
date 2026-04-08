"""Tests for integrations/telegram/intent_parser.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from integrations.telegram.intent_parser import IntentParser, ParsedIntent


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
        assert mock_adapter.quick_query.called

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

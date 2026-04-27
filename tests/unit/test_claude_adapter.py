"""Tests for integrations/llm/claude_adapter.py — quick_query method."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.llm.claude_adapter import ClaudeAdapter


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


class TestClaudeAdapterQuickQuery:
    @pytest.fixture
    def adapter(self):
        a = ClaudeAdapter(
            api_key="fake-key",
            default_model_provider=lambda: "claude-sonnet-4-5",
        )
        a._client = MagicMock()
        a._client.messages = MagicMock()
        return a

    async def test_returns_concatenated_text(self, adapter):
        adapter._client.messages.create = AsyncMock(
            return_value=_mock_response('{"intent": "status", "params": {}}'),
        )
        result = await adapter.quick_query(prompt="what's going on", system="ctx")
        assert result == '{"intent": "status", "params": {}}'

    async def test_passes_system_prompt(self, adapter):
        adapter._client.messages.create = AsyncMock(return_value=_mock_response("ok"))
        await adapter.quick_query(prompt="hi", system="SYS")
        kwargs = adapter._client.messages.create.call_args.kwargs
        assert kwargs["system"] == "SYS"
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["max_tokens"] == 200
        assert kwargs["model"] == "claude-sonnet-4-5"

    async def test_omits_system_when_empty(self, adapter):
        adapter._client.messages.create = AsyncMock(return_value=_mock_response("ok"))
        await adapter.quick_query(prompt="hi")
        kwargs = adapter._client.messages.create.call_args.kwargs
        assert "system" not in kwargs

    async def test_timeout_raises_runtime_error(self, adapter):
        async def _slow(**kwargs):
            await asyncio.sleep(10)
            return _mock_response("never")

        adapter._client.messages.create = _slow
        with pytest.raises(RuntimeError, match="timed out after 1s"):
            await adapter.quick_query(prompt="hi", timeout=1)

    async def test_concatenates_multiple_text_blocks(self, adapter):
        block1 = MagicMock(); block1.type = "text"; block1.text = "foo"
        block2 = MagicMock(); block2.type = "text"; block2.text = "bar"
        response = MagicMock()
        response.content = [block1, block2]
        adapter._client.messages.create = AsyncMock(return_value=response)
        result = await adapter.quick_query(prompt="hi")
        assert result == "foobar"

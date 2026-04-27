"""Tests for integrations/llm/claude_code_adapter.py — quick_query method."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from integrations.llm.claude_code_adapter import ClaudeCodeAdapter, QuotaExhaustedError


class TestQuickQuery:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-haiku-4-5-20251001")

    async def test_quick_query_returns_content(self, adapter):
        mock_result = json.dumps({
            "result": '{"intent": "status", "params": {}, "reply": "Here is the status"}',
            "is_error": False,
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "num_turns": 1,
            "stop_reason": "end_turn",
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await adapter.quick_query(
                prompt="what's going on",
                system="You are a command parser.",
            )
        assert "status" in result

    async def test_quick_query_uses_no_tools(self, adapter):
        mock_result = json.dumps({
            "result": "response",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "num_turns": 1,
            "stop_reason": "end_turn",
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.quick_query(prompt="test", system="sys")
            call_args = mock_exec.call_args
            cmd_list = list(call_args[0])
            assert "--allowedTools" in cmd_list
            tools_idx = cmd_list.index("--allowedTools")
            assert cmd_list[tools_idx + 1] == ""

    async def test_quick_query_uses_max_turns_1(self, adapter):
        mock_result = json.dumps({
            "result": "response",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "num_turns": 1,
            "stop_reason": "end_turn",
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.quick_query(prompt="test", system="sys")
            call_args = mock_exec.call_args
            cmd_list = list(call_args[0])
            assert "--max-turns" in cmd_list
            turns_idx = cmd_list.index("--max-turns")
            assert cmd_list[turns_idx + 1] == "1"

    async def test_quick_query_timeout(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="timed out"):
                await adapter.quick_query(prompt="test", system="sys")


class TestRunCliQuotaRaises:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")

    async def test_non_zero_rc_with_quota_marker_raises_quota(self, adapter):
        # epoch ms for 2026-04-14T20:00:00 UTC
        mock_stdout = json.dumps({
            "is_error": True,
            "result": "Claude AI usage limit reached|1776196800000",
        }).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_stdout, b""))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(QuotaExhaustedError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert exc_info.value.retry_at == datetime(
            2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc
        )

    async def test_non_zero_rc_unrelated_raises_runtime(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"file not found"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert not isinstance(exc_info.value, QuotaExhaustedError)
        assert "exited with code 1" in str(exc_info.value)

    async def test_zero_rc_with_is_error_quota_raises_quota(self, adapter):
        mock_stdout = json.dumps({
            "is_error": True,
            "result": "Claude AI usage limit reached|1776196800000",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_stdout, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(QuotaExhaustedError):
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )

    async def test_zero_rc_with_is_error_unrelated_raises_runtime(self, adapter):
        mock_stdout = json.dumps({
            "is_error": True,
            "result": "Tool execution failed",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_stdout, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert not isinstance(exc_info.value, QuotaExhaustedError)

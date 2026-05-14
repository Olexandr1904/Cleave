"""Tests for integrations/llm/claude_code_adapter.py — quick_query method."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from integrations.llm.claude_code_adapter import (
    ClaudeCodeAdapter,
    QuotaExhaustedError,
    _post_result_watchdog,
    _ProgressState,
)


def _make_streaming_proc(stdout_lines, stderr_lines, rc):
    """Build a fake asyncio subprocess that emits stream-json line-by-line.

    `stdout_lines` and `stderr_lines` are lists of bytes (without trailing
    newlines — readline() keeps them, so we add them here). After the last
    line, readline() returns b"" to signal EOF, the same way the real
    StreamReader does when the child closes the pipe.
    """
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = rc

    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    def _make_reader(lines):
        queue = [ln + b"\n" for ln in lines] + [b""]
        async def _readline():
            return queue.pop(0) if queue else b""
        return _readline

    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=_make_reader(stdout_lines))
    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(side_effect=_make_reader(stderr_lines))

    proc.wait = AsyncMock(return_value=rc)
    proc.kill = MagicMock()
    return proc


def _make_hanging_proc():
    """A fake subprocess that emits nothing and never exits until `.kill()`.

    Models the RTL-13824 hang: the CLI produced no `result` event and the
    process sat idle. stdout/stderr/wait all block on a shared event that
    `.kill()` sets, so the run only ends once the watchdog force-kills it.
    """
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None

    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    killed = asyncio.Event()

    async def _hang():
        await killed.wait()
        return b""

    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=_hang)
    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(side_effect=_hang)

    async def _wait():
        await killed.wait()
        return proc.returncode if proc.returncode is not None else -9

    proc.wait = AsyncMock(side_effect=_wait)

    def _kill():
        proc.returncode = -9
        killed.set()

    proc.kill = MagicMock(side_effect=_kill)
    return proc


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
        # Stream-json: a single terminal `result` event carrying is_error.
        result_event = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Claude AI usage limit reached|1776196800000",
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(QuotaExhaustedError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert exc_info.value.retry_at == datetime(
            2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc
        )

    async def test_non_zero_rc_unrelated_raises_runtime(self, adapter):
        # Empty stdout (no result event) + non-quota stderr + non-zero rc
        # falls through to the RuntimeError branch.
        mock_proc = _make_streaming_proc([], [b"file not found"], rc=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert not isinstance(exc_info.value, QuotaExhaustedError)
        assert "exited with code 1" in str(exc_info.value)

    async def test_zero_rc_with_is_error_quota_raises_quota(self, adapter):
        result_event = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Claude AI usage limit reached|1776196800000",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(QuotaExhaustedError):
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )

    async def test_zero_rc_with_is_error_unrelated_raises_runtime(self, adapter):
        result_event = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Tool execution failed",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert not isinstance(exc_info.value, QuotaExhaustedError)


class TestWatchdogIdleStall:
    """The watchdog must catch mid-run stalls, not only post-result idle."""

    async def test_kills_on_midrun_stall_without_result(self):
        # Agent issued a tool call and the CLI hung — no `result` event ever
        # arrived, so `last_result_at` stays None. This is the RTL-13824 hang.
        progress = _ProgressState()
        progress.last_event_at = time.monotonic() - 10_000
        assert progress.last_result_at is None
        stop = asyncio.Event()
        kill_signal = asyncio.Event()

        with patch(
            "integrations.llm.claude_code_adapter._WATCHDOG_POLL_SECONDS", 0.01
        ):
            await asyncio.wait_for(
                _post_result_watchdog(
                    progress, None, stop, kill_signal,
                    grace_seconds=600, idle_stall_seconds=1,
                ),
                timeout=2,
            )
        assert kill_signal.is_set()

    async def test_does_not_kill_active_run(self):
        # Recent activity, no result event — a healthy mid-run state.
        progress = _ProgressState()
        progress.last_event_at = time.monotonic()
        stop = asyncio.Event()
        kill_signal = asyncio.Event()

        async def _stopper():
            await asyncio.sleep(0.1)
            stop.set()

        with patch(
            "integrations.llm.claude_code_adapter._WATCHDOG_POLL_SECONDS", 0.01
        ):
            await asyncio.gather(
                _post_result_watchdog(
                    progress, None, stop, kill_signal,
                    grace_seconds=600, idle_stall_seconds=1,
                ),
                _stopper(),
            )
        assert not kill_signal.is_set()

    async def test_still_kills_on_post_result_idle(self):
        # Regression guard: the original post-result idle path keeps working.
        progress = _ProgressState()
        progress.last_result_at = time.monotonic() - 10_000
        progress.last_event_at = time.monotonic() - 10_000
        stop = asyncio.Event()
        kill_signal = asyncio.Event()

        with patch(
            "integrations.llm.claude_code_adapter._WATCHDOG_POLL_SECONDS", 0.01
        ):
            await asyncio.wait_for(
                _post_result_watchdog(
                    progress, None, stop, kill_signal,
                    grace_seconds=1, idle_stall_seconds=999_999,
                ),
                timeout=2,
            )
        assert kill_signal.is_set()

    async def test_midrun_stall_raises_instead_of_silent_success(self):
        # A hung run with no `result` event must surface as a RuntimeError,
        # not a silent empty success — otherwise the orchestrator records a
        # stalled agent as completed and the ticket never gets retried.
        adapter = ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")
        proc = _make_hanging_proc()

        with patch("asyncio.create_subprocess_exec", return_value=proc), \
             patch("integrations.llm.claude_code_adapter.IDLE_STALL_SECONDS", 0.05), \
             patch(
                 "integrations.llm.claude_code_adapter._WATCHDOG_POLL_SECONDS", 0.01
             ), \
             patch("os.killpg", side_effect=ProcessLookupError):
            with pytest.raises(RuntimeError) as exc_info:
                await asyncio.wait_for(
                    adapter.execute_in_workspace(
                        prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                    ),
                    timeout=5,
                )
        assert "stall" in str(exc_info.value).lower()
        assert proc.kill.called

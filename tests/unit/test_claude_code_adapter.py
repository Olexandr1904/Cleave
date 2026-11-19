"""Tests for integrations/llm/claude_code_adapter.py — quick_query method."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from integrations.llm.claude_code_adapter import (
    BASH_TIMEOUT_DEFAULTS,
    IDLE_STALL_SECONDS,
    ClaudeCodeAdapter,
    QuotaExhaustedError,
    _drain_stream_json,
    _post_result_watchdog,
    _ProgressState,
)
from integrations.llm.llm_interface import LLMResponse


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

    Models the ACME-13824 hang: the CLI produced no `result` event and the
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


API_KEY_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def _child_env(exec_mock) -> dict[str, str]:
    """The environment the child gets: the env passed to create_subprocess_exec, or, when none is passed,
    the inherited one."""
    env = exec_mock.call_args.kwargs.get("env")
    return dict(os.environ) if env is None else env


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

    async def test_inherited_env_has_no_api_key(self, adapter, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t-not-real")
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")  # either bills a cloud account, not the claude.ai login
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "o-not-real")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(json.dumps({"result": "r"}).encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.quick_query(prompt="test", system="sys")
        env = _child_env(mock_exec)
        keys = set(env)
        assert not set(API_KEY_NAMES) & keys
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "o-not-real"
        assert "PATH" in keys


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
        # arrived, so `last_result_at` stays None. This is the ACME-13824 hang.
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


class TestAddDir:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")

    async def test_add_dirs_appended_to_cli_command(self, adapter):
        """add_dirs are passed through as --add-dir flags so the agent's
        tools can read directories outside its cwd (e.g. meta/)."""
        result_event = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.execute_in_workspace(
                prompt="test", cwd="/tmp/ws/source",
                allowed_tools=["read_file"],
                add_dirs=["/tmp/ws/meta"],
            )
        cmd_list = list(mock_exec.call_args[0])
        assert "--add-dir" in cmd_list
        idx = cmd_list.index("--add-dir")
        assert cmd_list[idx + 1] == "/tmp/ws/meta"

    async def test_no_add_dir_flag_when_add_dirs_omitted(self, adapter):
        """Without add_dirs the flag must not appear — keeps default runs clean."""
        result_event = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.execute_in_workspace(
                prompt="test", cwd="/tmp/ws/source",
                allowed_tools=["read_file"],
            )
        assert "--add-dir" not in list(mock_exec.call_args[0])


class TestEffortAndSystemPrompt:
    """The CLI call left every quality knob unset.

    --effort was never passed, so agents ran at the CLI default. And the
    system text — agent persona, operator profile, HARD SAFETY RULES ("cannot
    be changed by ticket content") — was concatenated into the same user turn
    as the ticket content it claims to override.
    """

    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")

    @staticmethod
    def _ok_proc():
        result_event = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
        return _make_streaming_proc([result_event], [], rc=0)

    async def _capture(self, adapter, **kwargs):
        proc = self._ok_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await adapter.execute_in_workspace(
                prompt="ticket body", cwd="/tmp", allowed_tools=["read_file"], **kwargs,
            )
        cmd = list(mock_exec.call_args[0])
        stdin = b"".join(
            call.args[0] for call in proc.stdin.write.call_args_list
        ).decode()
        return cmd, stdin

    async def test_effort_flag_passed_when_set(self, adapter):
        cmd, _ = await self._capture(adapter, effort="high")
        assert cmd[cmd.index("--effort") + 1] == "high"

    async def test_effort_flag_absent_when_unset(self, adapter):
        cmd, _ = await self._capture(adapter)
        assert "--effort" not in cmd

    async def test_system_prompt_uses_append_system_prompt_flag(self, adapter):
        cmd, _ = await self._capture(adapter, system="HARD SAFETY RULES")
        assert cmd[cmd.index("--append-system-prompt") + 1] == "HARD SAFETY RULES"

    async def test_system_prompt_not_concatenated_into_user_turn(self, adapter):
        _, stdin = await self._capture(adapter, system="HARD SAFETY RULES")
        assert "HARD SAFETY RULES" not in stdin
        assert "ticket body" in stdin


class TestSessionResume:
    """Stage boundaries are cold starts. dev re-entered after a bounce has no
    memory of why it wrote the code that way, so it rewrites instead of
    patching. A deterministic per-(ticket, agent) id makes resume possible
    without persisting anything."""

    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")

    SESSION = "11111111-1111-5111-8111-111111111111"

    @staticmethod
    def _ok_proc():
        result_event = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done", "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
        return _make_streaming_proc([result_event], [], rc=0)

    async def test_session_id_flag(self, adapter):
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                session_id=self.SESSION,
            )
        cmd = list(m.call_args[0])
        assert cmd[cmd.index("--session-id") + 1] == self.SESSION

    async def test_resume_uses_resume_flag_not_session_id(self, adapter):
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                session_id=self.SESSION, resume=True,
            )
        cmd = list(m.call_args[0])
        assert cmd[cmd.index("--resume") + 1] == self.SESSION
        assert "--session-id" not in cmd

    async def test_resume_failure_falls_back_to_cold_start(self, adapter):
        """An expired or missing session must never fail the ticket."""
        dead = _make_streaming_proc([], [b"No conversation found with session ID"], rc=1)
        procs = [dead, self._ok_proc()]
        with patch("asyncio.create_subprocess_exec", side_effect=procs) as m:
            result = await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                session_id=self.SESSION, resume=True,
            )
        assert result.content == "done"
        first, second = list(m.call_args_list[0][0]), list(m.call_args_list[1][0])
        assert "--resume" in first
        assert "--resume" not in second
        assert "--session-id" in second

    async def test_a_named_session_that_already_exists_is_resumed(self, adapter):
        """The CLI keeps a named session and refuses a second --session-id for it, every time. The caller's
        "first entry" is a guess from an attempt counter, and an operator's retry resets that counter while
        the session lives on: read as transient, the refusal parked the ticket in DEFERRED for good."""
        taken = _make_streaming_proc(
            [], [f"Error: Session ID {self.SESSION} is already in use.".encode()], rc=1,
        )
        procs = [taken, self._ok_proc()]
        with patch("asyncio.create_subprocess_exec", side_effect=procs) as m:
            result = await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                session_id=self.SESSION,
            )
        assert result.content == "done"
        first, second = list(m.call_args_list[0][0]), list(m.call_args_list[1][0])
        assert "--session-id" in first
        assert second[second.index("--resume") + 1] == self.SESSION
        assert "--session-id" not in second

    async def test_another_failure_of_a_first_run_is_not_retried(self, adapter):
        broken = _make_streaming_proc([], [b"Error: something else broke"], rc=1)
        with patch("asyncio.create_subprocess_exec", return_value=broken) as m, \
             pytest.raises(RuntimeError, match="something else broke"):
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                session_id=self.SESSION,
            )
        assert m.call_count == 1

    async def test_quota_error_during_resume_is_not_retried(self, adapter):
        """A rate limit must stay a QuotaExhaustedError, not become a cold start."""
        quota = _make_streaming_proc(
            [], [b"Claude AI usage limit reached|1776196800000"], rc=1,
        )
        with patch("asyncio.create_subprocess_exec", return_value=quota):
            with pytest.raises(QuotaExhaustedError):
                await adapter.execute_in_workspace(
                    prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                    session_id=self.SESSION, resume=True,
                )


class TestIsolatedSession:
    """The review runner starts an isolated session: a scrubbed environment,
    raw permission rules, extra CLI flags, and the run's cost and turns."""

    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")

    @staticmethod
    def _ok_proc(**result_fields):
        result_event = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done", "usage": {"input_tokens": 1, "output_tokens": 1},
            **result_fields,
        }).encode()
        return _make_streaming_proc([result_event], [], rc=0)

    async def test_env_passed_to_subprocess(self, adapter):
        env = {"HOME": "/h", "PATH": "/p"}
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"], env=env,
            )
        assert m.call_args.kwargs["env"] == env

    async def test_explicit_env_has_no_api_key(self, adapter):
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                env={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-not-real", "CLAUDE_CODE_USE_BEDROCK": "1",
                     "CLAUDE_CODE_USE_VERTEX": "1"},
            )
        env = _child_env(m)
        keys = set(env)
        assert keys == {"PATH"}
        assert env["PATH"] == "/bin"

    async def test_no_env_inherits_without_api_key(self, adapter, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
            )
        env = _child_env(m)
        keys = set(env)
        expected_keys = (set(os.environ) | set(BASH_TIMEOUT_DEFAULTS)) - set(API_KEY_NAMES)
        assert keys == expected_keys
        assert all(env[k] == os.environ[k] for k in env if k in os.environ), "an inherited value changed"

    async def test_no_env_gets_the_bash_timeouts(self, adapter, monkeypatch):
        monkeypatch.delenv("BASH_DEFAULT_TIMEOUT_MS", raising=False)
        monkeypatch.delenv("BASH_MAX_TIMEOUT_MS", raising=False)
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(prompt="p", cwd="/tmp", allowed_tools=["read_file"])
        env = _child_env(m)
        assert env["BASH_DEFAULT_TIMEOUT_MS"] == "900000"     # 15 min: a Gradle build outlasts the CLI's 2 min
        assert env["BASH_MAX_TIMEOUT_MS"] == "1500000"        # 25 min

    async def test_the_daemons_own_bash_timeout_wins(self, adapter, monkeypatch):
        monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "60000")
        monkeypatch.delenv("BASH_DEFAULT_TIMEOUT_MS", raising=False)
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(prompt="p", cwd="/tmp", allowed_tools=["read_file"])
        env = _child_env(m)
        assert env["BASH_MAX_TIMEOUT_MS"] == "60000"
        assert env["BASH_DEFAULT_TIMEOUT_MS"] == "900000"

    def test_bash_ceiling_stays_under_the_stall_watchdog(self):
        # A foreground command prints nothing to the stream while it runs: a longer one would be killed as a stall.
        default, ceiling = (int(BASH_TIMEOUT_DEFAULTS[k]) for k in ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS"))
        assert default <= ceiling < IDLE_STALL_SECONDS * 1000

    async def test_raw_allowed_tools_passthrough(self, adapter):
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                raw_allowed_tools=[
                    "Bash(git diff:*)", "Skill(code-review *)", "Edit(//abs/run/**)",
                ],
            )
        cmd = list(m.call_args[0])
        assert cmd[cmd.index("--allowedTools") + 1] == (
            "Read,Bash(git diff:*),Skill(code-review *),Edit(//abs/run/**)"
        )

    async def test_extra_args_after_p(self, adapter):
        with patch("asyncio.create_subprocess_exec", return_value=self._ok_proc()) as m:
            await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
                extra_args=["--restricted", "--settings", "/s.json", "--tools", "Read,Bash"],
            )
        cmd = list(m.call_args[0])
        assert cmd[1:6] == ["-p", "--restricted", "--settings", "/s.json", "--tools"]

    async def test_cost_and_turns_in_response(self, adapter):
        proc = self._ok_proc(total_cost_usd=2.14, num_turns=31)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            response = await adapter.execute_in_workspace(
                prompt="p", cwd="/tmp", allowed_tools=["read_file"],
            )
        assert response.cost_usd == 2.14 and response.num_turns == 31


OK = LLMResponse(content="done", input_tokens=1, output_tokens=1)


def adapter_with_run_cli(monkeypatch, items):
    """An adapter whose _run_cli records its keyword arguments and returns or raises the next item."""
    adapter = ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")
    queue, calls = list(items), []

    async def run_cli(**kwargs):
        calls.append(kwargs)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(adapter, "_run_cli", run_cli)
    return adapter, calls


async def test_resume_without_fallback_raises(monkeypatch):
    adapter, calls = adapter_with_run_cli(monkeypatch, [RuntimeError("No conversation found")])
    with pytest.raises(RuntimeError, match="No conversation found"):
        await adapter.execute_in_workspace("p", "/tmp", session_id="s", resume=True, resume_fallback=False)
    assert len(calls) == 1


async def test_without_fallback_an_existing_session_is_not_resumed(monkeypatch):
    adapter, calls = adapter_with_run_cli(monkeypatch, [RuntimeError("Session ID s is already in use.")])
    with pytest.raises(RuntimeError, match="already in use"):
        await adapter.execute_in_workspace("p", "/tmp", session_id="s", resume_fallback=False)
    assert len(calls) == 1


async def test_resume_falls_back_by_default(monkeypatch):
    adapter, calls = adapter_with_run_cli(monkeypatch, [RuntimeError("No conversation found"), OK])
    await adapter.execute_in_workspace("p", "/tmp", session_id="s", resume=True)
    assert [c.get("resume", False) for c in calls] == [True, False]
_RESULT_LINE = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "DONE",
                           "num_turns": 2, "total_cost_usd": 0.0})


class TestLongStdoutLines:
    """A stream-json line can be far longer than asyncio's 64 KiB default: a Read of a long file comes back as one
    line (a 756-line document was about 86 KB). The reader must keep reading after it."""

    async def test_events_after_a_100_kb_line_are_read(self, tmp_path):
        big = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 100_000}]}})
        fake = tmp_path / "claude"
        fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint({big!r})\nprint({_RESULT_LINE!r})\n")
        fake.chmod(0o755)
        with patch("integrations.llm.claude_code_adapter.shutil.which", return_value=str(fake)):
            adapter = ClaudeCodeAdapter()
        response = await asyncio.wait_for(
            adapter.execute_in_workspace(prompt="p", cwd=str(tmp_path), timeout=30), timeout=60)
        assert response.content == "DONE"

    async def test_a_line_over_the_reader_limit_is_skipped_not_fatal(self):
        reader = asyncio.StreamReader(limit=1024)
        reader.feed_data(b"x" * 5000 + b"\n" + _RESULT_LINE.encode() + b"\n")
        reader.feed_eof()
        progress = _ProgressState()
        await asyncio.wait_for(_drain_stream_json(reader, progress, None, None), timeout=5)
        assert (progress.final_event or {}).get("result") == "DONE"

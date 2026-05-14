"""Claude Code CLI adapter — uses `claude -p` subprocess instead of API.

Uses the user's existing Claude Code authentication (Max subscription)
rather than requiring a separate Anthropic API key.

Claude Code handles its own tool execution internally (Read, Write, Bash, etc.),
so the ToolSandbox is not used with this adapter. Instead, tool access is
controlled via --allowedTools and --cwd flags.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from integrations.llm.llm_interface import LLMInterface, LLMResponse

logger = logging.getLogger(__name__)

_QUOTA_MARKER_RE = re.compile(
    r"Claude AI usage limit reached\|(\d+)",
    re.IGNORECASE,
)
_QUOTA_SUBSTRINGS = (
    "usage limit reached",
    "rate_limit",
    "overloaded_error",
)


class QuotaExhaustedError(RuntimeError):
    """Claude CLI hit a usage/rate limit. Carries the reset time if known."""

    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def _classify_cli_error(stdout: str, stderr: str) -> QuotaExhaustedError | None:
    """Return a QuotaExhaustedError if stdout/stderr look like a quota hit, else None."""
    # Structured parse first: JSON stdout with is_error=true.
    structured_text = ""
    api_error_status: int | None = None
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and data.get("is_error"):
            structured_text = str(data.get("result") or data.get("content") or "")
            raw_status = data.get("api_error_status")
            if isinstance(raw_status, int):
                api_error_status = raw_status
    except (json.JSONDecodeError, TypeError):
        pass

    if structured_text:
        m = _QUOTA_MARKER_RE.search(structured_text)
        if m:
            epoch_ms = int(m.group(1))
            retry_at = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            return QuotaExhaustedError(structured_text, retry_at=retry_at)

    # HTTP 429 in the CLI's JSON response is the canonical quota-exhausted signal
    # (e.g. result text "You've hit your limit · resets 5:50pm (UTC)"). The reset
    # time is encoded in human-readable text and not always parseable, so leave
    # retry_at unset and let agent_runtime apply the default delay.
    if api_error_status == 429:
        return QuotaExhaustedError(
            structured_text or "Claude API returned 429 (quota exhausted)",
            retry_at=None,
        )

    # Substring fallback across combined stdout + stderr.
    combined = f"{stdout}\n{stderr}".lower()
    for marker in _QUOTA_SUBSTRINGS:
        if marker in combined:
            return QuotaExhaustedError(
                f"Quota/rate limit detected: {marker}",
                retry_at=None,
            )

    return None


# Map our tool names to Claude Code's built-in tool names
TOOL_MAP = {
    "read_file": "Read",
    "write_file": "Write,Edit",
    "list_directory": "LS,Glob",
    "search_code": "Grep",
    "run_command": "Bash",
    "git_operation": "Bash",
}

DEFAULT_MAX_TURNS = 100
# Wall-clock timeout is owned by AgentBudget.wall_clock_seconds and threaded
# through execute_in_workspace(timeout=...) per call. The adapter no longer
# carries a hardcoded default — see config/schemas.py:AgentBudget.


class ClaudeCodeAdapter(LLMInterface):
    """Claude Code CLI adapter using `claude -p` subprocess."""

    def __init__(
        self,
        model_provider: Callable[[], str] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._claude_bin = shutil.which("claude") or "claude"
        self._model_provider = model_provider or (lambda: "")
        self._max_turns = max_turns

    async def send_message(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Send a prompt to Claude Code CLI and return the response.

        The `tools` parameter is ignored — tool access is controlled
        via allowed_tools passed to execute_in_workspace().
        """
        return await self._run_cli(
            prompt=prompt,
            model=model,
            system=system,
        )

    async def send_tool_results(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Not used with CLI adapter — Claude Code manages its own tool loop."""
        raise NotImplementedError(
            "ClaudeCodeAdapter handles tool execution internally. "
            "Use execute_in_workspace() instead of the multi-turn tool loop."
        )

    async def execute_in_workspace(
        self,
        prompt: str,
        cwd: str,
        allowed_tools: list[str] | None = None,
        model: str = "",
        system: str = "",
        max_turns: int | None = None,
        timeout: int | None = None,
        pid_callback: Callable[[int], None] | None = None,
        progress_log_path: Path | None = None,
        raw_stream_path: Path | None = None,
        add_dirs: list[str] | None = None,
    ) -> LLMResponse:
        """Execute a prompt with Claude Code in a specific workspace directory.

        This is the primary method for agent execution. Claude Code will:
        - Set working directory to `cwd` (the workspace source dir)
        - Use its own built-in tools (Read, Write, Bash, etc.)
        - Respect --allowedTools restrictions
        - Handle multi-turn tool execution internally

        Args:
            prompt: The full agent prompt.
            cwd: Working directory (workspace source dir).
            allowed_tools: Our tool names (read_file, write_file, etc.).
                          Mapped to Claude Code tool names automatically.
            model: Model override (optional).
            system: System prompt (prepended to prompt).
            max_turns: Override max turns for this call.
            timeout: Wall-clock cap (seconds) for the subprocess. Owned by
                AgentBudget.wall_clock_seconds and passed by the runtime per
                call. None = no inner cap (the outer asyncio.wait_for in
                agent_runtime is the only enforcer).
            progress_log_path: Append-mode log for structured per-event
                records (turn boundaries, tool calls, heartbeats, post-kill
                dump). One line per event so it's grep-friendly.
            raw_stream_path: If set, every raw line of the CLI's stream-json
                stdout is teed here for forensic replay.

        Returns:
            LLMResponse with the final text output and usage stats.
        """
        return await self._run_cli(
            prompt=prompt,
            model=model,
            system=system,
            cwd=cwd,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            timeout=timeout,
            pid_callback=pid_callback,
            progress_log_path=progress_log_path,
            raw_stream_path=raw_stream_path,
            add_dirs=add_dirs,
        )

    async def quick_query(
        self,
        prompt: str,
        system: str = "",
        timeout: int = 5,
    ) -> str:
        """Lightweight prompt-to-response call. No tools, single turn, short timeout.

        Used for intent parsing and other quick classification tasks.

        Args:
            prompt: The user message to classify.
            system: System prompt with context.
            timeout: Timeout in seconds (default 5).

        Returns:
            The raw text response from Claude.
        """
        cmd = [self._claude_bin, "-p"]

        full_prompt = prompt
        if system:
            full_prompt = f"{system}\n\n---\n\n{full_prompt}"

        use_model = self._model_provider()
        if use_model:
            cmd.extend(["--model", use_model])

        cmd.extend(["--output-format", "json"])
        cmd.extend(["--max-turns", "1"])
        cmd.extend(["--allowedTools", ""])

        logger.info("Claude Code quick_query: model=%s", use_model or "default")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # reap to avoid zombie + leaked pipes
            raise RuntimeError(f"Claude Code quick_query timed out after {timeout}s")

        stdout_str = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Claude Code quick_query failed (rc={proc.returncode}): {stderr_str}"
            )

        try:
            data = json.loads(stdout_str)
            return data.get("result", stdout_str)
        except json.JSONDecodeError:
            return stdout_str

    async def _run_cli(
        self,
        prompt: str,
        model: str = "",
        system: str = "",
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        timeout: int | None = None,
        pid_callback: Callable[[int], None] | None = None,
        progress_log_path: Path | None = None,
        raw_stream_path: Path | None = None,
        add_dirs: list[str] | None = None,
    ) -> LLMResponse:
        """Run claude CLI subprocess in stream-json mode.

        Reads stdout/stderr line-by-line concurrently so we have visibility
        into long-running calls instead of waiting for end-of-process. Each
        JSON event updates a shared progress dict; a heartbeat task writes
        a status line every HEARTBEAT_INTERVAL_SECONDS so a stalled run
        produces a clear "last activity at T-N seconds" trail. On timeout
        we dump the accumulated state to progress_log_path before raising.
        """
        cmd = [self._claude_bin, "-p"]

        # System prompt prepended
        full_prompt = prompt
        if system:
            full_prompt = f"{system}\n\n---\n\n{full_prompt}"

        # Model
        use_model = model or self._model_provider()
        if use_model:
            cmd.extend(["--model", use_model])

        # Output format: stream-json gives us one event per line so we can
        # observe progress as it happens. --verbose is required by the CLI
        # when --output-format=stream-json with --print.
        cmd.extend(["--output-format", "stream-json", "--verbose"])

        # Max turns
        turns = max_turns or self._max_turns
        cmd.extend(["--max-turns", str(turns)])

        # Allowed tools
        if allowed_tools is not None:
            cc_tools = self._map_tools(allowed_tools)
            cmd.extend(["--allowedTools", ",".join(cc_tools) if cc_tools else ""])

        # Extra directories the agent's tools may read beyond cwd. Ticket
        # metadata (attachments, comments) lives in meta/, a sibling of the
        # source/ cwd — without this the agent cannot reach it.
        for extra_dir in add_dirs or []:
            cmd.extend(["--add-dir", extra_dir])

        logger.info(
            "Claude Code CLI: model=%s, cwd=%s, tools=%s, max_turns=%d, "
            "timeout=%ss, prompt_chars=%d, log=%s",
            use_model or "default", cwd or ".", allowed_tools, turns,
            timeout, len(full_prompt), progress_log_path,
        )

        progress = _ProgressState()
        _write_progress(
            progress_log_path,
            f"start cmd_argc={len(cmd)} model={use_model or 'default'} "
            f"tools={allowed_tools} max_turns={turns} timeout={timeout}s "
            f"prompt_chars={len(full_prompt)} cwd={cwd}",
        )

        # start_new_session=True puts the child in its own process group so
        # we can kill the whole tree (claude CLI plus any tools it spawns)
        # via os.killpg on the pid.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
        if pid_callback is not None and proc.pid:
            try:
                pid_callback(proc.pid)
            except Exception:
                logger.exception("pid_callback raised; ignoring")

        # Send the prompt, then close stdin so the CLI knows input is done.
        try:
            assert proc.stdin is not None
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            # CLI died before reading prompt; downstream readers will surface it.
            pass

        # Open tee + progress files once; close in finally.
        raw_fh = None
        if raw_stream_path is not None:
            try:
                raw_stream_path.parent.mkdir(parents=True, exist_ok=True)
                raw_fh = open(raw_stream_path, "a", buffering=1, encoding="utf-8")
            except OSError as e:
                logger.warning("Could not open raw_stream_path %s: %s", raw_stream_path, e)
                raw_fh = None

        stop_event = asyncio.Event()
        kill_signal = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(progress, progress_log_path, stop_event)
        )
        watchdog_task = asyncio.create_task(
            _post_result_watchdog(
                progress, progress_log_path, stop_event, kill_signal,
                grace_seconds=POST_RESULT_IDLE_GRACE_SECONDS,
                idle_stall_seconds=IDLE_STALL_SECONDS,
            )
        )
        stdout_task = asyncio.create_task(
            _drain_stream_json(
                proc.stdout, progress, progress_log_path, raw_fh,
            )
        )
        stderr_task = asyncio.create_task(
            _drain_stderr(proc.stderr, progress, raw_fh)
        )

        wait_proc = asyncio.create_task(proc.wait())
        kill_waiter = asyncio.create_task(kill_signal.wait())
        timed_out = False
        graceful_idle = False
        stalled = False
        try:
            # Race three terminations: proc exits naturally, watchdog
            # signals post-result idle, or the wall-clock cap fires.
            done, _pending = await asyncio.wait(
                {wait_proc, kill_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if kill_waiter in done:
                # Watchdog fired. A cached result event means post-result
                # idle (graceful — we have the output); none means the run
                # stalled mid-flight and there is nothing to salvage.
                if progress.last_result_at is not None:
                    graceful_idle = True
                else:
                    stalled = True
            elif wait_proc in done:
                pass  # natural exit
            else:
                timed_out = True
        except asyncio.CancelledError:
            timed_out = True
            raise
        finally:
            stop_event.set()
            for bg_task in (heartbeat_task, watchdog_task):
                try:
                    await asyncio.wait_for(bg_task, timeout=2)
                except (asyncio.TimeoutError, Exception):
                    bg_task.cancel()

            need_kill = (
                graceful_idle or stalled or timed_out
            ) and proc.returncode is None
            if need_kill:
                if graceful_idle:
                    _write_progress(
                        progress_log_path,
                        f"graceful kill: post-result idle exceeded "
                        f"{POST_RESULT_IDLE_GRACE_SECONDS}s. "
                        f"turns_seen={progress.turns} tool_calls_seen={progress.tool_calls} "
                        f"events={progress.events_seen} "
                        f"last_event={progress.last_event_kind!r} "
                        f"since_last_event={progress.since_last_activity():.1f}s",
                    )
                elif stalled:
                    _write_progress(
                        progress_log_path,
                        f"stall kill: no result event, idle exceeded "
                        f"{IDLE_STALL_SECONDS}s. "
                        f"turns_seen={progress.turns} tool_calls_seen={progress.tool_calls} "
                        f"events={progress.events_seen} "
                        f"last_event={progress.last_event_kind!r} "
                        f"last_tool={progress.last_tool!r} "
                        f"since_last_event={progress.since_last_activity():.1f}s",
                    )
                else:
                    _write_progress(
                        progress_log_path,
                        f"timeout fired after {timeout}s; "
                        f"turns_seen={progress.turns} tool_calls_seen={progress.tool_calls} "
                        f"events={progress.events_seen} "
                        f"last_event={progress.last_event_kind!r} "
                        f"last_tool={progress.last_tool!r} "
                        f"since_last_event={progress.since_last_activity():.1f}s",
                    )
                # Kill the whole process group; reap so pipes don't leak.
                try:
                    if proc.pid:
                        os.killpg(os.getpgid(proc.pid), 9)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

            # Cancel and drain any task still pending so we don't leak.
            for t in (stdout_task, stderr_task, wait_proc, kill_waiter):
                if not t.done():
                    t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if raw_fh is not None:
                try:
                    raw_fh.close()
                except Exception:
                    pass

        stderr_str = "\n".join(progress.stderr_tail)

        if timed_out:
            _write_progress(
                progress_log_path,
                f"end status=timeout duration={progress.elapsed():.1f}s "
                f"stderr_tail_chars={len(stderr_str)}",
            )
            raise RuntimeError(f"Claude Code CLI timed out after {timeout}s")

        if stalled:
            _write_progress(
                progress_log_path,
                f"end status=stalled duration={progress.elapsed():.1f}s "
                f"last_tool={progress.last_tool!r} "
                f"stderr_tail_chars={len(stderr_str)}",
            )
            raise RuntimeError(
                f"Claude Code CLI stalled: no activity for >{IDLE_STALL_SECONDS}s "
                f"with no result event (last_tool={progress.last_tool!r})"
            )

        # Subprocess exited; capture rc.
        rc = proc.returncode if proc.returncode is not None else -1

        # The terminal `result` event is the source of truth for usage/content.
        final = progress.final_event or {}
        # Fallback: if the CLI exited with no result event, treat the last
        # observed assistant text as content (best-effort) so callers see
        # something meaningful for diagnosis.
        content = final.get("result", "") or progress.last_text or ""
        is_error = final.get("is_error", False)

        # Reconstruct a stdout-shaped string for _classify_cli_error so the
        # existing quota/rate-limit detection keeps working without us
        # rewriting the classifier. If we have the final result event, dump
        # it as JSON; otherwise feed the joined raw lines.
        classifier_stdout = (
            json.dumps(final) if final else "\n".join(progress.stdout_tail)
        )

        # Graceful kill (post-result idle) yields a SIGKILL rc but the
        # result event is already cached — treat as success. Only fall
        # into the error branch when we did not initiate the kill.
        if rc != 0 and not graceful_idle:
            logger.error(
                "Claude Code CLI failed (rc=%d) stderr_tail=%r last_event=%r",
                rc, stderr_str[:1500], progress.last_event_kind,
            )
            classified = _classify_cli_error(classifier_stdout, stderr_str)
            if classified is not None:
                _write_progress(
                    progress_log_path,
                    f"end status=quota_exhausted rc={rc} duration={progress.elapsed():.1f}s",
                )
                raise classified
            _write_progress(
                progress_log_path,
                f"end status=cli_error rc={rc} duration={progress.elapsed():.1f}s "
                f"stderr_chars={len(stderr_str)}",
            )
            raise RuntimeError(
                f"Claude Code CLI exited with code {rc}. "
                f"stderr={stderr_str[:500]!r} last_event={progress.last_event_kind!r}"
            )

        if is_error:
            logger.error("Claude Code CLI error: %s", str(content)[:200])
            classified = _classify_cli_error(classifier_stdout, stderr_str)
            if classified is not None:
                _write_progress(
                    progress_log_path,
                    f"end status=quota_exhausted rc=0 duration={progress.elapsed():.1f}s",
                )
                raise classified
            _write_progress(
                progress_log_path,
                f"end status=is_error rc=0 duration={progress.elapsed():.1f}s",
            )
            raise RuntimeError(f"Claude Code returned error: {str(content)[:500]}")

        usage = final.get("usage", {}) if isinstance(final, dict) else {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        total_cost = final.get("total_cost_usd", 0) if isinstance(final, dict) else 0
        num_turns = final.get("num_turns", progress.turns) if isinstance(final, dict) else progress.turns
        stop_reason = final.get("stop_reason", "") if isinstance(final, dict) else ""

        logger.info(
            "Claude Code CLI done: turns=%d, tool_calls=%d, tokens=%d/%d "
            "(cache_r=%d, cache_w=%d), cost=$%.4f, stop=%s, duration=%.1fs",
            num_turns, progress.tool_calls, input_tokens, output_tokens,
            cache_read, cache_create, total_cost, stop_reason, progress.elapsed(),
        )
        _write_progress(
            progress_log_path,
            f"end status={'ok_graceful_kill' if graceful_idle else 'ok'} "
            f"rc={rc} duration={progress.elapsed():.1f}s "
            f"turns={num_turns} tool_calls={progress.tool_calls} "
            f"in_tokens={input_tokens} out_tokens={output_tokens} "
            f"cache_r={cache_read} cache_w={cache_create} cost=${total_cost:.4f} "
            f"stop={stop_reason!r}",
        )

        return LLMResponse(
            content=content if isinstance(content, str) else str(content),
            input_tokens=input_tokens + cache_read + cache_create,
            output_tokens=output_tokens,
            model=use_model or (final.get("model", "") if isinstance(final, dict) else ""),
            stop_reason=stop_reason,
        )

    def _map_tools(self, our_tools: list[str]) -> list[str]:
        """Map our tool names to Claude Code tool names."""
        cc_tools: set[str] = set()
        for tool in our_tools:
            mapped = TOOL_MAP.get(tool, "")
            if mapped:
                for t in mapped.split(","):
                    cc_tools.add(t)
        return sorted(cc_tools)

    def supports_extended_thinking(self) -> bool:
        return True


# Heartbeat cadence for the streaming reader. 30s matches the operator request
# for 3887: catches stalls fast enough that "stuck for N minutes" is loud, but
# keeps log volume tractable for multi-hour runs (~480 lines / 4 h).
HEARTBEAT_INTERVAL_SECONDS = 30

# After we've seen any `result` event, if no further event arrives within this
# many seconds we force-kill the CLI and use the cached result.
# Why: observed on MBMOB-3887 QA — the CLI emits its terminal `result` event
# with stop='end_turn' but the subprocess does not exit, sitting idle for
# hours until the wall-clock cap kills it. We have everything we need from
# the result event, so wait a generous grace (covers the longest natural
# gap we saw between sub-result events: 8m 23s while QA polled gradle)
# then terminate. Turns a 4 h timeout into a ~10 min graceful exit.
POST_RESULT_IDLE_GRACE_SECONDS = 600

# Before any `result` event, if no event arrives within this many seconds the
# run is stalled mid-flight and we force-kill it. Why: observed on RTL-13824 —
# the CLI issued a tool call and hung with no terminal `result`, so the
# post-result watchdog never engaged and the run coasted until the wall-clock
# cap. Sits well above the longest legitimate intra-run gap (8m 23s gradle
# poll) so a long tool call is not mistaken for a stall.
IDLE_STALL_SECONDS = 1800

# How often the watchdog checks the grace condition. Cheap poll.
_WATCHDOG_POLL_SECONDS = 5

# Hard cap for the in-memory tail buffers we keep for post-kill diagnosis.
# Keep enough to reconstruct the last few minutes of activity without
# unbounded growth on busy runs.
_STDOUT_TAIL_LINES = 50
_STDERR_TAIL_LINES = 100


class _ProgressState:
    """Mutable progress tracker shared between stream readers and heartbeat.

    Not threadsafe — all updates happen from coroutines on the same event loop.
    """

    __slots__ = (
        "started_at", "last_event_at", "last_result_at",
        "events_seen", "turns", "tool_calls",
        "last_event_kind", "last_tool", "last_text",
        "stdout_tail", "stderr_tail", "final_event",
        "input_tokens_running", "output_tokens_running",
    )

    def __init__(self) -> None:
        now = time.monotonic()
        self.started_at = now
        self.last_event_at = now
        self.last_result_at: float | None = None
        self.events_seen = 0
        self.turns = 0
        self.tool_calls = 0
        self.last_event_kind: str | None = None
        self.last_tool: str | None = None
        self.last_text: str = ""
        self.stdout_tail: deque[str] = deque(maxlen=_STDOUT_TAIL_LINES)
        self.stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self.final_event: dict | None = None
        self.input_tokens_running = 0
        self.output_tokens_running = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def since_last_activity(self) -> float:
        return time.monotonic() - self.last_event_at


def _write_progress(path: Path | None, line: str) -> None:
    """Append one timestamped line to the per-call progress log.

    Best-effort: I/O errors are swallowed so logging glitches never break
    the agent run.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except OSError:
        pass


async def _heartbeat_loop(
    progress: _ProgressState,
    log_path: Path | None,
    stop: asyncio.Event,
) -> None:
    """Emit a heartbeat line every HEARTBEAT_INTERVAL_SECONDS until stop is set."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            return  # stop fired
        except asyncio.TimeoutError:
            pass
        _write_progress(
            log_path,
            f"heartbeat elapsed={progress.elapsed():.0f}s "
            f"since_last_event={progress.since_last_activity():.0f}s "
            f"turns={progress.turns} tool_calls={progress.tool_calls} "
            f"events={progress.events_seen} "
            f"last_event={progress.last_event_kind!r} "
            f"last_tool={progress.last_tool!r} "
            f"in_tok={progress.input_tokens_running} "
            f"out_tok={progress.output_tokens_running}",
        )


async def _post_result_watchdog(
    progress: _ProgressState,
    log_path: Path | None,
    stop: asyncio.Event,
    kill_signal: asyncio.Event,
    grace_seconds: int = POST_RESULT_IDLE_GRACE_SECONDS,
    idle_stall_seconds: int = IDLE_STALL_SECONDS,
) -> None:
    """Set `kill_signal` when the CLI has gone idle, in two cases.

    Post-result idle: `result_seen AND since_last_event > grace_seconds` —
    the model finished but the subprocess never exited; we have the cached
    result so this is a graceful kill.

    Mid-run stall: `NOT result_seen AND since_last_event > idle_stall_seconds`
    — the run hung before producing any result (RTL-13824); the caller treats
    this as a failure, not a success, since there is nothing cached.

    Any new event resets the idle timer via `progress.last_event_at`, so
    neither branch fires while the model is still producing output.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_WATCHDOG_POLL_SECONDS)
            return  # stop fired
        except asyncio.TimeoutError:
            pass
        idle = progress.since_last_activity()
        if progress.last_result_at is not None:
            if idle >= grace_seconds:
                _write_progress(
                    log_path,
                    f"post_result_grace exceeded ({grace_seconds}s); "
                    f"last_event={progress.last_event_kind!r} "
                    f"events={progress.events_seen} turns={progress.turns}; "
                    f"signalling graceful kill",
                )
                kill_signal.set()
                return
        elif idle >= idle_stall_seconds:
            _write_progress(
                log_path,
                f"idle_stall exceeded ({idle_stall_seconds}s); no result event seen; "
                f"last_event={progress.last_event_kind!r} "
                f"last_tool={progress.last_tool!r} "
                f"events={progress.events_seen} turns={progress.turns}; "
                f"signalling stall kill",
            )
            kill_signal.set()
            return


async def _drain_stream_json(
    stream: asyncio.StreamReader | None,
    progress: _ProgressState,
    log_path: Path | None,
    raw_fh: Any,
) -> None:
    """Read CLI stdout line-by-line and update progress state per JSON event."""
    if stream is None:
        return
    while True:
        try:
            line_bytes = await stream.readline()
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            raise
        if not line_bytes:
            return
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            continue
        progress.stdout_tail.append(line)
        if raw_fh is not None:
            try:
                raw_fh.write(line + "\n")
            except Exception:
                pass
        progress.events_seen += 1
        progress.last_event_at = time.monotonic()
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            progress.last_event_kind = "non_json"
            continue
        _handle_stream_event(evt, progress, log_path)


def _handle_stream_event(
    evt: dict,
    progress: _ProgressState,
    log_path: Path | None,
) -> None:
    """Update progress and append a structured log line for one stream event."""
    kind = evt.get("type", "unknown")
    progress.last_event_kind = kind

    if kind == "system":
        sub = evt.get("subtype", "")
        if sub == "init":
            _write_progress(
                log_path,
                f"event=system subtype=init session={evt.get('session_id', '')!r} "
                f"model={evt.get('model', '')!r} cwd={evt.get('cwd', '')!r}",
            )
        elif sub.startswith("hook_"):
            _write_progress(log_path, f"event=system subtype={sub}")
        else:
            _write_progress(log_path, f"event=system subtype={sub}")
        return

    if kind == "assistant":
        progress.turns += 1
        msg = evt.get("message", {}) or {}
        usage = msg.get("usage", {}) or {}
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        progress.input_tokens_running += in_tok
        progress.output_tokens_running += out_tok
        # Walk content blocks once: count tool_use, capture latest text snippet.
        content = msg.get("content", []) or []
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                txt = block.get("text", "") or ""
                if txt:
                    progress.last_text = txt[:2000]
                    _write_progress(
                        log_path,
                        f"event=assistant turn={progress.turns} "
                        f"block=text len={len(txt)} preview={txt[:120]!r}",
                    )
            elif btype == "tool_use":
                progress.tool_calls += 1
                name = block.get("name", "")
                progress.last_tool = name
                tool_input = block.get("input", {}) or {}
                # Truncate input preview hard — Edit/Write blocks can be huge.
                preview = json.dumps(tool_input, default=str)[:200]
                _write_progress(
                    log_path,
                    f"event=assistant turn={progress.turns} "
                    f"block=tool_use name={name!r} input={preview}",
                )
        if not content:
            _write_progress(
                log_path,
                f"event=assistant turn={progress.turns} block=empty "
                f"in_tok={in_tok} out_tok={out_tok}",
            )
        return

    if kind == "user":
        # Tool results echoed back to the model.
        msg = evt.get("message", {}) or {}
        content = msg.get("content", []) or []
        for block in content:
            if block.get("type") == "tool_result":
                tc = block.get("content", "")
                if isinstance(tc, list):
                    tc_str = json.dumps(tc, default=str)
                else:
                    tc_str = str(tc)
                is_err = block.get("is_error", False)
                _write_progress(
                    log_path,
                    f"event=tool_result is_error={is_err} "
                    f"len={len(tc_str)} preview={tc_str[:160]!r}",
                )
        return

    if kind == "rate_limit_event":
        info = evt.get("rate_limit_info", {}) or {}
        _write_progress(
            log_path,
            f"event=rate_limit status={info.get('status', '')!r} "
            f"resets_at={info.get('resetsAt', '')} "
            f"overage={info.get('overageStatus', '')!r}",
        )
        return

    if kind == "result":
        progress.final_event = evt
        progress.last_result_at = time.monotonic()
        usage = evt.get("usage", {}) or {}
        _write_progress(
            log_path,
            f"event=result subtype={evt.get('subtype', '')!r} "
            f"is_error={evt.get('is_error', False)} "
            f"num_turns={evt.get('num_turns', 0)} "
            f"duration_ms={evt.get('duration_ms', 0)} "
            f"in_tok={usage.get('input_tokens', 0)} "
            f"out_tok={usage.get('output_tokens', 0)} "
            f"cost=${evt.get('total_cost_usd', 0):.4f} "
            f"stop={evt.get('stop_reason', '')!r}",
        )
        return

    _write_progress(log_path, f"event=other type={kind!r}")


async def _drain_stderr(
    stream: asyncio.StreamReader | None,
    progress: _ProgressState,
    raw_fh: Any,
) -> None:
    """Read CLI stderr line-by-line into the rolling tail buffer."""
    if stream is None:
        return
    while True:
        try:
            line_bytes = await stream.readline()
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            raise
        if not line_bytes:
            return
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            continue
        progress.stderr_tail.append(line)
        if raw_fh is not None:
            try:
                raw_fh.write(f"[stderr] {line}\n")
            except Exception:
                pass

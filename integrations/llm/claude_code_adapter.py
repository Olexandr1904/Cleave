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
import re
import shutil
from datetime import datetime, timezone
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

DEFAULT_MAX_TURNS = 50
DEFAULT_TIMEOUT = 2400  # 40 minutes — accommodates large QA suites (compile + tests + lint)


class ClaudeCodeAdapter(LLMInterface):
    """Claude Code CLI adapter using `claude -p` subprocess."""

    def __init__(
        self,
        model_provider: Callable[[], str] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._claude_bin = shutil.which("claude") or "claude"
        self._model_provider = model_provider or (lambda: "")
        self._max_turns = max_turns
        self._timeout = timeout

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
        pid_callback: Callable[[int], None] | None = None,
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
            pid_callback=pid_callback,
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
        pid_callback: Callable[[int], None] | None = None,
    ) -> LLMResponse:
        """Run claude CLI subprocess."""
        cmd = [self._claude_bin, "-p"]

        # System prompt prepended
        full_prompt = prompt
        if system:
            full_prompt = f"{system}\n\n---\n\n{full_prompt}"

        # Model
        use_model = model or self._model_provider()
        if use_model:
            cmd.extend(["--model", use_model])

        # Output format
        cmd.extend(["--output-format", "json"])

        # Max turns
        turns = max_turns or self._max_turns
        cmd.extend(["--max-turns", str(turns)])

        # Allowed tools
        if allowed_tools is not None:
            cc_tools = self._map_tools(allowed_tools)
            cmd.extend(["--allowedTools", ",".join(cc_tools) if cc_tools else ""])

        logger.info(
            "Claude Code CLI: model=%s, cwd=%s, tools=%s, max_turns=%d",
            use_model or "default", cwd or ".", allowed_tools, turns,
        )

        try:
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
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"Claude Code CLI timed out after {self._timeout}s"
            )

        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error(
                "Claude Code CLI failed (rc=%d) stdout=%r stderr=%r",
                proc.returncode, stdout_str[:1500], stderr_str[:1500],
            )
            classified = _classify_cli_error(stdout_str, stderr_str)
            if classified is not None:
                raise classified
            raise RuntimeError(
                f"Claude Code CLI exited with code {proc.returncode}. "
                f"stderr={stderr_str[:500]!r} stdout={stdout_str[:500]!r}"
            )

        # Parse JSON output
        try:
            data = json.loads(stdout_str)
        except json.JSONDecodeError:
            # If JSON parsing fails, treat stdout as plain text
            logger.warning("Claude Code CLI returned non-JSON output")
            return LLMResponse(
                content=stdout_str,
                input_tokens=0,
                output_tokens=0,
                model=use_model,
            )

        # Extract fields from JSON response
        content = data.get("result", "")
        is_error = data.get("is_error", False)
        usage = data.get("usage", {})

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)

        total_cost = data.get("total_cost_usd", 0)
        num_turns = data.get("num_turns", 1)
        stop_reason = data.get("stop_reason", "")

        if is_error:
            logger.error("Claude Code CLI error: %s", content[:200])
            classified = _classify_cli_error(stdout_str, stderr_str)
            if classified is not None:
                raise classified
            raise RuntimeError(f"Claude Code returned error: {content[:500]}")

        logger.info(
            "Claude Code CLI done: turns=%d, tokens=%d/%d (cache_r=%d, cache_w=%d), "
            "cost=$%.4f, stop=%s",
            num_turns, input_tokens, output_tokens,
            cache_read, cache_create, total_cost, stop_reason,
        )

        return LLMResponse(
            content=content,
            input_tokens=input_tokens + cache_read + cache_create,
            output_tokens=output_tokens,
            model=use_model or data.get("model", ""),
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

"""Claude adapter implementing LLMInterface via Anthropic SDK."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import anthropic

from integrations.llm.llm_interface import LLMInterface, LLMResponse, ToolUseRequest

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]

# Anthropic ephemeral cache breakpoint marker.
_CACHE_EPHEMERAL: dict[str, Any] = {"type": "ephemeral"}


class ClaudeAdapter(LLMInterface):
    """Claude API adapter via Anthropic SDK."""

    def __init__(
        self,
        api_key: str,
        default_model_provider: Callable[[], str],
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model_provider = default_model_provider

    async def send_message(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Send a prompt to Claude and return the response."""
        messages = [{"role": "user", "content": prompt}]
        return await self._call_api(messages, model, max_tokens, tools, system)

    async def send_tool_results(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Continue a conversation with tool results."""
        return await self._call_api(messages, model, max_tokens, tools, system)

    async def _call_api(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Make a single Claude API call with retries.

        Applies prompt-cache breakpoints to the stable prefix so repeated
        agent rounds hit the cache. Three breakpoints are placed (max is 4):
        last tool, system block, and the first user message. Everything
        before the marked block is cached together; later messages (tool
        results) are volatile and re-evaluated each call.
        """
        use_model = model or self._default_model_provider()
        last_error = None

        cached_messages = _mark_first_user_for_cache(messages)
        kwargs: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens,
            "messages": cached_messages,
        }
        if tools:
            kwargs["tools"] = _mark_last_tool_for_cache(tools)
        if system:
            kwargs["system"] = _wrap_system_for_cache(system)

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.messages.create(**kwargs)
                return self._parse_response(response, use_model)

            except (anthropic.RateLimitError, anthropic.APITimeoutError,
                    anthropic.InternalServerError) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    logger.warning(
                        "Claude API retry %d/%d: %s", attempt + 1, MAX_RETRIES, e,
                    )
            except anthropic.AuthenticationError:
                raise  # Don't retry auth failures

        raise last_error  # type: ignore[misc]

    def _parse_response(self, response: Any, model: str) -> LLMResponse:
        """Parse Anthropic API response into LLMResponse."""
        content = ""
        tool_use_requests: list[ToolUseRequest] = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_use_requests.append(
                    ToolUseRequest(
                        id=block.id,
                        name=block.name,
                        input=block.input,
                    )
                )

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        # The SDK reports `input_tokens` excluding cached tokens. Mirror the
        # CLI adapter and include them so downstream budgets/dashboards count
        # the full prefix the model actually saw.
        result = LLMResponse(
            content=content,
            input_tokens=usage.input_tokens + cache_read + cache_create,
            output_tokens=usage.output_tokens,
            model=model,
            tool_use=tool_use_requests,
            stop_reason=response.stop_reason or "",
        )

        logger.info(
            "Claude API call: model=%s, input=%d (cache_r=%d cache_w=%d), output=%d, "
            "tool_calls=%d, stop=%s",
            model, result.input_tokens, cache_read, cache_create, result.output_tokens,
            len(tool_use_requests), result.stop_reason,
        )
        return result

    async def quick_query(
        self,
        prompt: str,
        system: str = "",
        timeout: int = 5,
    ) -> str:
        """Lightweight prompt-to-response call. No tools, single turn, short timeout.

        Used for intent parsing and other quick classification tasks. Matches
        the signature of ClaudeCodeAdapter.quick_query so IntentParser can use
        either adapter interchangeably.

        Args:
            prompt: The user message to classify.
            system: System prompt with context.
            timeout: Timeout in seconds (default 5).

        Returns:
            The raw text response from Claude.
        """
        model = self._default_model_provider()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        logger.info("Claude API quick_query: model=%s", model)
        try:
            response = await asyncio.wait_for(
                self._client.messages.create(**kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Claude API quick_query timed out after {timeout}s")

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text
        return text.strip()

    def supports_extended_thinking(self) -> bool:
        """Claude supports extended thinking."""
        return True


def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the last tool definition with cache_control.

    Anthropic caches everything up to and including the marked block, so
    flagging the trailing tool caches the entire tools array. Tool defs are
    stable per agent — they don't mutate between rounds — so this hits cache
    from round 2 onward.
    """
    if not tools:
        return tools
    out = [dict(t) for t in tools]  # shallow copy is enough; we only add a key
    out[-1] = {**out[-1], "cache_control": _CACHE_EPHEMERAL}
    return out


def _wrap_system_for_cache(system: str) -> list[dict[str, Any]]:
    """Wrap a system string as a single text block with cache_control."""
    return [{"type": "text", "text": system, "cache_control": _CACHE_EPHEMERAL}]


def _mark_first_user_for_cache(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the first user message's string content to a cached text block.

    The agent_runtime tool-use loop sends messages as
    [user(prompt_str), assistant(...), user(tool_results), assistant(...), ...]
    where messages[0].content is the stable assembled prompt. Wrapping that
    content in a list with cache_control caches the full prefix; subsequent
    tool-result messages are not marked and remain volatile.

    Already-structured content (lists, non-user roles) is left as-is.
    """
    if not messages:
        return messages
    first = messages[0]
    if first.get("role") != "user":
        return messages
    content = first.get("content")
    if not isinstance(content, str):
        return messages
    out = list(messages)
    out[0] = {
        **first,
        "content": [
            {"type": "text", "text": content, "cache_control": _CACHE_EPHEMERAL},
        ],
    }
    return out

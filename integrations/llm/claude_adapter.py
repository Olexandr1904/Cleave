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
        """Make a single Claude API call with retries."""
        use_model = model or self._default_model_provider()
        last_error = None

        kwargs: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if system:
            kwargs["system"] = system

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

        result = LLMResponse(
            content=content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=model,
            tool_use=tool_use_requests,
            stop_reason=response.stop_reason or "",
        )

        logger.info(
            "Claude API call: model=%s, input=%d, output=%d, tool_calls=%d, stop=%s",
            model, result.input_tokens, result.output_tokens,
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

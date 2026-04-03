"""Claude adapter implementing LLMInterface via Anthropic SDK."""

from __future__ import annotations

import asyncio
import logging

import anthropic

from integrations.llm.llm_interface import LLMInterface, LLMResponse

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]
TIMEOUT = 120  # Claude can take longer


class ClaudeAdapter(LLMInterface):
    """Claude API adapter via Anthropic SDK."""

    def __init__(self, api_key: str, default_model: str = "claude-sonnet-4-5") -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model = default_model

    async def send_message(
        self, prompt: str, model: str = "", max_tokens: int = 4096
    ) -> LLMResponse:
        """Send a prompt to Claude and return the response."""
        use_model = model or self._default_model
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.messages.create(
                    model=use_model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )

                content = ""
                for block in response.content:
                    if block.type == "text":
                        content += block.text

                result = LLMResponse(
                    content=content,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=use_model,
                )

                logger.info(
                    "Claude API call: model=%s, input=%d tokens, output=%d tokens",
                    use_model, result.input_tokens, result.output_tokens,
                )
                return result

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

    def supports_extended_thinking(self) -> bool:
        """Claude supports extended thinking."""
        return True

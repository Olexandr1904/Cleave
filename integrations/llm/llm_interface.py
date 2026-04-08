"""Abstract LLM interface for AI provider abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolUseRequest:
    """A tool call requested by the LLM."""
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM API call."""
    content: str
    input_tokens: int
    output_tokens: int
    model: str = ""
    tool_use: list[ToolUseRequest] = field(default_factory=list)
    stop_reason: str = ""


class LLMInterface(ABC):
    """Abstract interface for LLM providers (Claude, OpenAI, etc.)."""

    @abstractmethod
    async def send_message(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Send a prompt to the LLM and return the response.

        Args:
            prompt: The assembled prompt text.
            model: Model identifier to use.
            max_tokens: Maximum tokens in the response.
            tools: Optional tool definitions for function calling.
            system: Optional system prompt.

        Returns:
            LLMResponse with content, token usage, and any tool_use requests.
        """

    @abstractmethod
    async def send_tool_results(
        self,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Continue a conversation with tool results.

        Args:
            messages: Full conversation history including tool results.
            model: Model identifier to use.
            max_tokens: Maximum tokens in the response.
            tools: Tool definitions (same as original call).
            system: Optional system prompt.

        Returns:
            LLMResponse with content, token usage, and any further tool_use requests.
        """

    @abstractmethod
    def supports_extended_thinking(self) -> bool:
        """Whether this provider supports extended thinking mode."""

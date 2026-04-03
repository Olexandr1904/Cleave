"""Abstract LLM interface for AI provider abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    """Response from an LLM API call."""
    content: str
    input_tokens: int
    output_tokens: int
    model: str = ""


class LLMInterface(ABC):
    """Abstract interface for LLM providers (Claude, OpenAI, etc.)."""

    @abstractmethod
    async def send_message(
        self, prompt: str, model: str, max_tokens: int = 4096
    ) -> LLMResponse:
        """Send a prompt to the LLM and return the response.

        Args:
            prompt: The assembled prompt text.
            model: Model identifier to use.
            max_tokens: Maximum tokens in the response.

        Returns:
            LLMResponse with content and token usage.
        """

    @abstractmethod
    def supports_extended_thinking(self) -> bool:
        """Whether this provider supports extended thinking mode."""

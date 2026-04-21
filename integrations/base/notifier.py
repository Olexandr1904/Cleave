"""Abstract notifier interface for operator communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Button:
    """A labeled action button for inline display."""
    label: str   # User-visible text, e.g. "Approve"
    action: str  # Encoded action, e.g. "approve:ACME-14595"


class NotifierInterface(ABC):
    """Abstract interface for notification systems (Telegram, Slack, etc.)."""

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        message: str,
        buttons: list[Button] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Send a message to the operator.

        Args:
            chat_id: Target chat.
            message: Text content.
            buttons: Optional inline action buttons.
            reply_to_message_id: If set, send as a reply to this message.

        Returns the message ID for reply tracking.
        """

    @abstractmethod
    async def wait_for_reply(self, chat_id: str, message_id: int, timeout_seconds: int = 0) -> str | None:
        """Wait for a reply to a specific message.

        Args:
            chat_id: Chat to listen in.
            message_id: Original message ID to match replies against.
            timeout_seconds: Max seconds to wait (0 = wait indefinitely).

        Returns:
            The reply text, or None if timed out.
        """

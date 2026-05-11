"""Abstract tracker interface for ticket management systems."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TicketData:
    """Normalized ticket data from a tracker."""
    id: str
    url: str
    summary: str
    description: str
    acceptance_criteria: str = ""
    labels: list[str] = field(default_factory=list)
    priority: str = ""
    sprint: str | None = None
    linked_issues: list[dict[str, Any]] = field(default_factory=list)
    assignee: str | None = None
    reporter: str = ""
    created: str = ""  # ISO 8601 timestamp
    attachments: list[dict[str, str]] = field(default_factory=list)
    # Each: {"filename": "...", "url": "...", "mime_type": "..."}


@dataclass
class TicketComment:
    """A comment on a ticket — provider-neutral."""
    id: str
    author: str
    created: str        # ISO date "YYYY-MM-DD" suffices
    body: str           # plain text; adapter strips formatting markup


@dataclass
class StatusChange:
    """One step in a ticket's status history."""
    created: str
    from_status: str
    to_status: str
    author: str


class TrackerInterface(ABC):
    """Abstract interface for ticket tracking systems (Jira, Linear, etc.)."""

    @abstractmethod
    async def poll_tickets(self) -> list[TicketData]:
        """Fetch tickets matching trigger criteria.

        Returns tickets that have the trigger label, correct status,
        and are not in ignore labels.
        """

    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> TicketData:
        """Get full ticket details by ID."""

    @abstractmethod
    async def transition_ticket(self, ticket_id: str, status: str) -> None:
        """Transition a ticket to a new status."""

    @abstractmethod
    async def add_comment(self, ticket_id: str, comment: str) -> None:
        """Post a comment to a ticket."""

    @abstractmethod
    async def get_comments(self, ticket_id: str) -> list[TicketComment]:
        """Return all comments on a ticket, oldest first."""

    @abstractmethod
    async def get_status_history(self, ticket_id: str) -> list[StatusChange]:
        """Return the status-transition history of a ticket, oldest first."""

    @abstractmethod
    async def download_attachment(self, url: str) -> bytes:
        """Fetch an attachment's bytes. Adapter owns its auth headers."""

    @abstractmethod
    async def list_transitions(self, ticket_id: str) -> list[str]:
        """Return human-readable names of currently available transitions/lists."""

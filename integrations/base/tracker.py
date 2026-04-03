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

"""Analyze handler — validates and prepares tickets for manual mode analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of ticket validation."""
    valid: list[Any] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


class AnalyzeHandler:
    """Validates ticket IDs against Jira before creating workspaces."""

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker

    async def validate_tickets(self, ticket_ids: list[str]) -> ValidationResult:
        """Fetch each ticket from Jira to verify it exists."""
        result = ValidationResult()
        for tid in ticket_ids:
            try:
                ticket = await self._tracker.get_ticket(tid)
                result.valid.append(ticket)
            except Exception as e:
                logger.warning("Ticket %s not found or inaccessible: %s", tid, e)
                result.invalid.append(f"{tid}: {e}")
        return result

    def is_already_active(self, ticket_id: str, active_workspaces: list[Any]) -> bool:
        """Check if a ticket already has an active workspace."""
        return any(ws.state.ticket_id == ticket_id for ws in active_workspaces)

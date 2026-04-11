"""Structured event system for Sickle pipeline observability."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    """A single pipeline event."""

    event_type: str
    message: str
    timestamp: float = field(default_factory=time.time)
    project_id: str | None = None
    ticket_id: str | None = None
    agent_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "message": self.message,
            "timestamp": self.timestamp,
            "project_id": self.project_id,
            "ticket_id": self.ticket_id,
            "agent_id": self.agent_id,
            "data": self.data,
        }


class EventBus:
    """In-memory event buffer with listener support."""

    def __init__(self, max_buffer: int = 2000) -> None:
        self._buffer: deque[Event] = deque(maxlen=max_buffer)
        self._listeners: list[Callable[[Event], Any]] = []

    def emit(
        self,
        event_type: str,
        message: str,
        *,
        project_id: str | None = None,
        ticket_id: str | None = None,
        agent_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(
            event_type=event_type,
            message=message,
            project_id=project_id,
            ticket_id=ticket_id,
            agent_id=agent_id,
            data=data or {},
        )
        self._buffer.append(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                pass
        return event

    def add_listener(self, fn: Callable[[Event], Any]) -> None:
        self._listeners.append(fn)

    def recent(
        self,
        limit: int = 100,
        project_id: str | None = None,
        ticket_id: str | None = None,
    ) -> list[Event]:
        events = list(reversed(self._buffer))
        if project_id:
            events = [e for e in events if e.project_id == project_id]
        if ticket_id:
            events = [e for e in events if e.ticket_id == ticket_id]
        return events[:limit]

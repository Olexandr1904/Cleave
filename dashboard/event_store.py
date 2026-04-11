"""SQLite-backed event persistence for the Sickle dashboard."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite


class EventStore:
    """Async SQLite event storage."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                project_id TEXT,
                ticket_id TEXT,
                agent_id TEXT,
                data_json TEXT DEFAULT '{}'
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)"
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def insert(self, event: Any) -> None:
        """Insert an Event into the database."""
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO events (timestamp, event_type, message, project_id, ticket_id, agent_id, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.timestamp,
                event.event_type,
                event.message,
                event.project_id,
                event.ticket_id,
                event.agent_id,
                json.dumps(event.data),
            ),
        )
        await self._db.commit()

    async def query_recent(
        self,
        limit: int = 100,
        offset: int = 0,
        project_id: str | None = None,
        ticket_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query events newest-first with optional filters."""
        assert self._db is not None
        conditions = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if ticket_id:
            conditions.append("ticket_id = ?")
            params.append(ticket_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        rows = await self._db.execute_fetchall(
            f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_dict(row) for row in rows]

    async def get_projects(self) -> list[str]:
        """Return distinct project IDs."""
        assert self._db is not None
        rows = await self._db.execute_fetchall(
            "SELECT DISTINCT project_id FROM events WHERE project_id IS NOT NULL ORDER BY project_id"
        )
        return [row[0] for row in rows]

    async def get_tickets(self, project_id: str | None = None) -> list[str]:
        """Return distinct ticket IDs, optionally filtered by project."""
        assert self._db is not None
        if project_id:
            rows = await self._db.execute_fetchall(
                "SELECT DISTINCT ticket_id FROM events WHERE project_id = ? AND ticket_id IS NOT NULL ORDER BY ticket_id",
                (project_id,),
            )
        else:
            rows = await self._db.execute_fetchall(
                "SELECT DISTINCT ticket_id FROM events WHERE ticket_id IS NOT NULL ORDER BY ticket_id"
            )
        return [row[0] for row in rows]

    async def count(self, project_id: str | None = None) -> int:
        assert self._db is not None
        if project_id:
            rows = await self._db.execute_fetchall(
                "SELECT COUNT(*) FROM events WHERE project_id = ?", (project_id,)
            )
        else:
            rows = await self._db.execute_fetchall("SELECT COUNT(*) FROM events")
        return rows[0][0]

    @staticmethod
    def _row_to_dict(row: tuple) -> dict[str, Any]:
        return {
            "id": row[0],
            "timestamp": row[1],
            "event_type": row[2],
            "message": row[3],
            "project_id": row[4],
            "ticket_id": row[5],
            "agent_id": row[6],
            "data": json.loads(row[7]) if row[7] else {},
        }

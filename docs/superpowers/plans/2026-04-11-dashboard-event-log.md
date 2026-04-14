# Dashboard & Structured Event Log — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local web dashboard showing per-project ticket history, agent activity, and a global event log — so the operator can see exactly what Sickle did, when, how, and by which agent.

**Architecture:** A lightweight event bus captures structured events from orchestrator, agent runtime, and Telegram adapters, persists them to SQLite, and serves them via an embedded async web server (Starlette) running in the same process. The dashboard is a single HTML file with vanilla JS — no build step.

**Tech Stack:** `starlette` + `uvicorn` (async web server), `aiosqlite` (async SQLite), vanilla HTML/CSS/JS dashboard.

---

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `dashboard/__init__.py` | Package init |
| `dashboard/events.py` | `Event` dataclass + `EventBus` (in-memory fanout + store) |
| `dashboard/event_store.py` | `EventStore` — async SQLite persistence + queries |
| `dashboard/web.py` | Starlette app with API endpoints |
| `dashboard/static/index.html` | Single-page dashboard (HTML + inline CSS + inline JS) |
| `tests/unit/test_events.py` | Tests for Event model + EventBus |
| `tests/unit/test_event_store.py` | Tests for SQLite EventStore |
| `tests/unit/test_dashboard_web.py` | Tests for API endpoints |

### Modified files

| File | What changes |
|------|-------------|
| `pyproject.toml` | Add `starlette`, `uvicorn`, `aiosqlite` dependencies |
| `config/schemas.py` | Add `DashboardConfig` dataclass |
| `main.py` | Wire EventBus, EventStore, start web server |
| `orchestrator/orchestrator.py` | Emit events at key points |
| `orchestrator/agent_runtime.py` | Emit events on agent dispatch/completion |
| `integrations/telegram/telegram_adapter.py` | Emit events on message send/receive |
| `integrations/telegram/command_handler.py` | Emit events on intent parse + handler dispatch |

---

## Task 1: Event Model and EventBus

**Files:**
- Create: `dashboard/__init__.py`
- Create: `dashboard/events.py`
- Test: `tests/unit/test_events.py`

- [ ] **Step 1: Write the failing tests for Event dataclass**

```python
# tests/unit/test_events.py
from __future__ import annotations

import time

from dashboard.events import Event, EventBus


class TestEvent:
    def test_create_event_with_required_fields(self):
        e = Event(event_type="test_event", message="something happened")
        assert e.event_type == "test_event"
        assert e.message == "something happened"
        assert e.timestamp > 0
        assert e.project_id is None
        assert e.ticket_id is None
        assert e.agent_id is None
        assert e.data == {}

    def test_create_event_with_all_fields(self):
        e = Event(
            event_type="agent_completed",
            message="Dev agent finished",
            project_id="acme",
            ticket_id="ACME-123",
            agent_id="dev-agent",
            data={"duration": 42.5, "tokens": 1500},
        )
        assert e.project_id == "acme"
        assert e.ticket_id == "ACME-123"
        assert e.agent_id == "dev-agent"
        assert e.data["duration"] == 42.5

    def test_event_auto_timestamp(self):
        before = time.time()
        e = Event(event_type="test", message="x")
        after = time.time()
        assert before <= e.timestamp <= after

    def test_event_to_dict(self):
        e = Event(
            event_type="stage_transition",
            message="ANALYSIS -> DEV",
            project_id="acme",
            ticket_id="ACME-123",
        )
        d = e.to_dict()
        assert d["event_type"] == "stage_transition"
        assert d["message"] == "ANALYSIS -> DEV"
        assert d["project_id"] == "acme"
        assert d["ticket_id"] == "ACME-123"
        assert "timestamp" in d
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_events.py -v`
Expected: `ModuleNotFoundError: No module named 'dashboard'`

- [ ] **Step 3: Implement Event dataclass**

```python
# dashboard/__init__.py
```

```python
# dashboard/events.py
"""Structured event system for Sickle pipeline observability."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


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
```

- [ ] **Step 4: Run tests to verify Event passes**

Run: `pytest tests/unit/test_events.py::TestEvent -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Write failing tests for EventBus**

Add to `tests/unit/test_events.py`:

```python
class TestEventBus:
    def test_emit_stores_event(self):
        bus = EventBus()
        bus.emit("test_event", "hello")
        assert len(bus.recent()) == 1
        assert bus.recent()[0].message == "hello"

    def test_recent_returns_newest_first(self):
        bus = EventBus()
        bus.emit("a", "first")
        bus.emit("b", "second")
        events = bus.recent()
        assert events[0].message == "second"
        assert events[1].message == "first"

    def test_recent_respects_limit(self):
        bus = EventBus()
        for i in range(10):
            bus.emit("test", f"event {i}")
        assert len(bus.recent(limit=3)) == 3

    def test_recent_by_project(self):
        bus = EventBus()
        bus.emit("a", "proj1 event", project_id="p1")
        bus.emit("b", "proj2 event", project_id="p2")
        bus.emit("c", "global event")
        events = bus.recent(project_id="p1")
        assert len(events) == 1
        assert events[0].project_id == "p1"

    def test_recent_by_ticket(self):
        bus = EventBus()
        bus.emit("a", "ticket event", ticket_id="T-1")
        bus.emit("b", "other event", ticket_id="T-2")
        events = bus.recent(ticket_id="T-1")
        assert len(events) == 1

    def test_max_buffer_size(self):
        bus = EventBus(max_buffer=5)
        for i in range(10):
            bus.emit("test", f"event {i}")
        assert len(bus.recent(limit=100)) == 5

    def test_emit_with_all_fields(self):
        bus = EventBus()
        bus.emit(
            "agent_completed",
            "Dev finished",
            project_id="acme",
            ticket_id="ACME-1",
            agent_id="dev-agent",
            data={"duration": 10.0},
        )
        e = bus.recent()[0]
        assert e.agent_id == "dev-agent"
        assert e.data["duration"] == 10.0

    def test_listener_called_on_emit(self):
        bus = EventBus()
        received = []
        bus.add_listener(lambda e: received.append(e))
        bus.emit("test", "hello")
        assert len(received) == 1
        assert received[0].message == "hello"
```

- [ ] **Step 6: Run tests to verify EventBus tests fail**

Run: `pytest tests/unit/test_events.py::TestEventBus -v`
Expected: FAIL — `EventBus` has no methods yet

- [ ] **Step 7: Implement EventBus**

Add to `dashboard/events.py`:

```python
from collections import deque
from typing import Callable


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
```

- [ ] **Step 8: Run all event tests**

Run: `pytest tests/unit/test_events.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add dashboard/__init__.py dashboard/events.py tests/unit/test_events.py
git commit -m "feat: add Event model and EventBus for pipeline observability"
```

---

## Task 2: SQLite EventStore

**Files:**
- Create: `dashboard/event_store.py`
- Test: `tests/unit/test_event_store.py`

- [ ] **Step 1: Write failing tests for EventStore**

```python
# tests/unit/test_event_store.py
from __future__ import annotations

import pytest

from dashboard.event_store import EventStore
from dashboard.events import Event


@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test_events.db")
    s = EventStore(db_path)
    await s.initialize()
    yield s
    await s.close()


class TestEventStore:
    async def test_insert_and_query_recent(self, store):
        await store.insert(Event(event_type="test", message="hello"))
        events = await store.query_recent(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "test"
        assert events[0]["message"] == "hello"

    async def test_query_recent_newest_first(self, store):
        await store.insert(Event(event_type="a", message="first", timestamp=1.0))
        await store.insert(Event(event_type="b", message="second", timestamp=2.0))
        events = await store.query_recent(limit=10)
        assert events[0]["message"] == "second"
        assert events[1]["message"] == "first"

    async def test_query_by_project(self, store):
        await store.insert(Event(event_type="a", message="p1", project_id="proj1"))
        await store.insert(Event(event_type="b", message="p2", project_id="proj2"))
        events = await store.query_recent(project_id="proj1")
        assert len(events) == 1
        assert events[0]["project_id"] == "proj1"

    async def test_query_by_ticket(self, store):
        await store.insert(Event(event_type="a", message="t1", ticket_id="T-1"))
        await store.insert(Event(event_type="b", message="t2", ticket_id="T-2"))
        events = await store.query_recent(ticket_id="T-1")
        assert len(events) == 1

    async def test_query_respects_limit(self, store):
        for i in range(20):
            await store.insert(Event(event_type="test", message=f"e{i}"))
        events = await store.query_recent(limit=5)
        assert len(events) == 5

    async def test_query_with_offset(self, store):
        for i in range(10):
            await store.insert(Event(event_type="test", message=f"e{i}", timestamp=float(i)))
        events = await store.query_recent(limit=3, offset=3)
        assert len(events) == 3
        # Newest first, so offset=3 skips the 3 newest
        assert events[0]["message"] == "e6"

    async def test_data_json_round_trip(self, store):
        await store.insert(Event(
            event_type="agent_done",
            message="done",
            data={"duration": 12.5, "tokens": 999},
        ))
        events = await store.query_recent()
        assert events[0]["data"]["duration"] == 12.5
        assert events[0]["data"]["tokens"] == 999

    async def test_get_projects(self, store):
        await store.insert(Event(event_type="a", message="x", project_id="p1"))
        await store.insert(Event(event_type="b", message="y", project_id="p1"))
        await store.insert(Event(event_type="c", message="z", project_id="p2"))
        await store.insert(Event(event_type="d", message="w"))  # no project
        projects = await store.get_projects()
        assert set(projects) == {"p1", "p2"}

    async def test_get_tickets_for_project(self, store):
        await store.insert(Event(event_type="a", message="x", project_id="p1", ticket_id="T-1"))
        await store.insert(Event(event_type="b", message="y", project_id="p1", ticket_id="T-2"))
        await store.insert(Event(event_type="c", message="z", project_id="p2", ticket_id="T-3"))
        tickets = await store.get_tickets(project_id="p1")
        assert set(tickets) == {"T-1", "T-2"}

    async def test_count_events(self, store):
        for i in range(7):
            await store.insert(Event(event_type="test", message=f"e{i}"))
        count = await store.count()
        assert count == 7

    async def test_count_by_project(self, store):
        await store.insert(Event(event_type="a", message="x", project_id="p1"))
        await store.insert(Event(event_type="b", message="y", project_id="p1"))
        await store.insert(Event(event_type="c", message="z", project_id="p2"))
        assert await store.count(project_id="p1") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_event_store.py -v`
Expected: `ModuleNotFoundError: No module named 'dashboard.event_store'`

- [ ] **Step 3: Implement EventStore**

```python
# dashboard/event_store.py
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_event_store.py -v`
Expected: All tests PASS (requires `pip install aiosqlite` first)

- [ ] **Step 5: Commit**

```bash
git add dashboard/event_store.py tests/unit/test_event_store.py
git commit -m "feat: add SQLite EventStore for persistent event storage"
```

---

## Task 3: Add Dependencies and DashboardConfig

**Files:**
- Modify: `pyproject.toml`
- Modify: `config/schemas.py`
- Test: `tests/unit/test_config_loader.py` (verify existing tests still pass)

- [ ] **Step 1: Add dependencies to pyproject.toml**

In `pyproject.toml`, add to the `dependencies` list:

```toml
dependencies = [
    "pyyaml==6.0.2",
    "httpx==0.28.1",
    "python-telegram-bot==21.10",
    "anthropic==0.42.0",
    "starlette==0.45.3",
    "uvicorn==0.34.0",
    "aiosqlite==0.20.0",
]
```

Also add `dashboard*` to the packages list:

```toml
[tool.setuptools.packages.find]
include = ["orchestrator*", "config*", "workspace*", "integrations*", "dashboard*"]
```

- [ ] **Step 2: Install updated dependencies**

Run: `pip install -e ".[dev]"`

- [ ] **Step 3: Add DashboardConfig to schemas.py**

Add after the `IntentParserConfig` class in `config/schemas.py` (after line 72):

```python
@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    db_path: str = "data/events.db"
```

Add the field to `GlobalConfig`:

```python
@dataclass
class GlobalConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    workspaces: WorkspacesConfig = field(default_factory=WorkspacesConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    operator: OperatorProfile = field(default_factory=OperatorProfile)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    intent_parser: IntentParserConfig = field(default_factory=IntentParserConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
```

- [ ] **Step 4: Run existing config tests**

Run: `pytest tests/unit/test_config_loader.py tests/unit/test_config_cascade.py -v`
Expected: All existing tests PASS (new field has defaults, so backward-compatible)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml config/schemas.py
git commit -m "feat: add dashboard dependencies and DashboardConfig schema"
```

---

## Task 4: Web Server and API Endpoints

**Files:**
- Create: `dashboard/web.py`
- Create: `dashboard/static/index.html` (placeholder — real UI in Task 8)
- Test: `tests/unit/test_dashboard_web.py`

- [ ] **Step 1: Write failing tests for API endpoints**

```python
# tests/unit/test_dashboard_web.py
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import Event, EventBus
from dashboard.web import create_app


@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = EventStore(db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def client(bus, store):
    app = create_app(bus, store)
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestEventsEndpoint:
    async def test_get_events_empty(self, client, store):
        resp = client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    async def test_get_events_with_data(self, client, store):
        await store.insert(Event(event_type="test", message="hello"))
        resp = client.get("/api/events")
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["message"] == "hello"

    async def test_get_events_with_limit(self, client, store):
        for i in range(10):
            await store.insert(Event(event_type="test", message=f"e{i}"))
        resp = client.get("/api/events?limit=3")
        assert len(resp.json()["events"]) == 3

    async def test_get_events_filtered_by_project(self, client, store):
        await store.insert(Event(event_type="a", message="p1", project_id="proj1"))
        await store.insert(Event(event_type="b", message="p2", project_id="proj2"))
        resp = client.get("/api/events?project_id=proj1")
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["project_id"] == "proj1"

    async def test_get_events_filtered_by_ticket(self, client, store):
        await store.insert(Event(event_type="a", message="t1", ticket_id="T-1"))
        await store.insert(Event(event_type="b", message="t2", ticket_id="T-2"))
        resp = client.get("/api/events?ticket_id=T-1")
        events = resp.json()["events"]
        assert len(events) == 1


class TestProjectsEndpoint:
    async def test_get_projects(self, client, store):
        await store.insert(Event(event_type="a", message="x", project_id="p1"))
        await store.insert(Event(event_type="b", message="y", project_id="p2"))
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        projects = resp.json()["projects"]
        assert "p1" in projects
        assert "p2" in projects


class TestTicketsEndpoint:
    async def test_get_tickets_for_project(self, client, store):
        await store.insert(Event(
            event_type="a", message="x", project_id="p1", ticket_id="T-1",
        ))
        await store.insert(Event(
            event_type="b", message="y", project_id="p1", ticket_id="T-2",
        ))
        resp = client.get("/api/projects/p1/tickets")
        assert resp.status_code == 200
        tickets = resp.json()["tickets"]
        assert set(tickets) == {"T-1", "T-2"}


class TestTicketEventsEndpoint:
    async def test_get_ticket_events(self, client, store):
        await store.insert(Event(
            event_type="stage_transition",
            message="NEW -> ANALYSIS",
            ticket_id="T-1",
        ))
        await store.insert(Event(
            event_type="agent_completed",
            message="BA done",
            ticket_id="T-1",
            agent_id="ba-agent",
        ))
        resp = client.get("/api/tickets/T-1/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 2


class TestDashboardPage:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_dashboard_web.py -v`
Expected: `ModuleNotFoundError: No module named 'dashboard.web'`

- [ ] **Step 3: Implement web server**

```python
# dashboard/web.py
"""Embedded web server for the Sickle dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from dashboard.event_store import EventStore
from dashboard.events import EventBus


def create_app(bus: EventBus, store: EventStore) -> Starlette:
    """Create the Starlette dashboard application."""

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "events": await store.count()})

    async def get_events(request: Request) -> JSONResponse:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
        project_id = request.query_params.get("project_id")
        ticket_id = request.query_params.get("ticket_id")
        events = await store.query_recent(
            limit=limit, offset=offset,
            project_id=project_id, ticket_id=ticket_id,
        )
        return JSONResponse({"events": events})

    async def get_projects(request: Request) -> JSONResponse:
        projects = await store.get_projects()
        return JSONResponse({"projects": projects})

    async def get_project_tickets(request: Request) -> JSONResponse:
        project_id = request.path_params["project_id"]
        tickets = await store.get_tickets(project_id=project_id)
        return JSONResponse({"tickets": tickets})

    async def get_ticket_events(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        limit = int(request.query_params.get("limit", "200"))
        events = await store.query_recent(ticket_id=ticket_id, limit=limit)
        return JSONResponse({"events": events})

    async def index(request: Request) -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/events", get_events),
        Route("/api/projects", get_projects),
        Route("/api/projects/{project_id}/tickets", get_project_tickets),
        Route("/api/tickets/{ticket_id:path}/events", get_ticket_events),
    ]

    return Starlette(routes=routes)
```

- [ ] **Step 4: Create placeholder index.html**

```html
<!-- dashboard/static/index.html -->
<!DOCTYPE html>
<html><head><title>Sickle Dashboard</title></head>
<body><h1>Sickle Dashboard</h1><p>Loading...</p></body>
</html>
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/test_dashboard_web.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard/web.py dashboard/static/index.html tests/unit/test_dashboard_web.py
git commit -m "feat: add dashboard web server with API endpoints"
```

---

## Task 5: Instrument Orchestrator with Events

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Test: `tests/unit/test_orchestrator_modes.py` (verify existing tests still pass)

The orchestrator accepts an optional `event_bus` and emits events at key points. When `event_bus` is `None`, all emit calls are silently skipped — zero impact on existing behavior and tests.

- [ ] **Step 1: Add event_bus parameter to Orchestrator.__init__**

In `orchestrator/orchestrator.py`, add to `__init__` signature after `dry_run: bool = False`:

```python
    def __init__(
        self,
        global_config: GlobalConfig,
        projects: dict[str, LoadedProject],
        registry: ResourceRegistry,
        workflow: WorkflowDefinition,
        workspace_manager: WorkspaceManager,
        agent_runtime: AgentRuntime,
        tracker: TrackerInterface | None = None,
        vcs: VCSInterface | None = None,
        notifier: NotifierInterface | None = None,
        dry_run: bool = False,
        event_bus: Any | None = None,
    ) -> None:
```

Add to body: `self._events = event_bus`

Add a helper method:

```python
    def _emit(
        self,
        event_type: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        """Emit an event if the event bus is available."""
        if self._events:
            self._events.emit(event_type, message, **kwargs)
```

- [ ] **Step 2: Add emit calls at key orchestrator points**

Add these emit calls at the corresponding locations in `orchestrator.py`:

In `run()`, after "Orchestrator started" log (around line 236):
```python
        self._emit("daemon_started", f"Orchestrator started (mode={self._mode_handler.get_mode() if self._mode_handler else 'auto'}, dry_run={self._dry_run})")
```

In `_poll_and_create_workspaces()`, after successful workspace creation (around line 358):
```python
                    self._emit(
                        "workspace_created",
                        f"Created workspace for {pt.ticket.id}",
                        project_id=project_id,
                        ticket_id=pt.ticket.id,
                        data={"repo_id": pt.repo_id},
                    )
```

In `_handle_agent_stage()`, before agent dispatch (around line 490):
```python
        self._emit(
            "agent_dispatched",
            f"Dispatching {stage_def.agent} for {state.ticket_id}",
            project_id=state.company_id,
            ticket_id=state.ticket_id,
            agent_id=stage_def.agent,
            data={"stage": stage_id},
        )
```

In `_handle_agent_stage()`, after successful agent completion (around line 505):
```python
            self._emit(
                "agent_completed",
                f"{stage_def.agent} completed for {state.ticket_id}",
                project_id=state.company_id,
                ticket_id=state.ticket_id,
                agent_id=stage_def.agent,
                data={
                    "stage": stage_id,
                    "duration": result.duration_seconds,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
            )
```

In `_handle_agent_stage()`, after agent failure (around line 500):
```python
            self._emit(
                "agent_failed",
                f"{stage_def.agent} failed for {state.ticket_id}: {result.error}",
                project_id=state.company_id,
                ticket_id=state.ticket_id,
                agent_id=stage_def.agent,
                data={"stage": stage_id, "error": result.error},
            )
```

In the transition to `AWAITING_APPROVAL` (around line 514):
```python
                self._emit(
                    "approval_requested",
                    f"Awaiting approval for {state.ticket_id} after {current_state}",
                    project_id=state.company_id,
                    ticket_id=state.ticket_id,
                    data={"gate": current_state},
                )
```

In `_advance_to_stage()` (around line 722):
```python
        self._emit(
            "stage_transition",
            f"{workspace.state.ticket_id}: {workspace.state.current_state} -> {state_name}",
            project_id=workspace.state.company_id,
            ticket_id=workspace.state.ticket_id,
            data={"from_state": workspace.state.current_state, "to_state": state_name},
        )
```

In `_handle_escalate()`, after sending Telegram escalation:
```python
        self._emit(
            "escalation_sent",
            f"Escalated {workspace.state.ticket_id} to human",
            project_id=workspace.state.company_id,
            ticket_id=workspace.state.ticket_id,
            data={"reason": workspace.state.human_input_question or "unknown"},
        )
```

In `_action_push_and_open_pr()`, after successful PR creation:
```python
            self._emit(
                "pr_created",
                f"PR created for {workspace.state.ticket_id}: {result.pr_url}",
                project_id=workspace.state.company_id,
                ticket_id=workspace.state.ticket_id,
                data={"pr_url": result.pr_url, "pr_number": result.pr_number},
            )
```

In `poll_cycle()`, at the start — so operator can see when polls happen:
```python
        self._emit("poll_cycle", "Poll cycle started")
```

- [ ] **Step 3: Run existing orchestrator tests**

Run: `pytest tests/unit/test_orchestrator_modes.py tests/unit/test_main.py -v`
Expected: All existing tests PASS (event_bus defaults to None, _emit is a no-op)

- [ ] **Step 4: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "feat: instrument orchestrator with event emissions"
```

---

## Task 6: Instrument Telegram Adapters with Events

**Files:**
- Modify: `integrations/telegram/telegram_adapter.py`
- Modify: `integrations/telegram/command_handler.py`

- [ ] **Step 1: Add event_bus to TelegramAdapter**

In `telegram_adapter.py`, add to `__init__` an optional `event_bus` parameter:

```python
    def __init__(self, bot_token: str, event_bus: Any | None = None) -> None:
        self._bot = Bot(token=bot_token)
        self._app: Application | None = None
        self._command_handler: Any | None = None
        self._reply_futures: dict[int, asyncio.Future] = {}
        self._events = event_bus
```

In `send_message()`, after the existing `logger.info` call:
```python
        if self._events:
            self._events.emit(
                "tg_message_sent",
                f"Sent message to chat {chat_id}: {text[:80]}",
                data={"chat_id": chat_id, "text_preview": text[:200]},
            )
```

In `_handle_incoming()`, at the start (before command handler dispatch):
```python
        if self._events:
            self._events.emit(
                "tg_message_received",
                f"Received from chat {chat_id}: {message.text[:80]}",
                data={"chat_id": chat_id, "text_preview": message.text[:200]},
            )
```

- [ ] **Step 2: Add event_bus to CommandHandler**

In `command_handler.py`, add to `__init__` an optional `event_bus` parameter after `allowed_chat_ids`:

```python
        self._events = event_bus
```

In `handle_message()`, after the `logger.info("Parsed intent: ...")` call (line 64):
```python
        if self._events:
            self._events.emit(
                "intent_parsed",
                f"Intent: {intent.intent} (params={intent.params})",
                data={"intent": intent.intent, "params": intent.params, "raw_text": text[:200]},
            )
```

- [ ] **Step 3: Run existing Telegram tests**

Run: `pytest tests/unit/test_telegram_adapter.py tests/unit/test_command_handler.py -v`
Expected: All existing tests PASS (event_bus defaults to None)

- [ ] **Step 4: Commit**

```bash
git add integrations/telegram/telegram_adapter.py integrations/telegram/command_handler.py
git commit -m "feat: instrument Telegram adapter and command handler with events"
```

---

## Task 7: Instrument Agent Runtime with Events

**Files:**
- Modify: `orchestrator/agent_runtime.py`

- [ ] **Step 1: Add event_bus to AgentRuntime**

In `agent_runtime.py`, add to `__init__` an optional `event_bus` parameter:

```python
    def __init__(
        self,
        registry: ResourceRegistry,
        llm: Any,
        operator_profile: str = "",
        event_bus: Any | None = None,
    ) -> None:
```

Add: `self._events = event_bus`

- [ ] **Step 2: Add emit calls**

In `execute()`, after the `logger.info("Agent '%s' executed: ...")` call:
```python
            if self._events:
                self._events.emit(
                    "agent_execution_detail",
                    f"Agent {agent_id}: model={model}, tokens={result.input_tokens}/{result.output_tokens}, duration={result.duration_seconds:.1f}s",
                    agent_id=agent_id,
                    data={
                        "model": model,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "tool_calls": result.tool_calls,
                        "tool_rounds": result.tool_rounds,
                        "duration": result.duration_seconds,
                    },
                )
```

- [ ] **Step 3: Run existing agent runtime tests**

Run: `pytest tests/unit/test_agent_runtime.py -v`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add orchestrator/agent_runtime.py
git commit -m "feat: instrument agent runtime with events"
```

---

## Task 8: Dashboard HTML/JS

**Files:**
- Replace: `dashboard/static/index.html`

This is a single-file dashboard — HTML, CSS, and JS all inline. No build step, no dependencies.

- [ ] **Step 1: Write the dashboard HTML**

Replace the placeholder `dashboard/static/index.html` with the full dashboard. The file is large, so here's the structure:

```html
<!-- dashboard/static/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sickle Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            background: #0d1117; color: #c9d1d9;
            display: flex; min-height: 100vh;
        }
        /* Sidebar */
        .sidebar {
            width: 240px; background: #161b22; border-right: 1px solid #30363d;
            padding: 16px; flex-shrink: 0; overflow-y: auto;
        }
        .sidebar h2 { font-size: 14px; color: #8b949e; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
        .sidebar a {
            display: block; padding: 8px 12px; margin-bottom: 4px; border-radius: 6px;
            color: #c9d1d9; text-decoration: none; font-size: 14px;
        }
        .sidebar a:hover, .sidebar a.active { background: #21262d; color: #58a6ff; }
        .logo { font-size: 20px; font-weight: bold; color: #58a6ff; margin-bottom: 24px; padding: 8px 0; }
        /* Main */
        .main { flex: 1; padding: 24px; overflow-y: auto; }
        .main h1 { font-size: 20px; margin-bottom: 16px; }
        /* Event log */
        .event-list { list-style: none; }
        .event-item {
            padding: 10px 14px; border-bottom: 1px solid #21262d;
            font-size: 13px; line-height: 1.5;
        }
        .event-item:hover { background: #161b22; }
        .event-time { color: #8b949e; font-size: 12px; margin-right: 8px; }
        .event-type {
            display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: 11px; font-weight: 600; margin-right: 8px;
        }
        .event-type.agent_dispatched, .event-type.agent_completed { background: #1f3a2d; color: #3fb950; }
        .event-type.agent_failed, .event-type.escalation_sent { background: #3d1f20; color: #f85149; }
        .event-type.stage_transition { background: #1f2a3d; color: #58a6ff; }
        .event-type.tg_message_received, .event-type.tg_message_sent { background: #2d2a1f; color: #d29922; }
        .event-type.approval_requested { background: #2d1f3d; color: #bc8cff; }
        .event-type.workspace_created, .event-type.pr_created { background: #1f3a2d; color: #3fb950; }
        .event-type.poll_cycle, .event-type.daemon_started { background: #21262d; color: #8b949e; }
        .event-type.intent_parsed { background: #2a2d1f; color: #d2a822; }
        .event-msg { color: #c9d1d9; }
        .event-meta { color: #8b949e; font-size: 12px; margin-top: 4px; }
        /* Project cards */
        .project-card {
            display: inline-block; padding: 16px 20px; margin: 8px;
            background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            cursor: pointer; min-width: 200px;
        }
        .project-card:hover { border-color: #58a6ff; }
        .project-card h3 { font-size: 16px; color: #58a6ff; }
        .project-card .count { font-size: 13px; color: #8b949e; margin-top: 4px; }
        /* Ticket list */
        .ticket-item {
            padding: 10px 14px; border-bottom: 1px solid #21262d;
            cursor: pointer; font-size: 14px;
        }
        .ticket-item:hover { background: #161b22; color: #58a6ff; }
        /* Controls */
        .controls { margin-bottom: 16px; display: flex; gap: 8px; align-items: center; }
        .controls select, .controls input {
            background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
            padding: 6px 10px; border-radius: 4px; font-size: 13px;
        }
        .btn {
            background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
            padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 13px;
        }
        .btn:hover { background: #30363d; }
        .auto-refresh { font-size: 12px; color: #8b949e; }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="logo">Sickle</div>
        <h2>Navigation</h2>
        <a href="#" onclick="showGlobalLog()" class="active" id="nav-global">Global Log</a>
        <h2 style="margin-top: 20px;">Projects</h2>
        <div id="project-nav"></div>
    </div>
    <div class="main" id="main-content">
        <h1>Global Event Log</h1>
        <div class="controls">
            <select id="type-filter" onchange="refreshEvents()">
                <option value="">All event types</option>
                <option value="agent_dispatched">agent_dispatched</option>
                <option value="agent_completed">agent_completed</option>
                <option value="agent_failed">agent_failed</option>
                <option value="stage_transition">stage_transition</option>
                <option value="tg_message_received">tg_message_received</option>
                <option value="tg_message_sent">tg_message_sent</option>
                <option value="intent_parsed">intent_parsed</option>
                <option value="workspace_created">workspace_created</option>
                <option value="pr_created">pr_created</option>
                <option value="approval_requested">approval_requested</option>
                <option value="escalation_sent">escalation_sent</option>
                <option value="poll_cycle">poll_cycle</option>
                <option value="daemon_started">daemon_started</option>
            </select>
            <button class="btn" onclick="refreshEvents()">Refresh</button>
            <label class="auto-refresh">
                <input type="checkbox" id="auto-refresh" checked onchange="toggleAutoRefresh()"> Auto-refresh (5s)
            </label>
        </div>
        <ul class="event-list" id="event-list"></ul>
    </div>

    <script>
        let autoRefreshTimer = null;
        let currentView = 'global'; // 'global', 'project', 'ticket'
        let currentProjectId = null;
        let currentTicketId = null;

        function formatTime(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleString('en-GB', {
                day: '2-digit', month: '2-digit', hour: '2-digit',
                minute: '2-digit', second: '2-digit'
            });
        }

        function renderEvent(e) {
            const meta = [];
            if (e.project_id) meta.push(`project: ${e.project_id}`);
            if (e.ticket_id) meta.push(`ticket: ${e.ticket_id}`);
            if (e.agent_id) meta.push(`agent: ${e.agent_id}`);
            if (e.data && e.data.duration) meta.push(`duration: ${e.data.duration.toFixed(1)}s`);
            if (e.data && e.data.input_tokens) meta.push(`tokens: ${e.data.input_tokens}/${e.data.output_tokens}`);

            return `<li class="event-item">
                <span class="event-time">${formatTime(e.timestamp)}</span>
                <span class="event-type ${e.event_type}">${e.event_type}</span>
                <span class="event-msg">${escapeHtml(e.message)}</span>
                ${meta.length ? `<div class="event-meta">${meta.join(' | ')}</div>` : ''}
            </li>`;
        }

        function escapeHtml(s) {
            const div = document.createElement('div');
            div.textContent = s;
            return div.innerHTML;
        }

        async function loadProjects() {
            try {
                const resp = await fetch('/api/projects');
                const data = await resp.json();
                const nav = document.getElementById('project-nav');
                nav.innerHTML = data.projects.map(p =>
                    `<a href="#" onclick="showProject('${p}')" id="nav-${p}">${p}</a>`
                ).join('');
            } catch (e) {
                console.error('Failed to load projects:', e);
            }
        }

        async function refreshEvents() {
            let url = '/api/events?limit=200';
            if (currentView === 'project' && currentProjectId) {
                url += `&project_id=${encodeURIComponent(currentProjectId)}`;
            }
            if (currentView === 'ticket' && currentTicketId) {
                url = `/api/tickets/${encodeURIComponent(currentTicketId)}/events?limit=200`;
            }
            const typeFilter = document.getElementById('type-filter');
            // Type filter is client-side
            try {
                const resp = await fetch(url);
                const data = await resp.json();
                let events = data.events;
                if (typeFilter && typeFilter.value) {
                    events = events.filter(e => e.event_type === typeFilter.value);
                }
                document.getElementById('event-list').innerHTML =
                    events.length ? events.map(renderEvent).join('') : '<li class="event-item" style="color:#8b949e">No events yet</li>';
            } catch (e) {
                console.error('Failed to load events:', e);
            }
        }

        function showGlobalLog() {
            currentView = 'global';
            currentProjectId = null;
            currentTicketId = null;
            document.getElementById('main-content').querySelector('h1').textContent = 'Global Event Log';
            setActiveNav('nav-global');
            refreshEvents();
        }

        async function showProject(projectId) {
            currentView = 'project';
            currentProjectId = projectId;
            currentTicketId = null;
            document.getElementById('main-content').querySelector('h1').textContent = `Project: ${projectId}`;
            setActiveNav(`nav-${projectId}`);

            // Load tickets for this project
            try {
                const resp = await fetch(`/api/projects/${encodeURIComponent(projectId)}/tickets`);
                const data = await resp.json();
                if (data.tickets.length > 0) {
                    const ticketHtml = '<div style="margin-bottom:16px">' +
                        data.tickets.map(t =>
                            `<span class="ticket-item" style="display:inline-block;cursor:pointer;padding:4px 12px;margin:4px;background:#161b22;border:1px solid #30363d;border-radius:4px;" onclick="showTicket('${t}')">${t}</span>`
                        ).join('') + '</div>';
                    document.getElementById('event-list').insertAdjacentHTML('beforebegin', ticketHtml);
                }
            } catch (e) {}
            refreshEvents();
        }

        async function showTicket(ticketId) {
            currentView = 'ticket';
            currentTicketId = ticketId;
            document.getElementById('main-content').querySelector('h1').textContent = `Ticket: ${ticketId}`;
            refreshEvents();
        }

        function setActiveNav(id) {
            document.querySelectorAll('.sidebar a').forEach(a => a.classList.remove('active'));
            const el = document.getElementById(id);
            if (el) el.classList.add('active');
        }

        function toggleAutoRefresh() {
            if (document.getElementById('auto-refresh').checked) {
                autoRefreshTimer = setInterval(() => { refreshEvents(); loadProjects(); }, 5000);
            } else {
                clearInterval(autoRefreshTimer);
                autoRefreshTimer = null;
            }
        }

        // Init
        loadProjects();
        refreshEvents();
        toggleAutoRefresh();
    </script>
</body>
</html>
```

- [ ] **Step 2: Manually test by viewing in browser**

This will be testable once the web server is wired up in Task 9. For now, verify the file syntax is valid HTML.

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/index.html
git commit -m "feat: add dashboard single-page HTML/JS interface"
```

---

## Task 9: Wire Everything in main.py

**Files:**
- Modify: `main.py`

This is the final integration task — create the EventBus, EventStore, start the web server, and pass `event_bus` to all components.

- [ ] **Step 1: Add event system initialization to main.py**

In `main()`, after logging configuration (after line 143), add:

```python
    # Initialize event system
    from dashboard.events import EventBus
    from dashboard.event_store import EventStore

    event_bus = EventBus()
    db_path = global_config.dashboard.db_path
    if not Path(db_path).is_absolute():
        db_path = str(Path(__file__).parent / db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    event_store = EventStore(db_path)
```

- [ ] **Step 2: Pass event_bus to components**

Update the component initialization calls:

```python
    # Agent runtime — add event_bus
    agent_runtime = AgentRuntime(registry, llm, operator_profile=operator_profile, event_bus=event_bus)
```

```python
    # Telegram adapter — add event_bus
    notifier = TelegramAdapter(bot_token=tg_config.bot_token, event_bus=event_bus)
```

```python
    # Command handler — add event_bus
    command_handler = CommandHandler(
        ...,  # existing params unchanged
        event_bus=event_bus,
    )
```

```python
    # Orchestrator — add event_bus
    orchestrator = Orchestrator(
        ...,  # existing params unchanged
        event_bus=event_bus,
    )
```

- [ ] **Step 3: Wire EventStore as listener and start web server**

In `_run_all()`, replace the function with:

```python
    async def _run_all() -> None:
        from integrations.telegram.telegram_adapter import TelegramAdapter

        # Initialize persistent event store
        await event_store.initialize()
        event_bus.add_listener(lambda e: asyncio.ensure_future(event_store.insert(e)))

        # Start dashboard web server
        dash_config = global_config.dashboard
        web_server = None
        if dash_config.enabled:
            from dashboard.web import create_app
            import uvicorn

            app = create_app(event_bus, event_store)
            config = uvicorn.Config(
                app, host=dash_config.host, port=dash_config.port,
                log_level="warning",
            )
            web_server = uvicorn.Server(config)
            asyncio.create_task(web_server.serve())
            print(f"  Dashboard: http://{dash_config.host}:{dash_config.port}")

        event_bus.emit("daemon_started", f"Sickle v{version} started")

        tg_active = isinstance(notifier, TelegramAdapter)
        if tg_active:
            await notifier.start_polling()
            print("  Telegram polling started")
        try:
            await orchestrator.run()
        finally:
            if tg_active:
                try:
                    await notifier.stop_polling()
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Error stopping Telegram polling: %s", e,
                    )
            if web_server:
                web_server.should_exit = True
            await event_store.close()
    ```

- [ ] **Step 4: Run the full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS (event_bus is optional everywhere, defaults to None)

- [ ] **Step 5: Manual smoke test**

Run: `source .env && .venv/bin/python main.py --config config-live --dry-run`

Verify:
- Console shows `Dashboard: http://0.0.0.0:8080`
- Browser at `http://localhost:8080` shows the dashboard
- `http://localhost:8080/api/health` returns `{"status": "ok", "events": N}`
- Global log shows `daemon_started` event
- Send a Telegram message — events appear in the dashboard

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: wire dashboard event system into main daemon loop"
```

---

## Task 10: Update Feature Docs

**Files:**
- Create: `docs/features/dashboard.md`
- Modify: `docs/features/index.md`

Per the CONTRIBUTING.md pre-commit hook, any code change to tracked directories needs a feature doc update.

- [ ] **Step 1: Create feature doc**

```markdown
# Feature: Dashboard & Event Log

**Status:** Implemented
**Created:** 2026-04-11
**Updated:** 2026-04-11
**Author:** Oleksandr Brazhenko

## Description

Local web dashboard providing real-time visibility into the Sickle pipeline. Shows per-project ticket history, agent activity, state transitions, Telegram messages, and a global event log. Runs as an embedded web server in the daemon process.

## Requirements

- FR1: Structured event log capturing all pipeline activity
- FR2: SQLite persistence for event history
- FR3: Web dashboard with project list, ticket drill-down, global event log
- FR4: Auto-refreshing UI with event type filtering
- FR5: Configurable via global.yaml (host, port, db path, enable/disable)

## Technical Approach

- EventBus (in-memory) with listener pattern for async SQLite persistence
- Starlette embedded web server sharing the daemon's asyncio loop
- Single HTML file with inline CSS/JS, no build step
- REST API: /api/events, /api/projects, /api/projects/{id}/tickets, /api/tickets/{id}/events

## Dependencies

- starlette, uvicorn, aiosqlite (added to pyproject.toml)

## Acceptance Criteria

- [ ] Events emitted from orchestrator, agent runtime, Telegram adapters
- [ ] Events persisted to SQLite
- [ ] Dashboard accessible at configured host:port
- [ ] Project list shows all active projects
- [ ] Clicking a project shows its tickets and events
- [ ] Clicking a ticket shows its full event timeline
- [ ] Auto-refresh updates the view every 5 seconds
- [ ] Event type filter works
- [ ] All existing tests still pass
- [ ] Dashboard config in global.yaml

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-11 | Initial implementation |
```

- [ ] **Step 2: Update feature index**

Add a row to `docs/features/index.md` for the dashboard feature.

- [ ] **Step 3: Commit**

```bash
git add docs/features/dashboard.md docs/features/index.md
git commit -m "docs: add dashboard feature documentation"
```

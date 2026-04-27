from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import Event, EventBus
from dashboard.web import create_app, _scan_all_workspaces


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


class TestTitleBackfill:
    def _write_state(self, root: Path, **fields):
        root.mkdir(parents=True, exist_ok=True)
        defaults = {
            "ticket_id": "T-1",
            "company_id": "acme",
            "repo_id": "acme-app",
            "current_state": "ANALYSIS",
            "started_at": "2026-04-27T00:00:00+00:00",
            "last_updated_at": "2026-04-27T00:00:00+00:00",
        }
        defaults.update(fields)
        (root / "state.json").write_text(json.dumps(defaults), encoding="utf-8")

    def test_backfills_from_ticket_md(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-1"
        self._write_state(ws)
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text(
            "# T-1: Login screen flickers on cold start\n\n## Description\n",
            encoding="utf-8",
        )

        results = _scan_all_workspaces(str(tmp_path))

        assert results[0]["title"] == "Login screen flickers on cold start"
        # Disk was updated
        on_disk = json.loads((ws / "state.json").read_text())
        assert on_disk["title"] == "Login screen flickers on cold start"

    def test_backfill_handles_missing_id_prefix(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-2"
        self._write_state(ws, ticket_id="T-2")
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text("# Just a plain title\n", encoding="utf-8")

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Just a plain title"

    def test_backfill_setup_workspace(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "setup"
        self._write_state(ws, ticket_id="setup")
        # No meta/ticket.md

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Workspace setup"
        on_disk = json.loads((ws / "state.json").read_text())
        assert on_disk["title"] == "Workspace setup"

    def test_backfill_skipped_when_title_present(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-3"
        self._write_state(ws, ticket_id="T-3", title="Already set")
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text("# T-3: Different title\n", encoding="utf-8")

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Already set"

    def test_backfill_falls_back_to_ticket_id(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-4"
        self._write_state(ws, ticket_id="T-4")
        # No meta/ticket.md and not a setup dir

        results = _scan_all_workspaces(str(tmp_path))
        # API exposes ticket_id as a soft fallback
        assert results[0]["title"] == "T-4"
        # But state.json on disk is NOT polluted with ticket_id-as-title
        on_disk = json.loads((ws / "state.json").read_text())
        assert "title" not in on_disk or on_disk["title"] == ""

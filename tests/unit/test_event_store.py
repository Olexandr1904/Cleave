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

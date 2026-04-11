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

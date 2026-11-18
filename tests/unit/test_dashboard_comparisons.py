# tests/unit/test_dashboard_comparisons.py
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app


class FakeBoard:
    def overview(self):
        return {"rows": [], "tally": {"skill": 0, "staged": 0, "neither": 0, "undecided": 0}}


@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "e.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def client(store):
    return TestClient(create_app(EventBus(), store, comparisons=FakeBoard()))


def test_route(client):
    response = client.get("/api/comparisons")
    assert response.status_code == 200 and response.json()["tally"]["undecided"] == 0


def test_no_route_without_a_board(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/comparisons").status_code == 404

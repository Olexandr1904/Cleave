from __future__ import annotations
import pytest
from starlette.testclient import TestClient
from config.schemas import LoadedProject, RepoConfig, ReviewerConfig
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from orchestrator.reviewer.db import connect
from orchestrator.switches import Switches

@pytest.fixture
async def client(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    await store.initialize()
    projects = {"acme": LoadedProject(repos={"app": RepoConfig(reviewer=ReviewerConfig(enabled=True)),
                                             "web": RepoConfig()})}
    sw = Switches(connect(tmp_path / "r.db"))
    yield TestClient(create_app(EventBus(), store, projects=projects, switches=sw)), sw
    await store.close()

def test_get_snapshot(client):
    c, _ = client
    body = c.get("/api/switches").json()
    assert body["tickets"]["global"] == "paused"
    assert [r["scope"] for r in body["tickets"]["repos"]] == ["acme/app", "acme/web"]
    assert [r["scope"] for r in body["reviews"]["repos"]] == ["acme/app"]

def test_put_sets_and_returns_snapshot(client):
    c, sw = client
    r = c.put("/api/switches", json={"mode": "tickets", "state": "running"})
    assert r.status_code == 200 and r.json()["tickets"]["global"] == "running"
    r = c.put("/api/switches", json={"mode": "reviews", "state": "paused", "scope": "acme/app"})
    assert r.status_code == 200 and sw.get("reviews", "acme/app") == "paused"

@pytest.mark.parametrize("payload", [
    {"mode": "deploys", "state": "running"},
    {"mode": "tickets", "state": "stopped"},
    {"mode": "tickets", "state": "paused", "scope": "acme/nope"},
    {"mode": "reviews", "state": "paused", "scope": "acme/web"},
])
def test_put_rejects_bad_input(client, payload):
    c, sw = client
    r = c.put("/api/switches", json=payload)
    assert r.status_code == 400 and r.json()["error"]
    assert sw.get("reviews", "acme/web") == "running"

"""Tests for /api/settings/model routes."""

from __future__ import annotations

import aiosqlite
import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.settings_store import ALLOWED_MODELS, DEFAULT_MODEL, init_settings
from dashboard.web import create_app


@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "events.db")
    s = EventStore(db_path)
    await s.initialize()
    async with aiosqlite.connect(db_path) as conn:
        await init_settings(conn)
    yield s
    await s.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def client(bus, store):
    app = create_app(bus, store)
    return TestClient(app)


class TestGetModel:
    async def test_returns_default_and_options(self, client):
        r = client.get("/api/settings/model")
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == DEFAULT_MODEL
        assert body["options"] == list(ALLOWED_MODELS)


class TestPutModel:
    async def test_persists_valid_model(self, client):
        r = client.put(
            "/api/settings/model",
            json={"model": "claude-opus-4-7"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "claude-opus-4-7"

        r2 = client.get("/api/settings/model")
        assert r2.json()["model"] == "claude-opus-4-7"

    async def test_rejects_unknown_model(self, client):
        r = client.put(
            "/api/settings/model",
            json={"model": "claude-sonnet-3.5"},
        )
        assert r.status_code == 400
        r2 = client.get("/api/settings/model")
        assert r2.json()["model"] == DEFAULT_MODEL

    async def test_rejects_missing_model_key(self, client):
        r = client.put("/api/settings/model", json={})
        assert r.status_code == 400

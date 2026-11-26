"""Tests for /api/settings/model routes."""

from __future__ import annotations

import aiosqlite
import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.settings_store import ALLOWED_MODELS, DEFAULT_MODEL, init_settings
from dashboard.web import create_app
from integrations.telegram.quiet_hours import QuietHours


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
            json={"model": "claude-opus-5-5"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "claude-opus-5-5"

        r2 = client.get("/api/settings/model")
        assert r2.json()["model"] == "claude-opus-5-5"

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


_DEFAULT_QUIET = {"enabled": True, "start": "22:00", "end": "09:00"}


@pytest.fixture
def live_quiet():
    return QuietHours()


@pytest.fixture
def quiet_client(bus, store, live_quiet):
    return TestClient(create_app(bus, store, quiet_hours=live_quiet))


class TestQuietHours:
    async def test_get_quiet_hours_default(self, quiet_client):
        r = quiet_client.get("/api/settings/quiet-hours")
        assert r.status_code == 200
        assert r.json() == _DEFAULT_QUIET

    async def test_put_quiet_hours_persists_and_applies(self, quiet_client, live_quiet):
        saved = {"enabled": True, "start": "23:00", "end": "07:30"}
        r = quiet_client.put("/api/settings/quiet-hours", json=saved)
        assert r.status_code == 200
        assert r.json() == saved
        assert quiet_client.get("/api/settings/quiet-hours").json() == saved
        assert live_quiet == QuietHours(enabled=True, start="23:00", end="07:30")

    @pytest.mark.parametrize("body", [
        {"enabled": True, "start": "22:00", "end": "22:00"},
        {"enabled": True, "start": "25:00", "end": "09:00"},
        {"enabled": "yes", "start": "22:00", "end": "09:00"},
        {"start": "22:00", "end": "09:00"},
    ])
    async def test_put_quiet_hours_refuses_bad_input(self, quiet_client, live_quiet, body):
        r = quiet_client.put("/api/settings/quiet-hours", json=body)
        assert r.status_code == 400
        assert r.json()["error"]
        assert quiet_client.get("/api/settings/quiet-hours").json() == _DEFAULT_QUIET
        assert live_quiet == QuietHours()

    async def test_put_quiet_hours_refuses_invalid_json(self, quiet_client):
        r = quiet_client.put("/api/settings/quiet-hours", content=b"{",
                             headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    async def test_put_without_a_live_object_still_stores(self, client):
        saved = {"enabled": False, "start": "22:00", "end": "09:00"}
        assert client.put("/api/settings/quiet-hours", json=saved).status_code == 200
        assert client.get("/api/settings/quiet-hours").json() == saved

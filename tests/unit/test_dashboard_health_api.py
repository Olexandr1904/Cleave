from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from starlette.testclient import TestClient
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from health.credentials import CredentialStatus

T = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "test.db"))
    await s.initialize()
    yield s
    await s.close()

class FakeHealth:
    def __init__(self):
        self.check_all = AsyncMock()
    def snapshot(self):
        return [CredentialStatus("forge:acme/app", "forge", "github", "GitHub acme-org/acme-app", "acme-org/acme-app",
                                 "GH_T", "vcs.github.token", state="invalid", http_status=401, since=T, checked_at=T)]

def test_credentials_endpoint(store):
    fake = FakeHealth()
    c = TestClient(create_app(EventBus(), store, credential_health=fake))
    body = c.get("/api/health/credentials").json()
    assert body["credentials"][0]["state"] == "invalid" and body["credentials"][0]["env_var"] == "GH_T"
    assert body["checked_at"] == T.isoformat()
    fake.check_all.assert_not_awaited()
    c.get("/api/health/credentials?refresh=1")
    fake.check_all.assert_awaited_once()

def test_credentials_endpoint_without_service(store):
    body = TestClient(create_app(EventBus(), store)).get("/api/health/credentials").json()
    assert body == {"credentials": [], "checked_at": None}

def test_old_projects_health_route_is_gone(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/projects/health").status_code == 404

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from config.schemas import DashboardConfig
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app

ALLOWED = ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"]


@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "test.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def action_client(store):
    orch = MagicMock()
    orch.get_active_workspaces = MagicMock(return_value=[])
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.get_running = MagicMock(return_value=None)
    orch._agent_runtime.cancel = AsyncMock(return_value=False)
    mh = MagicMock()
    mh.get_mode = MagicMock(return_value="manual")
    return TestClient(create_app(EventBus(), store, orchestrator=orch, mode_handler=mh))


@pytest.mark.parametrize("peer,code", [("192.168.31.20", 403), ("100.101.1.2", 200), ("fd7a:115c:a1e0::1", 200),
                                       ("127.0.0.1", 200)])
def test_only_the_host_and_tailscale_get_in(store, peer, code):
    c = TestClient(create_app(EventBus(), store, allowed_networks=ALLOWED), client=(peer, 5000))
    assert c.get("/api/health").status_code == code


def test_no_allow_list_admits_any_peer(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/health").status_code == 200


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
@pytest.mark.parametrize("method,path", [("post", "/api/workspaces/ACME-1/approve"), ("post", "/api/daemon/mode"),
                                         ("put", "/api/settings/model"), ("post", "/api/workspaces/ACME-1/delete")])
def test_a_cross_site_write_is_refused_everywhere(action_client, method, path, headers):
    assert getattr(action_client, method)(path, json={}, headers=headers).status_code == 403


def test_a_same_origin_write_passes_the_guard(action_client):
    r = action_client.post("/api/workspaces/ACME-1/approve", json={}, headers={"Origin": "http://testserver"})
    assert r.status_code != 403


def test_default_allow_list():
    assert DashboardConfig().allowed_networks == ALLOWED

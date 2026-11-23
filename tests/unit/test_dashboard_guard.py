from __future__ import annotations

import socket
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
    c = TestClient(create_app(EventBus(), store, allowed_networks=ALLOWED), client=(peer, 5000),
                   base_url="http://localhost")
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


def _ws_client(peer):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import WebSocketRoute

    from dashboard.web_guard import GuardMiddleware

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)],
                    middleware=[Middleware(GuardMiddleware, allowed_networks=ALLOWED)])
    return TestClient(app, client=(peer, 5000))


def test_a_websocket_from_outside_the_allow_list_is_refused():
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client("192.168.31.20").websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008


def test_a_websocket_from_an_allowed_peer_connects():
    with _ws_client("100.101.1.2").websocket_connect("ws://localhost/ws") as ws:
        assert ws.receive_text() == "hi"


# --- The Host header: a DNS rebinding page reaches the dashboard from the operator's own browser ---

REBIND = "rebind.attacker.example:1024"


def _guarded(store, allowed_hosts=()):
    orch = MagicMock()
    orch.get_active_workspaces = MagicMock(return_value=[])
    mh = MagicMock()
    mh.get_mode = MagicMock(return_value="manual")
    app = create_app(EventBus(), store, orchestrator=orch, mode_handler=mh, allowed_networks=ALLOWED,
                     allowed_hosts=list(allowed_hosts))
    return TestClient(app, client=("127.0.0.1", 5000))


@pytest.mark.parametrize("host", [REBIND, "attacker.example", "localhost.attacker.example", "ts.net.attacker.example",
                                  "octo-box.attacker.example", "::1"])
def test_a_rebinding_host_is_refused_from_the_host_itself(store, monkeypatch, host):
    """Host and Origin both name the attacker's site, whose name now points at 127.0.0.1: same-origin, from an
    allowed peer, and refused all the same."""
    monkeypatch.setattr(socket, "gethostname", lambda: "octo-box.local")
    c = _guarded(store)
    headers = {"Host": host, "Origin": f"http://{host}"}
    assert c.get("/api/health", headers={"Host": host}).status_code == 403
    assert c.post("/api/daemon/mode", json={"mode": "auto"}, headers=headers).status_code == 403


@pytest.mark.parametrize("host", ["localhost", "LocalHost:8080", "127.0.0.1:1024", "100.101.1.2", "[::1]",
                                  "[::1]:1024", "octo-box.tail1234.ts.net", "OCTO-BOX.tail1234.TS.NET:1024",
                                  "octo-box", "Octo-Box.local:1024", "dash.example.lan", "DASH.example.lan:1024"])
def test_the_dashboards_own_names_get_in(store, monkeypatch, host):
    """An IP literal, localhost, a Tailscale name, the machine's hostname in full or short, an allowed_hosts name; any
    case, any port."""
    monkeypatch.setattr(socket, "gethostname", lambda: "Octo-Box.local")
    c = _guarded(store, allowed_hosts=["Dash.example.lan"])
    assert c.get("/api/health", headers={"Host": host}).status_code == 200
    r = c.post("/api/daemon/mode", json={"mode": "auto"}, headers={"Host": host, "Origin": f"http://{host}"})
    assert r.status_code == 200


def test_without_an_allow_list_any_host_gets_in(store):
    c = TestClient(create_app(EventBus(), store), client=("127.0.0.1", 5000))
    assert c.get("/api/health", headers={"Host": REBIND}).status_code == 200


async def test_a_request_without_a_host_is_refused():
    from dashboard.web_guard import GuardMiddleware

    async def app(scope, receive, send):
        raise AssertionError("the app is never reached")

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    guard = GuardMiddleware(app, allowed_networks=ALLOWED)
    await guard({"type": "http", "method": "GET", "path": "/api/health", "headers": [], "query_string": b"",
                 "client": ("127.0.0.1", 5000)}, receive, send)
    assert sent[0]["status"] == 403


def test_a_websocket_with_a_foreign_host_is_refused():
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client("127.0.0.1").websocket_connect(f"ws://{REBIND}/ws"):
            pass
    assert exc.value.code == 1008

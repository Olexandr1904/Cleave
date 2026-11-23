"""Access guard for the dashboard: peer allow list, Host check and cross-site write refusal."""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def foreign_origin(request: Request) -> bool:
    """True when the request's Sec-Fetch-Site or Origin header says another site sent it.

    curl and the tests send neither header and pass.
    """
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    return origin is not None and urlsplit(origin).netloc != request.headers.get("host")


def _host_name(host: str) -> str:
    """The name a Host header gives, lowercased and without its port: "[::1]:1024" is "::1"."""
    host = host.lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.partition(":")[0]


def _ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


class GuardMiddleware:
    """Refuses peers outside the allowed networks, a Host that is not one of the dashboard's names (DNS rebinding: a
    page of another site whose name now points here) and cross-site writes. allowed_networks None means any peer and
    any Host."""

    def __init__(self, app: ASGIApp, allowed_networks: list[str] | None = None,
                 allowed_hosts: tuple[str, ...] | list[str] = ()) -> None:
        self.app = app
        self._networks = (
            None if allowed_networks is None else [ipaddress.ip_network(n) for n in allowed_networks]
        )
        hostname = socket.gethostname().lower()
        # Besides an IP literal and a Tailscale name (*.ts.net): names no rebinding page can take.
        self._hosts = {"localhost", hostname, hostname.partition(".")[0], *(h.lower() for h in allowed_hosts)}

    def _host_allowed(self, scope: Scope) -> bool:
        if self._networks is None:
            return True
        host = Headers(scope=scope).get("host")
        name = _host_name(host) if host else ""
        if name and (name in self._hosts or name.endswith(".ts.net") or _ip_literal(name)):
            return True
        logger.warning("Dashboard refused Host %r: not a name of this dashboard", host)
        return False

    def _peer_allowed(self, scope: Scope) -> bool:
        if self._networks is None:
            return True
        client = scope.get("client")
        try:
            peer = ipaddress.ip_address(client[0])
        except (TypeError, ValueError):
            logger.warning("Dashboard refused a peer that is not an IP address: %r", client)
            return False
        if any(peer in net for net in self._networks if peer.version == net.version):
            return True
        logger.warning("Dashboard refused peer %s: not in the allowed networks", peer)
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if not self._peer_allowed(scope) or not self._host_allowed(scope):
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
                return
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return
        if scope["type"] == "http" and scope["method"] in _WRITE_METHODS and foreign_origin(Request(scope)):
            await JSONResponse({"error": "cross-site request refused"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)

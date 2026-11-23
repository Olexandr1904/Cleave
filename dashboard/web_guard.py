"""Access guard for the dashboard: peer allow list and cross-site write refusal."""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

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


class GuardMiddleware:
    """Refuses peers outside the allowed networks and cross-site writes. None means any peer."""

    def __init__(self, app: ASGIApp, allowed_networks: list[str] | None = None) -> None:
        self.app = app
        self._networks = (
            None if allowed_networks is None else [ipaddress.ip_network(n) for n in allowed_networks]
        )

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
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self._peer_allowed(scope):
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return
        if scope["method"] in _WRITE_METHODS and foreign_origin(Request(scope)):
            await JSONResponse({"error": "cross-site request refused"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)

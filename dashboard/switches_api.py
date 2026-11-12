"""Dashboard routes for the running/paused switches."""
from __future__ import annotations

from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from orchestrator.switches import GLOBAL, MODES, STATES, Switches, switch_scopes


def build_switch_routes(switches: Switches, get_projects: Callable[[], dict]) -> list[Route]:
    async def get_switches(request: Request) -> JSONResponse:
        return JSONResponse(switches.snapshot(switch_scopes(get_projects())))

    async def put_switches(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        mode = body.get("mode")
        state = body.get("state")
        scope = body.get("scope", GLOBAL)
        scopes = switch_scopes(get_projects())
        if mode not in MODES:
            return JSONResponse({"error": f"unknown mode: {mode!r}"}, status_code=400)
        if state not in STATES:
            return JSONResponse({"error": f"unknown state: {state!r}"}, status_code=400)
        if scope != GLOBAL and scope not in scopes[mode]:
            return JSONResponse({"error": f"unknown scope for {mode}: {scope!r}"}, status_code=400)
        switches.set(mode, state, scope, by="dashboard")
        return JSONResponse(switches.snapshot(scopes))

    return [
        Route("/api/switches", get_switches, methods=["GET"]),
        Route("/api/switches", put_switches, methods=["PUT"]),
    ]

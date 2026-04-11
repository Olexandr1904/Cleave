"""Embedded web server for the Sickle dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from dashboard.event_store import EventStore
from dashboard.events import EventBus


def create_app(bus: EventBus, store: EventStore) -> Starlette:
    """Create the Starlette dashboard application."""

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "events": await store.count()})

    async def get_events(request: Request) -> JSONResponse:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
        project_id = request.query_params.get("project_id")
        ticket_id = request.query_params.get("ticket_id")
        events = await store.query_recent(
            limit=limit, offset=offset,
            project_id=project_id, ticket_id=ticket_id,
        )
        return JSONResponse({"events": events})

    async def get_projects(request: Request) -> JSONResponse:
        projects = await store.get_projects()
        return JSONResponse({"projects": projects})

    async def get_project_tickets(request: Request) -> JSONResponse:
        project_id = request.path_params["project_id"]
        tickets = await store.get_tickets(project_id=project_id)
        return JSONResponse({"tickets": tickets})

    async def get_ticket_events(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        limit = int(request.query_params.get("limit", "200"))
        events = await store.query_recent(ticket_id=ticket_id, limit=limit)
        return JSONResponse({"events": events})

    async def index(request: Request) -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/events", get_events),
        Route("/api/projects", get_projects),
        Route("/api/projects/{project_id}/tickets", get_project_tickets),
        Route("/api/tickets/{ticket_id:path}/events", get_ticket_events),
    ]

    return Starlette(routes=routes)

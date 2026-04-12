"""Embedded web server for the Sickle dashboard."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from dashboard.event_store import EventStore
from dashboard.events import EventBus

logger = logging.getLogger(__name__)


def _scan_all_workspaces(base_dir: str) -> list[dict[str, Any]]:
    """Scan workspace base_dir for ALL workspaces (including terminal states)."""
    base = Path(base_dir)
    if not base.exists():
        return []
    results = []
    for state_file in base.rglob("state.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            ws_root = state_file.parent
            # List available reports
            reports_dir = ws_root / "reports"
            reports = sorted(f.name for f in reports_dir.iterdir() if f.is_file()) if reports_dir.exists() else []
            # List meta files
            meta_dir = ws_root / "meta"
            meta = sorted(f.name for f in meta_dir.iterdir() if f.is_file()) if meta_dir.exists() else []
            results.append({
                "ticket_id": data.get("ticket_id", ""),
                "company_id": data.get("company_id", ""),
                "repo_id": data.get("repo_id", ""),
                "current_state": data.get("current_state", "UNKNOWN"),
                "previous_state": data.get("previous_state"),
                "branch": data.get("branch"),
                "pr_url": data.get("pr_url"),
                "pr_number": data.get("pr_number"),
                "started_at": data.get("started_at", ""),
                "last_updated_at": data.get("last_updated_at", ""),
                "error": data.get("error"),
                "stage_iterations": data.get("stage_iterations", {}),
                "human_input_pending": data.get("human_input_pending", False),
                "reports": reports,
                "meta": meta,
                "workspace_root": str(ws_root),
            })
        except Exception as e:
            logger.warning("Failed to read workspace at %s: %s", state_file.parent, e)
    return results


def create_app(
    bus: EventBus,
    store: EventStore,
    workspace_base_dir: str = "",
) -> Starlette:
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

    async def get_workspaces(request: Request) -> JSONResponse:
        """Return all workspaces from disk, optionally filtered by project."""
        project_id = request.query_params.get("project_id")
        workspaces = _scan_all_workspaces(workspace_base_dir)
        if project_id:
            workspaces = [w for w in workspaces if w["company_id"] == project_id]
        # Sort: active first (by state order), then by last_updated_at descending
        state_order = {
            "BLOCKED": 0, "AWAITING_APPROVAL": 1, "DEV": 2, "ANALYSIS": 3,
            "SCOPE_CHECK": 4, "QA": 5, "PR_REVIEW": 6, "PUSHED": 7,
            "NEW": 8, "DONE": 9, "FAILED": 10, "ARCHIVED": 11,
        }
        workspaces.sort(key=lambda w: (
            state_order.get(w["current_state"], 99),
            w.get("last_updated_at", "") or "",
        ))
        return JSONResponse({"workspaces": workspaces})

    async def get_workspace_report(request: Request) -> PlainTextResponse:
        """Serve a report or meta file from a workspace."""
        ticket_id = request.path_params["ticket_id"]
        filename = request.path_params["filename"]
        folder = request.query_params.get("folder", "reports")
        if folder not in ("reports", "meta", "logs"):
            return PlainTextResponse("Invalid folder", status_code=400)
        # Find workspace on disk
        for ws in _scan_all_workspaces(workspace_base_dir):
            if ws["ticket_id"] == ticket_id:
                file_path = Path(ws["workspace_root"]) / folder / filename
                if file_path.exists() and file_path.is_file():
                    return PlainTextResponse(file_path.read_text(encoding="utf-8", errors="replace"))
                return PlainTextResponse(f"File not found: {folder}/{filename}", status_code=404)
        return PlainTextResponse(f"Workspace not found: {ticket_id}", status_code=404)

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
        Route("/api/workspaces", get_workspaces),
        Route("/api/workspaces/{ticket_id}/report/{filename:path}", get_workspace_report),
    ]

    return Starlette(routes=routes)

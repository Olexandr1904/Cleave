"""Embedded web server for the Cleave dashboard."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import aiosqlite
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from health.runner import check_all
from orchestrator.model_resolver import model_short_name
from workspace.workspace import Stage

logger = logging.getLogger(__name__)


def _build_external_links(
    data: dict,
    projects: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Build a list of external links for a workspace based on project/repo config."""
    links: list[dict[str, str]] = []

    ticket_id = data.get("ticket_id", "")
    company_id = data.get("company_id", "")
    repo_id = data.get("repo_id", "")

    project = projects.get(company_id) if projects else None

    # Jira ticket link
    tracker = getattr(project.config, "tracker", None) if project else None
    jira = tracker.jira if (tracker and tracker.provider == "jira") else None
    if jira and jira.url and ticket_id:
        base = jira.url.rstrip("/")
        links.append({
            "label": f"Jira: {ticket_id}",
            "url": f"{base}/browse/{ticket_id}",
            "type": "jira",
        })

    # PR link (already on workspace)
    pr_url = data.get("pr_url")
    if pr_url:
        pr_number = data.get("pr_number")
        label = f"PR #{pr_number}" if pr_number else "Pull Request"
        links.append({"label": label, "url": pr_url, "type": "pr"})

    # Repo link (root of the repo on the VCS)
    repo = project.repos.get(repo_id) if project and repo_id else None
    if repo:
        vcs = getattr(repo, "vcs", None)
        if vcs:
            if vcs.provider == "github" and vcs.github.owner and vcs.github.repo:
                links.append({
                    "label": "Repo",
                    "url": f"https://github.com/{vcs.github.owner}/{vcs.github.repo}",
                    "type": "repo",
                })
            elif vcs.provider == "gitlab" and vcs.gitlab.url and vcs.gitlab.project_id:
                base = vcs.gitlab.url.rstrip("/")
                links.append({
                    "label": "Repo",
                    "url": f"{base}/{vcs.gitlab.project_id}",
                    "type": "repo",
                })

    return links


def _maybe_backfill_title(ws_root: Path, data: dict) -> str:
    """Return the title for this workspace, backfilling state.json if needed.

    Reads ``meta/ticket.md`` first line for ticket workspaces, or assigns the
    static "Workspace setup" title for the setup directory. The result is
    written back to ``state.json`` so the parse only happens once per workspace.
    """
    title = data.get("title") or ""
    if title:
        return title

    if ws_root.name == "setup":
        title = "Workspace setup"
    else:
        ticket_md = ws_root / "meta" / "ticket.md"
        if ticket_md.exists():
            try:
                first_line = ticket_md.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, IndexError):
                first_line = ""
            # First line shape: "# TICKET-ID: Title" or "# Title"
            stripped = first_line.lstrip("# ").strip()
            ticket_id = data.get("ticket_id", "")
            if ticket_id and stripped.startswith(f"{ticket_id}:"):
                title = stripped[len(ticket_id) + 1:].strip()
            else:
                title = stripped

    if title:
        data["title"] = title
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(ws_root), suffix=".tmp", prefix="state_"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, str(ws_root / "state.json"))
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except OSError as e:
            logger.warning("Failed to backfill title for %s: %s", ws_root, e)
        return title

    # No title from meta/ticket.md or setup fallback — soft-fallback to ticket_id
    # for the API response only. Don't write ticket_id back to state.json.
    return data.get("ticket_id", "")


def _scan_all_workspaces(
    base_dir: str,
    projects: dict[str, Any] | None = None,
    default_model: str = "",
) -> list[dict[str, Any]]:
    """Scan workspace base_dir for ALL workspaces (including terminal states).

    `default_model` is used to backfill the model pill on legacy workspaces
    whose state.json predates the per-ticket model snapshot. Pass "" to skip
    backfilling (callers that don't need pills).
    """
    base = Path(base_dir)
    if not base.exists():
        return []
    results = []
    for state_file in base.rglob("state.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            ws_root = state_file.parent
            title = _maybe_backfill_title(ws_root, data)
            # List available reports.
            # Ticket workspaces store reports at `source/ai_pipeline/<ticket>/`;
            # legacy tickets used `source/reports/` or `reports/`; setup
            # workspaces use root `reports/`. Try in order, take the first hit.
            ticket_id = data.get("ticket_id", "")
            reports_dir = ws_root / "source" / "ai_pipeline" / ticket_id
            if not reports_dir.exists():
                reports_dir = ws_root / "source" / "reports"
            if not reports_dir.exists():
                reports_dir = ws_root / "reports"
            reports = sorted(f.name for f in reports_dir.iterdir() if f.is_file()) if reports_dir.exists() else []
            # List meta files
            meta_dir = ws_root / "meta"
            meta = sorted(f.name for f in meta_dir.iterdir() if f.is_file()) if meta_dir.exists() else []
            kind = "setup" if ws_root.name == "setup" else "ticket"
            model_id = data.get("model", "") or default_model
            results.append({
                "ticket_id": data.get("ticket_id", ""),
                "company_id": data.get("company_id", ""),
                "repo_id": data.get("repo_id", ""),
                "current_state": data.get("current_state", "UNKNOWN"),
                "previous_state": data.get("previous_state"),
                "title": title,
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
                "links": _build_external_links(data, projects),
                "kind": kind,
                "model": model_id,
                "model_short": model_short_name(model_id) or (model_id if model_id else None),
            })
        except Exception as e:
            logger.warning("Failed to read workspace at %s: %s", state_file.parent, e)
    return results


def create_app(
    bus: EventBus,
    store: EventStore,
    workspace_base_dir: str = "",
    orchestrator: Any | None = None,
    mode_handler: Any | None = None,
    global_config: Any | None = None,
    projects: dict[str, Any] | None = None,
    config_dir: str | None = None,
    atlas_fn: Any | None = None,
    env_path: Any | None = None,
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
        from dashboard.settings_store import get_model
        project_id = request.query_params.get("project_id")
        # Read the global default once so legacy workspaces (state.model == "")
        # still get a model pill on the dashboard.
        try:
            async with aiosqlite.connect(store._db_path) as conn:
                default_model = await get_model(conn)
        except Exception:
            default_model = ""
        workspaces = _scan_all_workspaces(workspace_base_dir, projects, default_model)
        if project_id:
            workspaces = [w for w in workspaces if w["company_id"] == project_id]
        # Sort: active first (by state order), then by last_updated_at descending
        state_order = {
            Stage.BLOCKED: 0, Stage.AWAITING_APPROVAL: 1, Stage.DEV: 2, Stage.ANALYSIS: 3,
            Stage.SCOPE_CHECK: 4, Stage.QA: 5, Stage.PR_REVIEW: 6, Stage.PUSHED: 7,
            Stage.NEW: 8, Stage.DONE: 9, Stage.FAILED: 10, Stage.ARCHIVED: 11,
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
        for ws in _scan_all_workspaces(workspace_base_dir, projects):
            if ws["ticket_id"] == ticket_id:
                ws_root = Path(ws["workspace_root"])
                if folder == "reports":
                    # Try new layout first (`source/ai_pipeline/<ticket>/`),
                    # then legacy locations.
                    candidate = ws_root / "source" / "ai_pipeline" / ticket_id / filename
                    if not candidate.exists():
                        candidate = ws_root / "source" / "reports" / filename
                    if not candidate.exists():
                        candidate = ws_root / "reports" / filename
                    file_path = candidate
                else:
                    file_path = ws_root / folder / filename
                if file_path.exists() and file_path.is_file():
                    return PlainTextResponse(file_path.read_text(encoding="utf-8", errors="replace"))
                return PlainTextResponse(f"File not found: {folder}/{filename}", status_code=404)
        return PlainTextResponse(f"Workspace not found: {ticket_id}", status_code=404)

    async def projects_health(request: Request) -> JSONResponse:
        force = request.query_params.get("refresh") == "1"
        if not projects:
            return JSONResponse({"projects": []})
        results = await check_all(projects, force=force)
        return JSONResponse({
            "projects": [
                {
                    "project_id": r.project_id,
                    "status": r.status,
                    "checks": [
                        {
                            "name": c.name,
                            "target": c.target,
                            "ok": c.ok,
                            "reason": c.reason,
                            "fix_hint": c.fix_hint,
                        }
                        for c in r.checks
                    ],
                    "checked_at": r.checked_at.isoformat(),
                }
                for r in results
            ]
        })

    async def get_settings_model(request: Request) -> JSONResponse:
        from dashboard.settings_store import ALLOWED_MODELS, get_model
        async with aiosqlite.connect(store._db_path) as conn:
            current = await get_model(conn)
        return JSONResponse({
            "model": current,
            "options": list(ALLOWED_MODELS),
        })

    async def put_settings_model(request: Request) -> JSONResponse:
        from dashboard.settings_store import set_model
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)
        try:
            async with aiosqlite.connect(store._db_path) as conn:
                await set_model(conn, model)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"model": model})

    async def index(request: Request) -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/events", get_events),
        Route("/api/projects", get_projects),
        Route("/api/projects/health", projects_health),
        Route("/api/projects/{project_id}/tickets", get_project_tickets),
        Route("/api/tickets/{ticket_id:path}/events", get_ticket_events),
        Route("/api/workspaces", get_workspaces),
        Route("/api/workspaces/{ticket_id}/report/{filename:path}", get_workspace_report),
        Route("/api/settings/model", get_settings_model, methods=["GET"]),
        Route("/api/settings/model", put_settings_model, methods=["PUT"]),
    ]

    # Action routes (only if orchestrator is available)
    if orchestrator is not None:
        from dashboard.actions import build_action_routes
        action_routes = build_action_routes(
            orchestrator=orchestrator,
            mode_handler=mode_handler,
            event_bus=bus,
            global_config=global_config,
        )
        routes.extend(action_routes)

    # Project-create route
    from dashboard.project_create import build_create_route

    async def _default_atlas_fn(workspace, config_dir):
        raise RuntimeError("atlas_fn not configured")

    create_route_handler = build_create_route(
        workspace_base_dir=Path(workspace_base_dir),
        config_dir=Path(config_dir) if config_dir else Path("config-live"),
        env_path=Path(env_path) if env_path is not None else Path(".env"),
        atlas_fn=atlas_fn or _default_atlas_fn,
        orchestrator=orchestrator,
    )
    routes.append(Route("/api/projects/create", create_route_handler, methods=["POST"]))

    async def validate_step(request: Request) -> JSONResponse:
        """Validate wizard step data against live APIs."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        step = body.get("step")
        data = body.get("data", {})

        if step == "jira":
            from health.validators import check_jira
            url = data.get("url", "")
            r = await check_jira(
                url=url, email=data.get("email", ""),
                token=data.get("token", ""), project_key=data.get("project_key", ""),
            )
            return JSONResponse({
                "ok": r.ok,
                "checks": [{"name": r.name, "ok": r.ok, "reason": r.reason, "fix_hint": r.fix_hint}],
            })

        if step == "vcs":
            provider = data.get("provider")
            if provider == "github":
                from health.validators import check_github
                r = await check_github(
                    token=data.get("token", ""),
                    owner=data.get("owner", ""), repo=data.get("repo", ""),
                )
            elif provider == "gitlab":
                from health.validators import check_gitlab
                r = await check_gitlab(
                    token=data.get("token", ""),
                    project_id=data.get("project_id", ""),
                    url=data.get("url", "https://gitlab.com"),
                )
            else:
                return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=400)
            return JSONResponse({
                "ok": r.ok,
                "checks": [{"name": r.name, "ok": r.ok, "reason": r.reason, "fix_hint": r.fix_hint}],
            })

        if step == "telegram":
            import os
            token = data.get("token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = data.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
            if not token or not chat_id:
                return JSONResponse({
                    "ok": False,
                    "checks": [{"name": "telegram", "ok": False,
                                "reason": "Bot token or chat ID missing",
                                "fix_hint": "Provide values or set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env"}],
                })
            import httpx
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": "Hello World"},
                    )
                    rj = resp.json()
                    if rj.get("ok"):
                        return JSONResponse({
                            "ok": True,
                            "checks": [{"name": "telegram", "ok": True, "reason": "", "fix_hint": ""}],
                        })
                    return JSONResponse({
                        "ok": False,
                        "checks": [{"name": "telegram", "ok": False,
                                    "reason": rj.get("description", f"HTTP {resp.status_code}"),
                                    "fix_hint": "Check bot token and chat ID"}],
                    })
            except Exception as e:
                return JSONResponse({
                    "ok": False,
                    "checks": [{"name": "telegram", "ok": False,
                                "reason": str(e), "fix_hint": "Check network and bot token"}],
                })

        return JSONResponse({"ok": True, "checks": []})

    routes.append(Route("/api/projects/validate-step", validate_step, methods=["POST"]))

    async def telegram_globals(request: Request) -> JSONResponse:
        """Check if global Telegram config exists in env."""
        import os
        has_token = bool(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        has_chat = bool(os.environ.get("TELEGRAM_CHAT_ID", ""))
        return JSONResponse({"has_global": has_token and has_chat})

    routes.append(Route("/api/projects/telegram-globals", telegram_globals))

    static_dir = str(Path(__file__).parent / "static")
    app = Starlette(routes=routes)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app

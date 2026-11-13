"""Dashboard routes for the Reviews tab: the board, Review now and Skip. Nothing here posts."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from orchestrator.reviewer.models import BoardRow, Job


def _job(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {"id": job.id, "kind": job.kind, "state": job.state, "head": job.head_sha[:9]}


def _pr(row: BoardRow, job: Job | None, skipped: bool) -> dict:
    return {
        "number": row.number, "title": row.title, "url": row.url, "ticket_key": row.ticket_key,
        "roles": row.roles,
        "my_turns": list(dict.fromkeys(row.my_actions)),  # one address_review per reviewer
        "other_turns": [{"login": t["login"], "action": t["action"]} for t in row.other_turns],
        "pipeline": row.pipeline, "draft": row.draft, "changed_files": row.changed_files,
        "head": row.head_sha[:9], "job": _job(job), "last_change": row.last_change,
        "skipped": skipped,  # the current head is skipped
    }


def _repo(repo: dict) -> dict:
    my_turn, waiting = [], []
    for row in sorted(repo["rows"], key=lambda r: r.number, reverse=True):
        group = my_turn if row.my_turns and not row.pipeline else waiting
        group.append(_pr(row, repo["jobs"].get(row.number), row.number in repo["skipped"]))
    return {
        "scope": repo["scope"], "status": repo["status"], "reason": repo["reason"],
        "last_poll_at": repo["last_poll_at"], "next_poll_at": repo["next_poll_at"],
        "my_turn": my_turn, "waiting": waiting,
    }


def _cross_site(request: Request) -> bool:
    """True for a request another site's page could have sent from the operator's browser.

    A JSON body cannot be sent cross-site without a CORS preflight, which the dashboard never
    grants; Origin and Sec-Fetch-Site catch the rest. curl sends neither header and passes.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return True
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    return origin is not None and urlsplit(origin).netloc != request.headers.get("host")


async def _target(request: Request) -> tuple[str, int] | JSONResponse:
    """The body's (scope, number), a 403 response for a cross-site request, or a 400 response."""
    if _cross_site(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    scope, number = body.get("scope"), body.get("number")
    if not isinstance(scope, str) or not scope:
        return JSONResponse({"error": "scope must be a non-empty string"}, status_code=400)
    if not isinstance(number, int) or isinstance(number, bool):
        return JSONResponse({"error": "number must be an integer"}, status_code=400)
    return scope, number


def build_review_routes(service: Any) -> list[Route]:
    async def get_reviews(request: Request) -> JSONResponse:
        return JSONResponse({"repos": [_repo(r) for r in service.overview()]})

    async def post_review_now(request: Request) -> JSONResponse:
        target = await _target(request)
        if isinstance(target, JSONResponse):
            return target
        try:
            job = service.review_now(*target)
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"job": _job(job)})

    async def post_skip(request: Request) -> JSONResponse:
        target = await _target(request)
        if isinstance(target, JSONResponse):
            return target
        try:
            service.skip(*target)
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"ok": True})

    return [
        Route("/api/reviews", get_reviews, methods=["GET"]),
        Route("/api/reviews/review-now", post_review_now, methods=["POST"]),
        Route("/api/reviews/skip", post_skip, methods=["POST"]),
    ]

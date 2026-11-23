"""Dashboard routes for the Reviews tab: the board, a PR's detail, Review now, Skip, Fix now and Fix findings…, and,
given the reviewer's action layer, a command on a job (a button's or a typed one) and the Yes or No on one the model
understood. Fix now only queues an address job and Fix findings… a findings job. A command is checked and run by
ReviewerActions, the layer Telegram's replies call, so a post or a push goes through the one Posting both channels
share; Telegram is told what it did."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dashboard.web_guard import foreign_origin
from orchestrator.reviewer.grammar import ReplyCommand, describe, parse_reply
from orchestrator.reviewer.models import BoardRow, Job
from orchestrator.reviewer.service import _where, utcnow
from orchestrator.tg_i18n import ENGLISH, telegram_language, tr

logger = logging.getLogger(__name__)

_DASHBOARD_CHAT = "dashboard"  # the chat id of the confirmations the dashboard stores
# A button's command, as ReplyCommand.to_dict() gives it: the fields it may hold, with their types (a field left out
# takes its default), and the values of action and event.
_COMMAND_FIELDS = {"action": str, "numbers": list, "event": str, "all_confirmed": bool, "no_summary": bool,
                   "anyway": bool, "instruction": str, "all_replies": bool}
_COMMAND_ACTIONS = ("post", "post_replies", "text", "edit", "draft", "review_again", "skip", "push", "push_post",
                    "diff", "again", "fix")
_COMMAND_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
_UNANNOUNCED = ("refused", "shown")  # the results that changed nothing: Telegram is not told


def _job(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {"id": job.id, "kind": job.kind, "state": job.state, "head": job.head_sha[:9]}


def _pr(row: BoardRow, repo: dict) -> dict:
    job = repo["jobs"].get(row.number)
    return {
        "number": row.number, "title": row.title, "url": row.url, "ticket_key": row.ticket_key,
        "roles": row.roles,
        "my_turns": list(dict.fromkeys(row.my_actions)),  # one address_review per reviewer
        # The reviewers who asked for changes on an older head: Re-request asks them again.
        "rerequest": list(dict.fromkeys(t["reviewer"] for t in row.my_turns
                                        if t["action"] == "request_review" and t.get("reviewer"))),
        "other_turns": [{"login": t["login"], "action": t["action"]} for t in row.other_turns],
        "pipeline": row.pipeline, "draft": row.draft, "changed_files": row.changed_files,
        "head": row.head_sha[:9],
        "job": None if job is None else {**_job(job), **repo["job_info"][row.number]},
        "last_change": row.last_change,
        "skipped": row.number in repo["skipped"],  # the current head is skipped
        "large": row.number in repo["large"],  # not reviewed automatically
        "review": repo["reviews"].get(row.number),  # the newest review, whatever job is newer
        "can_fix": row.number in repo["fixable"],  # Fix now applies
        "can_fix_findings": row.number in repo["findings_fixable"],  # Fix findings… applies
        "fix": repo["fixes"].get(row.number),  # the newest fix job of either kind, whatever job is newer
    }


def _jira_url(projects: dict, project_id: str) -> str:
    """The base URL of the project's Jira, "" when it tracks tickets elsewhere (as web.py's ticket links)."""
    tracker = getattr(getattr(projects.get(project_id), "config", None), "tracker", None)
    if getattr(tracker, "provider", None) != "jira":
        return ""
    return (getattr(getattr(tracker, "jira", None), "url", "") or "").rstrip("/")


def _repo(repo: dict, projects: dict) -> dict:
    my_turn, waiting = [], []
    for row in sorted(repo["rows"], key=lambda r: r.number, reverse=True):
        group = my_turn if row.my_turns else waiting
        group.append(_pr(row, repo))
    return {
        "scope": repo["scope"], "status": repo["status"], "reason": repo["reason"],
        "last_poll_at": repo["last_poll_at"], "next_poll_at": repo["next_poll_at"],
        "me": repo["me"], "jira_url": _jira_url(projects, repo["project_id"]),
        "my_turn": my_turn, "waiting": waiting,
    }


def _cross_site(request: Request) -> bool:
    """True for a request another site's page could have sent from the operator's browser.

    A JSON body cannot be sent cross-site without a CORS preflight, which the dashboard never
    grants; Origin and Sec-Fetch-Site catch the rest. curl sends neither header and passes.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return True
    return foreign_origin(request)


async def _body(request: Request) -> dict | JSONResponse:
    """The request's JSON object, a 403 response for a cross-site request, or a 400 response."""
    if _cross_site(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    return body


def _scope_number(body: dict) -> tuple[str, int] | JSONResponse:
    """The body's (scope, number), or a 400 response."""
    scope, number = body.get("scope"), body.get("number")
    if not isinstance(scope, str) or not scope:
        return JSONResponse({"error": "scope must be a non-empty string"}, status_code=400)
    if not isinstance(number, int) or isinstance(number, bool):
        return JSONResponse({"error": "number must be an integer"}, status_code=400)
    return scope, number


async def _target(request: Request) -> tuple[str, int] | JSONResponse:
    """The body's (scope, number), a 403 response for a cross-site request, or a 400 response."""
    body = await _body(request)
    return body if isinstance(body, JSONResponse) else _scope_number(body)


def _command(data: Any) -> ReplyCommand | None:
    """The command a button sent (ReplyCommand.to_dict(), any field left out taking its default); None for anything
    else. type(), not isinstance(): a bool is no int and 1 no bool."""
    if not (isinstance(data, dict) and data.keys() <= _COMMAND_FIELDS.keys()
            and all(type(value) is _COMMAND_FIELDS[name] for name, value in data.items())):
        return None
    if data.get("action") not in _COMMAND_ACTIONS or data.get("event", "COMMENT") not in _COMMAND_EVENTS:
        return None
    if not all(type(n) is int for n in data.get("numbers", [])):
        return None
    return ReplyCommand.from_dict(data)


def _refused(text: str) -> JSONResponse:
    return JSONResponse({"state": "refused", "texts": [text]})


def build_review_routes(service: Any, get_projects: Callable[[], dict] = dict, actions: Any | None = None,
                        replies: Any | None = None, *, now: Callable[[], datetime] = utcnow) -> list[Route]:
    """The Reviews tab's routes; with `actions` (ReviewerActions) the command routes too, whose results `replies`
    (ReviewerReplies, None without Telegram) announces in Telegram."""
    async def get_reviews(request: Request) -> JSONResponse:
        projects = get_projects()
        return JSONResponse({"repos": [_repo(r, projects) for r in service.overview()]})

    async def get_pr(request: Request) -> JSONResponse:
        scope = request.query_params.get("scope")
        if not scope:
            return JSONResponse({"error": "scope is required"}, status_code=400)
        try:
            number = int(request.query_params.get("number", ""))
        except ValueError:
            return JSONResponse({"error": "number must be an integer"}, status_code=400)
        try:
            return JSONResponse(service.detail(scope, number))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

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

    async def post_fix_now(request: Request) -> JSONResponse:
        target = await _target(request)
        if isinstance(target, JSONResponse):
            return target
        try:
            job = service.address_now(*target)
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:  # already queued, or not fixable
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"job": _job(job)})

    async def post_fix_findings(request: Request) -> JSONResponse:
        target = await _target(request)
        if isinstance(target, JSONResponse):
            return target
        try:
            job = service.fix_findings(*target)  # the PR's self-review at its head: no review id
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:  # already queued, a post under way, or not fixable
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"job": _job(job)})

    async def post_action(request: Request) -> JSONResponse:
        """A command on a job of the body's PR: a button's (`command`, a ReplyCommand dict) or a typed one (`text`).
        Text the grammar knows runs at once, as in Telegram; any other goes to the model (where the layer lets it),
        and what the model understood runs only after a Yes (post_confirm)."""
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        target = _scope_number(body)
        if isinstance(target, JSONResponse):
            return target
        job_id, command, text = body.get("job_id"), body.get("command"), body.get("text")
        if not isinstance(job_id, str) or not job_id:
            return JSONResponse({"error": "job_id must be a non-empty string"}, status_code=400)
        if (command is None) == (text is None):
            return JSONResponse({"error": "give a command or a text"}, status_code=400)
        cmd = None if command is None else _command(command)
        if command is not None and cmd is None:
            return JSONResponse({"error": "command is not a reviewer command"}, status_code=400)
        if text is not None and (not isinstance(text, str) or not text.strip()):
            return JSONResponse({"error": "text must be a non-empty string"}, status_code=400)
        job = actions.store.job(job_id)
        if job is None:
            return JSONResponse({"error": f"no reviewer job {job_id}"}, status_code=404)
        if (job.scope, job.number) != target:
            return JSONResponse({"error": f"job {job_id} is not one of #{target[1]} in {target[0]}"}, status_code=400)
        if cmd is None and (cmd := parse_reply(text)) is None:
            return await _understand(job, text)
        return await _run(job, cmd)

    async def post_confirm(request: Request) -> JSONResponse:
        """The Yes or No on a command the model understood from the dashboard's text (Telegram's own echoes are
        answered there). The layer claims it (ReviewerActions.claim): a Yes runs its command against its job as the
        store has it now, unless it was answered already, is older than CONFIRM_TTL or the job refuses it."""
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        conf_id, yes = body.get("confirmation"), body.get("yes")
        if not isinstance(conf_id, str) or not conf_id:
            return JSONResponse({"error": "confirmation must be a non-empty string"}, status_code=400)
        if not isinstance(yes, bool):
            return JSONResponse({"error": "yes must be true or false"}, status_code=400)
        store = actions.store
        conf = store.confirmation(conf_id)
        if conf is None or conf.chat_id != _DASHBOARD_CHAT:
            return JSONResponse({"error": f"no dashboard confirmation {conf_id}"}, status_code=404)
        with telegram_language(ENGLISH):
            claimed = actions.claim(conf_id, yes, now())
        if isinstance(claimed, str):
            return _refused(claimed)
        return await _execute(store.job(conf.job_id), claimed)

    async def _understand(job: Job, text: str) -> JSONResponse:
        """The model's command for `text`, checked and kept for a Yes, or why nothing is done (the layer decides what
        free text may reach the model)."""
        with telegram_language(ENGLISH):
            understood = await actions.understand(job, text)
            checked = understood if isinstance(understood, str) else actions.check(job, understood)
            if isinstance(checked, str):
                return _refused(checked)
            conf = actions.store.add_confirmation(job.id, _DASHBOARD_CHAT, checked.to_dict(), now().isoformat())
            question = tr("Understood: {command}. Correct?", command=describe(checked, job.head_sha))
        return JSONResponse({"state": "confirm", "question": question, "confirmation": conf.id})

    async def _run(job: Job, cmd: ReplyCommand) -> JSONResponse:
        """`cmd` checked against `job` and run (_execute), or why nothing is done, in English."""
        with telegram_language(ENGLISH):
            checked = actions.check(job, cmd)
        if isinstance(checked, str):
            return _refused(checked)
        return await _execute(job, checked)

    async def _execute(job: Job, cmd: ReplyCommand) -> JSONResponse:
        """A checked command run against `job`, its texts in English; a result that changed something is announced in
        Telegram."""
        with telegram_language(ENGLISH):
            result = await actions.run(job, cmd)
            command = describe(cmd, job.head_sha)
        if replies is not None and result.state not in _UNANNOUNCED:
            try:
                await replies.announce_dashboard(job.id, cmd, result.state)
            except Exception as e:  # the command is done: its answer does not depend on Telegram
                logger.warning("Dashboard reviewer command not announced in Telegram: %s", _where(e))
        return JSONResponse({"state": result.state, "texts": result.texts, "command": command})

    routes = [
        Route("/api/reviews", get_reviews, methods=["GET"]),
        Route("/api/reviews/pr", get_pr, methods=["GET"]),
        Route("/api/reviews/review-now", post_review_now, methods=["POST"]),
        Route("/api/reviews/skip", post_skip, methods=["POST"]),
        Route("/api/reviews/fix-now", post_fix_now, methods=["POST"]),
        Route("/api/reviews/fix-findings", post_fix_findings, methods=["POST"]),
    ]
    if actions is not None:
        routes += [
            Route("/api/reviews/action", post_action, methods=["POST"]),
            Route("/api/reviews/confirm", post_confirm, methods=["POST"]),
        ]
    return routes

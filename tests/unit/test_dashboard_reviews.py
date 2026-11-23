from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from config.schemas import ReviewerConfig
from dashboard import reviews_api
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.reviews_api import build_review_routes
from dashboard.web import create_app
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.grammar import ReplyCommand
from orchestrator.reviewer.lines import not_understood_line
from orchestrator.reviewer.models import BoardRow, Job, JobRun
from orchestrator.reviewer.posting import PostOutcome
from orchestrator.reviewer.replies import CONFIRM_TTL
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches
from orchestrator.tg_i18n import telegram_language
from tests.unit.test_reviewer_replies import (
    CHAT, EVERY_BUT_3, N, NEW_HEAD, SCOPE, T1, address_rig, confirmations,
)
from tests.unit.test_reviewer_report import HEAD as RHEAD, REPORT

HEAD = "0123456789abcdef0123456789abcdef01234567"
STAMP = "2026-11-12T10:00:00+00:00"
PR_KEYS = {
    "number", "title", "url", "ticket_key", "roles", "my_turns", "other_turns", "pipeline",
    "draft", "changed_files", "head", "job", "last_change", "skipped", "large", "review", "can_fix", "fix",
    "can_fix_findings",
}
QUEUED_INFO = {"retry_at": "", "started_at": "", "reason": "", "message_sent": False, "findings": None}
# A PR's newest review as the service gives it (ReviewerService._review_info), passed through.
REVIEW_INFO = {
    "id": "r1", "state": "done", "head": HEAD[:9], "started_at": STAMP, "finished_at": "2026-11-12T11:00:10+00:00",
    "duration_s": 3610, "cost_usd": 28.93, "message_sent": True,
    "findings": {"critical": 2, "major": 0, "minor": 1, "question": 3, "preexisting": 0, "dropped": 1},
}
# A PR's newest address job as the service gives it (ReviewerService._fix_info), passed through.
FIX_INFO = {"id": "a2", "kind": "address", "state": "done", "head": HEAD[:9], "started_at": STAMP, "fix": True,
            "pushed": False, "posting": False}


def _row(number, my_turns=(), other_turns=(), pipeline=False):
    return BoardRow(
        number=number, title=f"ACME-{number} change {number}",
        url=f"https://github.com/acme-org/acme-app/pull/{number}", author="dev",
        roles=["reviewer"], head_sha=HEAD, head_first_seen_at=STAMP, draft=False,
        changed_files=3, my_turns=list(my_turns), other_turns=list(other_turns),
        threads_truncated=False, pipeline=pipeline, ticket_key=f"ACME-{number}",
        updated_at=STAMP, last_change=STAMP,
    )


def _job(number):
    return Job(id=f"j{number}", scope="acme/app", number=number, kind="review", head_sha=HEAD,
               key=HEAD, state="queued", reason="", created_at=STAMP, updated_at=STAMP)


DEFAULT_ROWS = [
    _row(2, my_turns=[{"action": "answer_threads", "threads": ["T1"]}], pipeline=True),
    _row(1, my_turns=[{"action": "review", "threads": []}],
         other_turns=[{"login": "dev", "action": "address_review"}]),
    _row(3, other_turns=[{"login": "dev", "action": "address_review"}]),
]


class FakeService:
    def __init__(self, rows=None):
        self.rows = DEFAULT_ROWS if rows is None else rows
        self.calls = []
        self.review_now_error = None
        self.skip_error = None
        self.address_now_error = None
        self.fix_findings_error = None
        self.findings_fixable = {2}

    def overview(self):
        return [{
            "scope": "acme/app", "project_id": "acme", "me": "octo", "status": "ok", "reason": "",
            "last_poll_at": STAMP, "next_poll_at": "2026-11-12T10:05:00+00:00",
            "rows": self.rows, "jobs": {1: _job(1)}, "job_info": {1: QUEUED_INFO},
            "skipped": set(), "large": {3}, "reviews": {1: REVIEW_INFO}, "fixable": {2}, "fixes": {2: FIX_INFO},
            "findings_fixable": self.findings_fixable,
        }]

    def fix_findings(self, scope, number, *, review_id=None, instruction=None):
        self.calls.append(("fix_findings", scope, number, review_id, instruction))
        if self.fix_findings_error is not None:
            raise self.fix_findings_error
        return dataclasses.replace(_job(number), id=f"f{number}", kind="findings")

    def address_now(self, scope, number):
        self.calls.append(("address_now", scope, number))
        if self.address_now_error is not None:
            raise self.address_now_error
        return dataclasses.replace(_job(number), id=f"a{number}", kind="address")

    def review_now(self, scope, number):
        self.calls.append(("review_now", scope, number))
        if self.review_now_error is not None:
            raise self.review_now_error
        return _job(number)

    def skip(self, scope, number):
        self.calls.append(("skip", scope, number))
        if self.skip_error is not None:
            raise self.skip_error

    def detail(self, scope, number):
        self.calls.append(("detail", scope, number))
        if number != 1:
            raise LookupError(f"no PR #{number} in {scope}")
        return DETAIL


DETAIL = {
    "row": {"number": 1, "title": "ACME-1 change 1"},
    "jobs": [{"id": "j1", "kind": "review", "state": "done", "duration_s": 120}],
    "report_job": "j1", "report": {"event": "COMMENT", "items": []},
    "replies_job": None, "replies": [],
    "tg_lines": {"1": "npe"}, "report_text": "# Review",
    # An address job's: its fix, its patch and its pushed commit, passed through as the service gives them.
    "fix": {"files": [{"path": "a.kt", "added": 1, "deleted": 1}], "protected": [], "bytes": 9},
    "fix_patch": "diff --git a/a.kt b/a.kt\n", "pushed_sha": "b" * 40,
    # The newest findings job's verdicts, passed through.
    "verdicts_job": "f1", "verdicts": [{"n": 1, "verdict": "FIXED", "why": "null check added"}],
}


@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "e.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def client(store):
    svc = FakeService()
    return TestClient(create_app(EventBus(), store, reviewer=svc)), svc


def test_get_reviews_groups_and_trims(client):
    c, _ = client
    r = c.get("/api/reviews")
    assert r.status_code == 200
    [repo] = r.json()["repos"]
    assert {k: repo[k] for k in ("scope", "status", "reason", "last_poll_at", "next_poll_at")} == {
        "scope": "acme/app", "status": "ok", "reason": "",
        "last_poll_at": STAMP, "next_poll_at": "2026-11-12T10:05:00+00:00",
    }
    assert [p["number"] for p in repo["my_turn"]] == [2, 1]
    assert [p["number"] for p in repo["waiting"]] == [3]
    pr = repo["my_turn"][1]
    assert set(pr) == PR_KEYS
    assert pr["head"] == HEAD[:9] and len(pr["head"]) == 9
    assert pr["job"] == {"id": "j1", "kind": "review", "state": "queued", "head": HEAD[:9], **QUEUED_INFO}
    assert len(pr["job"]["head"]) == 9
    assert pr["my_turns"] == ["review"]
    assert pr["other_turns"] == [{"login": "dev", "action": "address_review"}]
    assert (pr["ticket_key"], pr["roles"], pr["pipeline"]) == ("ACME-1", ["reviewer"], False)
    assert repo["my_turn"][0]["pipeline"] is True
    assert repo["waiting"][0]["job"] is None
    assert [(p["number"], p["large"]) for p in repo["waiting"]] == [(3, True)]
    assert (repo["me"], repo["jira_url"]) == ("octo", "")   # no projects passed: no Jira link
    assert (pr["review"], pr["can_fix"]) == (REVIEW_INFO, False)
    assert repo["waiting"][0]["review"] is None
    assert repo["my_turn"][0]["can_fix"] is True
    assert (repo["my_turn"][0]["fix"], pr["fix"]) == (FIX_INFO, None)
    assert repo["my_turn"][0]["large"] is False
    assert pr["large"] is False


def test_my_turns_listed_once_in_first_seen_order(store):
    turns = [
        {"action": "address_review", "threads": []},
        {"action": "answer_threads", "threads": ["T1"]},
        {"action": "address_review", "threads": []},
    ]
    c = TestClient(create_app(EventBus(), store, reviewer=FakeService([_row(4, my_turns=turns)])))
    [repo] = c.get("/api/reviews").json()["repos"]
    assert repo["my_turn"][0]["my_turns"] == ["address_review", "answer_threads"]


def test_review_now_codes(client):
    c, svc = client
    r = c.post("/api/reviews/review-now", json={"scope": "acme/app", "number": 1})
    assert r.status_code == 200
    assert r.json() == {"job": {"id": "j1", "kind": "review", "state": "queued", "head": HEAD[:9]}}
    assert svc.calls == [("review_now", "acme/app", 1)]
    svc.review_now_error = LookupError("no PR #9 on the acme/app board")
    r = c.post("/api/reviews/review-now", json={"scope": "acme/app", "number": 9})
    assert r.status_code == 404 and r.json()["error"]
    svc.review_now_error = ValueError("already queued")
    r = c.post("/api/reviews/review-now", json={"scope": "acme/app", "number": 1})
    assert r.status_code == 409 and r.json()["error"] == "already queued"
    r = c.post("/api/reviews/review-now", json={})
    assert r.status_code == 400 and r.json()["error"]
    assert len(svc.calls) == 3  # a bad body never reaches the service


def test_skip_codes(client):
    c, svc = client
    r = c.post("/api/reviews/skip", json={"scope": "acme/app", "number": 1})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert svc.calls == [("skip", "acme/app", 1)]
    svc.skip_error = LookupError("no PR #9 on the acme/app board")
    r = c.post("/api/reviews/skip", json={"scope": "acme/app", "number": 9})
    assert r.status_code == 404 and r.json()["error"]
    r = c.post("/api/reviews/skip", json={})
    assert r.status_code == 400 and r.json()["error"]
    assert len(svc.calls) == 2


def test_fix_now_codes(client):
    c, svc = client
    r = c.post("/api/reviews/fix-now", json={"scope": "acme/app", "number": 2})
    assert r.status_code == 200
    assert r.json() == {"job": {"id": "a2", "kind": "address", "state": "queued", "head": HEAD[:9]}}
    assert svc.calls == [("address_now", "acme/app", 2)]
    svc.address_now_error = LookupError("no PR #9 on the acme/app board")
    r = c.post("/api/reviews/fix-now", json={"scope": "acme/app", "number": 9})
    assert r.status_code == 404 and r.json()["error"]
    svc.address_now_error = ValueError("already queued")
    r = c.post("/api/reviews/fix-now", json={"scope": "acme/app", "number": 2})
    assert r.status_code == 409 and r.json()["error"] == "already queued"
    svc.address_now_error = ValueError("not fixable: not your PR")
    r = c.post("/api/reviews/fix-now", json={"scope": "acme/app", "number": 1})
    assert r.status_code == 409 and r.json()["error"] == "not fixable: not your PR"
    r = c.post("/api/reviews/fix-now", json={})
    assert r.status_code == 400 and r.json()["error"]
    assert len(svc.calls) == 4  # a bad body never reaches the service


def _projects(tracker):
    return {"acme": SimpleNamespace(config=SimpleNamespace(tracker=tracker))}


JIRA = SimpleNamespace(provider="jira", jira=SimpleNamespace(url="https://acme.atlassian.net/"))


@pytest.mark.parametrize("projects, jira_url", [
    (_projects(JIRA), "https://acme.atlassian.net"),
    (_projects(SimpleNamespace(provider="linear", jira=JIRA.jira)), ""),
    (_projects(SimpleNamespace(provider="jira", jira=SimpleNamespace(url=""))), ""),
    (_projects(None), ""),
    ({"other": _projects(JIRA)["acme"]}, ""),
])
def test_jira_url_from_the_project_tracker(store, projects, jira_url):
    c = TestClient(create_app(EventBus(), store, reviewer=FakeService(), projects=projects))
    [repo] = c.get("/api/reviews").json()["repos"]
    assert repo["jira_url"] == jira_url


@pytest.mark.parametrize("path", ["/api/reviews/review-now", "/api/reviews/skip", "/api/reviews/fix-now",
                                  "/api/reviews/fix-findings"])
@pytest.mark.parametrize("body", [
    b"not json",
    b"[1]",
    b'{"scope": "acme/app"}',
    b'{"number": 1}',
    b'{"scope": "", "number": 1}',
    b'{"scope": 7, "number": 1}',
    b'{"scope": "acme/app", "number": "1"}',
    b'{"scope": "acme/app", "number": true}',
    b'{"scope": "acme/app", "number": 1.5}',
])
def test_bad_body_is_400(client, path, body):
    c, svc = client
    r = c.post(path, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]
    assert svc.calls == []


@pytest.mark.parametrize("path", ["/api/reviews/review-now", "/api/reviews/skip", "/api/reviews/fix-now",
                                  "/api/reviews/fix-findings"])
@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/plain"},  # a cross-site form or no-cors fetch needs no preflight
    {"Content-Type": "application/json", "Origin": "http://evil.example"},
    {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
    {"Content-Type": "application/json", "Sec-Fetch-Site": "same-site"},
])
def test_cross_site_action_is_refused(client, path, headers):
    c, svc = client
    r = c.post(path, content=b'{"scope": "acme/app", "number": 1}', headers=headers)
    assert r.status_code == 403 and r.json() == {"error": "cross-site request refused"}
    assert svc.calls == []


def test_same_origin_browser_action_is_allowed(client):
    c, svc = client
    r = c.post(
        "/api/reviews/skip", json={"scope": "acme/app", "number": 1},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200
    assert svc.calls == [("skip", "acme/app", 1)]


def test_skipped_follows_the_current_head(store, tmp_path):
    conn = connect(tmp_path / "rv" / "reviewer.db")
    rv = ReviewerStore(conn)
    ctx = RepoContext("acme", "app", ReviewerConfig(enabled=True, base_branch="develop"),
                      None, None, None)
    service = ReviewerService([ctx], rv, Switches(conn), lambda key: False, None,
                              lambda project_id, repo_id: set())
    review = [{"action": "review", "threads": []}]
    rv.replace_board("acme/app", {1: _row(1, my_turns=review), 2: _row(2, my_turns=review)})
    service.skip("acme/app", 1)  # no job yet: only the skip is recorded
    service.skip("acme/app", 2)
    pushed = dataclasses.replace(_row(2, my_turns=review), head_sha="b" * 40)
    rv.replace_board("acme/app", {1: _row(1, my_turns=review), 2: pushed})
    c = TestClient(create_app(EventBus(), store, reviewer=service))
    [repo] = c.get("/api/reviews").json()["repos"]
    prs = {p["number"]: p for p in repo["my_turn"]}
    assert (prs[1]["skipped"], prs[1]["job"]) == (True, None)
    assert prs[2]["skipped"] is False


def test_board_jobs_carry_their_run_and_large_prs_are_flagged(store, tmp_path):
    conn = connect(tmp_path / "rv" / "reviewer.db")
    rv = ReviewerStore(conn)
    cfg = ReviewerConfig(enabled=True, base_branch="develop", max_files_auto=10)
    service = ReviewerService([RepoContext("acme", "app", cfg, None, None, None)], rv, Switches(conn),
                              lambda key: False, None, lambda project_id, repo_id: set())
    review = [{"action": "review", "threads": []}]
    large = dataclasses.replace(_row(3, my_turns=review), changed_files=10)
    rv.replace_board("acme/app", {1: _row(1, my_turns=review), 2: _row(2, my_turns=review), 3: large})
    done = rv.add_job("acme/app", 1, "review", HEAD, HEAD, STAMP)
    rv.set_job_state(done.id, "done", "", STAMP)
    rv.save_run(JobRun(done.id, started_at=STAMP, report=parse_report(REPORT, RHEAD).to_dict(),
                       message_ids=[501, 502], message_sent=True))
    deferred = rv.add_job("acme/app", 2, "review", HEAD, HEAD, STAMP)
    rv.set_job_state(deferred.id, "deferred", "usage limit reached", STAMP,
                     retry_at="2026-11-12T10:48:00+00:00")
    queued = rv.add_job("acme/app", 3, "review", HEAD, f"{HEAD}:now", STAMP)
    c = TestClient(create_app(EventBus(), store, reviewer=service))
    [repo] = c.get("/api/reviews").json()["repos"]
    prs = {p["number"]: p for p in repo["my_turn"]}
    assert prs[1]["job"] == {
        "id": done.id, "kind": "review", "state": "done", "head": HEAD[:9], "retry_at": "",
        "started_at": STAMP, "reason": "", "message_sent": True,
        "findings": {"critical": 1, "major": 1, "minor": 1, "question": 2, "preexisting": 1, "dropped": 3},
        "fix": False,
    }
    assert {k: prs[2]["job"][k] for k in ("state", "retry_at", "reason", "message_sent")} == {
        "state": "deferred", "retry_at": "2026-11-12T10:48:00+00:00", "reason": "usage limit reached",
        "message_sent": False,
    }
    assert (prs[3]["job"]["id"], prs[3]["job"]["state"], prs[3]["job"]["findings"]) == (
        queued.id, "queued", None)
    assert [prs[n]["large"] for n in (1, 2, 3)] == [False, False, True]


def test_detail_route(client):
    c, svc = client
    r = c.get("/api/reviews/pr", params={"scope": "acme/app", "number": "1"})
    assert r.status_code == 200 and r.json() == DETAIL
    assert svc.calls == [("detail", "acme/app", 1)]
    for params in (
        {"scope": "acme/app", "number": "x"},
        {"scope": "acme/app", "number": "1.5"},
        {"scope": "acme/app"},
        {"number": "1"},
        {"scope": "", "number": "1"},
    ):
        r = c.get("/api/reviews/pr", params=params)
        assert r.status_code == 400 and r.json()["error"], params
    r = c.get("/api/reviews/pr", params={"scope": "acme/app", "number": "9"})
    assert r.status_code == 404 and r.json() == {"error": "no PR #9 in acme/app"}
    assert c.post("/api/reviews/pr", json={"scope": "acme/app", "number": 1}).status_code == 405
    assert len(svc.calls) == 2   # a bad query and a POST never reach the service


def test_no_posting_route():
    """Without the action layer nothing in the dashboard posts or confirms: its review routes are the board, a PR's
    detail, Review now, Skip and Fix now and Fix findings…, which only queue a fix job (Starlette answers HEAD on a GET
    route with the GET handler)."""
    routes = build_review_routes(FakeService())
    assert sorted((method, r.path) for r in routes for method in r.methods - {"HEAD"}) == [
        ("GET", "/api/reviews"), ("GET", "/api/reviews/pr"), ("POST", "/api/reviews/fix-findings"),
        ("POST", "/api/reviews/fix-now"), ("POST", "/api/reviews/review-now"), ("POST", "/api/reviews/skip"),
    ]
    assert len(routes) == 6


def test_routes_absent_without_service(store):
    c = TestClient(create_app(EventBus(), store))
    assert c.get("/api/reviews").status_code == 404
    r = c.post("/api/reviews/review-now", json={"scope": "acme/app", "number": 1})
    assert r.status_code == 404


def test_pr_can_fix_findings_and_fix_kind(client):
    c, svc = client
    [repo] = c.get("/api/reviews").json()["repos"]
    prs = {p["number"]: p for p in repo["my_turn"] + repo["waiting"]}
    assert [prs[n]["can_fix_findings"] for n in (1, 2, 3)] == [False, True, False]
    assert prs[2]["fix"]["kind"] == "address"
    svc.findings_fixable = {1, 3}
    [repo] = c.get("/api/reviews").json()["repos"]
    prs = {p["number"]: p for p in repo["my_turn"] + repo["waiting"]}
    assert [prs[n]["can_fix_findings"] for n in (1, 2, 3)] == [True, False, True]


def test_fix_findings_codes(client):
    c, svc = client
    r = c.post("/api/reviews/fix-findings", json={"scope": "acme/app", "number": 2})
    assert r.status_code == 200
    assert r.json() == {"job": {"id": "f2", "kind": "findings", "state": "queued", "head": HEAD[:9]}}
    assert svc.calls == [("fix_findings", "acme/app", 2, None, None)]   # no review id, no instruction
    svc.fix_findings_error = LookupError("no PR #9 on the acme/app board")
    r = c.post("/api/reviews/fix-findings", json={"scope": "acme/app", "number": 9})
    assert r.status_code == 404 and r.json()["error"]
    for error in ("already queued", "not fixable: no confirmed findings", "a post is under way"):
        svc.fix_findings_error = ValueError(error)
        r = c.post("/api/reviews/fix-findings", json={"scope": "acme/app", "number": 2})
        assert r.status_code == 409 and r.json() == {"error": error}
    r = c.post("/api/reviews/fix-findings", json={})
    assert r.status_code == 400 and r.json()["error"]
    assert len(svc.calls) == 5  # a bad body never reaches the service


def test_detail_carries_verdicts(client):
    c, _ = client
    d = c.get("/api/reviews/pr", params={"scope": "acme/app", "number": "1"}).json()
    assert (d["verdicts_job"], d["verdicts"]) == ("f1", [{"n": 1, "verdict": "FIXED", "why": "null check added"}])


# --- Commands on a reviewer job: the action layer Telegram's replies call too ---

class FakeReplies:
    """Records (job id, action, state) per announcement, then raises `error` when set."""

    def __init__(self):
        self.announced, self.error = [], None

    async def announce_dashboard(self, job_id, cmd, state):
        self.announced.append((job_id, cmd.action, state))
        if self.error is not None:
            raise self.error


def _in_ukrainian(app):
    """`app` run with Ukrainian as the current Telegram language: the dashboard's answers must be English anyway."""
    async def run(scope, receive, send):
        with telegram_language("uk"):
            await app(scope, receive, send)
    return run


@pytest.fixture
def dash(tmp_path):
    """address_rig's done review (message 100) and threads (message 200) jobs of PR 1712 at RHEAD, the review routes
    on the rig's action layer and clock, a recording replies; the parser understands EVERY_BUT_3 once."""
    rig = address_rig(tmp_path, parser=[EVERY_BUT_3])
    rig.posting.outcomes = {"push_fix": PostOutcome("posted", "Pushed …")}
    replies = FakeReplies()
    routes = build_review_routes(FakeService(), dict, rig.replies.actions, replies, now=lambda: rig.clock[0])
    return SimpleNamespace(c=TestClient(_in_ukrainian(Starlette(routes=routes))), rig=rig, replies=replies)


def act(d, job_id, **body):
    return d.c.post("/api/reviews/action", json={"scope": SCOPE, "number": N, "job_id": job_id, **body})


def confirm(d, conf_id, yes=True):
    return d.c.post("/api/reviews/confirm", json={"confirmation": conf_id, "yes": yes})


ASK = "please post all but the third"   # free text: the parser's EVERY_BUT_3
EVERY_BUT_3_LINE = f"post 1 2 4 5 6 + COMMENT at {RHEAD[:9]}"
HANDLED = {"state": "refused", "texts": ["Already handled."]}


def test_a_button_command_runs_and_answers_in_english(dash):
    d = dash
    r = act(d, d.rig.job.id, command={"action": "push"})
    assert r.status_code == 200 and r.json() == {"state": "posted", "texts": ["Pushed …"], "command": "push"}
    assert d.rig.posting.calls == [("push_fix", SCOPE, d.rig.job.id, None)]
    assert d.replies.announced == [(d.rig.job.id, "push", "posted")]
    assert d.rig.chat.sent == []   # Telegram hears of it through the announcement only
    r = act(d, d.rig.review.id, command={"action": "post", "numbers": [2, 1], "event": "APPROVE"})
    assert r.json() == {"state": "posted", "texts": ["Posted …"], "command": f"post 1 2 + APPROVE at {RHEAD[:9]}"}
    assert d.rig.posting.calls[-1] == ("post_review", SCOPE, d.rig.review.id, ReplyCommand("post", (1, 2), "APPROVE"))
    r = act(d, d.rig.job.id, command={"action": "post", "numbers": [1]})   # the layer's refusal, in English
    assert r.json() == {"state": "refused", "texts": [
        'This is a threads message: reply "push", "post all", "text 2", "diff" or "again: …". Nothing was done.']}
    assert len(d.rig.posting.calls) == 2 and len(d.replies.announced) == 2


def test_a_superseded_job_is_refused_and_no_newer_job_runs(dash):                     # Review Focus 2
    d = dash
    d.rig.add_job("review", 300)   # the newer review, done
    d.rig.store.set_job_state(d.rig.review.id, "superseded", "new head", T1.isoformat())
    r = act(d, d.rig.review.id, command={"action": "post", "numbers": [1]})
    assert r.status_code == 200
    assert r.json() == {"state": "refused", "texts": ["Outdated: there is a review of a newer head. Nothing was done."]}
    assert d.rig.posting.calls == [] and d.replies.announced == []


def test_a_job_of_another_pr_is_400_and_an_unknown_job_404(dash):
    d = dash
    for other in ({"number": 1730}, {"scope": "acme/other"}):
        r = d.c.post("/api/reviews/action", json={"scope": SCOPE, "number": N, "job_id": d.rig.job.id,
                                                  "command": {"action": "push"}, **other})
        assert r.status_code == 400 and r.json()["error"]
    r = act(d, "no-such-job", command={"action": "push"})
    assert r.status_code == 404 and r.json()["error"]
    assert d.rig.posting.calls == [] and d.replies.announced == []


def test_text_the_grammar_reads_runs_and_free_text_asks_first(dash):
    d, review = dash, dash.rig.review
    r = act(d, review.id, text="post 1 2")
    assert r.json() == {"state": "posted", "texts": ["Posted …"], "command": f"post 1 2 + COMMENT at {RHEAD[:9]}"}
    assert d.rig.posting.calls == [("post_review", SCOPE, review.id, ReplyCommand("post", (1, 2)))]
    assert d.rig.parser.calls == []
    body = act(d, review.id, text=ASK).json()
    assert body == {"state": "confirm", "question": f"Understood: {EVERY_BUT_3_LINE}. Correct?",
                    "confirmation": body["confirmation"]}
    conf = d.rig.store.confirmation(body["confirmation"])
    assert (conf.job_id, conf.chat_id, conf.state, conf.command) == (
        review.id, "dashboard", "pending", EVERY_BUT_3.to_dict())
    assert len(d.rig.posting.calls) == 1   # nothing runs before Yes
    r = confirm(d, conf.id)
    assert r.json() == {"state": "posted", "texts": ["Posted …"], "command": EVERY_BUT_3_LINE}
    assert d.rig.posting.calls[-1] == ("post_review", SCOPE, review.id, EVERY_BUT_3)
    assert d.replies.announced == [(review.id, "post", "posted")] * 2
    assert confirm(d, conf.id).json() == HANDLED
    assert len(d.rig.posting.calls) == 2


def test_an_expired_dashboard_confirmation_runs_nothing(dash):                        # Review Focus 5
    d = dash
    conf_id = act(d, d.rig.review.id, text=ASK).json()["confirmation"]
    d.rig.clock[0] += timedelta(minutes=16)
    r = confirm(d, conf_id)
    assert r.json() == {"state": "refused", "texts": ["Expired: reply again; nothing was done."]}
    assert d.rig.posting.calls == [] and d.replies.announced == []
    assert confirm(d, conf_id).json() == HANDLED   # spent


def test_a_confirmation_runs_until_its_ttl_is_over(dash):
    d = dash
    conf_id = act(d, d.rig.review.id, text=ASK).json()["confirmation"]
    d.rig.clock[0] += CONFIRM_TTL
    assert confirm(d, conf_id).json()["state"] == "posted" and len(d.rig.posting.calls) == 1


def test_no_cancels_and_a_second_answer_is_already_handled(dash):
    d = dash
    conf_id = act(d, d.rig.review.id, text=ASK).json()["confirmation"]
    assert confirm(d, conf_id, yes=False).json() == {"state": "refused", "texts": ["Cancelled: nothing was done."]}
    assert confirm(d, conf_id, yes=False).json() == HANDLED
    assert confirm(d, conf_id).json() == HANDLED
    assert d.rig.store.confirmation(conf_id).state == "declined"
    assert d.rig.posting.calls == [] and d.replies.announced == []


def test_a_yes_runs_against_the_job_as_it_is_now(dash):
    d = dash
    conf_id = act(d, d.rig.review.id, text=ASK).json()["confirmation"]
    d.rig.store.set_job_state(d.rig.review.id, "superseded", "new head", T1.isoformat())
    assert confirm(d, conf_id).json() == {
        "state": "refused", "texts": ["Outdated: there is a review of a newer head. Nothing was done."]}
    assert d.rig.posting.calls == [] and d.replies.announced == []


def test_confirm_takes_the_dashboards_confirmations_only(dash):
    d = dash
    telegram = d.rig.store.add_confirmation(d.rig.review.id, CHAT, EVERY_BUT_3.to_dict(), T1.isoformat())
    for conf_id in (telegram.id, "no-such-confirmation"):
        for yes in (True, False):
            r = confirm(d, conf_id, yes)
            assert r.status_code == 404 and r.json()["error"]
    assert d.rig.store.confirmation(telegram.id).state == "pending"   # Telegram's echo still answers
    assert d.rig.posting.calls == []


def test_free_text_goes_to_the_model_only_where_the_layer_lets_it(dash):             # Ruling 9
    d = dash
    r = act(d, d.rig.job.id, text="please push it now")
    assert r.json() == {"state": "refused", "texts": [not_understood_line("address")]}
    assert d.rig.parser.calls == [] and confirmations(d.rig) == 0


def test_an_understood_command_the_layer_refuses_asks_nothing(dash):
    d = dash
    d.rig.failing.add(f"forge:{SCOPE}")   # understand() passes it; check() refuses it
    r = act(d, d.rig.review.id, text=ASK)
    assert r.json() == {"state": "refused",
                        "texts": ["The forge token is failing (see the dashboard): nothing posted."]}
    d.rig.parser.results = [ReplyCommand("post", (12,))]
    r = act(d, d.rig.review.id, text=ASK)
    assert r.json() == {"state": "refused", "texts": ["Unknown number: 12. Nothing was done."]}
    assert len(d.rig.parser.calls) == 2 and confirmations(d.rig) == 0 and d.rig.posting.calls == []


def test_only_what_changed_something_is_announced(dash):
    d = dash
    shown = act(d, d.rig.review.id, command={"action": "text", "numbers": [2]}).json()
    assert (shown["state"], len(shown["texts"]), shown["command"]) == ("shown", 1, "text 2")
    d.rig.posting.outcomes = {"push_fix": PostOutcome("refused", "The fix touches …")}
    r = act(d, d.rig.job.id, command={"action": "push"})
    assert r.json() == {"state": "refused", "texts": ["The fix touches …"], "command": "push"}
    assert d.replies.announced == []
    r = act(d, d.rig.review.id, command={"action": "review_again"})
    assert r.json() == {"state": "queued", "texts": [f"Queued a review of #{N} at {NEW_HEAD[:9]}."],
                        "command": "review again"}
    assert d.replies.announced == [(d.rig.review.id, "review_again", "queued")]


def test_the_action_completes_when_the_announcement_fails(dash, caplog):              # Review Focus 3
    d = dash
    d.replies.error = RuntimeError("telegram down: ghp_not_real")
    r = act(d, d.rig.job.id, command={"action": "push"})
    assert r.status_code == 200 and r.json() == {"state": "posted", "texts": ["Pushed …"], "command": "push"}
    assert d.replies.announced == [(d.rig.job.id, "push", "posted")]
    assert any("not announced in Telegram: RuntimeError" in rec.getMessage() for rec in caplog.records)
    assert "ghp_not_real" not in caplog.text


@pytest.mark.parametrize("body", [
    {},                                                               # neither a command nor a text
    {"command": {"action": "push"}, "text": "push"},
    {"command": "push"},
    {"command": {}},
    {"command": {"action": "deploy"}},
    {"command": {"action": "push", "force": True}},
    {"command": {"action": "post", "numbers": "1 2"}},
    {"command": {"action": "post", "numbers": [1, "2"]}},
    {"command": {"action": "post", "numbers": [True]}},
    {"command": {"action": "post", "numbers": [1], "event": "MERGE"}},
    {"command": {"action": "push", "anyway": "yes"}},
    {"command": {"action": "again", "instruction": 7}},
    {"text": ""},
    {"text": "   "},
    {"text": 7},
    {"command": {"action": "push"}, "job_id": ""},
    {"command": {"action": "push"}, "job_id": 7},
    {"command": {"action": "push"}, "number": "1712"},
])
def test_a_malformed_action_is_400(dash, body):
    d = dash
    r = d.c.post("/api/reviews/action", json={"scope": SCOPE, "number": N, "job_id": d.rig.job.id, **body})
    assert r.status_code == 400 and r.json()["error"]
    assert d.rig.posting.calls == [] and d.rig.parser.calls == [] and d.replies.announced == []


@pytest.mark.parametrize("body", [
    b"not json", b"[1]", b"{}", b'{"confirmation": "c1"}', b'{"confirmation": "c1", "yes": "true"}',
    b'{"confirmation": 5, "yes": true}', b'{"confirmation": "", "yes": true}',
])
def test_a_malformed_confirm_is_400(dash, body):
    d = dash
    r = d.c.post("/api/reviews/confirm", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]


@pytest.mark.parametrize("path", ["/api/reviews/action", "/api/reviews/confirm"])
@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/plain"},
    {"Content-Type": "application/json", "Origin": "http://evil.example"},
    {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
])
def test_a_cross_site_command_is_refused(dash, path, headers):
    d = dash
    conf = d.rig.store.add_confirmation(d.rig.review.id, "dashboard", EVERY_BUT_3.to_dict(), T1.isoformat())
    body = {"scope": SCOPE, "number": N, "job_id": d.rig.job.id, "command": {"action": "push"},
            "confirmation": conf.id, "yes": True}
    r = d.c.post(path, content=json.dumps(body).encode(), headers=headers)
    assert r.status_code == 403 and r.json() == {"error": "cross-site request refused"}
    assert d.rig.posting.calls == [] and d.rig.store.confirmation(conf.id).state == "pending"


def test_the_command_fields_are_reply_commands(dash):
    """A button may send any field of ReplyCommand.to_dict() and no other."""
    assert set(reviews_api._COMMAND_FIELDS) == set(ReplyCommand("push").to_dict())


def test_create_app_serves_the_command_routes_with_the_layer_only(store, tmp_path):
    rig = address_rig(tmp_path)
    c = TestClient(create_app(EventBus(), store, reviewer=FakeService(), reviewer_actions=rig.replies.actions))
    r = c.post("/api/reviews/action", json={"scope": SCOPE, "number": N, "job_id": rig.job.id,
                                            "command": {"action": "push"}})
    assert r.json() == {"state": "posted", "texts": ["Posted …"], "command": "push"}   # no replies: no announcement
    replies = FakeReplies()
    c = TestClient(create_app(EventBus(), store, reviewer=FakeService(), reviewer_actions=rig.replies.actions,
                              reviewer_replies=replies))
    c.post("/api/reviews/action", json={"scope": SCOPE, "number": N, "job_id": rig.job.id,
                                        "command": {"action": "push"}})
    assert replies.announced == [(rig.job.id, "push", "posted")] and len(rig.posting.calls) == 2
    c = TestClient(create_app(EventBus(), store, reviewer=FakeService()))
    for path in ("/api/reviews/action", "/api/reviews/confirm"):
        assert c.post(path, json={}).status_code == 404

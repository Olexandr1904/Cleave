from __future__ import annotations

import dataclasses

import pytest
from starlette.testclient import TestClient

from config.schemas import ReviewerConfig
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.models import BoardRow, Job
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches

HEAD = "0123456789abcdef0123456789abcdef01234567"
STAMP = "2026-11-12T10:00:00+00:00"
PR_KEYS = {
    "number", "title", "url", "ticket_key", "roles", "my_turns", "other_turns", "pipeline",
    "draft", "changed_files", "head", "job", "last_change", "skipped",
}


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

    def overview(self):
        return [{
            "scope": "acme/app", "status": "ok", "reason": "",
            "last_poll_at": STAMP, "next_poll_at": "2026-11-12T10:05:00+00:00",
            "rows": self.rows, "jobs": {1: _job(1)}, "skipped": set(),
        }]

    def review_now(self, scope, number):
        self.calls.append(("review_now", scope, number))
        if self.review_now_error is not None:
            raise self.review_now_error
        return _job(number)

    def skip(self, scope, number):
        self.calls.append(("skip", scope, number))
        if self.skip_error is not None:
            raise self.skip_error


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
    assert [p["number"] for p in repo["my_turn"]] == [1]
    assert [p["number"] for p in repo["waiting"]] == [3, 2]
    pr = repo["my_turn"][0]
    assert set(pr) == PR_KEYS
    assert pr["head"] == HEAD[:9] and len(pr["head"]) == 9
    assert pr["job"] == {"id": "j1", "kind": "review", "state": "queued", "head": HEAD[:9]}
    assert len(pr["job"]["head"]) == 9
    assert pr["my_turns"] == ["review"]
    assert pr["other_turns"] == [{"login": "dev", "action": "address_review"}]
    assert (pr["ticket_key"], pr["roles"], pr["pipeline"]) == ("ACME-1", ["reviewer"], False)
    assert repo["waiting"][1]["pipeline"] is True
    assert repo["waiting"][0]["job"] is None


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


@pytest.mark.parametrize("path", ["/api/reviews/review-now", "/api/reviews/skip"])
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


@pytest.mark.parametrize("path", ["/api/reviews/review-now", "/api/reviews/skip"])
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


def test_routes_absent_without_service(store):
    c = TestClient(create_app(EventBus(), store))
    assert c.get("/api/reviews").status_code == 404
    r = c.post("/api/reviews/review-now", json={"scope": "acme/app", "number": 1})
    assert r.status_code == 404

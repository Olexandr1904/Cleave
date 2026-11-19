# tests/unit/test_reviewer_service.py
from __future__ import annotations
import asyncio
import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
import pytest
from config.schemas import ProcessPartConfig, ReviewerConfig
from dashboard.events import EventBus
from orchestrator.reviewer import service as service_module
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.clone import CloneError, ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import (
    Anchor, BoardRow, BoardSnapshot, JobRun, Post, PRDetails, RepoState, SnapshotPR, Turn,
)
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.report import Report, ReportItem, parse_report
from orchestrator.reviewer.service import RepoContext, ReviewerService, _why_not_fixable
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches
from tests.unit.test_reviewer_report import HEAD as RHEAD, REPORT

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
A = "a" * 40
DOC = "docs/review/code-review-process.md"
CFG = ReviewerConfig(enabled=True, base_branch="develop", process=ProcessPartConfig(doc=DOC))
DOC_LINE = f"[acme/app] reviews are off: {DOC} is not on develop."

class FakeClone:
    def __init__(self, path, doc=True, fail=None):
        self.path, self.doc, self.fail = path, doc, fail
        self.lock = asyncio.Lock()
    async def refresh(self):
        if self.fail: raise self.fail
        return "c" * 40
    def has_file(self, rel): return self.doc

class FakeReadiness:
    def __init__(self, snapshot=None, fail=None): self.snapshot, self.fail = snapshot, fail
    async def board(self, clone_dir):
        if self.fail: raise self.fail
        return self.snapshot

class FakeForge:
    def __init__(self, details): self.d = details
    async def open_prs(self): return {n: "t" for n in self.d}
    async def details(self, n, u): return self.d[n]
    async def reviewed_by(self, n, login, u): return False

class Untouchable:
    """Records every attribute read: a poll must not reach this part."""
    def __init__(self, name, touched): self._name, self._touched = name, touched
    def __getattr__(self, attr):
        self._touched.append(f"{self._name}.{attr}")
        raise AttributeError(attr)

def snap(*prs, me="octo"):
    return BoardSnapshot(me, tuple(prs))

def spr(n, turns, author="octo-author"):
    return SnapshotPR(n, f"Fix {n} ACME-{n}", f"u{n}", author, False, False, tuple(turns), {})

def det(n, head=A, author="octo-author", assignees=(), requested=("octo",), files=14):
    return PRDetails(n, f"Fix {n} ACME-{n}", f"u{n}", author, "open", head, False, tuple(assignees),
                     tuple(requested), files, "t")

def review_snap():
    return snap(spr(1, [Turn("octo", "review")]))

def review_pr():
    return FakeReadiness(review_snap()), FakeForge({1: det(1)})

def ctx(tmp_path, readiness, forge, clone=None, repo="app"):
    return RepoContext("acme", repo, CFG, clone or FakeClone(tmp_path / repo), readiness, forge)

def untouchable_ctx(repo, touched):
    parts = (Untouchable(name, touched) for name in ("clone", "readiness", "forge"))
    return RepoContext("acme", repo, CFG, *parts)

class EndsRound(FakeReadiness):
    """Sets `stop` while its repo is polled: run() finishes that round, then returns."""
    def __init__(self, stop, snapshot): super().__init__(snapshot); self.stop = stop
    async def board(self, clone_dir):
        self.stop.set()
        return await super().board(clone_dir)

class CountingClone(FakeClone):
    refreshes = 0
    async def refresh(self):
        self.refreshes += 1
        return await super().refresh()

class CountingReadiness(FakeReadiness):
    boards = 0
    async def board(self, clone_dir):
        self.boards += 1
        return await super().board(clone_dir)

async def run_briefly(service):
    """run() for 50 ms: its first round, then its wait. Raises if run() itself raises."""
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    await asyncio.wait_for(service.run(stop), 5)

def locked(*args):
    raise sqlite3.OperationalError("database is locked")

class Rig:
    """A service on a real store and switches in tmp_path, a settable clock and recorded lines."""
    def __init__(self, tmp_path, *contexts, failing=lambda key: False, send=None, bus=None,
                 compared=lambda scope: frozenset()):
        conn = connect(tmp_path / "rv" / "reviewer.db")
        self.store, self.switches = ReviewerStore(conn), Switches(conn)
        self.lines, self.t = [], T0
        self.service = ReviewerService(list(contexts), self.store, self.switches, failing,
                                       send or self._send, lambda project_id, repo_id: set(),
                                       event_bus=bus, now=lambda: self.t, compared_tickets=compared)
    async def _send(self, line, **anchor): self.lines.append(line)
    def state(self, scope="acme/app"): return self.store.repo_state(scope)

async def test_poll_builds_board_and_queues_review(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.store.board("acme/app")[1].head_first_seen_at == T0.isoformat()
    assert rig.store.jobs("acme/app") == []
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    jobs = rig.store.jobs("acme/app")
    assert [(j.number, j.kind, j.state, j.head_sha) for j in jobs] == [(1, "review", "queued", A)]
    s = rig.state()
    assert (s.status, s.reason, s.failures, s.first_poll_done) == ("ok", "", 0, True)
    assert (s.last_poll_at, s.last_ok_at) == (rig.t.isoformat(), rig.t.isoformat())
    assert s.next_poll_at == (rig.t + timedelta(seconds=300)).isoformat()

@pytest.mark.parametrize("title, compared, queued", [
    ("ACME-1: Fix 1 [staged]", {"acme/app": frozenset({"ACME-1"})}, []),
    ("ACME-1: Fix 1 [skill]", {"acme/app": frozenset({"ACME-1"})}, []),
    ("ACME-1: Fix 1 [staged]", {}, [1]),                                    # the verdict closed it
    ("ACME-1: Fix 1", {"acme/app": frozenset({"ACME-1"})}, [1]),             # not one of the two PRs
])
async def test_a_compared_tickets_pr_gets_no_review_until_the_verdict(tmp_path, title, compared, queued):
    c = ctx(tmp_path, FakeReadiness(review_snap()), FakeForge({1: dataclasses.replace(det(1), title=title)}))
    rig = Rig(tmp_path, c, compared=lambda scope: compared.get(scope, frozenset()))
    await rig.service.poll(c)
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    assert [j.number for j in rig.store.jobs("acme/app")] == queued
    assert 1 in rig.store.board("acme/app")      # still on the board

async def test_first_poll_sends_no_turn_lines(tmp_path):
    merge = snap(spr(2, [Turn("octo", "merge")], author="octo"))
    r = FakeReadiness(merge)
    c = ctx(tmp_path, r, FakeForge({2: det(2, author="octo", requested=())}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.lines == []
    r.snapshot = snap(spr(2, [], author="octo"))
    await rig.service.poll(c)
    r.snapshot = merge
    await rig.service.poll(c)
    assert rig.lines == ["[acme/app] #2: your turn — merge · u2"]

async def test_token_failing_skips_poll(tmp_path):
    touched, keys = [], []
    c = untouchable_ctx("app", touched)
    rig = Rig(tmp_path, c, failing=lambda key: keys.append(key) or True)
    await rig.service.poll(c)
    s = rig.state()
    assert touched == [] and keys == ["forge:acme/app"] and rig.lines == []
    assert (s.status, s.reason) == ("token_failing", "forge token failing")
    assert s.last_poll_at == T0.isoformat()
    assert s.next_poll_at == (T0 + timedelta(seconds=300)).isoformat()

async def test_readiness_failure_keeps_board_and_backs_off(tmp_path):
    r, forge = review_pr()
    c = ctx(tmp_path, r, forge)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    board = rig.store.board("acme/app")
    r.fail = ReadinessError("exit 3: boom")
    await rig.service.poll(c)
    s = rig.state()
    assert (s.status, s.reason, s.failures) == ("readiness_failed", "exit 3: boom", 1)
    assert s.next_poll_at == (T0 + timedelta(seconds=600)).isoformat()
    rig.t = T0 + timedelta(seconds=600)
    await rig.service.poll(c)
    s = rig.state()
    assert (s.status, s.reason, s.failures) == ("readiness_failed", "exit 3: boom", 2)
    assert s.next_poll_at == (rig.t + timedelta(seconds=1200)).isoformat()
    assert rig.store.board("acme/app") == board and s.first_poll_done
    assert rig.lines == ["[acme/app] readiness command failed: exit 3: boom."
                         " Keeping the last board; next try in 10 min."]

@pytest.mark.parametrize("error,reason", [
    (CloneError("git fetch failed: no route"), "git fetch failed: no route"),
    (ForgeError("GitHub 502"), "GitHub 502"),
    (KeyError("head"), "KeyError"),   # anything unexpected: the class name only
])
async def test_other_failures_are_errors_with_a_poll_failed_line(tmp_path, error, reason):
    r, forge = review_pr()
    clone = FakeClone(tmp_path / "app")
    c = ctx(tmp_path, r, forge, clone=clone)
    rig = Rig(tmp_path, c)
    clone.fail = error   # raised from the clone: the error's class alone decides the status
    await rig.service.poll(c)
    s = rig.state()
    assert (s.status, s.reason, s.failures) == ("error", reason, 1)
    assert s.next_poll_at == (T0 + timedelta(seconds=600)).isoformat()
    assert rig.lines == [f"[acme/app] poll failed: {reason}. Keeping the last board;"
                         " next try in 10 min."]
    clone.fail = None
    await rig.service.poll(c)
    assert (rig.state().status, rig.state().failures) == ("ok", 0)
    clone.fail = error
    await rig.service.poll(c)   # a new streak: one more line
    assert len(rig.lines) == 2

async def test_backoff_is_capped(tmp_path):
    c = ctx(tmp_path, FakeReadiness(fail=ReadinessError("exit 1: no")), FakeForge({}))
    rig = Rig(tmp_path, c)
    rig.store.save_repo_state(RepoState("acme/app", failures=5))
    await rig.service.poll(c)
    assert rig.state().next_poll_at == (T0 + timedelta(seconds=3600)).isoformat()

async def test_backoff_never_below_the_poll_interval(tmp_path):
    c = RepoContext("acme", "app", dataclasses.replace(CFG, poll_interval_seconds=7200),
                    FakeClone(tmp_path / "app"), FakeReadiness(fail=ReadinessError("exit 1: no")),
                    FakeForge({}))
    rig = Rig(tmp_path, c)   # max_backoff_seconds: the default 3600
    await rig.service.poll(c)
    assert rig.state().next_poll_at == (T0 + timedelta(seconds=7200)).isoformat()
    assert rig.lines == ["[acme/app] readiness command failed: exit 1: no."
                         " Keeping the last board; next try in 120 min."]

async def test_without_a_target_notices_stay_unsent(tmp_path, caplog):
    c = ctx(tmp_path, *review_pr(), clone=FakeClone(tmp_path / "app", doc=False))
    rig = Rig(tmp_path, c)
    no_target = ReviewerService([c], rig.store, rig.switches, lambda key: False, None,
                                lambda project_id, repo_id: set(), now=lambda: T0)
    with caplog.at_level(logging.WARNING):
        await no_target.poll(c)
    assert not rig.store.notice_sent("acme/app", 0, "doc_missing", DOC)
    assert "not sent" not in caplog.text
    await rig.service.poll(c)   # a later start that has a target sends it
    assert rig.lines == [DOC_LINE]

async def test_cancelled_poll_propagates(tmp_path):
    c = ctx(tmp_path, FakeReadiness(fail=asyncio.CancelledError()), FakeForge({}))
    rig = Rig(tmp_path, c)
    with pytest.raises(asyncio.CancelledError):
        await rig.service.poll(c)

async def test_doc_missing_no_jobs_one_line(tmp_path):
    clone = FakeClone(tmp_path / "app", doc=False)
    c = ctx(tmp_path, *review_pr(), clone=clone)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    s = rig.state()
    assert rig.store.jobs("acme/app") == [] and list(rig.store.board("acme/app")) == [1]
    assert (s.status, s.reason) == ("doc_missing", f"{DOC} not on develop")
    assert rig.lines == [DOC_LINE]
    clone.doc = True   # the doc lands: reviews run, the notice is cleared
    await rig.service.poll(c)
    assert rig.state().status == "ok" and len(rig.store.jobs("acme/app")) == 1
    clone.doc = False  # and goes again: a new line
    await rig.service.poll(c)
    assert rig.lines == [DOC_LINE, DOC_LINE]

async def test_reviews_paused_no_jobs_but_board(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    rig.switches.set("reviews", "paused", "acme/app")
    await rig.service.poll(c)
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    assert list(rig.store.board("acme/app")) == [1] and rig.store.jobs("acme/app") == []
    assert rig.state().status == "ok"

async def test_review_now_and_skip(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the head was just seen: the quiet window is not over
    assert rig.store.jobs("acme/app") == []
    job = rig.service.review_now("acme/app", 1)
    assert (job.number, job.kind, job.head_sha, job.key, job.state) == (1, "review", A, f"{A}:now", "queued")
    with pytest.raises(ValueError, match="already queued"):
        rig.service.review_now("acme/app", 1)
    rig.service.skip("acme/app", 1)
    [skipped] = rig.store.jobs("acme/app")
    assert (skipped.id, skipped.state, skipped.reason) == (job.id, "skipped", "skipped by operator")
    assert rig.store.is_skipped("acme/app", 1, A)
    rig.switches.set("reviews", "paused", "acme/app")
    assert rig.service.review_now("acme/app", 1).state == "queued"   # skip and switch do not apply
    with pytest.raises(LookupError):
        rig.service.review_now("acme/app", 99)
    with pytest.raises(LookupError):
        rig.service.skip("acme/app", 99)

async def test_review_now_supersedes_done_and_stale_at_head(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    now = T0.isoformat()
    done = rig.store.add_job("acme/app", 1, "review", A, A, now)
    stale = rig.store.add_job("acme/app", 1, "review", A, A, now)
    old = rig.store.add_job("acme/app", 1, "review", "b" * 40, "b" * 40, now)
    recheck = rig.store.add_job("acme/app", 1, "recheck", A, f"{A}:T1", now)
    for j, state in ((done, "done"), (stale, "stale"), (old, "done"), (recheck, "done")):
        rig.store.set_job_state(j.id, state, "", now)
    new = rig.service.review_now("acme/app", 1)
    by_id = {j.id: (j.state, j.reason) for j in rig.store.jobs("acme/app")}
    assert by_id[done.id] == by_id[stale.id] == ("superseded", "new review")
    assert by_id[old.id] == by_id[recheck.id] == ("done", "")
    assert by_id[new.id] == ("queued", "")

async def test_review_now_supersedes_posted(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    now = T0.isoformat()
    posted = rig.store.add_job("acme/app", 1, "review", A, A, now)
    rig.store.set_job_state(posted.id, "posted", "", now)
    new = rig.service.review_now("acme/app", 1)
    got = rig.store.job(posted.id)
    assert (got.state, got.reason) == ("superseded", "new review")
    assert (new.key, new.state) == (f"{A}:now", "queued") and new.id != posted.id

async def test_failing_repo_does_not_block_others(tmp_path):
    stop = asyncio.Event()
    bad = ctx(tmp_path, FakeReadiness(fail=ReadinessError("exit 1: no")), FakeForge({}), repo="web")
    good = ctx(tmp_path, EndsRound(stop, review_snap()), FakeForge({1: det(1)}))
    rig = Rig(tmp_path, bad, good)
    await asyncio.wait_for(rig.service.run(stop), 5)
    assert rig.state("acme/web").status == "readiness_failed"
    assert rig.state("acme/app").status == "ok"

async def test_run_polls_only_due_repos(tmp_path):
    stop, touched = asyncio.Event(), []
    due = ctx(tmp_path, EndsRound(stop, review_snap()), FakeForge({1: det(1)}))
    rig = Rig(tmp_path, untouchable_ctx("web", touched), due)
    later = (T0 + timedelta(seconds=1)).isoformat()
    rig.store.save_repo_state(RepoState("acme/web", next_poll_at=later))
    await asyncio.wait_for(rig.service.run(stop), 5)
    assert touched == [] and rig.state("acme/app").status == "ok"

async def test_send_line_failure_is_harmless(tmp_path, caplog):
    async def broken(line, **anchor): raise RuntimeError("chat 12345 unreachable")
    r = FakeReadiness(snap(spr(2, [], author="octo")))
    c = ctx(tmp_path, r, FakeForge({2: det(2, author="octo", requested=())}))
    rig = Rig(tmp_path, c, send=broken)
    await rig.service.poll(c)
    r.snapshot = snap(spr(2, [Turn("octo", "merge")], author="octo"))
    with caplog.at_level(logging.WARNING):
        await rig.service.poll(c)
    assert rig.state().status == "ok" and rig.store.board("acme/app")[2].my_actions == ["merge"]
    assert "RuntimeError" in caplog.text and "12345" not in caplog.text

async def test_notice_sent_again_after_a_failed_send(tmp_path):
    sent, failures = [], [RuntimeError("down")]
    async def flaky(line, **anchor):
        if failures: raise failures.pop()
        sent.append(line)
    c = ctx(tmp_path, *review_pr(), clone=FakeClone(tmp_path / "app", doc=False))
    rig = Rig(tmp_path, c, send=flaky)
    for _ in range(3):
        await rig.service.poll(c)
    assert sent == [DOC_LINE]

async def test_events(tmp_path):
    bus = EventBus()
    r, forge = review_pr()
    c = ctx(tmp_path, r, forge)
    rig = Rig(tmp_path, c, bus=bus)
    await rig.service.poll(c)
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    r.snapshot = snap(spr(1, []))   # the turn goes: the queued job is cancelled
    await rig.service.poll(c)
    r.fail = ReadinessError("exit 3: boom")
    await rig.service.poll(c)
    scope = {"scope": "acme/app"}
    assert [(e.event_type, e.project_id, e.data) for e in reversed(bus.recent())] == [
        ("reviewer_poll", "acme", {**scope, "prs": 1}),
        ("reviewer_turn", "acme", {**scope, "number": 1, "action": "review"}),
        ("reviewer_poll", "acme", {**scope, "prs": 1}),
        ("reviewer_job", "acme", {**scope, "number": 1, "kind": "review", "state": "queued"}),
        ("reviewer_poll", "acme", {**scope, "prs": 1}),
        ("reviewer_job", "acme", {**scope, "number": 1, "kind": "review", "state": "cancelled"}),
        ("reviewer_poll_failed", "acme", {**scope, "reason": "exit 3: boom"}),
    ]

async def test_overview(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    rig.store.add_job("acme/app", 1, "review", A, A, T0.isoformat())
    later = (T0 + timedelta(minutes=1)).isoformat()
    latest = rig.store.add_job("acme/app", 1, "recheck", A, f"{A}:T1", later)
    [repo] = rig.service.overview()
    assert {k: repo[k] for k in ("scope", "status", "reason", "last_poll_at", "next_poll_at")} == {
        "scope": "acme/app", "status": "ok", "reason": "", "last_poll_at": T0.isoformat(),
        "next_poll_at": (T0 + timedelta(seconds=300)).isoformat()}
    assert [r.number for r in repo["rows"]] == [1] and repo["jobs"] == {1: latest}
    assert repo["skipped"] == set()
    rig.service.skip("acme/app", 1)
    assert rig.service.overview()[0]["skipped"] == {1}

# --- Fix round 1 ---

async def test_unrecorded_poll_is_not_repeated_before_max_backoff(tmp_path, monkeypatch):
    clone = CountingClone(tmp_path / "app")
    c = ctx(tmp_path, *review_pr(), clone=clone)
    rig = Rig(tmp_path, c)
    monkeypatch.setattr(rig.store, "save_repo_state", locked)
    await run_briefly(rig.service)
    assert clone.refreshes == 1
    rig.t = T0 + timedelta(seconds=3599)
    await run_briefly(rig.service)
    assert clone.refreshes == 1
    rig.t = T0 + timedelta(seconds=3600)
    await run_briefly(rig.service)
    assert clone.refreshes == 2

async def test_run_survives_a_store_it_cannot_read(tmp_path, monkeypatch):
    reads = []
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    monkeypatch.setattr(rig.store, "repo_state", lambda scope: reads.append(scope) or locked())
    await run_briefly(rig.service)   # run() returns at stop instead of raising
    assert 1 <= len(reads) <= 2      # the due check and one poll: no spin

async def test_store_failure_is_logged_as_not_recorded(tmp_path, monkeypatch, caplog):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    monkeypatch.setattr(rig.store, "repo_state", locked)
    with caplog.at_level(logging.WARNING):
        await rig.service.poll(c)
    assert "Reviewer poll of acme/app not recorded: OperationalError at " in caplog.text
    assert "test_reviewer_service.py:" in caplog.text and "database is locked" not in caplog.text

async def test_interval_is_floored_at_60_s(tmp_path):
    r, forge = review_pr()
    c = RepoContext("acme", "app", dataclasses.replace(CFG, poll_interval_seconds=0),
                    FakeClone(tmp_path / "app"), r, forge)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.state().next_poll_at == (T0 + timedelta(seconds=60)).isoformat()
    r.fail = ReadinessError("exit 1: no")
    await rig.service.poll(c)
    assert rig.state().next_poll_at == (T0 + timedelta(seconds=120)).isoformat()   # 60 * 2 ** 1

async def test_store_error_after_the_last_write_keeps_the_poll_ok(tmp_path, monkeypatch):
    c = ctx(tmp_path, FakeReadiness(review_snap()), FakeForge({1: det(1, files=40)}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    rig.t = T0 + timedelta(minutes=31)
    monkeypatch.setattr(rig.store, "mark_notice", locked)
    await rig.service.poll(c)
    s = rig.state()
    assert (s.status, s.failures, s.last_ok_at) == ("ok", 0, rig.t.isoformat())
    assert rig.lines == ["[acme/app] #1 is large (40 files) — no automatic review · u1"]

async def test_failed_ok_save_continues_the_failure_streak(tmp_path, monkeypatch):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    streak = RepoState("acme/app", status="error", reason="GitHub 502", failures=2)
    rig.store.save_repo_state(streak)
    save = rig.store.save_repo_state
    def no_ok(state):
        if state.status == "ok":
            locked()
        save(state)
    monkeypatch.setattr(rig.store, "save_repo_state", no_ok)
    await rig.service.poll(c)
    s = rig.state()
    assert (s.status, s.reason, s.failures) == ("error", "OperationalError", 3)
    assert s.next_poll_at == (T0 + timedelta(seconds=2400)).isoformat()   # 300 * 2 ** 3
    assert rig.lines == []   # not the first failure of the streak

class Notifier:
    """A Telegram notifier whose message ids count up from 501."""
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        self.sent.append(text)
        return 500 + len(self.sent)

async def test_large_notice_anchored_with_pr(tmp_path):
    def board(merge_turns):
        return snap(spr(1730, [Turn("octo", "review")]), spr(2, merge_turns, author="octo"))
    r = FakeReadiness(board([]))
    c = ctx(tmp_path, r, FakeForge({1730: det(1730, files=40), 2: det(2, author="octo", requested=())}))
    rig = Rig(tmp_path, c)
    notifier = Notifier()
    chat = ReviewerChat(notifier, "-1001", store=rig.store, now=lambda: rig.t)
    service = ReviewerService([c], rig.store, rig.switches, lambda key: False, chat.send,
                              lambda project_id, repo_id: set(), now=lambda: rig.t)
    await service.poll(c)
    rig.t = T0 + timedelta(minutes=31)   # the large PR's quiet window is over, and a merge turn comes
    r.snapshot = board([Turn("octo", "merge")])
    await service.poll(c)
    assert notifier.sent == ["[acme/app] #2: your turn — merge · u2",
                             "[acme/app] #1730 is large (40 files) — no automatic review · u1730"]
    assert rig.store.anchor("-1001", 501) == Anchor("", "acme/app", 0)   # a turn line: the repo's
    assert rig.store.anchor("-1001", 502) == Anchor("", "acme/app", 1730)

async def test_next_poll_far_ahead_counts_as_due(tmp_path):
    clone = CountingClone(tmp_path / "app")
    c = ctx(tmp_path, *review_pr(), clone=clone)
    rig = Rig(tmp_path, c)
    ahead = (T0 + timedelta(days=1)).isoformat()   # the clock went back after it was written
    rig.store.save_repo_state(RepoState("acme/app", next_poll_at=ahead))
    await run_briefly(rig.service)
    assert clone.refreshes == 1

async def test_unexpected_error_logs_class_and_frame_only(tmp_path, caplog):
    clone = FakeClone(tmp_path / "app", fail=KeyError("hidden-detail"))
    c = ctx(tmp_path, *review_pr(), clone=clone)
    rig = Rig(tmp_path, c)
    with caplog.at_level(logging.WARNING):
        await rig.service.poll(c)
    assert "KeyError at " in caplog.text and "test_reviewer_service.py:" in caplog.text
    assert "hidden-detail" not in caplog.text

# --- Plan 4: deferred jobs, the clone lock, the operator's login ---

def deferred_review(rig):
    job = rig.store.add_job("acme/app", 1, "review", A, A, T0.isoformat())
    rig.store.set_job_state(job.id, "deferred", "", T0.isoformat())
    return job

async def test_review_now_refuses_with_deferred(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    deferred_review(rig)
    with pytest.raises(ValueError, match="already queued"):
        rig.service.review_now("acme/app", 1)

async def test_skip_drops_deferred(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    job = deferred_review(rig)
    rig.service.skip("acme/app", 1)
    [skipped] = rig.store.jobs("acme/app")
    assert (skipped.id, skipped.state, skipped.reason) == (job.id, "skipped", "skipped by operator")

async def test_poll_waits_for_a_job_preparing(tmp_path):
    """A job holds the review clone only for Prepare's git there: the poll waits for it, then refreshes and reads."""
    clone = CountingClone(tmp_path / "app")
    readiness = CountingReadiness(review_snap())
    c = ctx(tmp_path, readiness, FakeForge({1: det(1)}), clone=clone)
    rig = Rig(tmp_path, c)
    await clone.lock.acquire()   # a job's Prepare
    asyncio.get_running_loop().call_later(0.05, clone.lock.release)
    await asyncio.wait_for(rig.service.poll(c), 5)
    assert (clone.refreshes, readiness.boards) == (1, 1) and rig.state().status == "ok"

async def test_poll_leaves_a_clone_held_past_its_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "_CLONE_WAIT_SECONDS", 0.01)
    clone = CountingClone(tmp_path / "app")
    readiness = CountingReadiness(review_snap())
    c = ctx(tmp_path, readiness, FakeForge({1: det(1)}), clone=clone)
    rig = Rig(tmp_path, c)
    async with clone.lock:   # held past the poll's wait (a post's dry run and confirm, say)
        await run_briefly(rig.service)
    assert (clone.refreshes, readiness.boards) == (0, 0)
    assert rig.state().last_poll_at == "" and rig.store.board("acme/app") == {}   # nothing recorded
    rig.t = T0 + timedelta(seconds=4)
    await run_briefly(rig.service)
    assert readiness.boards == 0   # tried again a few seconds later, not at once
    rig.t = T0 + timedelta(seconds=5)
    await run_briefly(rig.service)
    assert (clone.refreshes, readiness.boards) == (1, 1) and rig.state().status == "ok"

async def test_a_post_holding_the_clone_does_not_hold_up_other_repos(tmp_path, monkeypatch):
    """A post holds the review clone through its runs: the poll of that repo waits for it at most a bound and
    leaves the repo for later, and the next repo is polled all the same."""
    monkeypatch.setattr(service_module, "_CLONE_WAIT_SECONDS", 0.05)
    clone = ReviewClone(tmp_path / "app", "file:///nowhere", "develop", "tok")
    inside, done, stop = (asyncio.Event() for _ in range(3))
    async def post():
        async with clone.reader():
            inside.set()
            await done.wait()
    readiness = CountingReadiness(review_snap())
    busy = ctx(tmp_path, readiness, FakeForge({1: det(1)}), clone=clone)
    rig = Rig(tmp_path, busy, ctx(tmp_path, EndsRound(stop, review_snap()), FakeForge({1: det(1)}), repo="web"))
    task = asyncio.create_task(post())
    await inside.wait()
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(rig.service.run(stop), 5)
    finally:
        done.set()
        await task
    assert loop.time() - started < 1
    assert readiness.boards == 0 and rig.state("acme/app").last_poll_at == ""   # left for later
    assert rig.state("acme/web").status == "ok"

async def test_poll_waits_at_most_a_bound_on_a_lock_that_looks_free(tmp_path, monkeypatch):
    """locked() reads False while a queued post is about to take the lock: the poll waits for it at most a
    bound, then leaves the repo for later."""
    monkeypatch.setattr(service_module, "_CLONE_WAIT_SECONDS", 0.05)
    clone, readiness = CountingClone(tmp_path / "app"), CountingReadiness(review_snap())
    c = ctx(tmp_path, readiness, FakeForge({1: det(1)}), clone=clone)
    rig = Rig(tmp_path, c)
    queued, inside, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def post():   # a post's lease, queued behind the previous holder
        queued.set()
        async with clone.lock:
            inside.set()
            await done.wait()
    await clone.lock.acquire()
    task = asyncio.create_task(post())
    await queued.wait()
    clone.lock.release()
    assert not clone.lock.locked()   # looks free; the post takes it first
    try:
        await asyncio.wait_for(rig.service.poll(c), 5)
    finally:
        done.set()
        await task
    assert inside.is_set() and (clone.refreshes, readiness.boards) == (0, 0)
    assert rig.state().last_poll_at == ""   # left for later

async def test_poll_records_me(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.store.repo_state("acme/app").me == "octo"

# --- Plan 4: the PR detail view ---

DETAIL_KEYS = {"row", "jobs", "report_job", "report", "replies_job", "replies", "tg_lines",
               "report_text", "number_states", "reply_states", "posts", "fix", "fix_patch", "pushed_sha",
               "verdicts_job", "verdicts"}
JOB_KEYS = {"id", "kind", "state", "reason", "head", "created_at", "updated_at", "retry_at",
            "started_at", "finished_at", "duration_s", "cost_usd", "turns", "message_ids", "message_sent",
            "notes"}

def finished_job(rig, tmp_path, state, report_text=None, report=None, kind="review", at=T0, **run):
    """A job at RHEAD in `state` with a saved run; its copy holds `report_text` when given."""
    job = rig.store.add_job("acme/app", 1, kind, RHEAD, RHEAD, at.isoformat())
    rig.store.set_job_state(job.id, state, "", at.isoformat())
    job_dir = tmp_path / "jobs" / job.id
    job_dir.mkdir(parents=True)
    if report_text is not None:
        (job_dir / f"report_{RHEAD[:9]}.md").write_text(report_text)
    if run.get("replies"):   # the plan's copy, as the runner keeps it
        (job_dir / "replies_plan.json").write_text(json.dumps(run["replies"]))
    rig.store.save_run(JobRun(job.id, job_dir=str(job_dir), report=report or {}, **run))
    return job

async def test_detail_shapes(tmp_path):
    c = ctx(tmp_path, FakeReadiness(review_snap()), FakeForge({1: det(1, head=RHEAD)}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the board row
    job = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict(),
                       started_at=T0.isoformat(),
                       finished_at=(T0 + timedelta(seconds=120)).isoformat(),
                       cost_usd=2.0, turns=14, message_ids=[501, 502], tg_lines={"1": "npe"})
    d = rig.service.detail("acme/app", 1)
    assert set(d) == DETAIL_KEYS
    assert (d["row"]["number"], d["row"]["head_sha"]) == (1, RHEAD)
    [j] = d["jobs"]
    assert set(j) == JOB_KEYS
    assert j["duration_s"] == 120
    assert (j["id"], j["kind"], j["state"], j["head"], j["cost_usd"], j["turns"]) == (
        job.id, "review", "done", RHEAD[:9], 2.0, 14)
    assert j["message_ids"] == [501, 502]
    assert d["report_job"] == job.id
    assert d["report"]["items"][0]["number"] == 1
    assert d["report_text"].startswith("# Review")
    assert (d["replies_job"], d["replies"], d["tg_lines"]) == (None, [], {"1": "npe"})

async def test_detail_without_report(tmp_path):
    rig = Rig(tmp_path)
    job = rig.store.add_job("acme/app", 1, "review", RHEAD, RHEAD, T0.isoformat())
    d = rig.service.detail("acme/app", 1)   # no board row: its job alone finds the PR
    assert d["row"] is None and [j["id"] for j in d["jobs"]] == [job.id]
    assert (d["jobs"][0]["state"], d["jobs"][0]["duration_s"]) == ("queued", None)
    assert (d["report_job"], d["report"], d["report_text"]) == (None, None, None)

async def test_detail_shows_unreadable_report(tmp_path):
    rig = Rig(tmp_path)
    finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    unreadable = "# Review: no event\n" + "x" * 300_000
    failed = finished_job(rig, tmp_path, "failed", unreadable, at=T0 + timedelta(minutes=5))
    d = rig.service.detail("acme/app", 1)
    assert d["report_job"] == failed.id   # the newest report, parsed or not
    assert d["report"] is None
    assert d["report_text"] == unreadable[:200_000]

async def test_detail_replies_from_newest_recheck(tmp_path):
    rig = Rig(tmp_path)
    old = [{"n": 1, "thread_id": "T1", "body": "Old."}]
    new = [{"n": 2, "thread_id": "T2", "body": "Fixed, thanks."}]
    finished_job(rig, tmp_path, "done", kind="recheck", replies=old)
    newest = finished_job(rig, tmp_path, "done", kind="recheck", replies=new,
                          at=T0 + timedelta(minutes=5))
    d = rig.service.detail("acme/app", 1)
    assert (d["replies_job"], d["replies"]) == (newest.id, new)

# --- Fix round 1 (Task 10) ---

async def test_detail_prefers_the_report_named_by_the_head(tmp_path):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    (tmp_path / "jobs" / job.id / "report_0.md").write_text("# Decoy")   # sorts first
    d = rig.service.detail("acme/app", 1)
    assert (d["report_job"], d["report_text"]) == (job.id, REPORT)

async def test_detail_skips_a_directory_named_like_a_report(tmp_path):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "failed")   # its copy holds no report_<head9>.md
    job_dir = tmp_path / "jobs" / job.id
    (job_dir / "report_0.md").mkdir()
    (job_dir / "report_0.md" / "inside.md").write_text("# Inside")
    d = rig.service.detail("acme/app", 1)
    assert (d["report_job"], d["report_text"]) == (None, None)
    (job_dir / "report_1.md").write_text("# Review: misnamed")
    d = rig.service.detail("acme/app", 1)
    assert (d["report_job"], d["report_text"]) == (job.id, "# Review: misnamed")

async def test_detail_never_takes_a_recheck_copy_as_the_report(tmp_path):
    rig = Rig(tmp_path)
    review = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    finished_job(rig, tmp_path, "done", "# Review: in a re-check copy", kind="recheck",
                 at=T0 + timedelta(minutes=5))
    d = rig.service.detail("acme/app", 1)
    assert (d["report_job"], d["report_text"]) == (review.id, REPORT)

async def test_detail_replies_survive_a_running_recheck(tmp_path):
    rig = Rig(tmp_path)
    drafted = [{"n": 1, "thread_id": "T1", "body": "Fixed, thanks."}]
    done = finished_job(rig, tmp_path, "done", kind="recheck", replies=drafted)
    finished_job(rig, tmp_path, "running", kind="recheck", at=T0 + timedelta(minutes=5),
                 started_at=(T0 + timedelta(minutes=5)).isoformat())
    d = rig.service.detail("acme/app", 1)
    assert (d["replies_job"], d["replies"]) == (done.id, drafted)

async def test_detail_unknown_pr(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(LookupError):
        rig.service.detail("acme/app", 1)

# --- Final fix wave ---

async def test_review_now_is_still_a_review_at_its_head_for_the_poll(tmp_path):
    """Its key marks it (Review now), but plan 3's rules see a review at that head: done, it keeps the poll
    from queueing another; queued, a new head supersedes it."""
    r, forge = review_pr()
    c = ctx(tmp_path, r, forge)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    job = rig.service.review_now("acme/app", 1)
    rig.store.set_job_state(job.id, "done", "", T0.isoformat())
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    assert [j.id for j in rig.store.jobs("acme/app")] == [job.id]
    rig.store.set_job_state(job.id, "queued", "", T0.isoformat())
    forge.d[1] = det(1, head="b" * 40)
    await rig.service.poll(c)
    assert (rig.store.job(job.id).state, rig.store.job(job.id).reason) == ("superseded", "new head")

def recheck_with_plan(rig, tmp_path, state, plan=True, at=T0):
    job = finished_job(rig, tmp_path, state, kind="recheck", at=at)
    if plan:
        (tmp_path / "jobs" / job.id / "replies_plan.json").write_text("[]")
    return job

@pytest.mark.parametrize("state", ["superseded", "cancelled", "deferred", "stale", "queued"])
async def test_detail_replies_only_from_a_run_with_a_plan(tmp_path, state):
    """A re-check superseded or cancelled at Prepare, or deferred by the usage limit, has a stored run but
    no plan: it never hides the last drafted replies."""
    rig = Rig(tmp_path)
    drafted = [{"n": 1, "thread_id": "T1", "body": "Fixed, thanks."}]
    done = finished_job(rig, tmp_path, "done", kind="recheck", replies=drafted)
    recheck_with_plan(rig, tmp_path, state, plan=False, at=T0 + timedelta(minutes=5))
    d = rig.service.detail("acme/app", 1)
    assert (d["replies_job"], d["replies"]) == (done.id, drafted)

async def test_detail_replies_never_from_a_failed_run(tmp_path):
    rig = Rig(tmp_path)
    done = recheck_with_plan(rig, tmp_path, "done")
    recheck_with_plan(rig, tmp_path, "failed", at=T0 + timedelta(minutes=5))   # a plan it could not read
    assert rig.service.detail("acme/app", 1)["replies_job"] == done.id

async def test_detail_survives_an_unreadable_report(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    real_open = type(tmp_path).open
    def refusing(path, *args, **kwargs):
        if path.name.startswith("report_"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(type(tmp_path), "open", refusing)
    d = rig.service.detail("acme/app", 1)
    assert (d["report_job"], d["report_text"]) == (job.id, None)
    assert d["report"]["items"][0]["number"] == 1   # the parsed report still shows

@pytest.mark.parametrize("started_at, finished_at", [
    ("not a time", T0.isoformat()), (T0.isoformat(), "2026-13-40T99:00:00"), (T0.isoformat(), 5)])
async def test_detail_survives_malformed_run_times(tmp_path, started_at, finished_at):
    rig = Rig(tmp_path)
    finished_job(rig, tmp_path, "done", started_at=started_at, finished_at=finished_at)
    [j] = rig.service.detail("acme/app", 1)["jobs"]
    assert j["duration_s"] is None

# --- Task 12: the Reviews tab redesign ---

async def test_overview_job_info(tmp_path):
    """The latest job of each PR as the board shows it: its run's start and delivery, its next try and
    reason, and a review's findings counted by kind."""
    three = snap(*(spr(n, [Turn("octo", "review")]) for n in (1, 2, 3)))
    c = ctx(tmp_path, FakeReadiness(three), FakeForge({n: det(n) for n in (1, 2, 3)}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the board rows; the quiet window keeps it from queueing
    done = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict(),
                        started_at=T0.isoformat(), message_ids=[501, 502], message_sent=True)
    retry = (T0 + timedelta(minutes=48)).isoformat()
    deferred = rig.store.add_job("acme/app", 2, "review", A, A, T0.isoformat())
    rig.store.set_job_state(deferred.id, "deferred", "usage limit reached", T0.isoformat(), retry_at=retry)
    rig.store.add_job("acme/app", 3, "review", A, A, T0.isoformat())   # queued, no run yet
    [repo] = rig.service.overview()
    assert repo["jobs"][1].id == done.id
    assert repo["job_info"][1] == {
        "retry_at": "", "started_at": T0.isoformat(), "reason": "", "message_sent": True,
        "findings": {"critical": 1, "major": 1, "minor": 1, "question": 2, "preexisting": 1,
                     "dropped": 3}, "fix": False,
    }
    assert repo["job_info"][2] == {
        "retry_at": retry, "started_at": "", "reason": "usage limit reached", "message_sent": False,
        "findings": None, "fix": False,
    }
    assert repo["job_info"][3]["findings"] is None

async def test_overview_findings_only_for_a_review_with_a_parsed_report(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the board row
    finished_job(rig, tmp_path, "failed", "# Review: no event")   # a report that did not parse
    assert rig.service.overview()[0]["job_info"][1]["findings"] is None
    finished_job(rig, tmp_path, "done", kind="recheck", replies=[{"n": 1, "thread_id": "T1", "body": "x"}],
                 report=parse_report(REPORT, RHEAD).to_dict(), at=T0 + timedelta(minutes=5))
    assert rig.service.overview()[0]["job_info"][1]["findings"] is None

async def test_overview_flags_large_prs(tmp_path):
    snapshot = snap(spr(1, [Turn("octo", "review")]), spr(2, [Turn("octo", "review")]))
    c = ctx(tmp_path, FakeReadiness(snapshot), FakeForge({1: det(1, files=40), 2: det(2, files=39)}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.service.overview()[0]["large"] == {1}   # CFG.max_files_auto is 40

async def test_detail_jobs_carry_message_sent(tmp_path):
    rig = Rig(tmp_path)
    finished_job(rig, tmp_path, "done", message_ids=[501], message_sent=True)
    finished_job(rig, tmp_path, "done", message_ids=[502], at=T0 + timedelta(minutes=5))
    assert [j["message_sent"] for j in rig.service.detail("acme/app", 1)["jobs"]] == [False, True]

# --- Code-review fix wave 2 ---

async def test_review_now_survives_the_turn_going(tmp_path):
    r, forge = review_pr()
    c = ctx(tmp_path, r, forge)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    job = rig.service.review_now("acme/app", 1)
    r.snapshot = snap(spr(1, []))   # the PR now waits on others: the review turn is gone
    await rig.service.poll(c)
    assert (rig.store.job(job.id).state, rig.store.job(job.id).reason) == ("queued", "")
    rig.service.skip("acme/app", 1)   # a skip still drops it
    assert rig.store.job(job.id).state == "skipped"

async def test_overview_reads_runs_only_for_prs_on_the_board(tmp_path, monkeypatch):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # PR 1 on the board
    on_board = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    rig.store.add_job("acme/app", 9, "review", A, A, T0.isoformat())   # a PR closed since: off the board
    read, real = [], rig.store.run
    monkeypatch.setattr(rig.store, "run", lambda job_id: read.append(job_id) or real(job_id))
    [repo] = rig.service.overview()
    assert read == [on_board.id] and set(repo["job_info"]) == {1}

# --- Plan 5 Task 17: number states and posts ---

def landed(item, where="inline"):
    """A report item as a Post records it once it landed."""
    return {"n": item["number"], "path": item["path"], "line": item["line"], "title": item["title"], "where": where}

def post(rig, post_id, job, state, at=T0, kind="review", **fields):
    p = Post(post_id, job.id, job.scope, job.number, job.head_sha, kind, state, at.isoformat(), **fields)
    rig.store.save_post(p)
    return p

async def test_detail_number_states(tmp_path):
    rig = Rig(tmp_path)
    report = parse_report(REPORT, RHEAD).to_dict()
    review = finished_job(rig, tmp_path, "posted", REPORT, report)
    post(rig, "p1", review, "posted", numbers=[1], landed=[landed(report["items"][0])], comments=1)
    post(rig, "p2", review, "failed", at=T0 + timedelta(minutes=1), numbers=[1, 4], error="GitHub head moved")
    replies = [{"n": 1, "thread_id": "T1", "body": "Fixed, thanks."}, {"n": 3, "thread_id": "T3", "body": "Done."}]
    recheck = finished_job(rig, tmp_path, "done", kind="recheck", replies=replies, at=T0 + timedelta(minutes=5))
    post(rig, "p3", recheck, "posted", at=T0 + timedelta(minutes=6), kind="replies", numbers=[1],
         landed=[{"n": 1, "path": "", "line": None, "title": "", "where": "reply"}])
    d = rig.service.detail("acme/app", 1)
    assert d["number_states"] == {"1": "posted", "2": "open", "3": "open", "4": "ticked", "5": "open", "6": "open"}
    assert d["reply_states"] == {"1": "posted", "3": "open"}

async def test_detail_reads_the_posts_once(tmp_path, monkeypatch):
    """The posts detail() loads are the ones the number states come from (/code-review C15)."""
    rig = Rig(tmp_path)
    report = parse_report(REPORT, RHEAD).to_dict()
    review = finished_job(rig, tmp_path, "posted", REPORT, report)
    post(rig, "p1", review, "posted", numbers=[1], landed=[landed(report["items"][0])], comments=1)
    replies = [{"n": 1, "thread_id": "T1", "body": "Fixed, thanks."}]
    recheck = finished_job(rig, tmp_path, "done", kind="recheck", replies=replies, at=T0 + timedelta(minutes=5))
    post(rig, "p2", recheck, "posted", at=T0 + timedelta(minutes=6), kind="replies", numbers=[1],
         landed=[{"n": 1, "path": "", "line": None, "title": "", "where": "reply"}])
    reads, real = [], rig.store.posts
    monkeypatch.setattr(rig.store, "posts", lambda scope, number: reads.append((scope, number)) or real(scope, number))
    d = rig.service.detail("acme/app", 1)
    assert reads == [("acme/app", 1)]
    assert (d["number_states"]["1"], d["number_states"]["2"], d["reply_states"]) == ("posted", "open", {"1": "posted"})

@pytest.mark.parametrize("state", ["posting", "failed", "partial", "unverified", "interrupted"])
async def test_detail_ticked_in_a_post_of_that_job_that_did_not_land(tmp_path, state):
    rig = Rig(tmp_path)
    report = parse_report(REPORT, RHEAD).to_dict()
    older = finished_job(rig, tmp_path, "superseded", REPORT, report)
    review = finished_job(rig, tmp_path, "posted", REPORT, report, at=T0 + timedelta(minutes=5))
    post(rig, "p1", older, "failed", numbers=[3])   # another job's post: no tick here
    post(rig, "p2", review, state, at=T0 + timedelta(minutes=6), numbers=[1, 2],
         landed=[landed(report["items"][0])] if state == "partial" else [])
    states = rig.service.detail("acme/app", 1)["number_states"]
    assert (states["1"], states["2"], states["3"]) == ("posted" if state == "partial" else "ticked", "ticked", "open")

async def test_detail_posted_at_the_report_head_only(tmp_path):
    rig = Rig(tmp_path)
    report = parse_report(REPORT, RHEAD).to_dict()
    review = finished_job(rig, tmp_path, "posted", REPORT, report)
    elsewhere = dataclasses.replace(review, head_sha="b" * 40)   # the same finding, landed at another head
    post(rig, "p1", elsewhere, "posted", numbers=[2], landed=[landed(report["items"][1], "moved")])
    assert rig.service.detail("acme/app", 1)["number_states"]["2"] == "open"

async def test_detail_without_report_or_replies_has_no_states(tmp_path):
    rig = Rig(tmp_path)
    rig.store.add_job("acme/app", 1, "review", RHEAD, RHEAD, T0.isoformat())
    d = rig.service.detail("acme/app", 1)
    assert (d["number_states"], d["reply_states"], d["posts"]) == ({}, {}, [])

async def test_detail_posts(tmp_path):
    rig = Rig(tmp_path)
    review = finished_job(rig, tmp_path, "posted", REPORT, parse_report(REPORT, RHEAD).to_dict())
    url = "https://github.com/acme-org/acme-app/pull/1#pullrequestreview-9"
    post(rig, "p1", review, "failed", numbers=[4], error="GitHub head moved")
    post(rig, "p2", review, "posted", at=T0 + timedelta(minutes=1), numbers=[1, 2], event="REQUEST_CHANGES",
         urls=[url], comments=2, review_ids=[9], summary_posted=True)
    post(rig, "p3", dataclasses.replace(review, number=2), "posted")   # another PR's
    assert rig.service.detail("acme/app", 1)["posts"] == [
        {"id": "p2", "kind": "review", "state": "posted", "job_id": review.id, "head": RHEAD[:9],
         "created_at": (T0 + timedelta(minutes=1)).isoformat(), "numbers": [1, 2], "event": "REQUEST_CHANGES",
         "urls": [url], "comments": 2, "error": ""},
        {"id": "p1", "kind": "review", "state": "failed", "job_id": review.id, "head": RHEAD[:9],
         "created_at": T0.isoformat(), "numbers": [4], "event": "", "urls": [], "comments": 0,
         "error": "GitHub head moved"},
    ]

async def test_overview_latest_ignores_small_jobs(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the board row
    review = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    for n, kind in enumerate(("edit", "draft"), start=1):
        at = (T0 + timedelta(minutes=n)).isoformat()   # newer than the review
        small = rig.store.add_job("acme/app", 1, kind, RHEAD, f"{RHEAD}:{kind}", at)
        rig.store.set_job_state(small.id, "done", "", at)
    [repo] = rig.service.overview()
    assert repo["jobs"][1].id == review.id
    assert repo["job_info"][1]["findings"]["critical"] == 1

async def test_report_text_prefers_base(tmp_path):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "done", REPORT, parse_report(REPORT, RHEAD).to_dict())
    base = tmp_path / "jobs" / job.id / "posting" / "report.base.md"
    base.parent.mkdir()
    edited = REPORT.replace("NPE on an empty list", "NPE on an empty cart")
    base.write_text(edited)
    d = rig.service.detail("acme/app", 1)
    assert (d["report_job"], d["report_text"]) == (job.id, edited)


# --- Address jobs ---

ADDR_CFG = dataclasses.replace(CFG, process=ProcessPartConfig(doc=DOC, address_doc="docs/threads.md"))

async def test_answered_threads_are_the_threads_of_the_landed_replies(tmp_path):
    rig = Rig(tmp_path)
    replies = [{"n": 1, "thread_id": "T1", "body": "x"}, {"n": 2, "thread_id": "T2", "body": "y"},
               {"n": 3, "review_id": 7, "body": "z"}]
    job = finished_job(rig, tmp_path, "posted", kind="address", replies=replies)
    post(rig, "p1", job, "posted", kind="replies", numbers=[1, 3],
         landed=[{"n": 1, "path": "", "line": None, "title": "", "where": "reply"},
                 {"n": 3, "path": "", "line": None, "title": "", "where": "comment"}])
    assert rig.service._answered_threads(job) == frozenset({"T1"})
    assert rig.service._answered_threads(rig.store.add_job("acme/app", 1, "address", A, f"{A}:", T0.isoformat())) \
        == frozenset()

async def test_poll_queues_an_address_job_on_the_operators_pr(tmp_path):
    mine = snap(spr(1, [Turn("octo", "answer_threads", ("T2", "T1"))], author="octo"))
    c = RepoContext("acme", "app", ADDR_CFG, FakeClone(tmp_path / "app"), FakeReadiness(mine),
                    FakeForge({1: det(1, author="octo", requested=())}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    rig.t = T0 + timedelta(minutes=31)
    await rig.service.poll(c)
    assert [(j.kind, j.key) for j in rig.store.jobs("acme/app")] == [("address", f"{A}:T1,T2")]
    await rig.service.poll(c)
    assert len(rig.store.jobs("acme/app")) == 1

async def test_address_again_supersedes_and_queues_with_the_instruction(tmp_path):
    turns = [Turn("octo", "answer_threads", ("T2",)), Turn("octo", "resolve_threads", ("T1",))]
    c = RepoContext("acme", "app", ADDR_CFG, FakeClone(tmp_path / "app"),
                    FakeReadiness(snap(spr(1, turns, author="octo"))), FakeForge({1: det(1, author="octo", requested=())}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the board row
    now = T0.isoformat()
    done = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T1", now)
    rig.store.set_job_state(done.id, "done", "", now)
    failed = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T2", now)
    rig.store.set_job_state(failed.id, "failed", "boom", now)
    new = rig.service.address_again("acme/app", 1, "leave 2 as it is")
    assert (rig.store.job(done.id).state, rig.store.job(done.id).reason) == ("superseded", "new address")
    assert rig.store.job(failed.id).state == "failed"
    assert (new.kind, new.head_sha, new.key, new.state) == ("address", A, f"{A}:T1,T2", "queued")
    assert rig.store.run(new.id).request == {"instruction": "leave 2 as it is"}
    with pytest.raises(ValueError, match="already queued"):
        rig.service.address_again("acme/app", 1, "")
    with pytest.raises(LookupError):
        rig.service.address_again("acme/app", 99, "")

ADDRESS_REPLIES = [{"n": 1, "thread_id": "T1", "body": "Fixed in {fix_sha}: x.", "resolve": True, "fixed": True}]
ADDRESS_FIX = {"files": [{"path": "a.kt", "added": 1, "deleted": 1}], "protected": [], "bytes": 9}

async def test_detail_of_an_address_job(tmp_path):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "done", kind="address", replies=ADDRESS_REPLIES, fix=ADDRESS_FIX)
    (tmp_path / "jobs" / job.id / "fix.patch").write_text("diff --git a/a.kt b/a.kt\n")
    d = rig.service.detail("acme/app", 1)
    assert (d["replies_job"], d["replies"], d["reply_states"]) == (job.id, ADDRESS_REPLIES, {"1": "open"})
    assert (d["fix"], d["fix_patch"], d["pushed_sha"]) == (ADDRESS_FIX, "diff --git a/a.kt b/a.kt\n", None)
    post(rig, "p1", job, "posted", kind="push", sha="b" * 40)
    assert rig.service.detail("acme/app", 1)["pushed_sha"] == "b" * 40

async def test_detail_of_an_address_job_without_a_fix_and_of_a_newer_recheck(tmp_path):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "done", kind="address", replies=ADDRESS_REPLIES)
    d = rig.service.detail("acme/app", 1)
    assert (d["replies_job"], d["fix"], d["fix_patch"], d["pushed_sha"]) == (job.id, None, None, None)
    newer = finished_job(rig, tmp_path, "done", kind="recheck", replies=[{"n": 1, "thread_id": "T1", "body": "x"}],
                         at=T0 + timedelta(minutes=5))
    d = rig.service.detail("acme/app", 1)
    assert (d["replies_job"], d["fix"], d["fix_patch"], d["pushed_sha"]) == (newer.id, None, None, None)

async def test_overview_shows_an_address_job(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)   # the board row
    job = finished_job(rig, tmp_path, "done", kind="address", replies=ADDRESS_REPLIES, fix=ADDRESS_FIX)
    [repo] = rig.service.overview()
    assert repo["jobs"][1].id == job.id
    assert (repo["job_info"][1]["fix"], repo["job_info"][1]["findings"]) == (True, None)

async def test_address_again_needs_an_address_turn(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    with pytest.raises(LookupError, match="no address turn"):
        rig.service.address_again("acme/app", 1, "")
    assert rig.store.jobs("acme/app") == []


# --- The Reviews board: the newest review, the login, Fix now ---

REVIEW_INFO_KEYS = {"id", "state", "head", "started_at", "finished_at", "duration_s", "cost_usd",
                    "message_sent", "findings"}


async def test_overview_carries_me_project_and_the_newest_review(tmp_path):
    c = ctx(tmp_path, FakeReadiness(review_snap()), FakeForge({1: det(1, head=RHEAD)}))
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    review = finished_job(rig, tmp_path, "done", started_at=T0.isoformat(),
                          finished_at=(T0 + timedelta(seconds=3610)).isoformat(), cost_usd=28.93)
    rig.store.add_job("acme/app", 1, "recheck", RHEAD, f"{RHEAD}:T1", (T0 + timedelta(minutes=5)).isoformat())
    [repo] = rig.service.overview()
    assert (repo["me"], repo["project_id"]) == ("octo", "acme")
    info = repo["reviews"][1]
    assert set(info) == REVIEW_INFO_KEYS
    assert (info["id"], info["state"], info["head"], info["duration_s"], info["cost_usd"]) == (
        review.id, "done", RHEAD[:9], 3610, 28.93)
    assert repo["jobs"][1].kind == "recheck"   # the row's job is still the newest of any kind


async def test_overview_has_no_review_for_a_pr_without_one(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.service.overview()[0]["reviews"] == {}


def fix_row(**changes):
    row = BoardRow(number=1, title="Fix 1 ACME-1", url="u1", author="octo", roles=["author"], head_sha=A,
                   head_first_seen_at=T0.isoformat(), draft=False, changed_files=3,
                   my_turns=[{"action": "answer_threads", "threads": ["T1"]}], other_turns=[],
                   threads_truncated=False, pipeline=False, ticket_key="ACME-1",
                   updated_at=T0.isoformat(), last_change=T0.isoformat())
    return dataclasses.replace(row, **changes)


@pytest.mark.parametrize("cfg, changes, held, why", [
    (ADDR_CFG, {}, frozenset(), ""),
    (ADDR_CFG, {"my_turns": [{"action": "resolve_threads", "threads": ["T1"]}]}, frozenset(), ""),
    (ADDR_CFG, {"my_turns": [{"action": "address_review", "threads": []}]}, frozenset(), ""),
    (CFG, {}, frozenset(), "no address_doc"),
    (ADDR_CFG, {"roles": ["reviewer"]}, frozenset(), "not your PR"),
    (ADDR_CFG, {"my_turns": [{"action": "request_review", "threads": []}]}, frozenset(), "no thread turn"),
    (ADDR_CFG, {"pipeline": True}, frozenset(), "a ticket workspace holds it"),
    (ADDR_CFG, {}, frozenset({1}), "held by a comparison"),
])
def test_why_not_fixable(cfg, changes, held, why):
    assert _why_not_fixable(cfg, fix_row(**changes), held) == why


def address_ctx(tmp_path, cfg):
    turns = [Turn("octo", "answer_threads", ("T2",)), Turn("octo", "resolve_threads", ("T1",))]
    return RepoContext("acme", "app", cfg, FakeClone(tmp_path / "app"),
                       FakeReadiness(snap(spr(1, turns, author="octo"))),
                       FakeForge({1: det(1, author="octo", requested=())}))


@pytest.mark.parametrize("cfg, fixable", [(ADDR_CFG, {1}), (CFG, set())])
async def test_overview_fixable_follows_the_rule(tmp_path, cfg, fixable):
    c = address_ctx(tmp_path, cfg)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.service.overview()[0]["fixable"] == fixable


async def test_address_now_queues_without_an_instruction(tmp_path):
    c = address_ctx(tmp_path, ADDR_CFG)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    now = T0.isoformat()
    done = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T1", now)
    rig.store.set_job_state(done.id, "done", "", now)
    failed = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T2", now)
    rig.store.set_job_state(failed.id, "failed", "boom", now)
    new = rig.service.address_now("acme/app", 1)
    assert (rig.store.job(done.id).state, rig.store.job(done.id).reason) == ("superseded", "new address")
    assert rig.store.job(failed.id).state == "failed"
    assert (new.kind, new.head_sha, new.key, new.state) == ("address", A, f"{A}:T1,T2", "queued")
    assert rig.store.run(new.id) is None   # no instruction


async def test_address_now_refuses(tmp_path):
    c = address_ctx(tmp_path / "addr", ADDR_CFG)
    rig = Rig(tmp_path / "addr", c)
    await rig.service.poll(c)
    rig.service.address_now("acme/app", 1)
    with pytest.raises(ValueError, match="already queued"):
        rig.service.address_now("acme/app", 1)
    with pytest.raises(LookupError):
        rig.service.address_now("acme/app", 99)
    plain = address_ctx(tmp_path / "plain", CFG)
    rig = Rig(tmp_path / "plain", plain)
    await rig.service.poll(plain)
    with pytest.raises(ValueError, match="not fixable: no address_doc"):
        rig.service.address_now("acme/app", 1)
    assert rig.store.jobs("acme/app") == []


async def test_overview_carries_the_newest_address_job_whatever_job_is_newer(tmp_path):
    c = address_ctx(tmp_path, ADDR_CFG)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    now = T0.isoformat()
    fix = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T1,T2", now)
    rig.store.set_job_state(fix.id, "done", "", now)
    rig.store.save_run(JobRun(fix.id, started_at=now, fix={"files": [], "protected": [], "bytes": 9}))
    rig.service.review_now("acme/app", 1)   # a self-review after the fix: the row's job is the review now
    [repo] = rig.service.overview()
    assert repo["jobs"][1].kind == "review"
    assert repo["fixes"][1] == {"id": fix.id, "kind": "address", "state": "done", "head": A[:9], "started_at": now,
                                "fix": True, "pushed": False, "posting": False}
    post(rig, "p1", fix, "posted", kind="push")
    post(rig, "p2", fix, "posting", kind="replies")
    assert {k: rig.service.overview()[0]["fixes"][1][k] for k in ("pushed", "posting")} == {
        "pushed": True, "posting": True}


async def test_address_now_refuses_while_a_post_of_its_fix_is_under_way(tmp_path):
    c = address_ctx(tmp_path, ADDR_CFG)
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    now = T0.isoformat()
    fix = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T1,T2", now)
    rig.store.set_job_state(fix.id, "done", "", now)
    post(rig, "p1", fix, "posting", kind="replies")
    with pytest.raises(ValueError, match="a post is under way"):
        rig.service.address_now("acme/app", 1)
    assert rig.store.job(fix.id).state == "done"


async def test_address_now_refuses_a_repo_no_longer_configured(tmp_path):
    c = address_ctx(tmp_path, ADDR_CFG)
    await Rig(tmp_path, c).service.poll(c)   # its board stays in the store
    with pytest.raises(LookupError):
        Rig(tmp_path).service.address_now("acme/app", 1)


# --- Findings jobs: Fix findings… and "fix" ---

B = "b" * 40


def self_review(rig, *kinds, head=A, state="done", stamp=None):
    """A review job of PR 1 at `head` with a report of these item kinds (see test_reviewer_report._report_dict)."""
    stamp = stamp or T0.isoformat()
    job = rig.store.add_job("acme/app", 1, "review", head, f"{head}:now", stamp)
    rig.store.set_job_state(job.id, state, "", stamp)
    items = tuple(ReportItem(i, k, f"t{i}", "a.kt", i, "", f"b{i}") for i, k in enumerate(kinds, 1))
    rig.store.save_run(JobRun(job.id, report=Report("T", head, "", None, "COMMENT", "", items, (), "", "").to_dict()))
    return job


async def findings_rig(tmp_path, cfg=ADDR_CFG, **kw):
    """Rig with the operator's own PR 1 at head A on the board (address_ctx), polled once."""
    c = address_ctx(tmp_path, cfg)
    rig = Rig(tmp_path, c, **kw)
    await rig.service.poll(c)
    return rig


async def test_fix_findings_queues_a_findings_job(tmp_path):
    rig = await findings_rig(tmp_path)
    review = self_review(rig, "red", "question", "yellow")
    old = rig.store.add_job("acme/app", 1, "findings", A, f"{A}:{review.id}", T0.isoformat())
    rig.store.set_job_state(old.id, "done", "", T0.isoformat())
    failed = rig.store.add_job("acme/app", 1, "findings", A, f"{A}:{review.id}", T0.isoformat())
    rig.store.set_job_state(failed.id, "failed", "boom", T0.isoformat())
    address = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T1,T2", T0.isoformat())
    rig.store.set_job_state(address.id, "done", "", T0.isoformat())
    job = rig.service.fix_findings("acme/app", 1, instruction="leave 3 as it is")
    assert (job.kind, job.head_sha, job.key, job.state) == ("findings", A, f"{A}:{review.id}", "queued")
    assert rig.store.run(job.id).request == {"review": review.id, "instruction": "leave 3 as it is"}
    assert (rig.store.job(old.id).state, rig.store.job(old.id).reason) == ("superseded", "new findings fix")
    assert rig.store.job(failed.id).state == "failed"
    assert rig.store.job(address.id).state == "done"      # the other fix kind is not superseded


async def test_fix_findings_without_an_instruction_and_with_the_reviews_id(tmp_path):
    rig = await findings_rig(tmp_path)
    review = self_review(rig, "orange", state="posted")
    job = rig.service.fix_findings("acme/app", 1, review_id=review.id)
    assert rig.store.run(job.id).request == {"review": review.id}


@pytest.mark.parametrize("setup, why", [
    ("no_address_doc", "no address_doc"),
    ("not_author", "not your PR"),
    ("pipeline", "a ticket workspace holds it"),
    ("held", "held by a comparison"),
    ("no_review", "no finished self-review at this head"),
    ("review_running", "no finished self-review at this head"),
    ("review_failed", "no finished self-review at this head"),
    ("review_of_old_head", "no finished self-review at this head"),
    ("questions_only", "no confirmed findings"),
    ("no_report", "no confirmed findings"),
])
async def test_fix_findings_refuses_what_is_not_fixable(tmp_path, setup, why):
    compared = {"acme/app": frozenset({"ACME-1"})} if setup == "held" else {}
    rig = await findings_rig(tmp_path, CFG if setup == "no_address_doc" else ADDR_CFG,
                             compared=lambda scope: compared.get(scope, frozenset()))
    row = rig.store.board("acme/app")[1]
    changes = {"not_author": {"roles": ["reviewer"]}, "pipeline": {"pipeline": True},
               "held": {"title": "ACME-1: Fix 1 [skill]", "ticket_key": "ACME-1"}}.get(setup)
    if changes:
        rig.store.replace_board("acme/app", {1: dataclasses.replace(row, **changes)})
    if setup == "review_running":
        self_review(rig, "red", state="running")
    elif setup == "review_failed":
        self_review(rig, "red", state="failed")
    elif setup == "review_of_old_head":
        self_review(rig, "red", head=B)
    elif setup == "questions_only":
        self_review(rig, "question", "preexisting")
    elif setup == "no_report":
        review = rig.store.add_job("acme/app", 1, "review", A, f"{A}:now", T0.isoformat())
        rig.store.set_job_state(review.id, "done", "", T0.isoformat())
    elif setup != "no_review":
        self_review(rig, "red")
    with pytest.raises(ValueError, match=f"^not fixable: {why}$"):
        rig.service.fix_findings("acme/app", 1)
    assert [j.kind for j in rig.store.jobs("acme/app", 1) if j.kind == "findings"] == []
    assert (1 in rig.service.overview()[0]["findings_fixable"]) is False


async def test_fix_findings_refuses_a_review_that_is_not_the_newest(tmp_path):
    rig = await findings_rig(tmp_path)
    older = self_review(rig, "red", stamp=T0.isoformat())
    newer = self_review(rig, "red", stamp=(T0 + timedelta(minutes=1)).isoformat())
    with pytest.raises(ValueError, match="^not the newest self-review$"):
        rig.service.fix_findings("acme/app", 1, review_id=older.id)
    assert [j for j in rig.store.jobs("acme/app", 1) if j.kind == "findings"] == []
    assert rig.service.fix_findings("acme/app", 1, review_id=newer.id).key == f"{A}:{newer.id}"


async def test_one_fix_of_a_pr_at_a_time(tmp_path):
    rig = await findings_rig(tmp_path)
    self_review(rig, "red")
    address = rig.service.address_now("acme/app", 1)                    # an address job pending
    with pytest.raises(ValueError, match="^already queued$"):
        rig.service.fix_findings("acme/app", 1)
    rig.store.set_job_state(address.id, "failed", "boom", T0.isoformat())
    findings = rig.service.fix_findings("acme/app", 1)                 # a findings job pending
    for state in ("queued", "running", "deferred"):
        rig.store.set_job_state(findings.id, state, "", T0.isoformat())
        for call in (lambda: rig.service.address_now("acme/app", 1),
                     lambda: rig.service.address_again("acme/app", 1, "x"),
                     lambda: rig.service.fix_findings("acme/app", 1)):
            with pytest.raises(ValueError, match="^already queued$"):
                call()
    assert [j.state for j in rig.store.jobs("acme/app", 1) if j.kind in ("address", "findings")] == [
        "deferred", "failed"]


@pytest.mark.parametrize("kind", ["address", "findings"])
async def test_no_fix_is_queued_while_a_push_is_under_way(tmp_path, kind):
    rig = await findings_rig(tmp_path)
    review = self_review(rig, "red")
    fix = rig.store.add_job("acme/app", 1, kind, A, f"{A}:{review.id}", T0.isoformat())
    rig.store.set_job_state(fix.id, "done", "", T0.isoformat())
    post(rig, "p1", fix, "posting", kind="push")
    for call in (lambda: rig.service.fix_findings("acme/app", 1),
                 lambda: rig.service.address_now("acme/app", 1),
                 lambda: rig.service.address_again("acme/app", 1, "x")):
        with pytest.raises(ValueError, match="^a post is under way$"):
            call()
    assert rig.store.job(fix.id).state == "done"
    assert len(rig.store.jobs("acme/app", 1)) == 2


async def test_fix_findings_lookup_errors(tmp_path):
    rig = await findings_rig(tmp_path)
    self_review(rig, "red")
    with pytest.raises(LookupError):
        rig.service.fix_findings("acme/app", 999)
    with pytest.raises(LookupError):     # its board outlived its configuration
        Rig(tmp_path).service.fix_findings("acme/app", 1)


async def test_overview_shows_findings_fixable_and_the_newest_fix_of_either_kind(tmp_path):
    rig = await findings_rig(tmp_path)
    assert rig.service.overview()[0]["findings_fixable"] == set()
    self_review(rig, "red")
    [repo] = rig.service.overview()
    assert repo["findings_fixable"] == {1}
    address = rig.store.add_job("acme/app", 1, "address", A, f"{A}:T1,T2", T0.isoformat())
    rig.store.set_job_state(address.id, "failed", "boom", T0.isoformat())
    job = rig.service.fix_findings("acme/app", 1)
    [repo] = rig.service.overview()
    assert (repo["fixes"][1]["id"], repo["fixes"][1]["kind"]) == (job.id, "findings")
    assert repo["findings_fixable"] == {1}         # a pending fix is the button's state, not the rule's


VERDICTS = [{"n": 1, "verdict": "FIXED", "why": "null check added"}]
FINDINGS_FIX = {"files": [{"path": "b.kt", "added": 2, "deleted": 0}], "protected": [], "bytes": 12}


async def test_detail_of_a_findings_job(tmp_path):
    rig = Rig(tmp_path)
    address = finished_job(rig, tmp_path, "done", kind="address", replies=ADDRESS_REPLIES, fix=ADDRESS_FIX)
    (tmp_path / "jobs" / address.id / "fix.patch").write_text("diff --git a/a.kt b/a.kt\n")
    job = finished_job(rig, tmp_path, "done", kind="findings", verdicts=VERDICTS, fix=FINDINGS_FIX,
                       at=T0 + timedelta(minutes=5))
    (tmp_path / "jobs" / job.id / "fix.patch").write_text("diff --git a/b.kt b/b.kt\n")
    d = rig.service.detail("acme/app", 1)
    assert (d["verdicts_job"], d["verdicts"]) == (job.id, VERDICTS)
    assert (d["fix"], d["fix_patch"], d["pushed_sha"]) == (FINDINGS_FIX, "diff --git a/b.kt b/b.kt\n", None)
    assert d["replies_job"] == address.id                      # the threads' replies still show
    post(rig, "p1", job, "posted", kind="push", sha="c" * 40)
    assert rig.service.detail("acme/app", 1)["pushed_sha"] == "c" * 40


async def test_detail_takes_the_fix_of_the_newer_kind(tmp_path):
    rig = Rig(tmp_path)
    job = finished_job(rig, tmp_path, "done", kind="findings", verdicts=VERDICTS, fix=FINDINGS_FIX)
    address = finished_job(rig, tmp_path, "done", kind="address", replies=ADDRESS_REPLIES, fix=ADDRESS_FIX,
                           at=T0 + timedelta(minutes=5))
    d = rig.service.detail("acme/app", 1)
    assert (d["verdicts_job"], d["verdicts"], d["fix"]) == (job.id, VERDICTS, ADDRESS_FIX)
    post(rig, "p1", address, "posted", kind="push", sha="d" * 40)
    assert rig.service.detail("acme/app", 1)["pushed_sha"] == "d" * 40


async def test_detail_without_a_findings_job(tmp_path):
    rig = Rig(tmp_path)
    finished_job(rig, tmp_path, "failed", kind="findings", verdicts=VERDICTS, fix=FINDINGS_FIX)
    d = rig.service.detail("acme/app", 1)
    assert (d["verdicts_job"], d["verdicts"], d["fix"]) == (None, [], None)

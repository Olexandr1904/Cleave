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
from orchestrator.reviewer.clone import CloneError
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import (
    BoardSnapshot, JobRun, PRDetails, RepoState, SnapshotPR, Turn,
)
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches
from tests.unit.test_reviewer_report import HEAD as RHEAD, REPORT

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
A = "a" * 40
DOC = "docs/review/code-review-process.md"
CFG = ReviewerConfig(enabled=True, base_branch="develop", process=ProcessPartConfig(doc=DOC))
DOC_LINE = f"[acme/app] reviews are off: {DOC} is not on develop."

class FakeClone:
    in_session = False
    def __init__(self, path, doc=True, fail=None):
        self.path, self.doc, self.fail = path, doc, fail
        self.lock, self.session_lock = asyncio.Lock(), asyncio.Lock()
    async def refresh(self):
        if self.fail: raise self.fail
        return "c" * 40
    def has_file(self, rel): return self.doc
    def changed_git_files(self): return []

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
    def __init__(self, tmp_path, *contexts, failing=lambda key: False, send=None, bus=None):
        conn = connect(tmp_path / "rv" / "reviewer.db")
        self.store, self.switches = ReviewerStore(conn), Switches(conn)
        self.lines, self.t = [], T0
        self.service = ReviewerService(list(contexts), self.store, self.switches, failing,
                                       send or self._send, lambda project_id, repo_id: set(),
                                       event_bus=bus, now=lambda: self.t)
    async def _send(self, line): self.lines.append(line)
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
    async def broken(line): raise RuntimeError("chat 12345 unreachable")
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
    async def flaky(line):
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

async def test_poll_skips_refresh_during_a_jobs_session(tmp_path):
    clone = CountingClone(tmp_path / "app")
    readiness = CountingReadiness(review_snap())
    c = ctx(tmp_path, readiness, FakeForge({1: det(1)}), clone=clone)
    rig = Rig(tmp_path, c)
    async with clone.lock:   # a job holds the clone, in its session phase
        clone.in_session = True
        await rig.service.poll(c)
    assert (clone.refreshes, readiness.boards) == (0, 1)
    assert rig.state().status == "ok"

class PreparingClone(CountingClone):
    """A clone a job holds outside its session phase (Prepare or cleanup); counts how often a poll looks."""
    looks = 0
    @property
    def in_session(self):
        self.looks += 1
        return False

async def test_poll_leaves_a_clone_a_job_prepares_or_cleans_up(tmp_path):
    clone = PreparingClone(tmp_path / "app")
    readiness = CountingReadiness(review_snap())
    c = ctx(tmp_path, readiness, FakeForge({1: det(1)}), clone=clone)
    rig = Rig(tmp_path, c)
    async with clone.lock:   # the job preparing it holds the clone
        await run_briefly(rig.service)
    assert (clone.refreshes, readiness.boards, clone.looks) == (0, 0, 1)   # no readiness, and no spin
    assert rig.state().last_poll_at == "" and rig.store.board("acme/app") == {}   # nothing recorded
    rig.t = T0 + timedelta(seconds=4)
    await run_briefly(rig.service)
    assert readiness.boards == 0   # tried again a few seconds later, not at once
    rig.t = T0 + timedelta(seconds=5)
    await run_briefly(rig.service)
    assert (clone.refreshes, readiness.boards) == (1, 1) and rig.state().status == "ok"

async def test_poll_records_me(tmp_path):
    c = ctx(tmp_path, *review_pr())
    rig = Rig(tmp_path, c)
    await rig.service.poll(c)
    assert rig.store.repo_state("acme/app").me == "octo"

# --- Plan 4: the PR detail view ---

DETAIL_KEYS = {"row", "jobs", "report_job", "report", "replies_job", "replies", "tg_lines",
               "report_text"}
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
                     "dropped": 3},
    }
    assert repo["job_info"][2] == {
        "retry_at": retry, "started_at": "", "reason": "usage limit reached", "message_sent": False,
        "findings": None,
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

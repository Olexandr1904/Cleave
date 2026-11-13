# tests/unit/test_reviewer_service.py
from __future__ import annotations
import asyncio
import dataclasses
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
import pytest
from config.schemas import ProcessPartConfig, ReviewerConfig
from dashboard.events import EventBus
from orchestrator.reviewer.clone import CloneError
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import BoardSnapshot, PRDetails, RepoState, SnapshotPR, Turn
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
A = "a" * 40
DOC = "docs/review/code-review-process.md"
CFG = ReviewerConfig(enabled=True, base_branch="develop", process=ProcessPartConfig(doc=DOC))
DOC_LINE = f"[acme/app] reviews are off: {DOC} is not on develop."

class FakeClone:
    def __init__(self, path, doc=True, fail=None): self.path, self.doc, self.fail = path, doc, fail
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
    assert (job.number, job.kind, job.head_sha, job.key, job.state) == (1, "review", A, A, "queued")
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

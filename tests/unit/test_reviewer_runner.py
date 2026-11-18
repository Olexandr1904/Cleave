# tests/unit/test_reviewer_runner.py
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import stat
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from config.schemas import ProcessPartConfig, ReviewerConfig, ReviewerJobConfig
from orchestrator.reviewer import runner as runner_module
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.fix import FixError
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.lines import drift_line, login_line
from orchestrator.reviewer.messages import address_message, recheck_message, review_message, split_message
from orchestrator.reviewer.models import (
    Anchor, BoardRow, BoardSnapshot, Job, JobRun, Post, PRDetails, PRFull, RepoState, SnapshotPR, Turn,
)
from orchestrator.reviewer.prepare import Prepared, PrepareError
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.runner import JobRunner, RunnerConfig, may_still_save, settle
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.session import SessionOutcome, address_prompt, recheck_prompt, review_prompt
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import fill
from orchestrator.switches import Switches
from tests.unit.test_reviewer_messages import REPLIES, TG, URL
from tests.unit.test_reviewer_messages import review as task4_review
from tests.unit.test_reviewer_report import HEAD, REPORT

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
SCOPE = "acme/app"
CHAT_ID = "-1001"
DOC = "docs/review/process.md"
CFG = ReviewerConfig(enabled=True, base_branch="develop", process=ProcessPartConfig(
    doc=DOC, run_dir="tools/review/runs/PR_{pr}", replies_plan="tools/threads/runs/PR_{pr}/replies_{head9}.json",
    worktree_dir="../review-wt/PR_{pr}", helper_scripts=["tools/review/*.py"], address_doc="docs/threads.md"))
JOBS = {"review": ReviewerJobConfig("opus", "xhigh", 5400), "recheck": ReviewerJobConfig("opus", "high", 3600),
        "address": ReviewerJobConfig("opus", "xhigh", 5400)}
ENV = {"HOME": "/home/octo", "GH_TOKEN": "ghp_not_real"}
PR = PRFull(1712, "Fix cart sync ACME-1234", "Keeps the cart in sync.", URL, "open", HEAD, "develop", 20, 10, 14)
CONFIG_TEXT = '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://github.com/acme-org/acme-app.git\n'
WAITS = [timedelta(minutes=m) for m in (1, 5, 15, 60)]  # before the 2nd to 5th delivery attempt
FAILED = "[acme/app] #1712: review failed — "
DRIFT = drift_line(SCOPE, DOC, "Prepare")
_HEAD_PART, _REST = REPORT.split("## Dropped (never posted)\n")
LONG_REPORT = (_HEAD_PART + "## Dropped (never posted)\n"
               + "".join(f"- {n}. Candidate {n} - {'too speculative to keep ' * 6}(xhigh)\n" for n in range(7, 67))
               + _REST[_REST.index("\n## ✅"):])  # 60 dropped items with long lines

def board_row(head=HEAD, actions=("review",), number=1712):
    turns = [{"action": a, "threads": ["T1"] if a == "recheck" else []} for a in actions]
    return BoardRow(number, "Fix cart sync", URL, "octo-author", ["reviewer"], head, T0.isoformat(), False, 14,
                    turns, [], False, False, "ACME-1234", T0.isoformat(), T0.isoformat())

def write_review(paths, report=REPORT, tg=TG):
    (paths.run_dir / f"report_{HEAD[:9]}.md").write_text(report, encoding="utf-8")
    (paths.run_dir / "tg_lines.json").write_text(json.dumps({str(n): line for n, line in tg.items()}))

class FakeClone(ReviewClone):
    """The review clone's locks and session phase as they are; its git calls faked."""
    def __init__(self, path):
        super().__init__(path, "file:///nowhere", "develop", "tok")
        self.left, self.removed = "", []
        (path / ".git").mkdir(parents=True)
        (path / ".git" / "config").write_text(CONFIG_TEXT)
    async def remove_worktree(self, path):
        self.removed.append((path, (self.path / ".git" / "config").read_text()))
        return self.left

class FakeForge:
    """The PR as GitHub has it now: open at HEAD unless a test changes it; counts its reads."""
    def __init__(self): self.pr, self.error, self.reads = PR, None, 0
    async def pr_full(self, number):
        self.reads += 1
        if self.error:
            raise self.error
        return self.pr

class FakeChat:
    """Message ids from 100; each message sent is anchored in the store, as ReviewerChat does."""
    chat_id = CHAT_ID
    def __init__(self, store, now):
        self.texts, self.next_id, self.fail, self.tries = [], 100, 0, 0
        self.store, self.now = store, now
    async def send(self, text, *, job_id="", scope="", number=0, reply_to=None, buttons=None):
        self.tries += 1
        if self.fail:
            self.fail -= 1
            raise RuntimeError("telegram down")
        self.texts.append(text)
        self.next_id += 1
        self.store.add_anchor(CHAT_ID, self.next_id - 1, job_id, self.now().isoformat(), scope=scope, number=number)
        return self.next_id - 1
    async def translations(self, texts):  # ReviewerChat without a translator
        return {}
    async def localize(self, text):
        return fill(text, {})

class Rig:
    """A runner on a real store and switches, a fixed clock, a fake clone, chat and event bus; prepare and
    run_session patched on the runner module (the fake session writes what `write` writes)."""
    def __init__(self, tmp_path, monkeypatch, repos=("app",), idle_seconds=0.01, chat=True, clone=FakeClone,
                 ledgers_dir=None, max_parallel=6, min_free_bytes=0):
        conn = connect(tmp_path / "rv" / "reviewer.db")
        self.store, self.switches = ReviewerStore(conn), Switches(conn)
        self.t, self.tmp, self.forge = T0, tmp_path, FakeForge()
        self.contexts = [RepoContext("acme", r, CFG, clone(tmp_path / f"clone-{r}"), None, self.forge)
                         for r in repos]
        self.clone = self.contexts[0].clone
        self.store.replace_board(SCOPE, {1712: board_row()})
        self.store.save_repo_state(RepoState(SCOPE, me="octo"))
        self.chat = FakeChat(self.store, lambda: self.t) if chat else None
        self.failing, self.auth, self.events = set(), [], []
        self.outcome = SessionOutcome("ok", cost_usd=1.25, turns=42)
        self.drift, self.prepare_error, self.prepares = False, None, 0
        self.write, self.during, self.session_kw, self.paths = write_review, None, None, None
        self.runner = JobRunner(
            self.contexts, self.store, self.switches, self.failing.__contains__,
            lambda key, status: self.auth.append((key, status)), self.chat, object(), {"acme": "tracker"},
            RunnerConfig(tmp_path / "rv" / "jobs", [tmp_path / ".env"], JOBS, ledgers_dir,
                         max_parallel=max_parallel, min_free_bytes=min_free_bytes),
            event_bus=self, now=lambda: self.t, environ=ENV, idle_seconds=idle_seconds)
        monkeypatch.setattr(runner_module, "prepare", self._prepare)
        monkeypatch.setattr(runner_module, "run_session", self._session)
    def emit(self, event_type, message, *, project_id=None, data=None):
        self.events.append((event_type, project_id, data))
    async def _prepare(self, ctx, job, paths, *, store, tracker, now, job_clone_dir):
        assert store is self.store and tracker == "tracker" and now == self.t.isoformat()
        assert job_clone_dir == self.root(job) / "clone"
        async with ctx.clone.lock:   # as the real one: the review clone's lock for its own git only
            await self._locked_part()
        self.prepares += 1
        job_clone = FakeClone(job_clone_dir)   # made before a later failure, as the real one may be
        if self.prepare_error:
            raise self.prepare_error
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        paths.replies_plan.parent.mkdir(parents=True, exist_ok=True)
        if job.kind == "address":   # the PR's worktree, as add_worktree leaves it
            paths.worktree.mkdir(parents=True, exist_ok=True)
            (paths.worktree / ".git").write_text(f"gitdir: {job_clone_dir}/.git/worktrees/PR_1712\n")
        return Prepared(PR, self.drift, job_clone)
    async def _locked_part(self):
        await asyncio.sleep(0)  # a real prepare yields: a runner that loops on a job must not starve the clock
    async def _session(self, llm, **kw):
        self.session_kw, self.paths, self.session_clone = kw, kw["paths"], kw["clone_dir"]
        self.write(kw["paths"])
        if self.during:
            self.during(self)
        return self.outcome
    def add_job(self, kind="review", number=1712, key=HEAD, scope=SCOPE):
        return self.store.add_job(scope, number, kind, HEAD, key, self.t.isoformat())
    def state(self, job):
        got = self.store.job(job.id)
        return got.state, got.reason
    def job_dir(self, job):
        return self.tmp / "rv" / "jobs" / job.id
    def root(self, job):
        """The directory the job owns while it runs (its job clone and worktree)."""
        return self.tmp / "rv" / "work" / "acme" / job.scope.split("/")[1] / job.id

async def test_review_job_done_and_message_sent(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.during = lambda r: setattr(r, "t", T0 + timedelta(minutes=40))
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    run = rig.store.run(job.id)
    assert run.report["event"] == "REQUEST_CHANGES"
    assert rig.chat.texts == [task4_review()]
    assert rig.chat.texts[0].startswith('[acme/app] Review of PR #1712 "Fix cart sync" · ACME-1234\n')
    assert rig.store.anchor(CHAT_ID, 100).job_id == job.id
    assert run.message_sent and run.message_ids == [100]
    job_dir = rig.job_dir(job)
    assert run.job_dir == str(job_dir) and stat.S_IMODE(job_dir.stat().st_mode) == 0o700
    assert (job_dir / f"report_{HEAD[:9]}.md").read_text(encoding="utf-8") == REPORT
    assert run.tg_lines == {str(n): line for n, line in TG.items()}
    assert (run.started_at, run.finished_at, run.cost_usd, run.turns) == (
        T0.isoformat(), (T0 + timedelta(minutes=40)).isoformat(), 1.25, 42)
    assert (run.title, run.url, run.ticket_key, run.files) == ("Fix cart sync", URL, "ACME-1234", 14)
    assert rig.events == [("reviewer_job", "acme", {"scope": SCOPE, "number": 1712, "kind": "review", "state": s})
                          for s in ("running", "done")]
    kw = rig.session_kw
    assert kw["prompt"] == review_prompt(DOC, "Prepare", 1712, rig.paths)
    assert (kw["kind"], kw["clone_dir"], kw["job_cfg"], kw["environ"]) == (
        "review", rig.root(job) / "clone", JOBS["review"], ENV)
    assert (kw["helper_scripts"], kw["secret_paths"]) == (["tools/review/*.py"], [tmp_path / ".env"])
    assert not rig.root(job).exists() and rig.clone.removed == []   # the job's directory went with its worktree

async def test_head_moved_during_run_supersedes(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.during = lambda r: r.store.replace_board(SCOPE, {1712: board_row(head="b" * 40)})
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("superseded", "new head")
    assert rig.chat.texts == [] and not rig.store.run(job.id).message_sent

async def test_turn_gone_during_run_marks_stale(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.during = lambda r: r.store.replace_board(SCOPE, {1712: board_row(actions=())})
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("stale", "turn gone")
    assert rig.chat.texts == []

async def test_pr_gone_during_run_marks_stale(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.during = lambda r: r.store.replace_board(SCOPE, {})
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("stale", "PR gone")
    run = rig.store.run(job.id)
    assert (run.title, run.url, run.ticket_key, run.files) == (PR.title, URL, "ACME-1234", 14)
    assert rig.chat.texts == []

def test_settle():
    stamp, later, earlier = (t.isoformat() for t in (T0, T0 + timedelta(seconds=1), T0 - timedelta(seconds=1)))
    job = Job("j1", SCOPE, 1712, "review", HEAD, HEAD, "running", "", stamp, stamp)
    row = board_row()
    assert settle(job, None, [job]) == ("stale", "PR gone")
    assert settle(job, board_row(head="b" * 40), [job]) == ("superseded", "new head")
    assert settle(job, board_row(actions=("recheck",)), [job]) == ("stale", "turn gone")
    assert settle(job, row, [job]) == ("done", "")
    for state in ("queued", "running", "deferred", "done"):
        newer = dataclasses.replace(job, id="j2", state=state, created_at=later)
        assert settle(job, row, [newer, job]) == ("superseded", "newer job")
        assert settle(job, row, [job, newer]) == ("superseded", "newer job")
    for other in (dataclasses.replace(job, id="j2", state="failed", created_at=later),
                  dataclasses.replace(job, id="j2", kind="recheck", state="queued", created_at=later),
                  dataclasses.replace(job, id="j0", state="queued", created_at=earlier)):
        assert settle(job, row, [other, job]) == ("done", "")
    # Created at the same time: store.jobs lists the newer one first.
    same = dataclasses.replace(job, id="j2", state="queued")
    assert settle(job, row, [same, job]) == ("superseded", "newer job")
    assert settle(job, row, [job, same]) == ("done", "")

async def test_newer_job_supersedes(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    def queue_newer(r):
        r.t = T0 + timedelta(minutes=1)
        r.newer = r.add_job()
    rig.during = queue_newer
    await rig.runner.run_job(job)
    assert rig.state(job) == ("superseded", "newer job")
    assert rig.chat.texts == []

async def test_recover_marks_running_failed(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job, queued = rig.add_job(), rig.add_job(number=1713)
    rig.store.set_job_state(job.id, "running", "", T0.isoformat())
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(rig.runner.run(stop), 5)
    assert rig.state(job) == ("failed", "interrupted (daemon restarted)")
    assert rig.state(queued) == ("queued", "")
    assert rig.events == [("reviewer_job", "acme", {"scope": SCOPE, "number": 1712, "kind": "review",
                                                    "state": "failed"})]

async def test_start_recovers_and_loop_does_not(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    rig.store.set_job_state(job.id, "running", "", T0.isoformat())
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(rig.runner.loop(stop), 5)
    assert rig.state(job) == ("running", "")   # main runs start() at once, loop() after the first round
    await rig.runner.start()
    assert rig.state(job) == ("failed", "interrupted (daemon restarted)")

async def test_quota_defers_then_resumes(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.outcome = SessionOutcome("quota", retry_at=T0 + timedelta(hours=1))
    job = rig.add_job()
    await rig.runner.run_job(job)
    got = rig.store.job(job.id)
    assert (got.state, got.retry_at) == ("deferred", (T0 + timedelta(hours=1)).isoformat())
    assert rig.chat.texts == ["[acme/app] #1712: review deferred — usage limit reached; next try at 11:00 UTC"]
    assert rig.store.anchor(CHAT_ID, 100) == Anchor(job.id, "", 0)
    rig.t = T0 + timedelta(minutes=59)
    assert rig.runner.next_job() is None
    rig.t = T0 + timedelta(hours=1)
    assert rig.runner.next_job().id == job.id

@pytest.mark.parametrize("retry_at", [T0 + timedelta(minutes=30), None])
async def test_quota_retry_in_the_past_is_an_hour_from_now(tmp_path, monkeypatch, retry_at):
    # A reset time the session computed from its start can be behind the clock when a long run ends; the CLI
    # may give none.
    rig = Rig(tmp_path, monkeypatch)
    rig.outcome = SessionOutcome("quota", retry_at=retry_at)
    rig.during = lambda r: setattr(r, "t", T0 + timedelta(minutes=80))
    job = rig.add_job()
    await rig.runner.run_job(job)
    got = rig.store.job(job.id)
    assert (got.state, got.retry_at) == ("deferred", (T0 + timedelta(minutes=140)).isoformat())
    assert rig.runner.next_job() is None

async def test_login_failure_defers_and_reports(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.outcome = SessionOutcome("login")
    first, second = rig.add_job(), rig.add_job(number=1713)
    await rig.runner.run_job(first)
    got = rig.store.job(first.id)
    assert (got.state, got.reason, got.retry_at) == (
        "deferred", "claude login failing", (T0 + timedelta(minutes=15)).isoformat())
    assert rig.auth == [("claude_login", 401)]
    assert rig.chat.texts == [login_line()]
    assert rig.store.anchor(CHAT_ID, 100) == Anchor("", "", 0)   # neither a job's nor a repo's
    rig.t = T0 + timedelta(minutes=14, seconds=59)
    assert rig.runner.next_job() is None  # the second job waits too
    rig.t = T0 + timedelta(minutes=15)
    assert rig.runner.next_job().id == first.id
    await rig.runner.run_job(rig.store.job(second.id))
    assert rig.state(second) == ("deferred", "claude login failing")
    assert rig.auth == [("claude_login", 401)] * 2
    assert rig.chat.texts == [login_line()]  # one line per failure streak
    # A session that ends well ends the streak: the next login failure sends the line again.
    rig.t = T0 + timedelta(minutes=30)
    rig.outcome = SessionOutcome("ok")
    await rig.runner.run_job(rig.store.job(first.id))
    assert rig.state(first) == ("done", "")
    rig.outcome = SessionOutcome("login")
    await rig.runner.run_job(rig.add_job(number=1714))
    assert rig.chat.texts.count(login_line()) == 2

async def test_gates(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, repos=("app", "web"))
    rig.store.add_job("acme/other", 5, "review", HEAD, HEAD, T0.isoformat())  # no context: skipped
    job = rig.add_job()
    web = rig.add_job(scope="acme/web")
    assert rig.runner.next_job().id == job.id
    rig.switches.set("reviews", "paused")
    assert rig.runner.next_job() is None
    rig.switches.set("reviews", "running")
    rig.switches.set("reviews", "paused", scope=SCOPE)
    assert rig.runner.next_job().id == web.id
    rig.switches.set("reviews", "paused", scope="acme/web")
    assert rig.runner.next_job() is None
    rig.switches.set("reviews", "running", scope=SCOPE)
    rig.switches.set("reviews", "running", scope="acme/web")
    rig.failing.add(f"forge:{SCOPE}")
    assert rig.runner.next_job().id == web.id
    rig.failing.add("forge:acme/web")
    assert rig.runner.next_job() is None
    rig.failing.clear()
    rig.failing.add("claude_login")
    assert rig.runner.next_job() is None
    rig.failing.clear()
    assert rig.runner.next_job().id == job.id

@pytest.mark.parametrize("outcome, state, lines", [
    ("failed", "failed", [FAILED + "boom"]),
    ("superseded", "superseded", []),
    ("cancelled", "cancelled", []),
    ("requeue", "queued", []),
])
async def test_prepare_outcomes(tmp_path, monkeypatch, outcome, state, lines):
    rig = Rig(tmp_path, monkeypatch)
    rig.prepare_error = PrepareError(outcome, "boom")
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == (state, "boom")
    assert rig.chat.texts == lines
    assert rig.session_kw is None
    assert not rig.root(job).exists()  # in `finally`, whatever prepare did

async def test_bad_paths_fail_the_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    ctx = rig.contexts[0]
    ctx.cfg = dataclasses.replace(CFG, process=dataclasses.replace(CFG.process, run_dir="../outside/PR_{pr}"))
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "run_dir is outside the review clone")
    assert rig.chat.texts == [FAILED + "run_dir is outside the review clone"]
    assert rig.prepares == 0 and rig.store.run(job.id) is None

async def test_drift_line_whatever_the_outcome(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.drift = True
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    assert sorted(rig.chat.texts) == sorted([DRIFT, task4_review()])
    rig.chat.texts.clear()
    rig.outcome = SessionOutcome("failed", error="exit 1: boom")
    failed = rig.add_job(number=1713)
    await rig.runner.run_job(failed)
    assert rig.state(failed) == ("failed", "exit 1: boom")
    assert sorted(rig.chat.texts) == sorted([DRIFT, "[acme/app] #1713: review failed — exit 1: boom"])
    rig.chat.texts.clear()
    rig.prepare_error = PrepareError("failed", "prepare step 1 failed: boom", drift=True)
    step = rig.add_job(number=1714)
    await rig.runner.run_job(step)
    assert rig.chat.texts == ["[acme/app] #1714: review failed — prepare step 1 failed: boom", DRIFT]
    rig.chat.texts.clear()
    rig.prepare_error = PrepareError("superseded", "head moved", drift=True)
    moved = rig.add_job(number=1715)
    await rig.runner.run_job(moved)
    assert rig.state(moved) == ("superseded", "head moved") and rig.chat.texts == [DRIFT]

async def test_session_failure_fails_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.outcome = SessionOutcome("failed", error="exit 1: boom")
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "exit 1: boom")
    assert rig.chat.texts == [FAILED + "exit 1: boom"]
    assert not rig.root(job).exists()

@pytest.mark.parametrize("where", ["prepare", "run_session"])
async def test_unexpected_error_fails_job(tmp_path, monkeypatch, caplog, where):
    rig = Rig(tmp_path, monkeypatch)
    async def broken(*args, **kwargs):
        raise ValueError("text that is not ours")
    monkeypatch.setattr(runner_module, where, broken)
    job = rig.add_job()
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "internal error: ValueError")
    assert rig.chat.texts == [FAILED + "internal error: ValueError"]
    assert not rig.root(job).exists()
    assert "ValueError at " in caplog.text and "not ours" not in caplog.text

@pytest.mark.parametrize("guarded", ["config", "info/attributes", "commondir"])
async def test_session_changing_its_job_clones_git_files_fails_job(tmp_path, monkeypatch, guarded):
    rig = Rig(tmp_path, monkeypatch)
    def tamper(paths):
        write_review(paths)
        target = rig.session_clone / ".git" / guarded
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write('[credential]\n\thelper = "!sh evil.sh"\n')
    rig.write = tamper
    job = rig.add_job()
    await rig.runner.run_job(job)
    reason = "the session changed its job clone's git config"
    assert rig.state(job) == ("failed", reason)
    assert (rig.clone.path / ".git" / "config").read_text() == CONFIG_TEXT   # the review clone untouched
    assert not rig.root(job).exists()   # the tampered job clone is gone, never used again
    assert rig.chat.texts == [FAILED + reason]

async def test_cancelled_job_leaves_no_job_dir_and_stays_running(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    started = asyncio.Event()
    async def hanging(llm, **kw):
        rig.paths = kw["paths"]
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(runner_module, "run_session", hanging)
    job = rig.add_job()
    task = asyncio.create_task(rig.runner.run_job(job))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rig.root(job).exists()
    assert rig.state(job) == ("running", "") and not rig.clone.lock.locked()   # recover() fails it at start

async def test_unreadable_report_fails(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    other = REPORT.replace(HEAD, "b" * 40)
    rig.write = lambda paths: write_review(paths, report=other)
    job = rig.add_job()
    await rig.runner.run_job(job)
    reason = f"report unreadable: report is for head {'b' * 40}, not {HEAD[:9]}"
    assert rig.state(job) == ("failed", reason)
    assert (rig.job_dir(job) / f"report_{HEAD[:9]}.md").read_text(encoding="utf-8") == other
    assert rig.chat.texts == [FAILED + reason]
    assert rig.store.run(job.id).report == {}

@pytest.mark.parametrize("kind, reason", [("review", "no report written"), ("recheck", "no replies plan written")])
async def test_missing_output_fails(tmp_path, monkeypatch, kind, reason):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=(kind,))})
    rig.write = lambda paths: None
    job = rig.add_job(kind=kind)
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", reason)
    assert rig.chat.texts == [f"[acme/app] #1712: {kind} failed — {reason}"]

async def test_archive_copies_regular_files_only(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the job copy")
    def write(paths):
        write_review(paths)
        (paths.run_dir / "anchors").mkdir()
        (paths.run_dir / "anchors" / "added.json").write_text("{}")
        (paths.run_dir / "leak.txt").symlink_to(secret)
        (paths.run_dir / "linked").symlink_to(tmp_path, target_is_directory=True)
    rig.write = write
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    job_dir = rig.job_dir(job)
    assert sorted(p.relative_to(job_dir).as_posix() for p in job_dir.rglob("*")) == [
        "anchors", "anchors/added.json", f"report_{HEAD[:9]}.md", "tg_lines.json"]

RESERVED_NOTES = ["not kept from the run directory: replies_plan.json (a name the job copy reserves)",
                  "not kept from the run directory: session (a name the job copy reserves)"]

async def test_archive_keeps_the_job_copys_own_names(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    def write(paths):
        write_review(paths)
        (paths.job_dir / "session").mkdir()  # as run_session leaves it
        (paths.job_dir / "session" / "settings.json").write_text("cleave")
        (paths.run_dir / "session").mkdir()
        (paths.run_dir / "session" / "settings.json").write_text("model")
        (paths.run_dir / "replies_plan.json").write_text(json.dumps(REPLIES))
    rig.write = write
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    job_dir = rig.job_dir(job)
    assert (job_dir / "session" / "settings.json").read_text() == "cleave"
    assert not (job_dir / "replies_plan.json").exists()
    assert rig.store.run(job.id).notes == RESERVED_NOTES

async def test_plan_in_the_run_directory_does_not_pass_as_the_replies_plan(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=("recheck",))})
    rig.write = lambda paths: (paths.run_dir / "replies_plan.json").write_text(json.dumps(REPLIES))
    job = rig.add_job(kind="recheck", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "no replies plan written")
    assert rig.store.run(job.id).notes == RESERVED_NOTES[:1]

def reserved_note(name):
    return f"not kept from the run directory: {name} (a name the job copy reserves)"

# One name per run: on a case-insensitive filesystem two of them could not live side by side.
@pytest.mark.parametrize("name", ["Session", "SESSION", "ſession"])  # ſ: the long s, casefolded to s
async def test_reserved_session_name_matches_whatever_the_case(tmp_path, monkeypatch, name):
    rig = Rig(tmp_path, monkeypatch)
    def write(paths):
        write_review(paths)
        (paths.job_dir / "session").mkdir()  # as run_session leaves it
        (paths.job_dir / "session" / "stream.jsonl").write_text("cleave")
        (paths.run_dir / name).mkdir()
        (paths.run_dir / name / "stream.jsonl").write_text("model")
    rig.write = write
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    job_dir = rig.job_dir(job)
    assert (job_dir / "session" / "stream.jsonl").read_text() == "cleave"
    assert sorted(p.name for p in job_dir.iterdir()) == [f"report_{HEAD[:9]}.md", "session", "tg_lines.json"]
    assert rig.store.run(job.id).notes == [reserved_note(name)]

@pytest.mark.parametrize("name", ["Replies_Plan.json", "REPLIES_PLAN.JSON"])
async def test_reserved_plan_name_matches_whatever_the_case(tmp_path, monkeypatch, name):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=("recheck",))})
    rig.write = lambda paths: (paths.run_dir / name).write_text(json.dumps(REPLIES))
    job = rig.add_job(kind="recheck", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "no replies plan written")
    assert list(rig.job_dir(job).iterdir()) == []
    assert rig.store.run(job.id).notes == [reserved_note(name)]

async def test_symlinked_run_directory_fails(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    def write(paths):
        elsewhere.mkdir()
        write_review(dataclasses.replace(paths, run_dir=elsewhere))
        shutil.rmtree(paths.run_dir)
        paths.run_dir.symlink_to(elsewhere, target_is_directory=True)
    rig.write = write
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "run directory is a symlink")
    assert rig.chat.texts == [FAILED + "run directory is a symlink"]
    assert list(rig.job_dir(job).iterdir()) == []

async def test_a_job_dir_not_removed_is_noted(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, monkeypatch)
    def failing_rmtree(path, *args, **kwargs):
        raise OSError("busy")
    monkeypatch.setattr(runner_module.shutil, "rmtree", failing_rmtree)
    job = rig.add_job()
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    assert rig.store.run(job.id).notes == ["job directory not removed: OSError"]
    assert f"Reviewer job {job.id}: its directory was not removed" in caplog.text

async def test_unsent_message_is_resent(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.chat.fail = 1
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    run = rig.store.run(job.id)
    assert (run.message_sent, run.send_attempts, run.last_attempt_at) == (False, 1, T0.isoformat())
    assert rig.chat.texts == [] and rig.store.anchor(CHAT_ID, 100) is None
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.chat.texts == [task4_review()]
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == [100] and run.send_attempts == 1
    assert rig.store.anchor(CHAT_ID, 100).job_id == job.id
    assert rig.store.done_unsent() == []
    await rig.runner.resend_unsent()
    assert len(rig.chat.texts) == 1

async def test_resend_waits_between_attempts(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.chat.fail = 99
    job = rig.add_job()
    await rig.runner.run_job(job)  # attempt 1
    await rig.runner.resend_unsent()  # the same cycle: not again yet
    rig.t = T0 + timedelta(seconds=59)
    await rig.runner.resend_unsent()
    assert rig.chat.tries == 1
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.chat.tries == 2 and rig.store.run(job.id).last_attempt_at == rig.t.isoformat()
    rig.t += timedelta(minutes=4, seconds=59)
    await rig.runner.resend_unsent()
    assert rig.chat.tries == 2
    rig.chat.fail = 0
    rig.t += timedelta(seconds=1)  # 5 minutes after the second attempt
    await rig.runner.resend_unsent()
    assert rig.chat.texts == [task4_review()] and rig.store.run(job.id).message_sent

async def test_no_resend_in_the_cycle_after_the_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, idle_seconds=3600)
    rig.chat.fail = 99
    job = rig.add_job()
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.1, stop.set)
    await asyncio.wait_for(rig.runner.run(stop), 5)
    assert rig.state(job) == ("done", "") and rig.chat.tries == 1

async def test_bad_saved_run_does_not_hold_back_the_others(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(), 1713: board_row(number=1713)})
    bad, good = rig.add_job(), rig.add_job(number=1713)
    for job, report in ((bad, {"event": "COMMENT"}), (good, parse_report(REPORT, HEAD).to_dict())):
        rig.store.set_job_state(job.id, "done", "", T0.isoformat())
        rig.store.save_run(JobRun(job.id, title="Fix cart sync", url=URL, ticket_key="ACME-1234", files=14,
                                  report=report))
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        await rig.runner.resend_unsent()
    assert rig.chat.texts == [review_message(SCOPE, 1713, "Fix cart sync", "ACME-1234", URL, 14,
                                             parse_report(REPORT, HEAD), {})]
    assert [job.id for job, run in rig.store.done_unsent()] == [bad.id]
    assert f"Reviewer message of job {bad.id} not resent: KeyError at " in caplog.text
    run = rig.store.run(bad.id)
    assert (run.send_attempts, run.last_attempt_at) == (1, T0.isoformat())  # a failed attempt too

async def test_unreadable_attempt_time_counts_and_then_waits(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    rig.store.set_job_state(job.id, "done", "", T0.isoformat())
    rig.store.save_run(JobRun(job.id, title="Fix cart sync", url=URL, last_attempt_at="not a time",
                              report=parse_report(REPORT, HEAD).to_dict()))
    await rig.runner.resend_unsent()
    run = rig.store.run(job.id)
    assert (run.send_attempts, run.last_attempt_at) == (1, T0.isoformat())
    await rig.runner.resend_unsent()  # now its wait applies
    assert rig.store.run(job.id).send_attempts == 1 and rig.chat.tries == 0

async def test_broken_saved_run_reaches_the_cap(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    rig.store.set_job_state(job.id, "done", "", T0.isoformat())
    rig.store.save_run(JobRun(job.id, title="Fix cart sync", url=URL, report={"event": "COMMENT"}))
    for wait in WAITS:
        await rig.runner.resend_unsent()
        rig.t += wait
    await rig.runner.resend_unsent()
    run = rig.store.run(job.id)
    assert run.send_attempts == 5 and run.notes == ["message not delivered after 5 attempts"]
    caplog.clear()
    rig.t += timedelta(days=1)
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        await rig.runner.resend_unsent()
    assert rig.store.run(job.id).send_attempts == 5 and caplog.text == ""

def long_parts():
    return split_message(review_message(SCOPE, 1712, "Fix cart sync", "ACME-1234", URL, 14,
                                        parse_report(LONG_REPORT, HEAD), {}), limit=3900)

async def test_long_message_is_split_and_anchored(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.write = lambda paths: write_review(paths, report=LONG_REPORT, tg={})
    job = rig.add_job()
    await rig.runner.run_job(job)
    parts = long_parts()
    assert len(parts) >= 3 and rig.chat.texts == parts
    assert max(len(p) for p in parts) == 3900
    ids = list(range(100, 100 + len(parts)))
    assert [rig.store.anchor(CHAT_ID, i).job_id for i in ids] == [job.id] * len(parts)
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == ids

async def test_a_note_the_chat_adds_while_the_message_goes_out_is_kept(tmp_path, monkeypatch):
    """ReviewerChat notes a message it could not anchor in the job's run (/code-review C5): the runner, which holds
    the run across its sends, keeps that note."""
    rig = Rig(tmp_path, monkeypatch)
    sends = rig.chat.send

    async def unanchored(text, **anchor):  # as ReviewerChat does once its anchor write failed 3 times
        message_id = await sends(text, **anchor)
        run = rig.store.run(anchor["job_id"])
        run.notes.append(f"message {message_id} is not registered; replies to it are not recognised")
        rig.store.save_run(run)
        return message_id

    rig.chat.send = unanchored
    job = rig.add_job()
    await rig.runner.run_job(job)
    run = rig.store.run(job.id)
    assert run.message_sent and run.notes == ["message 100 is not registered; replies to it are not recognised"]

async def test_resend_resumes_at_the_first_unsent_part(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.write = lambda paths: write_review(paths, report=LONG_REPORT, tg={})
    sends, calls = rig.chat.send, []
    async def second_part_fails(text, **anchor):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("telegram down")
        return await sends(text, **anchor)
    rig.chat.send = second_part_fails
    job = rig.add_job()
    await rig.runner.run_job(job)
    run = rig.store.run(job.id)
    assert (run.message_sent, run.message_ids, run.send_attempts) == (False, [100], 1)
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    parts = long_parts()
    assert len(parts) >= 3 and rig.chat.texts == parts  # no part is sent twice
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == list(range(100, 100 + len(parts)))
    assert [rig.store.anchor(CHAT_ID, i).job_id for i in run.message_ids] == [job.id] * len(parts)

async def test_fifth_failed_delivery_stops_the_resend(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.chat.fail = 99
    job = rig.add_job()
    await rig.runner.run_job(job)  # attempt 1
    for attempt, wait in enumerate(WAITS, 2):  # 1, 5, 15 and 60 minutes after the 1st to 4th failure
        assert rig.store.run(job.id).notes == []
        rig.t += wait - timedelta(seconds=1)
        await rig.runner.resend_unsent()
        assert rig.store.run(job.id).send_attempts == attempt - 1
        rig.t += timedelta(seconds=1)
        await rig.runner.resend_unsent()
        assert rig.store.run(job.id).send_attempts == attempt
    run = rig.store.run(job.id)
    assert not run.message_sent and run.notes == ["message not delivered after 5 attempts"]
    rig.chat.fail = 0
    rig.t += timedelta(days=1)
    await rig.runner.resend_unsent()
    assert rig.chat.texts == [] and rig.chat.tries == 5 and rig.store.run(job.id).send_attempts == 5

async def test_resend_settles_against_the_board_first(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.chat.fail = 1
    job = rig.add_job()
    await rig.runner.run_job(job)
    rig.store.replace_board(SCOPE, {1712: board_row(head="b" * 40)})
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.state(job) == ("superseded", "new head")
    assert rig.chat.texts == [] and rig.store.done_unsent() == []
    assert rig.events[-1] == ("reviewer_job", "acme", {"scope": SCOPE, "number": 1712, "kind": "review",
                                                       "state": "superseded"})

async def test_resend_stops_at_the_first_failed_send(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(), 1713: board_row(number=1713)})
    rig.chat.fail = 2
    first, second = rig.add_job(), rig.add_job(number=1713)
    await rig.runner.run_job(first)
    await rig.runner.run_job(second)
    rig.chat.fail = 1
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.chat.texts == [] and rig.chat.tries == 3  # the second was not tried after the first failed
    assert [rig.store.run(j.id).send_attempts for j in (first, second)] == [2, 1]
    rig.t += timedelta(minutes=5)
    await rig.runner.resend_unsent()
    assert len(rig.chat.texts) == 2 and rig.store.done_unsent() == []

async def test_recheck_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=("recheck",))})
    tg = {1: "fixed · CartRepository.kt:88 · null check added"}
    def write(paths):
        paths.replies_plan.write_text(json.dumps(REPLIES))
        (paths.run_dir / "tg_lines.json").write_text(json.dumps({"1": tg[1]}))
    rig.write = write
    job = rig.add_job(kind="recheck", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    run = rig.store.run(job.id)
    assert run.replies == REPLIES and run.report == {} and run.tg_lines == {"1": tg[1]}
    assert rig.chat.texts == [recheck_message(SCOPE, 1712, "Fix cart sync", URL, HEAD, REPLIES, tg)]
    assert rig.store.anchor(CHAT_ID, 100).job_id == job.id and run.message_sent
    assert json.loads((rig.job_dir(job) / "replies_plan.json").read_text()) == REPLIES
    assert rig.session_kw["kind"] == "recheck" and rig.session_kw["job_cfg"] == JOBS["recheck"]
    assert rig.session_kw["prompt"] == recheck_prompt(DOC, "Re-check", 1712, rig.paths, HEAD, "octo")

async def test_no_chat(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, chat=False)
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "") and not rig.store.run(job.id).message_sent
    await rig.runner.resend_unsent()
    assert not rig.store.run(job.id).message_sent

async def test_one_cycle_error_does_not_end_run(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, monkeypatch)
    stop, calls = asyncio.Event(), []
    def next_job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) == 3:
            stop.set()
    monkeypatch.setattr(rig.runner, "next_job", next_job)
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        await asyncio.wait_for(rig.runner.run(stop), 5)
    assert len(calls) == 3
    assert "RuntimeError at " in caplog.text and "boom" not in caplog.text

async def test_run_takes_jobs_one_after_another(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, idle_seconds=3600)
    first, second = rig.add_job(), rig.add_job(number=1713)
    rig.store.replace_board(SCOPE, {1712: board_row(), 1713: board_row(number=1713)})
    stop = asyncio.Event()
    def done(r):
        if r.prepares == 2:
            stop.set()
    rig.during = done
    await asyncio.wait_for(rig.runner.run(stop), 5)
    assert rig.state(first) == ("done", "") and rig.state(second) == ("done", "")

async def test_requeued_job_waits_for_the_next_cycle(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, idle_seconds=3600)
    rig.prepare_error = PrepareError("requeue", "forge token failing")
    job = rig.add_job()
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.1, stop.set)
    await asyncio.wait_for(rig.runner.run(stop), 5)
    assert rig.state(job) == ("queued", "forge token failing")
    assert rig.prepares == 1

# --- Final fix wave ---

DEFERRED_3 = "deferred 3 times (usage limit or login)"

def quota(rig):
    return SessionOutcome("quota", retry_at=rig.t + timedelta(hours=1))

@pytest.mark.parametrize("kinds", [("quota",) * 3, ("login",) * 3, ("quota", "login", "quota")])
async def test_third_deferral_fails_the_job(tmp_path, monkeypatch, kinds):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    for i, kind in enumerate(kinds, 1):
        rig.outcome = quota(rig) if kind == "quota" else SessionOutcome("login")
        await rig.runner.run_job(rig.store.job(job.id))
        assert rig.store.run(job.id).deferrals == i
        assert rig.state(job)[0] == ("deferred" if i < 3 else "failed")
        rig.t += timedelta(hours=2)
    assert rig.state(job) == ("failed", DEFERRED_3)
    assert rig.chat.texts[-1] == FAILED + DEFERRED_3
    assert rig.store.run(job.id).error == DEFERRED_3

def test_runs_stored_before_the_deferral_count_load():
    assert JobRun.from_json(json.dumps({"job_id": "j1", "turns": 3})).deferrals == 0

async def test_quota_gates_the_runner_with_one_line_per_streak(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, max_parallel=1)   # one at a time: what the gate holds back shows
    numbers = (1712, 1713, 1714)
    rig.store.replace_board(SCOPE, {n: board_row(number=n) for n in numbers})
    first, second, third = (rig.add_job(number=n) for n in numbers)
    rig.outcome = quota(rig)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)
    await asyncio.wait_for(rig.runner.run(stop), 5)   # many cycles at T0
    assert rig.prepares == 1   # one session: no Prepare, no CLI start for the others
    assert rig.chat.texts == ["[acme/app] #1712: review deferred — usage limit reached; next try at 11:00 UTC"]
    assert [rig.state(j)[0] for j in (first, second, third)] == ["deferred", "queued", "queued"]
    rig.t = T0 + timedelta(minutes=59, seconds=59)
    assert rig.runner.next_job() is None
    rig.t = T0 + timedelta(hours=1)
    assert rig.runner.next_job().id == first.id
    rig.outcome = quota(rig)
    await rig.runner.run_job(rig.store.job(second.id))   # the streak goes on: no second line
    assert len(rig.chat.texts) == 1 and rig.runner.next_job() is None
    rig.t = T0 + timedelta(hours=2)
    rig.outcome = SessionOutcome("ok")   # a session that ends well ends the streak
    await rig.runner.run_job(rig.store.job(third.id))
    assert rig.state(third) == ("done", "")
    rig.outcome = quota(rig)
    await rig.runner.run_job(rig.store.job(first.id))
    assert rig.chat.texts[-1] == "[acme/app] #1712: review deferred — usage limit reached; next try at 13:00 UTC"

async def test_review_now_is_delivered_without_a_review_turn(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=())})
    job = rig.add_job(key=f"{HEAD}:now")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "") and rig.chat.texts == [task4_review()]

def test_settle_address_needs_one_of_its_turns():
    j = Job("a1", SCOPE, 1712, "address", HEAD, f"{HEAD}:T1", "running", "", "t", "t")
    assert settle(j, board_row(actions=("resolve_threads",)), [j]) == ("done", "")
    assert settle(j, board_row(actions=("merge",)), [j]) == ("stale", "turn gone")

def test_settle_review_now_and_skip():
    stamp = T0.isoformat()
    now = Job("j1", SCOPE, 1712, "review", HEAD, f"{HEAD}:now", "running", "", stamp, stamp)
    assert settle(now, board_row(actions=()), [now]) == ("done", "")
    assert settle(now, board_row(actions=("recheck",)), [now]) == ("done", "")
    assert settle(now, None, [now]) == ("stale", "PR gone")
    assert settle(now, board_row(head="b" * 40, actions=()), [now]) == ("superseded", "new head")
    newer = dataclasses.replace(now, id="j2", key=HEAD, state="queued",
                                created_at=(T0 + timedelta(seconds=1)).isoformat())
    assert settle(now, board_row(), [newer, now]) == ("superseded", "newer job")
    recheck = dataclasses.replace(now, kind="recheck")
    assert settle(recheck, board_row(actions=()), [recheck]) == ("stale", "turn gone")
    auto = dataclasses.replace(now, key=HEAD)
    assert settle(auto, board_row(actions=()), [auto]) == ("stale", "turn gone")
    assert settle(auto, board_row(), [auto], skipped=True) == ("skipped", "skipped by operator")
    assert settle(auto, board_row(head="b" * 40), [auto], skipped=True) == ("superseded", "new head")

async def test_head_skipped_during_the_run_ends_skipped(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    def skip(r):
        r.t = T0 + timedelta(minutes=5)
        r.store.skip(SCOPE, 1712, HEAD, r.t.isoformat())
    rig.during = skip
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("skipped", "skipped by operator")
    assert rig.chat.texts == [] and not rig.store.run(job.id).message_sent

async def test_review_now_at_a_skipped_head_is_delivered_until_skipped_again(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.skip(SCOPE, 1712, HEAD, T0.isoformat())
    rig.t = T0 + timedelta(minutes=1)
    job = rig.add_job(key=f"{HEAD}:now")   # queued after the skip
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "") and rig.chat.texts == [task4_review()]
    again = rig.add_job(key=f"{HEAD}:now")
    def skip(r):
        r.t = T0 + timedelta(minutes=5)
        r.store.skip(SCOPE, 1712, HEAD, r.t.isoformat())   # the same head, skipped again while it runs
    rig.during = skip
    await rig.runner.run_job(again)
    assert rig.state(again) == ("skipped", "skipped by operator") and len(rig.chat.texts) == 1

@pytest.mark.parametrize("pr, verdict", [
    (dataclasses.replace(PR, head_sha="b" * 40), ("superseded", "new head")),
    (dataclasses.replace(PR, state="closed"), ("stale", "PR gone")),
])
async def test_github_is_read_before_settling(tmp_path, monkeypatch, pr, verdict):
    rig = Rig(tmp_path, monkeypatch)
    rig.during = lambda r: setattr(r.forge, "pr", pr)   # the board has not seen it yet
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == verdict and rig.forge.reads == 1
    assert rig.chat.texts == [] and not rig.store.run(job.id).message_sent

async def test_github_error_before_settling_falls_back_to_the_board(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.forge.error = ForgeError("GitHub 502")
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "") and rig.chat.texts == [task4_review()] and rig.forge.reads == 1
    rig.during = lambda r: r.store.replace_board(SCOPE, {1712: board_row(head="b" * 40)})
    moved = rig.add_job(number=1712)
    await rig.runner.run_job(moved)
    assert rig.state(moved) == ("superseded", "new head")

async def test_resend_reads_github_first(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.chat.fail = 1
    job = rig.add_job()
    await rig.runner.run_job(job)
    rig.forge.pr = dataclasses.replace(PR, state="closed")
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.state(job) == ("stale", "PR gone") and rig.chat.texts == []

async def test_recheck_waits_for_the_first_poll(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.save_repo_state(RepoState(SCOPE))   # no `me` recorded yet
    rig.store.replace_board(SCOPE, {1712: board_row(actions=("recheck",)), 1713: board_row(number=1713)})
    recheck = rig.add_job(kind="recheck", key=f"{HEAD}:T1")
    review = rig.add_job(number=1713)
    assert rig.runner.next_job().id == review.id   # the re-check does not hold the others back
    assert rig.state(recheck) == ("queued", "waiting for the first poll")
    events = len(rig.events)
    assert rig.runner.next_job().id == review.id and len(rig.events) == events   # requeued once
    rig.store.save_repo_state(RepoState(SCOPE, me="octo"))
    assert rig.runner.next_job().id == recheck.id

@pytest.mark.parametrize("state", ["queued", "deferred", "running"])
async def test_recheck_waits_for_the_prs_review(tmp_path, monkeypatch, state):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=("review", "recheck")), 1713: board_row(number=1713)})
    recheck = rig.add_job(kind="recheck", key=f"{HEAD}:T1")
    review = rig.add_job()
    later = (T0 + timedelta(hours=1)).isoformat() if state == "deferred" else ""
    rig.store.set_job_state(review.id, state, "", T0.isoformat(), later)
    other = rig.add_job(number=1713)
    assert rig.runner.next_job().id == (review.id if state == "queued" else other.id)
    assert rig.state(recheck) == ("queued", "")
    rig.store.set_job_state(review.id, "done", "", T0.isoformat())
    assert rig.runner.next_job().id == recheck.id

@pytest.mark.parametrize("setup, reason", [
    (lambda r: setattr(r, "outcome", SessionOutcome("failed", error="exit 1: boom")), "exit 1: boom"),
    (lambda r: setattr(r, "prepare_error", PrepareError("failed", "prepare step 1 failed: boom")),
     "prepare step 1 failed: boom"),
    (lambda r: setattr(r, "write", lambda paths: None), "no report written"),
])
async def test_failed_job_keeps_its_reason_in_the_run(tmp_path, monkeypatch, setup, reason):
    rig = Rig(tmp_path, monkeypatch)
    setup(rig)
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", reason) and rig.store.run(job.id).error == reason

async def test_interrupted_job_keeps_its_reason_in_the_run(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    rig.store.set_job_state(job.id, "running", "", T0.isoformat())
    rig.store.save_run(JobRun(job.id, started_at=T0.isoformat()))
    rig.runner.recover()
    assert rig.store.run(job.id).error == "interrupted (daemon restarted)"

HUGE_REPORT = (_HEAD_PART + "## Dropped (never posted)\n"
               + "".join(f"- {n}. Candidate {n} - {'too speculative to keep ' * 6}(xhigh)\n" for n in range(7, 407))
               + _REST[_REST.index("\n## ✅"):])

async def test_message_capped_at_ten_parts(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.write = lambda paths: write_review(paths, report=HUGE_REPORT, tg={})
    job = rig.add_job()
    await rig.runner.run_job(job)
    full = split_message(review_message(SCOPE, 1712, "Fix cart sync", "ACME-1234", URL, 14,
                                        parse_report(HUGE_REPORT, HEAD), {}), limit=3900)
    assert len(full) > 10
    assert len(rig.chat.texts) == 10 and max(len(p) for p in rig.chat.texts) <= 3900
    assert rig.chat.texts[-1].endswith(" more lines — the full report is on the dashboard")
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == list(range(100, 110))

async def test_reviewer_chat_sends_to_its_chat():
    class Notifier:
        def __init__(self): self.sent = []
        async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
            self.sent.append((chat_id, text))
            return 77
    notifier = Notifier()
    assert await ReviewerChat(notifier, CHAT_ID).send("hello") == 77
    assert notifier.sent == [(CHAT_ID, "hello")]

# --- Code-review fix wave 2 ---

class Step:
    """A step a job or a poll takes on the clone: it logs its start and end; held, it waits for go() between."""
    def __init__(self, name, log):
        self.name, self.log = name, log
        self.started, self.ended, self._go = asyncio.Event(), asyncio.Event(), None
    def hold(self):
        self._go = asyncio.Event()
    def go(self):
        self._go.set()
    async def __call__(self):
        self.log.append(f"{self.name} start")
        self.started.set()
        if self._go is not None:
            await self._go.wait()
        self.log.append(f"{self.name} end")
        self.ended.set()

class SharedClone(FakeClone):
    """One review clone for a job and a poll: the poll's refresh is a step."""
    def __init__(self, path, steps):
        super().__init__(path)
        self.steps = steps
    async def refresh(self):
        await self.steps["refresh"]()
        return "c" * 40
    def has_file(self, rel):
        return True

class PollReadiness:
    repo = "acme-org/acme-app"
    def __init__(self, step):
        self.step = step
    async def board(self, clone_dir):
        await self.step()
        return BoardSnapshot("octo", (SnapshotPR(1712, PR.title, URL, "octo-author", False, False,
                                                 (Turn("octo", "review"),), {}),))

class PollForge:
    async def open_prs(self):
        return {1712: "t"}
    async def details(self, number, updated_at):
        return PRDetails(1712, PR.title, URL, "octo-author", "open", HEAD, False, (), ("octo",), 14, "t")
    async def reviewed_by(self, number, login, updated_at):
        return False

class Clash(Rig):
    """A job and a poll on one review clone, on the same store. Every step either takes is logged; a test holds
    the ones whose overlap it checks. The job's Prepare step is the part it takes under the clone's lock."""
    def __init__(self, tmp_path, monkeypatch):
        self.log = []
        self.steps = {name: Step(name, self.log) for name in ("refresh", "readiness", "prepare", "session")}
        self.running = asyncio.Event()
        super().__init__(tmp_path, monkeypatch, clone=lambda path: SharedClone(path, self.steps))
        ctx = RepoContext("acme", "app", CFG, self.clone, PollReadiness(self.steps["readiness"]), PollForge())
        self.service = ReviewerService([ctx], self.store, self.switches, lambda key: False, None,
                                       lambda project_id, repo_id: set(), now=lambda: self.t)
        self.poll = lambda: self.service.poll(ctx)
    def emit(self, event_type, message, *, project_id=None, data=None):
        super().emit(event_type, message, project_id=project_id, data=data)
        if data["state"] == "running":
            self.running.set()
    async def _locked_part(self):
        await self.steps["prepare"]()
    async def _session(self, llm, **kw):
        await self.steps["session"]()
        return await super()._session(llm, **kw)

async def test_a_job_prepares_only_after_a_polls_refresh_and_readiness(tmp_path, monkeypatch):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["readiness"].hold()
    job = rig.add_job()
    poll = asyncio.create_task(rig.poll())
    await rig.steps["readiness"].started.wait()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.running.wait()   # the job has gone as far as it can while the poll's readiness runs
    rig.steps["readiness"].go()
    await asyncio.wait_for(asyncio.gather(poll, run), 5)
    assert rig.log[:5] == ["refresh start", "refresh end", "readiness start", "readiness end", "prepare start"]
    assert rig.state(job) == ("done", "") and rig.store.repo_state(SCOPE).status == "ok"

async def test_a_poll_refreshes_during_a_session(tmp_path, monkeypatch):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["session"].hold()
    job = rig.add_job()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.steps["session"].started.wait()
    await asyncio.wait_for(rig.poll(), 5)   # the session runs in its job clone: the review clone is free
    assert rig.log == ["prepare start", "prepare end", "session start", "refresh start", "refresh end",
                       "readiness start", "readiness end"]
    state = rig.store.repo_state(SCOPE)
    assert (state.status, state.last_poll_at) == ("ok", T0.isoformat())
    rig.steps["session"].go()
    await asyncio.wait_for(run, 5)
    assert rig.state(job) == ("done", "")

async def test_a_job_ends_while_a_polls_readiness_runs(tmp_path, monkeypatch):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["session"].hold()
    rig.steps["readiness"].hold()
    job = rig.add_job()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.steps["session"].started.wait()
    poll = asyncio.create_task(rig.poll())
    await rig.steps["readiness"].started.wait()
    rig.steps["session"].go()
    await asyncio.wait_for(run, 5)   # nothing of the job's end waits for the review clone
    assert rig.state(job) == ("done", "") and "readiness end" not in rig.log
    rig.steps["readiness"].go()
    await asyncio.wait_for(poll, 5)

@pytest.mark.parametrize("kind", ["quota", "login"])
async def test_a_session_ending_otherwise_ends_the_streak(tmp_path, monkeypatch, kind):
    rig = Rig(tmp_path, monkeypatch)
    numbers = (1712, 1713, 1714)
    rig.store.replace_board(SCOPE, {n: board_row(number=n) for n in numbers})
    first, failed, second = (rig.add_job(number=n) for n in numbers)
    def streak():
        return quota(rig) if kind == "quota" else SessionOutcome("login")
    def line(number):
        return (f"[acme/app] #{number}: review deferred — usage limit reached; next try at "
                f"{(rig.t + timedelta(hours=1)):%H:%M} UTC") if kind == "quota" else login_line()
    rig.outcome = streak()
    expected = [line(1712)]
    await rig.runner.run_job(first)
    rig.t += timedelta(hours=2)
    rig.outcome = SessionOutcome("failed", error="exit 1: boom")
    await rig.runner.run_job(failed)
    expected.append(FAILED.replace("1712", "1713") + "exit 1: boom")
    rig.t += timedelta(hours=2)
    rig.outcome = streak()
    expected.append(line(1714))
    await rig.runner.run_job(second)
    assert rig.chat.texts == expected

async def test_a_prepare_error_leaves_the_streak_as_it_is(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    numbers = (1712, 1713, 1714)
    rig.store.replace_board(SCOPE, {n: board_row(number=n) for n in numbers})
    first, unprepared, second = (rig.add_job(number=n) for n in numbers)
    rig.outcome = quota(rig)
    await rig.runner.run_job(first)
    rig.t += timedelta(hours=2)
    rig.prepare_error = PrepareError("failed", "boom")
    await rig.runner.run_job(unprepared)
    rig.prepare_error = None
    rig.t += timedelta(hours=2)
    rig.outcome = quota(rig)
    await rig.runner.run_job(second)
    assert [t for t in rig.chat.texts if "deferred" in t] == [
        "[acme/app] #1712: review deferred — usage limit reached; next try at 11:00 UTC"]

UNAVAILABLE = PrepareError("unavailable", "GitHub unavailable")

async def test_github_unavailable_backs_off_then_fails_the_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.prepare_error = UNAVAILABLE
    job = rig.add_job()
    for minutes in (1, 5, 15, 60, 60):
        await rig.runner.run_job(rig.store.job(job.id))
        got, retry_at = rig.store.job(job.id), rig.t + timedelta(minutes=minutes)
        assert (got.state, got.reason, got.retry_at) == ("queued", "GitHub unavailable", retry_at.isoformat())
        rig.t = retry_at - timedelta(seconds=1)
        assert rig.runner.next_job() is None
        rig.t = retry_at
        assert rig.runner.next_job().id == job.id
    await rig.runner.run_job(rig.store.job(job.id))
    reason = "GitHub unavailable (6 attempts)"
    assert rig.state(job) == ("failed", reason) and rig.store.run(job.id).requeues == 6
    assert rig.chat.texts == [FAILED + reason]

async def test_a_job_waiting_for_github_holds_back_no_other(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, repos=("app", "web"))
    rig.prepare_error = UNAVAILABLE
    job = rig.add_job()
    await rig.runner.run_job(job)
    other = rig.add_job(scope="acme/web")
    assert rig.runner.next_job().id == other.id

async def test_a_prepare_that_gets_through_resets_the_unavailable_count(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.prepare_error = UNAVAILABLE
    job = rig.add_job()
    await rig.runner.run_job(job)
    await rig.runner.run_job(rig.store.job(job.id))
    assert rig.store.run(job.id).requeues == 2
    rig.prepare_error, rig.outcome = None, quota(rig)
    await rig.runner.run_job(rig.store.job(job.id))   # prepared, then deferred
    assert rig.store.run(job.id).requeues == 0
    rig.t += timedelta(hours=2)
    rig.prepare_error = UNAVAILABLE
    await rig.runner.run_job(rig.store.job(job.id))
    assert rig.store.job(job.id).retry_at == (rig.t + timedelta(minutes=1)).isoformat()   # the first wait again

def test_runs_stored_before_the_requeue_count_load():
    assert JobRun.from_json(json.dumps({"job_id": "j1", "turns": 3})).requeues == 0

async def test_a_rerun_keeps_the_earlier_runs_notes_cost_and_turns(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rmtree = runner_module.shutil.rmtree
    def failing_rmtree(path, *args, **kwargs):
        raise OSError("busy")
    monkeypatch.setattr(runner_module.shutil, "rmtree", failing_rmtree)
    rig.outcome = SessionOutcome("quota", retry_at=rig.t + timedelta(hours=1), cost_usd=1.5, turns=10)
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job)[0] == "deferred"
    rmtree(rig.root(job))   # as the next start's recover() would
    monkeypatch.setattr(runner_module.shutil, "rmtree", rmtree)
    rig.t += timedelta(hours=1)
    rig.outcome = SessionOutcome("ok", cost_usd=2.0, turns=30)
    await rig.runner.run_job(rig.store.job(job.id))
    run = rig.store.run(job.id)
    assert rig.state(job) == ("done", "") and run.message_sent
    assert (run.notes, run.cost_usd, run.turns, run.deferrals) == (
        ["job directory not removed: OSError"], 3.5, 40, 1)
    assert (run.started_at, run.finished_at) == ((T0 + timedelta(hours=1)).isoformat(),) * 2

# --- Plan 5: posted reports and small jobs ---

def test_settle_newer_posted_supersedes():
    stamp, later = T0.isoformat(), (T0 + timedelta(seconds=1)).isoformat()
    job = Job("j1", SCOPE, 1712, "review", HEAD, HEAD, "running", "", stamp, stamp)
    newer = dataclasses.replace(job, id="j2", state="posted", created_at=later)
    assert settle(job, board_row(), [newer, job]) == ("superseded", "newer job")

@pytest.mark.parametrize("kind", ["edit", "draft"])
async def test_recover_leaves_small_jobs(tmp_path, monkeypatch, kind):
    rig = Rig(tmp_path, monkeypatch)
    review = rig.add_job()
    small = rig.add_job(kind=kind, key=f"{review.id}:2")
    for job in (review, small):
        rig.store.set_job_state(job.id, "running", "", T0.isoformat())
    rig.runner.recover()
    assert rig.state(review) == ("failed", "interrupted (daemon restarted)")
    assert rig.state(small) == ("running", "")

async def test_archive_keeps_posting_out(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    def write(paths):
        write_review(paths)
        (paths.run_dir / "posting").mkdir()
        (paths.run_dir / "posting" / "report.base.md").write_text("a planted base")
    rig.write = write
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    assert not (rig.job_dir(job) / "posting").exists()
    assert rig.store.run(job.id).notes == [reserved_note("posting")]

THREADS = {"number": 1712, "author": "octo-author", "head_sha": HEAD,
           "threads": [{"id": "T2", "turn": "octo-author"}], "review_bodies": []}

async def test_prepared_threads_kept_before_the_session(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    prepared = json.dumps(THREADS).encode()
    rewritten = prepared.replace(b'"turn": "octo-author"', b'"turn": "octo"')
    async def prepare_writing_threads(ctx, job, paths, **kw):
        result = await rig._prepare(ctx, job, paths, **kw)
        (paths.run_dir / "threads.json").write_bytes(prepared)
        return result
    monkeypatch.setattr(runner_module, "prepare", prepare_writing_threads)
    def write(paths):
        write_review(paths)
        (paths.run_dir / "threads.json").write_bytes(rewritten)
        (paths.run_dir / "threads.prepared.json").write_bytes(rewritten)
    rig.write = write
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    job_dir = rig.job_dir(job)
    assert (job_dir / "threads.prepared.json").read_bytes() == prepared
    assert (job_dir / "threads.json").read_bytes() == rewritten
    assert rig.store.run(job.id).notes == [reserved_note("threads.prepared.json")]

TWO_PART_REPORT = (_HEAD_PART + "## Dropped (never posted)\n"
                   + "".join(f"- {n}. Candidate {n} - {'too speculative to keep ' * 6}(xhigh)\n"
                             for n in range(7, 31))
                   + _REST[_REST.index("\n## ✅"):])

async def test_posted_job_gets_the_rest_of_its_message(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: board_row(actions=())})   # posted: its review turn is gone
    job = rig.add_job()
    rig.store.set_job_state(job.id, "posted", "", T0.isoformat())
    report = parse_report(TWO_PART_REPORT, HEAD)
    rig.store.save_run(JobRun(job.id, title="Fix cart sync", url=URL, ticket_key="ACME-1234", files=14,
                              report=report.to_dict(), message_ids=[501], send_attempts=1,
                              last_attempt_at=T0.isoformat()))
    parts = split_message(review_message(SCOPE, 1712, "Fix cart sync", "ACME-1234", URL, 14, report, {}),
                          limit=3900)
    assert len(parts) == 2
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.chat.texts == [parts[1]]
    assert rig.store.anchor(CHAT_ID, 100).job_id == job.id
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == [501, 100]
    assert rig.state(job) == ("posted", "")
    assert rig.store.done_unsent() == []

async def test_failure_line_anchored_to_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.prepare_error = PrepareError("failed", "boom", drift=True)
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.chat.texts == [FAILED + "boom", DRIFT]
    assert rig.store.anchor(CHAT_ID, 100) == Anchor(job.id, "", 0)
    assert rig.store.anchor(CHAT_ID, 101) == Anchor("", SCOPE, 0)   # the drift line: the repo's

async def test_posted_at_head_counted(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    stamp = T0.isoformat()
    rig.store.save_post(Post("p1", "j0", SCOPE, 1712, HEAD, "review", "posted", stamp, comments=3))
    rig.store.save_post(Post("p2", "j9", SCOPE, 1712, HEAD, "replies", "posted", stamp, comments=2))   # not counted
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    [message] = rig.chat.texts
    assert message.splitlines()[3] == "Already posted at this head: 3 comments"
    assert rig.store.run(job.id).posted_at_head == 3

# --- Plan 5: retention sweep ---

@pytest.mark.parametrize("ledgers", [True, False])
async def test_runner_sweeps_when_idle_once_a_day(tmp_path, monkeypatch, caplog, ledgers):
    ledgers_dir = tmp_path / "rv" / "ledgers" if ledgers else None
    rig = Rig(tmp_path, monkeypatch, repos=("app", "web"), ledgers_dir=ledgers_dir)
    swept, dirs, memory = [], set(), {}
    async def sweep(ctx, store, *, jobs_dir, ledgers_dir, now, last_read):   # an assert here would be logged
        swept.append((ctx.repo_id, now))
        dirs.add((store is rig.store, jobs_dir, ledgers_dir))
        memory.setdefault(ctx.repo_id, []).append(last_read)
        last_read[len(swept)] = now   # as sweep records a read
        if ctx.repo_id == "app":
            raise RuntimeError("boom")   # logged; the next context goes on
        return [7, 8]
    monkeypatch.setattr(runner_module, "sweep", sweep)
    stop, times = asyncio.Event(), [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=25)]
    def next_job():   # one idle cycle at each time, then stop
        if times:
            rig.t = times.pop(0)
        else:
            stop.set()
    monkeypatch.setattr(rig.runner, "next_job", next_job)
    with caplog.at_level(logging.INFO, logger="orchestrator.reviewer.runner"):
        await asyncio.wait_for(rig.runner.run(stop), 5)
    if not ledgers:
        assert swept == []
        return
    later = T0 + timedelta(hours=25)
    assert swept == [("app", T0), ("web", T0), ("app", later), ("web", later)]
    assert dirs == {(True, tmp_path / "rv" / "jobs", ledgers_dir)}
    # One memory of the PRs read per repo, kept from one sweep to the next.
    [app_first, app_later], [web_first, web_later] = memory["app"], memory["web"]
    assert app_first is app_later and web_first is web_later and app_first is not web_first
    assert app_first == {1: T0, 3: later} and web_first == {2: T0, 4: later}
    assert "RuntimeError at " in caplog.text and "boom" not in caplog.text
    assert caplog.text.count("Reviewer retention sweep of acme/web: swept #7, #8") == 2
    assert "acme/app: swept" not in caplog.text and str(tmp_path) not in caplog.text


@pytest.mark.parametrize("state, sent, attempts, expected", [
    ("done", False, 0, True),
    ("posted", False, 4, True),
    ("done", False, 5, False),  # the fifth failed delivery: no more resends
    ("posted", True, 0, False),
    ("superseded", False, 1, False),
    ("stale", False, 0, False),
])
def test_may_still_save(state, sent, attempts, expected):
    """The rule the resend follows: the runner saves a run again only while it holds a done or posted job's
    message that is not all out and that it has not given up on."""
    job = Job("j1", SCOPE, 1712, "review", HEAD, HEAD, state, "", T0.isoformat(), T0.isoformat())
    assert may_still_save(job, JobRun("j1", message_sent=sent, send_attempts=attempts)) is expected


# --- Parallel review jobs ---

class Gate:
    """A session per job that waits for its release; counts the sessions in flight and the most at once."""
    def __init__(self, rig, monkeypatch):
        self.rig, self.now, self.most, self.released = rig, 0, 0, {}
        self.started: dict[int, asyncio.Event] = {}
        self.outcomes: dict[int, SessionOutcome] = {}
        self.per_pr_overlap = False
        self.running_prs: set[int] = set()
        monkeypatch.setattr(runner_module, "run_session", self.session)
    def number(self, kw):
        return int(kw["paths"].run_dir.name.removeprefix("PR_"))
    async def session(self, llm, **kw):
        n = self.number(kw)
        if n in self.running_prs:
            self.per_pr_overlap = True
        self.running_prs.add(n)
        self.now += 1
        self.most = max(self.most, self.now)
        self.started.setdefault(n, asyncio.Event()).set()
        try:
            await self.released.setdefault(n, asyncio.Event()).wait()
            write_review(kw["paths"])
            return self.outcomes.get(n, SessionOutcome("ok", cost_usd=1.0, turns=1))
        finally:
            self.now -= 1
            self.running_prs.discard(n)
    async def wait_started(self, *numbers):
        await asyncio.wait_for(asyncio.gather(*(self.started.setdefault(n, asyncio.Event()).wait()
                                                for n in numbers)), 5)
    def release(self, *numbers):
        for n in numbers:
            self.released.setdefault(n, asyncio.Event()).set()

def three_prs(rig, numbers=(1712, 1713, 1714)):
    rig.store.replace_board(SCOPE, {n: board_row(number=n) for n in numbers})
    return [rig.add_job(number=n) for n in numbers]

async def run_loop(rig):
    stop = asyncio.Event()
    return stop, asyncio.create_task(rig.runner.loop(stop))

async def test_jobs_of_different_prs_run_together(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    gate = Gate(rig, monkeypatch)
    jobs = three_prs(rig)
    stop, loop = await run_loop(rig)
    await gate.wait_started(1712, 1713, 1714)
    assert gate.most == 3
    gate.release(1712, 1713, 1714)
    for _ in range(200):
        if all(rig.state(j)[0] == "done" for j in jobs):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert [rig.state(j)[0] for j in jobs] == ["done"] * 3
    assert not any(rig.root(j).exists() for j in jobs)

async def test_cap_limits_jobs_in_flight(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, max_parallel=2)
    gate = Gate(rig, monkeypatch)
    jobs = three_prs(rig)
    stop, loop = await run_loop(rig)
    await gate.wait_started(1712, 1713)
    await asyncio.sleep(0.1)   # several cycles: the third waits for a free place
    assert gate.most == 2 and rig.state(jobs[2])[0] == "queued"
    gate.release(1712)
    await gate.wait_started(1714)
    gate.release(1713, 1714)
    for _ in range(200):
        if all(rig.state(j)[0] == "done" for j in jobs):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert gate.most == 2 and [rig.state(j)[0] for j in jobs] == ["done"] * 3

async def test_one_job_per_pr_at_a_time(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    gate = Gate(rig, monkeypatch)
    first = rig.add_job()
    second = rig.add_job(key=HEAD + ":now")   # a second review of the same PR (Review now, say)
    stop, loop = await run_loop(rig)
    await gate.wait_started(1712)
    await asyncio.sleep(0.1)
    assert rig.state(second)[0] == "queued" and gate.most == 1
    gate.release(1712)
    for _ in range(300):
        if rig.state(first)[0] != "running" and rig.state(second)[0] not in ("queued", "running"):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert not gate.per_pr_overlap

@pytest.mark.parametrize("ending", ["done", "session failed", "prepare error", "unexpected"])
async def test_job_dir_removed_whatever_the_outcome(tmp_path, monkeypatch, ending):
    rig = Rig(tmp_path, monkeypatch)
    if ending == "session failed":
        rig.outcome = SessionOutcome("failed", error="exit 1")
    elif ending == "prepare error":
        rig.prepare_error = PrepareError("failed", "boom")
    elif ending == "unexpected":
        def broken(paths):
            raise ValueError("not ours")
        rig.write = broken
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert not rig.root(job).exists() and rig.root(job).parent.is_dir()
    assert rig.job_dir(job).is_dir()   # the job's copy is kept

async def test_recover_removes_leftover_job_dirs(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    work = tmp_path / "rv" / "work"
    leftover = work / "acme" / "app" / "dead1" / "clone" / ".git"
    leftover.mkdir(parents=True)
    (leftover / "objects.pack").write_bytes(b"x")
    await rig.runner.start()   # off the event loop: a job clone can hold many files
    assert list(work.iterdir()) == []

async def test_low_disk_holds_new_jobs(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, monkeypatch, min_free_bytes=5 * 1024**3)
    free = {"bytes": 1024**3}
    monkeypatch.setattr(runner_module.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=0, used=0, free=free["bytes"]))
    job = rig.add_job()
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        assert rig.runner.next_job() is None
        assert rig.runner.next_job() is None
    assert caplog.text.count("Reviewer jobs held") == 1 and rig.state(job)[0] == "queued"
    free["bytes"] = 50 * 1024**3
    assert rig.runner.next_job().id == job.id

@pytest.mark.parametrize("kind", ["quota", "login"])
async def test_a_wait_is_not_lifted_by_a_job_started_before_it(tmp_path, monkeypatch, kind):
    rig = Rig(tmp_path, monkeypatch)
    gate = Gate(rig, monkeypatch)
    early, failing, later = three_prs(rig)
    gate.outcomes[1713] = quota(rig) if kind == "quota" else SessionOutcome("login")
    early_task = asyncio.create_task(rig.runner.run_job(early))
    await gate.wait_started(1712)
    rig.t += timedelta(minutes=1)
    gate.release(1713)
    await rig.runner.run_job(failing)   # the wait begins
    assert rig.state(failing)[0] == "deferred"
    gate.release(1712)
    await asyncio.wait_for(early_task, 5)   # started before the wait: ends well, lifts nothing
    assert rig.state(early)[0] == "done"
    assert rig.runner.next_job() is None    # still waiting
    rig.t += timedelta(hours=2)
    gate.release(1714)
    if kind == "login":
        rig.failing.discard("claude_login")
    job = rig.runner.next_job()
    assert job is not None and job.id in (failing.id, later.id)

async def test_cancelled_loop_cancels_its_jobs(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    gate = Gate(rig, monkeypatch)
    jobs = three_prs(rig, (1712, 1713))
    stop, loop = await run_loop(rig)
    await gate.wait_started(1712, 1713)
    inner = [task for _, task in rig.runner._in_flight.values()]
    assert len(inner) == 2
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop
    assert all(task.done() for task in inner) and rig.runner._in_flight == {}   # no job task outlives the loop
    assert [rig.state(j)[0] for j in jobs] == ["running", "running"]   # recover() fails them at the next start
    assert not any(rig.root(j).exists() for j in jobs)

async def test_resend_skips_a_job_still_delivering(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.add_job()
    rig.chat.fail = 1
    await rig.runner.run_job(job)   # done, its message not out
    assert rig.state(job) == ("done", "") and not rig.store.run(job.id).message_sent
    rig.runner._in_flight[job.id] = (job, asyncio.get_running_loop().create_future())   # its task still running
    await rig.runner.resend_unsent()
    assert rig.chat.tries == 1   # nothing sent beside the job's own delivery

async def test_jobs_hitting_the_limit_together_send_one_line(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    jobs = three_prs(rig)
    rig.outcome = quota(rig)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)
    await asyncio.wait_for(rig.runner.run(stop), 5)
    assert rig.prepares == 3   # all three started together, before any hit the limit
    assert [rig.state(j)[0] for j in jobs] == ["deferred"] * 3
    assert len([t for t in rig.chat.texts if "usage limit reached" in t]) == 1

async def test_a_requeued_job_waits_for_an_idle_cycle(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, idle_seconds=3600)
    gate = Gate(rig, monkeypatch)
    requeued, other = three_prs(rig, (1712, 1713))
    tries = []
    fake = rig._prepare
    async def prepare(ctx, job, paths, **kw):
        if job.number == 1712:
            tries.append(job.id)
            raise PrepareError("requeue", "forge token failing")
        return await fake(ctx, job, paths, **kw)
    monkeypatch.setattr(runner_module, "prepare", prepare)
    stop, loop = await run_loop(rig)
    await gate.wait_started(1713)
    gate.release(1713)   # the other job ends: the loop wakes, but no idle wait has passed
    for _ in range(100):
        if rig.state(other)[0] == "done":
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert tries == [requeued.id] and rig.state(requeued)[0] == "queued"   # not prepared again yet
    stop.set()
    await asyncio.wait_for(loop, 5)

async def test_a_wait_extended_by_a_later_hit_is_not_lifted_by_a_job_started_before_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    gate = Gate(rig, monkeypatch)
    first, d, e = three_prs(rig)
    gate.outcomes[1712] = quota(rig)
    gate.release(1712)
    await rig.runner.run_job(first)   # the first hit: wait until T0 + 1 h
    rig.t += timedelta(hours=1)       # the wait over: D and E start
    d_task, e_task = (asyncio.create_task(rig.runner.run_job(j)) for j in (rig.store.job(d.id), rig.store.job(e.id)))
    await gate.wait_started(1713, 1714)
    rig.t += timedelta(minutes=1)
    gate.outcomes[1713] = quota(rig)
    gate.release(1713)
    await asyncio.wait_for(d_task, 5)   # a later hit: the wait runs again
    gate.release(1714)
    await asyncio.wait_for(e_task, 5)   # E started before that hit: it lifts nothing
    assert rig.state(e)[0] == "done"
    assert rig.runner.next_job() is None


# --- Final review fixes ---

async def test_a_failed_job_keeps_its_error_when_its_dir_is_not_removed(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.outcome = SessionOutcome("failed", error="exit 1: boom")
    def failing_rmtree(path, *args, **kwargs):
        raise OSError("busy")
    monkeypatch.setattr(runner_module.shutil, "rmtree", failing_rmtree)
    job = rig.add_job()
    await rig.runner.run_job(job)
    run = rig.store.run(job.id)
    assert rig.state(job) == ("failed", "exit 1: boom")
    assert (run.error, run.notes) == ("exit 1: boom", ["job directory not removed: OSError"])

async def test_an_error_escaping_a_job_is_logged_and_the_job_waits_a_cycle(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, monkeypatch, idle_seconds=3600)
    job = rig.add_job()
    calls = []
    async def broken(j):
        calls.append(j.id)
        raise RuntimeError("store gone")
    monkeypatch.setattr(rig.runner, "run_job", broken)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.runner"):
        await asyncio.wait_for(rig.runner.run(stop), 5)
    assert calls == [job.id]   # not started again at once: no idle wait has passed
    assert f"Reviewer job {job.id} (acme/app #1712) failed: RuntimeError at " in caplog.text
    assert "store gone" not in caplog.text


# --- Address jobs ---

ADDRESS_PLAN = [{"n": 1, "thread_id": "T1", "body": "Fixed in {fix_sha}: null check.", "resolve": True, "fixed": True},
                {"n": 2, "thread_id": "T2", "body": "Intended.", "resolve": False}]
FIX = {"files": [{"path": "a.kt", "added": 3, "deleted": 1}], "protected": [], "bytes": 120}

def write_address(paths, plan=ADDRESS_PLAN):
    paths.replies_plan.write_text(json.dumps(plan))
    (paths.run_dir / "tg_lines.json").write_text(json.dumps({"1": "FIXED · a.kt:3 · null check"}))
    (paths.worktree / "a.kt").write_text("fixed\n")

def address_rig(tmp_path, monkeypatch, fix=FIX):
    """The rig with the operator's own PR on the board and capture_fix faked (FakeClone runs no git)."""
    rig = Rig(tmp_path, monkeypatch)
    rig.store.replace_board(SCOPE, {1712: dataclasses.replace(board_row(actions=("answer_threads",)), roles=["author"])})
    rig.write, rig.captured = write_address, []
    async def capture(worktree, patch, protected, **kw):
        rig.captured.append((worktree, patch, protected))
        if isinstance(fix, Exception):
            raise fix
        return fix
    monkeypatch.setattr(runner_module, "capture_fix", capture)
    return rig

async def test_address_job_keeps_plan_fix_and_sends_the_message(tmp_path, monkeypatch):
    rig = address_rig(tmp_path, monkeypatch)
    job = rig.add_job("address", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    run = rig.store.run(job.id)
    assert run.replies == ADDRESS_PLAN and run.fix == FIX
    assert (rig.job_dir(job) / "replies_plan.json").is_file()
    assert rig.captured == [(rig.paths.worktree, rig.job_dir(job) / "fix.patch", [".github/**"])]
    assert rig.chat.texts == [address_message(SCOPE, 1712, "Fix cart sync", URL, HEAD, ADDRESS_PLAN,
                                              {1: "FIXED · a.kt:3 · null check"}, FIX)]
    assert rig.chat.texts[-1].startswith('[acme/app] Threads on your PR #1712')
    kw = rig.session_kw
    assert (kw["kind"], kw["job_cfg"]) == ("address", JOBS["address"])
    assert kw["prompt"] == address_prompt("docs/threads.md", 1712, rig.paths, "octo", "")
    assert not rig.root(job).exists()

async def test_address_prompt_carries_the_operators_instruction(tmp_path, monkeypatch):
    rig = address_rig(tmp_path, monkeypatch)
    job = rig.add_job("address", key=f"{HEAD}:T1")
    rig.store.save_run(JobRun(job.id, request={"instruction": "leave 2 as it is"}))
    await rig.runner.run_job(job)
    assert "The operator's instruction for this run: leave 2 as it is\n" in rig.session_kw["prompt"]
    assert rig.store.run(job.id).request == {"instruction": "leave 2 as it is"}

async def test_address_job_fails_when_the_worktree_git_pointer_changed(tmp_path, monkeypatch):
    rig = address_rig(tmp_path, monkeypatch)
    rig.during = lambda r: (r.paths.worktree / ".git").write_text("gitdir: /elsewhere\n")
    job = rig.add_job("address", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", runner_module._CONFIG_CHANGED) and rig.captured == []

async def test_address_job_fails_on_an_unreadable_plan_or_a_fix_error(tmp_path, monkeypatch):
    rig = address_rig(tmp_path, monkeypatch)
    rig.write = lambda p: write_address(p, [{"n": 1, "thread_id": "T1", "body": "x", "fix_sha": "abc"}])
    job = rig.add_job("address", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "report unreadable: #1 carries fix_sha") and rig.captured == []
    rig = address_rig(tmp_path / "second", monkeypatch, fix=FixError("the fix is too large: 2000000 bytes"))
    job = rig.add_job("address", key=f"{HEAD}:T1")
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "the fix is too large: 2000000 bytes")

async def test_address_jobs_are_recovered_and_wait_for_the_first_poll(tmp_path, monkeypatch):
    rig = address_rig(tmp_path, monkeypatch)
    running = rig.add_job("address", key=f"{HEAD}:T1")
    rig.store.set_job_state(running.id, "running", "", T0.isoformat())
    rig.runner.recover()
    assert rig.state(running) == ("failed", "interrupted (daemon restarted)")
    rig.store.save_repo_state(RepoState(SCOPE))   # no `me` recorded yet
    job = rig.add_job("address", key=f"{HEAD}:T2")
    assert rig.runner.next_job() is None and rig.state(job) == ("queued", "waiting for the first poll")
    rig.store.save_repo_state(RepoState(SCOPE, me="octo"))
    assert rig.runner.next_job().id == job.id

def test_a_patch_in_the_run_directory_is_never_kept(tmp_path):
    run_dir, job_dir = tmp_path / "run", tmp_path / "job"
    run_dir.mkdir(); job_dir.mkdir()
    (run_dir / "fix.patch").write_text("forged")
    assert runner_module._archive(run_dir, job_dir) == ["fix.patch"] and not (job_dir / "fix.patch").exists()

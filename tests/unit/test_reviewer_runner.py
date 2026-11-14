# tests/unit/test_reviewer_runner.py
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import stat
from datetime import datetime, timedelta, timezone

import pytest

from config.schemas import ProcessPartConfig, ReviewerConfig, ReviewerJobConfig
from orchestrator.reviewer import runner as runner_module
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.lines import drift_line, login_line
from orchestrator.reviewer.messages import recheck_message, review_message, split_message
from orchestrator.reviewer.models import (
    BoardRow, BoardSnapshot, Job, JobRun, PRDetails, PRFull, RepoState, SnapshotPR, Turn,
)
from orchestrator.reviewer.prepare import Prepared, PrepareError
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.runner import JobRunner, RunnerConfig, settle
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.session import SessionOutcome, recheck_prompt, review_prompt
from orchestrator.reviewer.store import ReviewerStore
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
    worktree_dir="../review-wt/PR_{pr}", helper_scripts=["tools/review/*.py"]))
JOBS = {"review": ReviewerJobConfig("opus", "xhigh", 5400), "recheck": ReviewerJobConfig("opus", "high", 3600)}
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
    chat_id = CHAT_ID
    def __init__(self): self.texts, self.next_id, self.fail, self.tries = [], 100, 0, 0
    async def send(self, text):
        self.tries += 1
        if self.fail:
            self.fail -= 1
            raise RuntimeError("telegram down")
        self.texts.append(text)
        self.next_id += 1
        return self.next_id - 1

class Rig:
    """A runner on a real store and switches, a fixed clock, a fake clone, chat and event bus; prepare and
    run_session patched on the runner module (the fake session writes what `write` writes)."""
    def __init__(self, tmp_path, monkeypatch, repos=("app",), idle_seconds=0.01, chat=True, clone=FakeClone):
        conn = connect(tmp_path / "rv" / "reviewer.db")
        self.store, self.switches = ReviewerStore(conn), Switches(conn)
        self.t, self.tmp, self.forge = T0, tmp_path, FakeForge()
        self.contexts = [RepoContext("acme", r, CFG, clone(tmp_path / f"clone-{r}"), None, self.forge)
                         for r in repos]
        self.clone = self.contexts[0].clone
        self.store.replace_board(SCOPE, {1712: board_row()})
        self.store.save_repo_state(RepoState(SCOPE, me="octo"))
        self.chat, self.failing, self.auth, self.events = FakeChat() if chat else None, set(), [], []
        self.outcome = SessionOutcome("ok", cost_usd=1.25, turns=42)
        self.drift, self.prepare_error, self.prepares = False, None, 0
        self.write, self.during, self.session_kw, self.paths = write_review, None, None, None
        self.runner = JobRunner(
            self.contexts, self.store, self.switches, self.failing.__contains__,
            lambda key, status: self.auth.append((key, status)), self.chat, object(), {"acme": "tracker"},
            RunnerConfig(tmp_path / "rv" / "jobs", [tmp_path / ".env"], JOBS),
            event_bus=self, now=lambda: self.t, environ=ENV, idle_seconds=idle_seconds)
        monkeypatch.setattr(runner_module, "prepare", self._prepare)
        monkeypatch.setattr(runner_module, "run_session", self._session)
    def emit(self, event_type, message, *, project_id=None, data=None):
        self.events.append((event_type, project_id, data))
    async def _prepare(self, ctx, job, paths, *, store, tracker, now):
        assert ctx.clone.lock.locked() and store is self.store and tracker == "tracker" and now == self.t.isoformat()
        await asyncio.sleep(0)  # a real prepare yields: a runner that loops on a job must not starve the clock
        self.prepares += 1
        if self.prepare_error:
            raise self.prepare_error
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        paths.replies_plan.parent.mkdir(parents=True, exist_ok=True)
        return Prepared(PR, self.drift)
    async def _session(self, llm, **kw):
        assert self.clone.lock.locked()
        self.session_kw, self.paths = kw, kw["paths"]
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
    assert rig.store.anchor_job(CHAT_ID, 100) == job.id
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
        "review", rig.clone.path, JOBS["review"], ENV)
    assert (kw["helper_scripts"], kw["secret_paths"]) == (["tools/review/*.py"], [tmp_path / ".env"])
    assert rig.clone.removed == [(rig.paths.worktree, CONFIG_TEXT)]

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

async def test_quota_defers_then_resumes(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.outcome = SessionOutcome("quota", retry_at=T0 + timedelta(hours=1))
    job = rig.add_job()
    await rig.runner.run_job(job)
    got = rig.store.job(job.id)
    assert (got.state, got.retry_at) == ("deferred", (T0 + timedelta(hours=1)).isoformat())
    assert rig.chat.texts == ["[acme/app] #1712: review deferred — usage limit reached; next try at 11:00 UTC"]
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
    assert len(rig.clone.removed) == 1  # in `finally`, whatever prepare did

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
    assert len(rig.clone.removed) == 1

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
    assert len(rig.clone.removed) == 1
    assert "ValueError at " in caplog.text and "not ours" not in caplog.text

async def test_session_changing_git_config_fails_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    config = rig.clone.path / ".git" / "config"
    def tamper(paths):
        write_review(paths)
        with config.open("a") as f:
            f.write('[credential]\n\thelper = "!sh evil.sh"\n')
    rig.write = tamper
    job = rig.add_job()
    await rig.runner.run_job(job)
    reason = "the session changed the review clone's git config"
    assert rig.state(job) == ("failed", reason)
    assert config.read_text() == CONFIG_TEXT
    assert rig.clone.removed == [(rig.paths.worktree, CONFIG_TEXT)]  # restored before the next git call
    assert rig.chat.texts == [FAILED + reason]

async def test_git_config_restore_ignores_a_planted_spare_path(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    git_dir = rig.clone.path / ".git"
    def tamper(paths):
        write_review(paths)
        (git_dir / "config.cleave").mkdir()
        (git_dir / "config").write_text('[core]\n\thooksPath = /tmp/evil\n')
    rig.write = tamper
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "the session changed the review clone's git config")
    assert (git_dir / "config").read_text() == CONFIG_TEXT
    assert sorted(p.name for p in git_dir.iterdir()) == ["config", "config.cleave"]  # no temp file left

async def test_cancelled_job_restores_config_and_stays_running(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    config, started = rig.clone.path / ".git" / "config", asyncio.Event()
    async def hanging(llm, **kw):
        rig.paths = kw["paths"]
        config.write_text("[core]\n\tfsmonitor = /tmp/evil\n")
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(runner_module, "run_session", hanging)
    job = rig.add_job()
    task = asyncio.create_task(rig.runner.run_job(job))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert config.read_text() == CONFIG_TEXT
    assert rig.clone.removed == [(rig.paths.worktree, CONFIG_TEXT)]
    assert rig.state(job) == ("running", "") and not rig.clone.lock.locked()

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

async def test_worktree_left_is_reported(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.clone.left = "has changes"
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    assert f"[acme/app] #1712: worktree {rig.paths.worktree} was not removed — has changes" in rig.chat.texts
    assert rig.store.run(job.id).notes == ["has changes"]

async def test_unsent_message_is_resent(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.chat.fail = 1
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("done", "")
    run = rig.store.run(job.id)
    assert (run.message_sent, run.send_attempts, run.last_attempt_at) == (False, 1, T0.isoformat())
    assert rig.chat.texts == [] and rig.store.anchor_job(CHAT_ID, 100) is None
    rig.t = T0 + timedelta(minutes=1)
    await rig.runner.resend_unsent()
    assert rig.chat.texts == [task4_review()]
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == [100] and run.send_attempts == 1
    assert rig.store.anchor_job(CHAT_ID, 100) == job.id
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
    assert [rig.store.anchor_job(CHAT_ID, i) for i in ids] == [job.id] * len(parts)
    run = rig.store.run(job.id)
    assert run.message_sent and run.message_ids == ids

async def test_resend_resumes_at_the_first_unsent_part(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.write = lambda paths: write_review(paths, report=LONG_REPORT, tg={})
    sends, calls = rig.chat.send, []
    async def second_part_fails(text):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("telegram down")
        return await sends(text)
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
    assert [rig.store.anchor_job(CHAT_ID, i) for i in run.message_ids] == [job.id] * len(parts)

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
    assert rig.store.anchor_job(CHAT_ID, 100) == job.id and run.message_sent
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
    rig = Rig(tmp_path, monkeypatch)
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
        async def send_message(self, chat_id, text):
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
    """One clone for a job and a poll: the poll's refresh and the job's worktree removal are steps."""
    def __init__(self, path, steps):
        super().__init__(path)
        self.steps = steps
    async def refresh(self):
        await self.steps["refresh"]()
        return "c" * 40
    def has_file(self, rel):
        return True
    async def remove_worktree(self, path):
        await self.steps["remove"]()
        return await super().remove_worktree(path)

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
    """A job and a poll on one clone, on the same store. Every step either takes on the clone is logged (the
    config restore too); a test holds the ones whose overlap it checks."""
    def __init__(self, tmp_path, monkeypatch):
        self.log = []
        self.steps = {name: Step(name, self.log) for name in ("refresh", "readiness", "prepare", "session", "remove")}
        self.running = asyncio.Event()
        super().__init__(tmp_path, monkeypatch, clone=lambda path: SharedClone(path, self.steps))
        restore = runner_module._restore
        def logged_restore(path, saved):
            self.log.append("restore")
            return restore(path, saved)
        monkeypatch.setattr(runner_module, "_restore", logged_restore)
        ctx = RepoContext("acme", "app", CFG, self.clone, PollReadiness(self.steps["readiness"]), PollForge())
        self.service = ReviewerService([ctx], self.store, self.switches, lambda key: False, None,
                                       lambda project_id, repo_id: set(), now=lambda: self.t)
        self.poll = lambda: self.service.poll(ctx)
    def emit(self, event_type, message, *, project_id=None, data=None):
        super().emit(event_type, message, project_id=project_id, data=data)
        if data["state"] == "running":
            self.running.set()
    async def _prepare(self, ctx, job, paths, **kw):
        await self.steps["prepare"]()
        return await super()._prepare(ctx, job, paths, **kw)
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

async def test_the_board_updates_during_a_session_without_a_refresh(tmp_path, monkeypatch):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["session"].hold()
    job = rig.add_job()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.steps["session"].started.wait()
    await asyncio.wait_for(rig.poll(), 5)
    assert rig.log == ["prepare start", "prepare end", "session start", "readiness start", "readiness end"]
    state = rig.store.repo_state(SCOPE)
    assert (state.status, state.last_poll_at) == ("ok", T0.isoformat())
    rig.steps["session"].go()
    await asyncio.wait_for(run, 5)
    assert rig.state(job) == ("done", "")

async def test_a_job_leaves_its_session_only_after_a_polls_readiness(tmp_path, monkeypatch):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["session"].hold()
    rig.steps["readiness"].hold()
    job = rig.add_job()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.steps["session"].started.wait()
    poll = asyncio.create_task(rig.poll())
    await rig.steps["readiness"].started.wait()
    rig.steps["session"].go()
    await rig.steps["session"].ended.wait()   # the job has gone as far as it can while the readiness runs
    rig.steps["readiness"].go()
    await asyncio.wait_for(asyncio.gather(poll, run), 5)
    log = rig.log
    assert log.index("session end") < log.index("readiness end") < log.index("restore") < log.index("remove start")
    assert rig.state(job) == ("done", "")

async def test_a_poll_leaves_the_clone_alone_while_a_job_prepares_or_cleans_up(tmp_path, monkeypatch):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["prepare"].hold()
    rig.steps["remove"].hold()
    job = rig.add_job()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.steps["prepare"].started.wait()
    await asyncio.wait_for(rig.poll(), 5)   # during Prepare
    rig.steps["prepare"].go()
    await rig.steps["remove"].started.wait()
    await asyncio.wait_for(rig.poll(), 5)   # during the worktree removal
    rig.steps["remove"].go()
    await asyncio.wait_for(run, 5)
    assert "refresh start" not in rig.log and "readiness start" not in rig.log
    assert rig.store.repo_state(SCOPE).last_poll_at == ""   # nothing recorded: the repo is polled again shortly
    assert rig.state(job) == ("done", "")

async def test_no_readiness_on_a_clone_whose_git_config_the_session_changed(tmp_path, monkeypatch, caplog):
    rig = Clash(tmp_path, monkeypatch)
    rig.steps["session"].hold()
    config = rig.clone.path / ".git" / "config"
    job = rig.add_job()
    run = asyncio.create_task(rig.runner.run_job(job))
    await rig.steps["session"].started.wait()
    with config.open("a") as f:
        f.write('[filter "x"]\n\tclean = sh evil.sh\n')
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.service"):
        await asyncio.wait_for(rig.poll(), 5)
    assert "readiness start" not in rig.log
    assert f"Reviewer poll of {SCOPE}: the session changed {config}; no readiness" in caplog.text
    assert rig.store.repo_state(SCOPE).last_poll_at == ""
    rig.steps["session"].go()
    await asyncio.wait_for(run, 5)
    assert rig.state(job) == ("failed", "the session changed the review clone's git config")
    assert config.read_text() == CONFIG_TEXT

async def test_git_attributes_the_session_created_are_removed(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    attributes = rig.clone.path / ".git" / "info" / "attributes"
    def plant(paths):
        write_review(paths)
        attributes.parent.mkdir()
        attributes.write_text("* filter=x\n")
    rig.write = plant
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "the session changed the review clone's git config")
    assert not attributes.exists() and not attributes.is_symlink()

async def test_git_attributes_the_session_changed_are_written_back(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    attributes = rig.clone.path / ".git" / "info" / "attributes"
    attributes.parent.mkdir()
    attributes.write_text("*.png binary\n")
    def tamper(paths):
        write_review(paths)
        attributes.write_text("* filter=x\n")
    rig.write = tamper
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job) == ("failed", "the session changed the review clone's git config")
    assert attributes.read_text() == "*.png binary\n"

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
    rig.clone.left = "has changes"
    rig.outcome = SessionOutcome("quota", retry_at=rig.t + timedelta(hours=1), cost_usd=1.5, turns=10)
    job = rig.add_job()
    await rig.runner.run_job(job)
    assert rig.state(job)[0] == "deferred"
    rig.t += timedelta(hours=1)
    rig.clone.left = ""
    rig.outcome = SessionOutcome("ok", cost_usd=2.0, turns=30)
    await rig.runner.run_job(rig.store.job(job.id))
    run = rig.store.run(job.id)
    assert rig.state(job) == ("done", "") and run.message_sent
    assert (run.notes, run.cost_usd, run.turns, run.deferrals) == (["has changes"], 3.5, 40, 1)
    assert (run.started_at, run.finished_at) == ((T0 + timedelta(hours=1)).isoformat(),) * 2

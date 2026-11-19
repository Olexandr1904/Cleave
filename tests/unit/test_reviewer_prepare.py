# tests/unit/test_reviewer_prepare.py
from __future__ import annotations
import asyncio, dataclasses, hashlib, json, subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import pytest
from config.schemas import ProcessPartConfig, ReviewerConfig
from integrations.base.tracker import TicketComment, TicketData
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.commands import CommandResult, base_env
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import Job, JobRun, PRComments, PRFull
from orchestrator.reviewer.prepare import (
    JobPaths, PrepareError, existing_comments_md, job_paths, prepare, section_hash, section_text,
    ticket_json,
)
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.report import Report, ReportItem, confirmed_findings
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore

T0 = "2026-11-20T10:00:00+00:00"
DOC = "docs/review/process.md"
TOKEN = "tok-SECRET"
PROCESS = (
    "# Review process\n\n## Prepare\n\nFetch the PR.\n\n```sh\n# anchor maps\npython3 tools/anchors.py\n```\n\n"
    "### Details\n\nmeta.env holds HEAD.\n\n## Review\n\nRead the diff.\n"
)
STEP = """#!/bin/sh
printf '{"pr": "%s", "worktree": "%s", "merge_base": "%s"}\\n' "$1" "$2" "$3" > "tools/review/runs/PR_$1/anchors.json"
"""
COMMENTS = PRComments(
    inline=({"login": "octo-rev", "path": "app/Cart.kt", "line": 12, "id": 501, "body": "Why not\nthe store?"},
            {"login": "octo-rev", "path": "app/Old.kt", "line": None, "id": 502, "body": "Outdated."}),
    reviews=({"login": "octo-rev", "state": "CHANGES_REQUESTED", "body": "See inline."},),
    conversation=({"login": "octo-author", "body": "Ready\r\nfor review."},),
)
TICKET = TicketData(id="ACME-1234", url="https://acme.example/browse/ACME-1234", summary="Cart sync",
                    description="Keep the cart in sync across devices.")

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

def commit(work, message):
    git("add", "-A", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message, cwd=work)

def push_doc(work, text):
    (work / DOC).write_text(text)
    commit(work, "doc")
    git("push", "origin", "HEAD:develop", cwd=work)

@pytest.fixture
def origin(tmp_path):
    """develop with the process document, and PR 7 (feature.txt on top of it) at refs/pull/7/head."""
    bare, work, pr_work = tmp_path / "origin.git", tmp_path / "work", tmp_path / "pr-work"
    git("init", "--bare", "-b", "develop", str(bare))
    git("clone", str(bare), str(work))
    (work / "docs" / "review").mkdir(parents=True)
    push_doc(work, PROCESS)
    git("clone", str(bare), str(pr_work))
    (pr_work / "feature.txt").write_text("feature")
    commit(pr_work, "pr")
    git("push", "origin", "HEAD:refs/pull/7/head", cwd=pr_work)
    return SimpleNamespace(bare=bare, work=work, base=git("rev-parse", "HEAD", cwd=work),
                           head=git("rev-parse", "HEAD", cwd=pr_work))

def pr_full(head, state="open"):
    return PRFull(7, "Fix cart sync ACME-1234", "Keeps the cart in sync.", "https://github.com/acme-org/acme-app/pull/7",
                  state, head, "develop", 20, 10, 14)

class FakeForge:
    def __init__(self, pr):
        self.pr, self.error = pr, None
    async def pr_full(self, number):
        if self.error:
            raise self.error
        return self.pr
    async def comments(self, number):
        return COMMENTS

class FakeReadiness:
    repo = "acme-org/acme-app"
    def __init__(self, head):
        self.snapshot = {"number": 7, "author": "octo-author", "head_sha": head,
                         "threads": [{"id": "T1", "turn": "octo"}], "review_bodies": []}
        self.calls, self.error = [], None
    async def threads(self, clone_dir, pr):
        self.calls.append((clone_dir, pr))
        if self.error:
            raise self.error
        return dict(self.snapshot)

class FakeTracker:
    def __init__(self):
        self.keys = []
    async def get_ticket(self, key):
        self.keys.append(key)
        return TICKET
    async def get_comments(self, key):
        self.keys.append(key)
        return [TicketComment(id="1", author="octo", created="2026-11-10", body="Also on tablets.")]

@dataclass
class World:
    ctx: RepoContext
    job: Job
    store: ReviewerStore
    forge: FakeForge
    readiness: FakeReadiness
    tracker: FakeTracker
    script: Path
    work: Path
    runs: int = 0

    def next_job_dir(self) -> Path:
        """A fresh directory per prepare, as the runner gives every job its own; paths and worktree follow it."""
        self.runs += 1
        self.root = self.work / f"job{self.runs}"
        self.paths = job_paths(self.root / "clone", self.ctx.cfg.process, 7, self.job.head_sha,
                               self.work.parent / "jobs", self.job.id)
        self.worktree = self.root / "review-wt" / "PR_7"
        return self.root / "clone"

    async def prepare(self):
        clone_dir = self.next_job_dir()
        return await prepare(self.ctx, self.job, self.paths, store=self.store, tracker=self.tracker, now=T0,
                             job_clone_dir=clone_dir)

@pytest.fixture
def world(tmp_path, origin):
    script = tmp_path / "anchors.sh"
    script.write_text(STEP)
    script.chmod(0o755)
    cfg = ReviewerConfig(enabled=True, base_branch="develop", process=ProcessPartConfig(
        doc=DOC, run_dir="tools/review/runs/PR_{pr}",
        replies_plan="tools/threads/runs/PR_{pr}/replies_{head9}.json", worktree_dir="../review-wt/PR_{pr}",
        prepare_steps=[[str(script), "{pr}", "{worktree}", "{merge_base}"]],
    ))
    clone = ReviewClone(tmp_path / "clone", f"file://{origin.bare}", "develop", TOKEN)
    forge, readiness = FakeForge(pr_full(origin.head)), FakeReadiness(origin.head)
    ctx = RepoContext("acme", "app", cfg, clone, readiness, forge)
    job = Job("job1", ctx.scope, 7, "review", origin.head, origin.head, "running", "", T0, T0)
    store = ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
    return World(ctx, job, store, forge, readiness, FakeTracker(), script, tmp_path / "work")

async def outcome(world):
    with pytest.raises(PrepareError) as e:
        await world.prepare()
    return e.value.outcome, e.value.reason

def test_job_paths(tmp_path):
    cfg = ProcessPartConfig(doc=DOC, run_dir="tools/review/runs/PR_{pr}",
                            replies_plan="tools/threads/runs/PR_{pr}/replies_{head9}.json",
                            worktree_dir="../review-wt/PR_{pr}")
    clone = tmp_path / "repos" / "acme" / "app"
    head = "abcdef123" + "0" * 31
    assert job_paths(clone, cfg, 7, head, tmp_path / "jobs", "j1") == JobPaths(
        run_dir=clone / "tools" / "review" / "runs" / "PR_7",
        worktree=tmp_path / "repos" / "acme" / "review-wt" / "PR_7",
        report=clone / "tools" / "review" / "runs" / "PR_7" / "report_abcdef123.md",
        replies_plan=clone / "tools" / "threads" / "runs" / "PR_7" / "replies_abcdef123.json",
        job_dir=tmp_path / "jobs" / "j1",
    )
    for run_dir in ("../x", "."):   # "." would have Prepare empty the clone itself
        with pytest.raises(PrepareError, match="^run_dir is outside the review clone$") as e:
            job_paths(clone, dataclasses.replace(cfg, run_dir=run_dir), 7, head, tmp_path / "jobs", "j1")
        assert e.value.outcome == "failed"
    with pytest.raises(PrepareError, match="^replies_plan is outside the review clone$"):
        job_paths(clone, dataclasses.replace(cfg, replies_plan="../plan.json"), 7, head, tmp_path / "jobs", "j1")
    # Placeholders config validation accepts but a job path has no value for.
    with pytest.raises(PrepareError, match=r"^run_dir: placeholder \{report\} has no value$") as e:
        job_paths(clone, dataclasses.replace(cfg, run_dir="tools/{report}"), 7, head, tmp_path / "jobs", "j1")
    assert e.value.outcome == "failed"
    with pytest.raises(PrepareError, match=r"^worktree_dir: placeholder \{job_dir\} has no value$"):
        job_paths(clone, dataclasses.replace(cfg, worktree_dir="../wt/{job_dir}"), 7, head, tmp_path / "jobs", "j1")
    clone.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (clone / "tools").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PrepareError, match="^run_dir is outside the review clone$"):
        job_paths(clone, cfg, 7, head, tmp_path / "jobs", "j1")

async def test_prepare_writes_artifacts(world, origin):
    prepared = await world.prepare()
    run, wt = world.paths.run_dir, world.worktree
    assert (run / "meta.env").read_text().splitlines() == [
        f"HEAD={origin.head}", "BASE=develop", "FILES=14", "CHANGED=30", f"MB={origin.base}", f"WT={wt}",
    ]
    assert (run / "pr_body.md").read_text() == "Fix cart sync ACME-1234\n\nKeeps the cart in sync.\n"
    assert (run / "existing_comments.md").read_text().splitlines() == [
        "- octo-rev `app/Cart.kt:12` [id 501]: Why not the store?",
        "- octo-rev `app/Old.kt` [id 502]: Outdated.",
        "- review by octo-rev [CHANGES_REQUESTED]: See inline.",
        "- conversation, octo-author: Ready for review.",
    ]
    assert existing_comments_md(PRComments((), (), ())) == "(none)\n"
    assert json.loads((run / "threads.json").read_text()) == world.readiness.snapshot
    assert world.readiness.calls == [(world.ctx.clone.path, 7)]   # with the token: from the review clone
    assert json.loads((run / "ticket.json").read_text()) == {
        "summary": "Cart sync", "description": "Keep the cart in sync across devices.",
        "comments": [{"author": "octo", "created": "2026-11-10", "body": "Also on tablets."}],
    }
    assert world.tracker.keys == ["ACME-1234", "ACME-1234"]
    assert json.loads((run / "anchors.json").read_text()) == {"pr": "7", "worktree": str(wt), "merge_base": origin.base}
    assert (wt / "feature.txt").read_text() == "feature"
    assert git("rev-parse", "HEAD", cwd=wt) == origin.head
    assert world.paths.replies_plan.parent.is_dir() and not world.paths.replies_plan.exists()
    assert (prepared.pr, prepared.drift, prepared.clone.path) == (world.forge.pr, False, world.root / "clone")
    assert git("rev-parse", "HEAD", cwd=prepared.clone.path) == origin.base

async def test_prepare_step_failure_is_redacted(world):
    world.ctx.cfg.process.prepare_steps = [["sh", "-c", f"echo 'fatal: bad token {TOKEN}' >&2; exit 1"]]
    state, reason = await outcome(world)
    assert state == "failed" and reason.startswith("prepare step 1 failed: ") and "***" in reason
    assert "SECRET" not in reason

async def test_prepare_step_timeout(world, origin, monkeypatch):
    calls = []
    async def timing_out(argv, *, cwd, env, timeout):
        calls.append((argv, cwd, env, timeout))
        return CommandResult(returncode=-9, stdout="", stderr="", timed_out=True)
    monkeypatch.setattr("orchestrator.reviewer.prepare.run_command", timing_out)
    assert await outcome(world) == ("failed", "prepare step 1 failed: timed out after 300 s")
    # A step's own git must never read the host's system or global config (credential helpers).
    env = {**base_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    assert calls == [([str(world.script), "7", str(world.worktree), origin.base], world.root / "clone", env, 300)]

async def test_prepare_step_bad_placeholder(world):
    world.ctx.cfg.process.prepare_steps = [["sh", "-c", "true", "{report}"]]
    assert await outcome(world) == ("failed", "prepare step 1: placeholder {report} has no value")

async def test_prepare_head_moved(world):
    pr = world.forge.pr
    world.forge.pr = dataclasses.replace(pr, head_sha="f" * 40)
    assert await outcome(world) == ("superseded", "head moved")
    # The API and the job agree, the fetched ref does not.
    world.job = dataclasses.replace(world.job, head_sha="f" * 40)
    assert await outcome(world) == ("superseded", "head moved")
    assert not world.worktree.exists()
    world.forge.pr, world.job = pr, dataclasses.replace(world.job, head_sha=pr.head_sha)
    world.readiness.snapshot["head_sha"] = "f" * 40
    assert await outcome(world) == ("superseded", "head moved")
    assert not (world.paths.run_dir / "threads.json").exists()

async def test_prepare_pr_closed(world):
    world.forge.pr = dataclasses.replace(world.forge.pr, state="closed")
    assert await outcome(world) == ("cancelled", "PR closed")

async def test_prepare_pr_base_differs(world):
    world.forge.pr = dataclasses.replace(world.forge.pr, base="release/2.0")
    assert await outcome(world) == ("failed", "PR base is release/2.0, not develop")

async def test_prepare_refuses_a_symlink_on_the_way(world, origin, tmp_path):
    outside = tmp_path / "outside" / "runs" / "PR_7"
    outside.mkdir(parents=True)
    world.next_job_dir()   # the plan's file name, the same for every job at this head
    plan = outside / world.paths.replies_plan.name
    for kept in (outside / "keep.txt", plan):
        kept.write_text("keep")
    # The base branch now has a symlink above run_dir, then one above the replies plan.
    (origin.work / "tools").mkdir()
    (origin.work / "tools" / "review").symlink_to(tmp_path / "outside")
    commit(origin.work, "link review")
    git("push", "origin", "HEAD:develop", cwd=origin.work)
    assert await outcome(world) == ("failed", "run_dir is outside the review clone")
    (origin.work / "tools" / "review").unlink()
    (origin.work / "tools" / "threads").symlink_to(tmp_path / "outside")
    commit(origin.work, "link threads")
    git("push", "origin", "HEAD:develop", cwd=origin.work)
    assert await outcome(world) == ("failed", "replies_plan is outside the review clone")
    assert sorted(p.name for p in outside.iterdir()) == sorted(["keep.txt", plan.name])

async def test_prepare_doc_missing(world, origin):
    (origin.work / DOC).unlink()
    commit(origin.work, "drop doc")
    git("push", "origin", "HEAD:develop", cwd=origin.work)
    assert await outcome(world) == ("failed", "docs/review/process.md not on develop")

async def test_prepare_forge_401_requeues(world):
    for status in ("GitHub 401", "GitHub 403"):
        world.forge.error = ForgeError(status)
        assert await outcome(world) == ("requeue", "forge token failing")

async def test_prepare_github_unavailable_requeues(world):
    # A 5xx, a timeout or a connection error (the forge says which): GitHub, not the job.
    for error in ("GitHub 500", "GitHub 502", "GitHub 503", "ConnectError", "ReadTimeout"):
        world.forge.error = ForgeError(error, unavailable=True)
        assert await outcome(world) == ("unavailable", "GitHub unavailable")
    for error in ("GitHub 404", "GitHub 422", "GitHub pagination did not end"):
        world.forge.error = ForgeError(error)
        assert await outcome(world) == ("failed", error)

async def test_prepare_fails_on_any_other_forge_error_with_its_text(world):
    world.forge.error = ForgeError("JSONDecodeError")   # a response GitHub sent, not one it failed to send
    assert await outcome(world) == ("failed", "JSONDecodeError")
    world.forge.error = ForgeError("GitHub " + "x" * 300)
    assert await outcome(world) == ("failed", ("GitHub " + "x" * 300)[:200])

async def test_prepare_errors_fail_with_their_text(world, tmp_path):
    world.forge.error = ForgeError("GitHub 404")
    assert await outcome(world) == ("failed", "GitHub 404")
    world.forge.error = None
    world.readiness.error = ReadinessError("exit 1: no such PR")
    assert await outcome(world) == ("failed", "exit 1: no such PR")
    world.ctx.clone = ReviewClone(tmp_path / "other", f"file://{tmp_path}/missing.git", "develop", TOKEN)
    state, reason = await outcome(world)
    assert state == "failed" and reason.startswith("git clone failed: ")

async def test_drift_alarm(world, origin):
    scope = world.ctx.scope
    assert (await world.prepare()).drift is False
    first = world.store.prepare_hash(scope)
    assert first == section_hash(section_text(PROCESS, "Prepare"))
    assert (await world.prepare()).drift is False
    changed = PROCESS.replace("python3 tools/anchors.py", "python3 tools/anchors.py --added")
    push_doc(origin.work, changed)
    assert (await world.prepare()).drift is True
    assert world.store.prepare_hash(scope) == section_hash(section_text(changed, "Prepare")) != first
    push_doc(origin.work, changed.replace("Read the diff.", "Read the whole diff."))   # outside the section
    assert (await world.prepare()).drift is False
    push_doc(origin.work, changed.replace("## Prepare", "## Preparation"))
    assert await outcome(world) == ("failed", 'section "Prepare" not found in docs/review/process.md')

async def test_drift_is_recorded_when_a_step_fails(world, origin):
    await world.prepare()
    changed = PROCESS.replace("Fetch the PR.", "Fetch the PR and its threads.")
    push_doc(origin.work, changed)
    world.ctx.cfg.process.prepare_steps = [["sh", "-c", "exit 1"]]
    with pytest.raises(PrepareError) as e:
        await world.prepare()
    assert (e.value.outcome, e.value.drift) == ("failed", True)
    assert e.value.reason.startswith("prepare step 1 failed:")
    assert world.store.prepare_hash(world.ctx.scope) == section_hash(section_text(changed, "Prepare"))
    with pytest.raises(PrepareError) as e:   # the same section again: no drift
        await world.prepare()
    assert e.value.drift is False

def test_section_text_levels():
    doc = ("# Process\n\n## Prepare\n\nStep one.\n\n### Details\n\nmore\n\n```sh\n# a shell comment\n```\n\n"
           "## Review\n\nRead.\n")
    details = "### Details\n\nmore\n\n```sh\n# a shell comment\n```\n\n"
    assert section_text(doc, "Prepare") == "## Prepare\n\nStep one.\n\n" + details
    assert section_text(doc, "Details") == details
    assert section_text(doc, "Review") == "## Review\n\nRead.\n"
    assert section_text(doc, "a shell comment") is None
    assert section_text(doc, "Re-check") is None
    closed = "## Prepare ##\n\nx\n\n## C# #\n\ny\n"   # CommonMark closing hashes; C#'s own # stays
    assert section_text(closed, "Prepare") == "## Prepare ##\n\nx\n\n"
    assert section_text(closed, "C#") == "## C# #\n\ny\n"
    assert section_hash("## Prepare  \nStep one.\t\n") == hashlib.sha256(b"## Prepare\nStep one.").hexdigest()

async def test_ticket_json_errors():
    assert await ticket_json(FakeTracker(), "") == {"error": "no ticket key in the PR title"}
    assert await ticket_json(None, "ACME-1234") == {"error": "no tracker for this project"}
    class Failing:
        async def get_ticket(self, key):
            raise RuntimeError("token abc")
    assert await ticket_json(Failing(), "ACME-1234") == {"error": "tracker read failed: RuntimeError"}


# --- Code-review fix wave 1 ---

def test_job_paths_fill_every_placeholder_a_path_may_use(tmp_path):
    """Config validation accepts exactly JOB_PATH_PLACEHOLDERS in these paths: job_paths fills each."""
    from config.schemas import JOB_PATH_PLACEHOLDERS
    names = "_".join(f"{{{n}}}" for n in sorted(JOB_PATH_PLACEHOLDERS))
    cfg = ProcessPartConfig(doc=DOC, run_dir=f"runs/{names}", replies_plan=f"plans/{names}/plan.json",
                            worktree_dir=f"../wt/{names}")
    paths = job_paths(tmp_path / "clone", cfg, 7, "abcdef123" + "0" * 31, tmp_path / "jobs", "j1")
    assert "{" not in f"{paths.run_dir} {paths.replies_plan} {paths.worktree}"

async def test_prepare_fills_every_placeholder_a_step_may_use(world, monkeypatch):
    """Config validation accepts exactly PREPARE_STEP_PLACEHOLDERS in a step: Prepare fills each."""
    from config.schemas import PREPARE_STEP_PLACEHOLDERS
    calls = []
    async def ran(argv, *, cwd, env, timeout):
        calls.append(argv)
        return CommandResult(returncode=0, stdout="", stderr="", timed_out=False)
    monkeypatch.setattr("orchestrator.reviewer.prepare.run_command", ran)
    names = sorted(PREPARE_STEP_PLACEHOLDERS)
    world.ctx.cfg.process.prepare_steps = [["tool", *(f"{{{n}}}" for n in names)]]
    await world.prepare()
    assert len(calls) == 1 and len(calls[0]) == 1 + len(names) and "{" not in " ".join(calls[0])


# --- Parallel review jobs: Prepare in a job clone ---

async def test_review_clone_is_locked_only_for_its_own_part(world, monkeypatch):
    lock = world.ctx.clone.lock
    seen = {}
    def record(name, fn):
        async def wrapped(*args, **kwargs):
            seen[name] = lock.locked()
            return await fn(*args, **kwargs)
        return wrapped
    for name in ("refresh", "fetch_pr_head", "make_job_clone", "detach_at_base", "merge_base", "add_worktree"):
        monkeypatch.setattr(ReviewClone, name, record(name, getattr(ReviewClone, name)))
    threads = world.readiness.threads
    async def recorded_threads(clone_dir, pr):
        seen["threads"] = lock.locked()
        return await threads(clone_dir, pr)
    world.readiness.threads = recorded_threads
    await world.prepare()
    assert seen == {"refresh": True, "fetch_pr_head": True, "make_job_clone": True, "threads": True,
                    "detach_at_base": False, "merge_base": False, "add_worktree": False}
    assert not lock.locked()

async def test_steps_run_in_the_job_clone_and_threads_in_the_review_clone(world):
    await world.prepare()
    # The readiness command needs the forge token: never from a job clone.
    assert world.readiness.calls == [(world.ctx.clone.path, 7)]
    assert json.loads((world.paths.run_dir / "threads.json").read_text()) == world.readiness.snapshot
    assert (world.root / "clone" / "tools" / "review" / "runs" / "PR_7" / "anchors.json").is_file()
    assert not (world.ctx.clone.path / "tools" / "review" / "runs").exists()   # nothing in the review clone

async def test_parallel_prepares_report_drift_once(world, origin):
    await world.prepare()   # the section's hash stored
    push_doc(origin.work, PROCESS.replace("Fetch the PR.", "Fetch the PR and its threads."))
    async def one():
        clone_dir = world.next_job_dir()
        return await prepare(world.ctx, world.job, world.paths, store=world.store, tracker=world.tracker,
                             now=T0, job_clone_dir=clone_dir)
    results = await asyncio.gather(one(), one())
    assert sorted(r.drift for r in results) == [False, True]

def test_worktree_outside_the_job_dir_is_refused(tmp_path):
    cfg = ProcessPartConfig(doc=DOC, run_dir="tools/review/runs/PR_{pr}",
                            replies_plan="tools/threads/runs/PR_{pr}/replies_{head9}.json",
                            worktree_dir="../../elsewhere/PR_{pr}")
    with pytest.raises(PrepareError, match="^worktree_dir is outside the job's directory$") as e:
        job_paths(tmp_path / "work" / "j1" / "clone", cfg, 7, "a" * 40, tmp_path / "jobs", "j1")
    assert e.value.outcome == "failed"

async def test_drift_is_carried_by_a_later_failure(world, origin):
    await world.prepare()
    push_doc(origin.work, PROCESS.replace("Fetch the PR.", "Fetch the PR first."))
    world.readiness.error = ReadinessError("exit 1: no such PR")
    with pytest.raises(PrepareError) as e:
        await world.prepare()
    assert (e.value.outcome, e.value.reason, e.value.drift) == ("failed", "exit 1: no such PR", True)


async def test_address_job_needs_its_document_on_the_base(world, origin):
    process = dataclasses.replace(world.ctx.cfg.process, address_doc="docs/threads.md")
    world.ctx.cfg = dataclasses.replace(world.ctx.cfg, process=process)
    world.job = dataclasses.replace(world.job, kind="address", key=f"{origin.head}:T1")
    assert await outcome(world) == ("failed", "docs/threads.md not on develop")
    (origin.work / "docs" / "threads.md").write_text("# Threads\n")
    commit(origin.work, "threads doc")
    git("push", "origin", "HEAD:develop", cwd=origin.work)
    assert (await world.prepare()).pr.head_sha == origin.head


async def test_a_review_job_does_not_need_the_address_document(world):
    process = dataclasses.replace(world.ctx.cfg.process, address_doc="docs/threads.md")
    world.ctx.cfg = dataclasses.replace(world.ctx.cfg, process=process)
    assert (await world.prepare()).pr.number == 7


# --- Findings jobs ---

def findings_job(world, origin, *kinds, review_head=None, doc=True, request=True):
    """world.job becomes a findings job of the operator's self-review at the job's head (review_head: the review's
    head), whose report has items of `kinds`; with `doc`, the address document is on the base."""
    process = dataclasses.replace(world.ctx.cfg.process, address_doc="docs/threads.md")
    world.ctx.cfg = dataclasses.replace(world.ctx.cfg, process=process)
    if doc:
        (origin.work / "docs" / "threads.md").write_text("# Threads\n")
        commit(origin.work, "threads doc")
        git("push", "origin", "HEAD:develop", cwd=origin.work)
    head = review_head or origin.head
    review = world.store.add_job("acme/app", 7, "review", head, f"{head}:now", T0)
    world.store.set_job_state(review.id, "done", "", T0)
    items = tuple(ReportItem(i, k, f"t{i}", "feature.txt", i if i != 2 else None, "", f"b{i}")
                  for i, k in enumerate(kinds, 1))
    report = Report("T", head, "", None, "COMMENT", "", items, (), "", "").to_dict()
    world.store.save_run(JobRun(review.id, report=report))
    world.job = dataclasses.replace(world.job, kind="findings", key=f"{origin.head}:{review.id}")
    world.store.save_run(JobRun(world.job.id, request={"review": review.id} if request else {}))
    return review, report


async def test_findings_job_writes_its_findings(world, origin):
    _, report = findings_job(world, origin, "red", "yellow", "question", "orange", "preexisting")
    await world.prepare()
    got = json.loads((world.paths.run_dir / "findings.json").read_text())
    assert got == confirmed_findings(report)
    assert [(f["n"], f["severity"], f["line"]) for f in got] == [(1, "critical", 1), (2, "minor", None),
                                                                  (4, "major", 4)]
    assert git("rev-parse", "HEAD", cwd=world.worktree) == origin.head


async def test_findings_job_needs_the_address_document_on_the_base(world, origin):
    findings_job(world, origin, "red", doc=False)
    assert await outcome(world) == ("failed", "docs/threads.md not on develop")


async def test_findings_job_fails_without_its_self_review(world, origin):
    findings_job(world, origin, "red", request=False)
    assert await outcome(world) == ("failed", "the self-review's report is gone")
    world.store.save_run(JobRun(world.job.id, request={"review": "gone"}))
    assert await outcome(world) == ("failed", "the self-review's report is gone")


async def test_findings_job_fails_when_the_review_has_no_report(world, origin):
    review, _ = findings_job(world, origin, "red")
    world.store.save_run(JobRun(review.id))
    assert await outcome(world) == ("failed", "the self-review's report is gone")


async def test_findings_job_fails_on_a_report_of_another_head(world, origin):
    findings_job(world, origin, "red", review_head="b" * 40)
    assert await outcome(world) == ("failed", "the self-review's report is gone")


async def test_findings_job_fails_without_a_confirmed_finding(world, origin):
    findings_job(world, origin, "question", "preexisting")
    assert await outcome(world) == ("failed", "no confirmed findings")


async def test_findings_job_fails_before_the_review_clones_lock(world, origin):
    findings_job(world, origin, "question")
    await world.ctx.clone.lock.acquire()     # held: a findings check after it would wait forever
    try:
        assert await asyncio.wait_for(outcome(world), 5) == ("failed", "no confirmed findings")
    finally:
        world.ctx.clone.lock.release()

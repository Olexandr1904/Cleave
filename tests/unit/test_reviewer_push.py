# tests/unit/test_reviewer_push.py
"""Posting.push_fix: an address job's patch committed on the PR head and pushed, on the operator's "push"."""
from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config.schemas import ProcessPartConfig, ReviewerConfig
from orchestrator.reviewer.board import BoardUpdate
from orchestrator.reviewer.clone import CloneError
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.jobs import plan_jobs
from orchestrator.reviewer.lines import interrupted_line
from orchestrator.reviewer.models import BoardRow, JobRun, Post, PRFull, RepoState
from orchestrator.reviewer.posting import Posting
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore

T0 = "2026-11-20T10:00:00+00:00"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
SCOPE, N = "acme/app", 1712
HEAD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40
URL = f"https://github.com/acme-org/acme-app/pull/{N}"
CFG = ReviewerConfig(enabled=True, base_branch="develop", process=ProcessPartConfig(address_doc="docs/threads.md"))
FIX = {"files": [{"path": "a.kt", "added": 1, "deleted": 1}], "protected": [], "bytes": 10}
REPLIES = [{"n": 1, "thread_id": "T1", "body": "Fixed in {fix_sha}: null check.", "resolve": True, "fixed": True},
           {"n": 2, "thread_id": "T2", "body": "Intended.", "resolve": False}]
VERDICTS = [{"n": 1, "verdict": "FIXED", "why": "null check added"}]


def pr(head=HEAD, **kw):
    fields = dict(state="open", head_sha=head, head_ref="feature/x", head_repo="acme-org/acme-app")
    return PRFull(N, "Fix cart sync ACME-1234", "", URL, base="develop", additions=1, deletions=1,
                  changed_files=1, **{**fields, **kw})


def row(head=HEAD, pipeline=False, threads=("T1", "T2")):
    return BoardRow(N, "Fix cart sync ACME-1234", URL, "octo", ["author"], head, T0, False, 1,
                    [{"action": "answer_threads", "threads": list(threads)}], [], False, pipeline, "ACME-1234",
                    T0, T0)


class FakeClone:
    """The review clone's lock and lease; push_patch records its keywords and leaves its worktree behind."""

    def __init__(self):
        self.lock, self.pushed, self.error, self.leases = asyncio.Lock(), [], None, 0

    @asynccontextmanager
    async def reader(self, *, wait=600.0):
        async with self.lock:
            self.leases += 1
            yield

    async def push_patch(self, *, number, head, branch, patch, message, author, worktree):
        assert self.lock.locked() and patch.read_text() == "the patch"
        worktree.mkdir(parents=True)
        (worktree / "a.kt").write_text("fixed")
        self.worktree = worktree
        if self.error is not None:
            raise self.error
        self.pushed.append({"number": number, "head": head, "branch": branch, "message": message, "author": author})
        return NEW


class FakeForge:
    """pr_full answers from `prs` in turn (the last one again once they run out); an exception is raised."""

    def __init__(self, *prs):
        self.prs, self.reads = list(prs), 0

    async def pr_full(self, number):
        self.reads += 1
        answer = self.prs.pop(0) if len(self.prs) > 1 else self.prs[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Rig:
    def __init__(self, tmp_path, *prs, fix=FIX, ticket="ACME-1234", kind="address"):
        self.store = ReviewerStore(connect(tmp_path / "rv.db"))
        self.store.save_repo_state(RepoState(SCOPE, me="octo"))
        self.store.replace_board(SCOPE, {N: row()})
        self.job = self.store.add_job(SCOPE, N, kind, HEAD, f"{HEAD}:T1" if kind == "address" else f"{HEAD}:r1", T0)
        self.store.set_job_state(self.job.id, "done", "", T0)
        job_dir = tmp_path / "jobs" / self.job.id
        job_dir.mkdir(parents=True)
        (job_dir / "fix.patch").write_text("the patch")
        self.job_dir = job_dir
        self.store.save_run(JobRun(self.job.id, job_dir=str(job_dir), replies=REPLIES if kind == "address" else [],
                                   verdicts=[] if kind == "address" else VERDICTS, fix=fix, ticket_key=ticket, url=URL))
        self.clone, self.forge = FakeClone(), FakeForge(*(prs or (pr(), pr(NEW))))
        self.ctx = RepoContext("acme", "app", CFG, self.clone, SimpleNamespace(repo="acme-org/acme-app"), self.forge,
                               commit_author=("Cleave Bot", "cleave@pipeline.local"))
        self.work = tmp_path / "work"
        self.posting = Posting(self.store, ledgers_dir=tmp_path / "ledgers", work_dir=self.work,
                               sleep=self._sleep, verify_wait=0)

    async def _sleep(self, seconds):
        pass

    async def push(self):
        return await self.posting.push_fix(self.ctx, self.job)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


PUSHED = (f"Pushed {NEW[:9]} to #1712: https://github.com/acme-org/acme-app/commit/{NEW}\n"
          'Reply "post replies 1" to answer the fixed threads.')


async def test_push_records_the_post_and_moves_the_job(rig):
    out = await rig.push()
    assert out.state == "posted" and out.text == PUSHED
    job = rig.store.job(rig.job.id)
    assert (job.head_sha, job.key, job.state) == (NEW, f"{NEW}:T1", "done")
    post = rig.store.push_of(job)
    assert (post.kind, post.state, post.head_sha, post.sha) == ("push", "posted", HEAD, NEW)
    assert post.urls == [f"https://github.com/acme-org/acme-app/commit/{NEW}"]
    assert rig.clone.pushed == [{"number": 1712, "head": HEAD, "branch": "feature/x", "message":
        "fix(ACME-1234): address review comments on #1712", "author": ("Cleave Bot", "cleave@pipeline.local")}]
    assert rig.clone.worktree.parent.parent == rig.work / "push" and not rig.clone.worktree.parent.exists()
    assert rig.clone.leases == 1 and not rig.clone.lock.locked()


async def test_push_without_a_ticket_key_and_with_the_fixed_reply_posted(tmp_path):
    rig = Rig(tmp_path, ticket="")
    rig.store.save_post(Post("r1", rig.job.id, SCOPE, N, HEAD, "replies", "posted", T0, T0, numbers=[1],
                             landed=[{"n": 1, "path": "", "line": None, "title": "", "where": "reply"}]))
    out = await rig.push()
    assert out.text == f"Pushed {NEW[:9]} to #1712: https://github.com/acme-org/acme-app/commit/{NEW}"
    assert rig.clone.pushed[0]["message"] == "fix: address review comments on #1712"


async def test_push_takes_the_repo_named_in_another_case(tmp_path):
    rig = Rig(tmp_path, pr(head_repo="Acme-Org/Acme-App"), pr(NEW))     # GitHub's spelling, not the config's
    assert (await rig.push()).state == "posted" and len(rig.clone.pushed) == 1

async def test_second_push_is_refused(rig):
    await rig.push()
    out = await rig.push()
    assert (out.state, out.text) == ("refused", f"Already pushed as {NEW[:9]}. Nothing was done.")
    assert len(rig.clone.pushed) == 1


async def test_push_refused_when_the_head_moved(tmp_path):
    rig = Rig(tmp_path, pr(OTHER))
    out = await rig.push()
    assert (out.state, out.text) == ("refused", f'Head moved ({HEAD[:9]} → {OTHER[:9]}): nothing pushed. '
                                                'Reply "again" for a new run on the new head.')
    assert rig.clone.pushed == [] and rig.store.posts(SCOPE, N) == [] and rig.clone.leases == 0


@pytest.mark.parametrize("change, text", [
    ({"fix": {}}, "This job changed no code: nothing to push."),
    ({"fix": {**FIX, "protected": [".github/ci.yml"]}},
     "The fix touches protected files (.github/ci.yml): Cleave does not push it. Nothing was done."),
    ({"pr": pr(head_repo="octo/acme-app")},
     "The PR's branch is in a fork (octo/acme-app): Cleave does not push there. Nothing was done."),
    ({"pr": pr(head_repo="")}, "The PR's branch is in a fork (?): Cleave does not push there. Nothing was done."),
    ({"pr": pr(head_ref="develop")}, "Cleave does not push to the branch develop. Nothing was done."),
    ({"pr": pr(head_ref="-x")}, "Cleave does not push to the branch -x. Nothing was done."),
    ({"pr": pr(head_ref="a b")}, "Cleave does not push to the branch a b. Nothing was done."),
    ({"pr": pr(state="closed")}, "PR #1712 is closed: nothing posted."),
    ({"pr": ForgeError("GitHub 502", unavailable=True)}, "GitHub could not be read for #1712 (GitHub 502): nothing posted."),
], ids=["no-fix", "protected", "fork", "fork-gone", "base", "dash", "space", "closed", "unreadable"])
async def test_push_refusals(tmp_path, change, text):
    rig = Rig(tmp_path, change.get("pr", pr()), fix=change.get("fix", FIX))
    out = await rig.push()
    assert (out.state, out.text) == ("refused", text)
    assert rig.clone.pushed == [] and rig.store.posts(SCOPE, N) == []


async def test_push_refused_without_the_patch_or_the_job(rig):
    (rig.job_dir / "fix.patch").unlink()
    (rig.job_dir / "fix.patch").symlink_to(rig.job_dir / "elsewhere")
    assert (await rig.push()).text == "Not pushed to #1712: the patch is not in this job's copy."
    rig.store.set_job_state(rig.job.id, "superseded", "new head", T0)
    assert (await rig.push()).text == "Outdated: there is a review of a newer head. Nothing was done."
    assert rig.clone.pushed == [] and rig.forge.reads == 0


async def test_push_refused_while_a_ticket_workspace_holds_the_pr(rig):
    rig.store.replace_board(SCOPE, {N: row(pipeline=True)})
    out = await rig.push()
    assert out.state == "refused"
    assert out.text == ("A ticket of the pipeline still holds #1712's branch and may overwrite it: "
                        'nothing pushed. Reply "push" again when the ticket is done.')
    assert rig.clone.pushed == [] and rig.forge.reads == 0


async def test_push_failure_is_reported_and_retryable(rig):
    rig.clone.error = CloneError("git push failed: rejected")
    out = await rig.push()
    assert (out.state, out.text) == ("failed", "Not pushed to #1712: git push failed: rejected.")
    job = rig.store.job(rig.job.id)
    assert (job.head_sha, job.key) == (HEAD, f"{HEAD}:T1")
    [post] = rig.store.posts(SCOPE, N)
    assert (post.kind, post.state, post.error) == ("push", "failed", "git push failed: rejected")
    assert not rig.clone.worktree.parent.exists()
    rig.clone.error = None
    rig.forge.prs = [pr(), pr(NEW)]
    assert (await rig.push()).state == "posted" and len(rig.clone.pushed) == 1


async def test_push_unverified_keeps_the_job_where_it_was(tmp_path):
    rig = Rig(tmp_path, pr(), pr(HEAD))   # GitHub still shows HEAD after the push, read after read
    out = await rig.push()
    assert out.state == "unverified"
    assert out.text == (f"The push to #1712 ran, but GitHub does not show {NEW[:9]} as the head. Check the PR's "
                        f"commits before pushing again: {URL}")
    [post] = rig.store.posts(SCOPE, N)
    assert (post.state, post.sha) == ("unverified", NEW)
    assert rig.store.job(rig.job.id).head_sha == HEAD and rig.forge.reads == 1 + 3


async def test_push_verified_once_github_catches_up(tmp_path):
    rig = Rig(tmp_path, pr(), pr(HEAD), ForgeError("GitHub 502", unavailable=True), pr(NEW))
    assert (await rig.push()).state == "posted" and rig.store.job(rig.job.id).head_sha == NEW


def test_interrupted_push_line():
    assert interrupted_line(SCOPE, N, "push") == (
        "[acme/app] #1712: a push was interrupted by a restart. Check the PR's commits, then reply \"push\" again "
        "if the fix is not there.")


def _plan(row_, jobs, posting=frozenset()):
    return plan_jobs(SCOPE, BoardUpdate({N: row_}, [], []), {N: jobs}, lambda n, h: False, CFG, True, NOW, False,
                     posting=posting)


async def test_board_poll_after_a_push_queues_nothing(rig):
    await rig.push()
    moved = rig.store.job(rig.job.id)
    assert _plan(row(NEW, threads=("T1",)), [moved]).queue == []


async def test_board_poll_during_a_push_queues_nothing(rig):
    # The commit is on GitHub, the job not moved yet: its push Post is `posting`.
    during = _plan(row(NEW, threads=("T1",)), [rig.job], posting=frozenset({rig.job.id}))
    assert (during.queue, during.states) == ([], [])
    assert _plan(row(NEW, threads=("T1",)), [rig.job]).queue == [(N, "address", NEW, f"{NEW}:T1")]


# --- Findings jobs ---

async def test_push_of_a_findings_fix(tmp_path):
    rig = Rig(tmp_path, kind="findings")
    out = await rig.push()
    assert (out.state, out.text) == (
        "posted", f"Pushed {NEW[:9]} to #1712: https://github.com/acme-org/acme-app/commit/{NEW}")   # no "post replies"
    assert rig.clone.pushed[0]["message"] == "fix(ACME-1234): self-review findings on #1712"
    job = rig.store.job(rig.job.id)
    assert (job.head_sha, job.key, job.state) == (NEW, f"{NEW}:r1", "done")
    plain = Rig(tmp_path / "plain", kind="findings", ticket="")
    await plain.push()
    assert plain.clone.pushed[0]["message"] == "fix: self-review findings on #1712"


async def test_push_refuses_a_findings_job_without_a_patch(tmp_path):
    rig = Rig(tmp_path, kind="findings", fix={})
    assert (await rig.push()).text == "This job changed no code: nothing to push."
    assert rig.clone.pushed == [] and rig.forge.reads == 0


async def test_board_poll_after_a_findings_push_keeps_the_job(tmp_path):
    """GitHub's list lags the push: a poll that still shows the old head leaves the pushed job alone."""
    rig = Rig(tmp_path, kind="findings")
    await rig.push()
    moved = rig.store.job(rig.job.id)

    def pushed_from(job):   # as ReviewerService._pushed_from
        push = rig.store.push_of(job)
        return push.head_sha if push else ""

    def poll(head):   # the operator's PR with no turn of theirs, at `head`
        update = BoardUpdate({N: dataclasses.replace(row(head), my_turns=[])}, [], [])
        return plan_jobs(SCOPE, update, {N: [moved]}, lambda n, h: False, CFG, True, NOW, False, pushed_from=pushed_from)

    for head in (HEAD, NEW):
        assert (poll(head).queue, poll(head).states) == ([], [])
    assert poll(OTHER).states == [(moved.id, "superseded", "new head")]

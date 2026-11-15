# tests/unit/test_reviewer_retention.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from orchestrator.reviewer import service as service_module
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import Post, PRFull
from orchestrator.reviewer.retention import MAX_READS, sweep
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore
from tests.unit.test_reviewer_runner import CFG, board_row

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
SCOPE = "acme/app"
HEAD = "a" * 40

def pr(number, closed_days=None):
    """PR `number` as GitHub has it: open, or closed `closed_days` before NOW (in GitHub's Z form)."""
    closed = "" if closed_days is None else (NOW - timedelta(days=closed_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return PRFull(number, "Fix cart sync", "", f"https://github.com/acme-org/acme-app/pull/{number}",
                  "closed" if closed else "open", HEAD, "develop", 1, 1, 1, closed_at=closed)

class Clone(ReviewClone):
    """The clone's lock as it is; delete_branch records the number and whether the lock was held, and raises
    for the numbers in `fail`."""
    def __init__(self, path):
        super().__init__(path, "file:///nowhere", "develop", "tok")
        self.deleted, self.fail = [], set()
    async def delete_branch(self, number):
        if number in self.fail:
            raise RuntimeError("boom")
        self.deleted.append((number, self.lock.locked()))

class Forge:
    """Each PR as GitHub has it (`prs[n]`, or the exception reading it raises); records the numbers read."""
    def __init__(self, prs):
        self.prs, self.reads = prs, []
    async def pr_full(self, number):
        self.reads.append(number)
        got = self.prs[number]
        if isinstance(got, Exception):
            raise got
        return got

class Rig:
    """A repo with a real store, the reviewer data dir under tmp_path, a fake clone and forge; no board. The
    PRs read, as the runner keeps them between sweeps, in `last_read`."""
    def __init__(self, tmp_path, prs):
        self.store = ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
        self.jobs_dir, self.ledgers_dir = tmp_path / "rv" / "jobs", tmp_path / "rv" / "ledgers"
        self.forge, self.clone = Forge(prs), Clone(tmp_path / "clone")
        self.ctx = RepoContext("acme", "app", CFG, self.clone, None, self.forge)
        self.made, self.last_read = 0, {}
    def job(self, number, kind="review"):
        """A job of PR `number`, created after the rig's earlier ones, with its copy in the jobs directory."""
        self.made += 1
        created = NOW - timedelta(days=60) + timedelta(minutes=self.made)
        job = self.store.add_job(SCOPE, number, kind, HEAD, HEAD, created.isoformat())
        (self.jobs_dir / job.id).mkdir(parents=True)
        (self.jobs_dir / job.id / f"report_{HEAD[:9]}.md").write_text("report")
        return job
    def ledger(self, number):
        path = self.ledgers_dir / "acme" / "app" / f"PR_{number}"
        path.mkdir(parents=True)
        (path / f"posted_{HEAD[:9]}.json").write_text("{}")
        return path
    def copy(self, job):
        return self.jobs_dir / job.id
    def marked(self, number):
        """Whether the PR is marked swept at its newest job."""
        return self.store.notice_sent(SCOPE, number, "swept", self.store.jobs(SCOPE, number)[0].id)
    async def sweep(self, now=NOW):
        return await sweep(self.ctx, self.store, jobs_dir=self.jobs_dir, ledgers_dir=self.ledgers_dir, now=now,
                           last_read=self.last_read)

async def test_sweep_removes_copies_closed_14_days_ago(tmp_path):
    rig = Rig(tmp_path, {7: pr(7, closed_days=15)})
    jobs = [rig.job(7), rig.job(7, "recheck")]
    ledger = rig.ledger(7)
    assert await rig.sweep() == [7]
    assert not any(rig.copy(j).exists() for j in jobs)
    assert not ledger.exists() and ledger.parent.is_dir()
    assert rig.clone.deleted == [(7, True)]   # under clone.lock
    assert rig.store.notice_sent(SCOPE, 7, "swept", jobs[1].id)   # marked at its newest job
    assert sorted(j.id for j in rig.store.jobs(SCOPE, 7)) == sorted(j.id for j in jobs)   # database rows stay
    reads = list(rig.forge.reads)
    assert await rig.sweep() == []
    assert rig.forge.reads == reads   # the next sweep reads nothing
    assert rig.clone.deleted == [(7, True)]

async def test_sweep_keeps_recent_open_and_on_board(tmp_path):
    rig = Rig(tmp_path, {7: pr(7, closed_days=13), 8: pr(8), 9: pr(9, closed_days=30)})
    jobs = [rig.job(7), rig.job(8), rig.job(9)]
    ledgers = [rig.ledger(7), rig.ledger(8), rig.ledger(9)]
    rig.store.replace_board(SCOPE, {9: board_row(number=9)})
    assert await rig.sweep() == []
    assert await rig.sweep(NOW + timedelta(hours=1)) == []
    assert rig.forge.reads == [7, 8, 7, 8]   # open: read again next sweep; on the board: never read
    assert all(rig.copy(j).is_dir() for j in jobs) and all(path.is_dir() for path in ledgers)
    assert rig.clone.deleted == []
    assert not any(rig.marked(n) for n in (7, 8, 9))
    assert await rig.sweep(NOW + timedelta(days=1)) == [7]   # closed 14 days ago by then
    assert not rig.copy(jobs[0]).exists() and rig.copy(jobs[1]).is_dir() and rig.copy(jobs[2]).is_dir()

async def test_sweep_never_leaves_its_dirs(tmp_path):
    rig = Rig(tmp_path, {7: pr(7, closed_days=15)})
    job = rig.store.add_job(SCOPE, 7, "review", HEAD, HEAD, NOW.isoformat())
    outside = tmp_path / "outside"
    (outside / "copy").mkdir(parents=True)
    (outside / "copy" / "file.txt").write_text("mine")
    (outside / "ledger").mkdir()
    (outside / "ledger" / "posted.json").write_text("{}")
    rig.jobs_dir.mkdir(parents=True)
    rig.copy(job).symlink_to(outside / "copy", target_is_directory=True)
    (rig.ledgers_dir / "acme" / "app").mkdir(parents=True)
    (rig.ledgers_dir / "acme" / "app" / "PR_7").symlink_to(outside / "ledger", target_is_directory=True)
    assert await rig.sweep() == [7]
    assert not rig.copy(job).is_symlink() and not rig.copy(job).exists()
    assert not (rig.ledgers_dir / "acme" / "app" / "PR_7").is_symlink()
    assert (outside / "copy" / "file.txt").read_text() == "mine"
    assert (outside / "ledger" / "posted.json").read_text() == "{}"

async def test_sweep_never_follows_a_symlink_on_the_way(tmp_path):
    rig = Rig(tmp_path, {7: pr(7, closed_days=15)})
    rig.job(7)
    outside = tmp_path / "outside"
    (outside / "PR_7").mkdir(parents=True)
    (outside / "PR_7" / "posted.json").write_text("{}")
    (rig.ledgers_dir / "acme").mkdir(parents=True)
    (rig.ledgers_dir / "acme" / "app").symlink_to(outside, target_is_directory=True)
    assert await rig.sweep() == [7]
    assert (outside / "PR_7" / "posted.json").read_text() == "{}"

async def test_sweep_skips_forge_errors(tmp_path):
    rig = Rig(tmp_path, {7: ForgeError("GitHub 502", unavailable=True), 8: pr(8, closed_days=15)})
    kept, swept = rig.job(7), rig.job(8)
    ledger = rig.ledger(7)
    assert await rig.sweep() == [8]
    assert rig.copy(kept).is_dir() and ledger.is_dir() and not rig.copy(swept).exists()
    assert rig.clone.deleted == [(8, True)]
    assert not rig.marked(7)
    rig.forge.prs[7] = pr(7, closed_days=15)
    assert await rig.sweep() == [7]   # read again on the next sweep
    assert not rig.copy(kept).exists() and not ledger.exists()

async def test_sweep_keeps_prs_in_use(tmp_path):
    rig = Rig(tmp_path, {7: pr(7, closed_days=15), 8: pr(8, closed_days=15)})
    review, edit = rig.job(7), rig.job(7, "edit")
    rig.store.set_job_state(edit.id, "running", "", NOW.isoformat())   # its session reads the review's copy
    posted = rig.job(8)
    post = Post("p1", posted.id, SCOPE, 8, HEAD, "review", "posting", NOW.isoformat())
    rig.store.save_post(post)
    ledger = rig.ledger(8)
    assert await rig.sweep() == []
    assert rig.forge.reads == []   # in use: no read spent on it
    assert all(rig.copy(j).is_dir() for j in (review, edit, posted)) and ledger.is_dir()
    assert rig.clone.deleted == []
    assert not any(rig.marked(n) for n in (7, 8))
    rig.store.set_job_state(edit.id, "done", "", NOW.isoformat())
    post.state = "posted"
    rig.store.save_post(post)
    assert await rig.sweep() == [7, 8]   # tried again on the next sweep
    assert not any(rig.copy(j).exists() for j in (review, edit, posted)) and not ledger.exists()

async def test_sweep_reads_at_most_max_reads_oldest_first(tmp_path):
    numbers = range(1, MAX_READS + 3)
    rig = Rig(tmp_path, {1: pr(1, closed_days=20), **{n: pr(n, closed_days=3) for n in numbers[1:]}})
    for n in numbers:   # PR 1's job is the oldest
        rig.job(n)
    assert await rig.sweep() == [1]
    assert rig.forge.reads == list(numbers[:MAX_READS])

# --- Fix round 1 ---

async def test_sweep_reaches_every_pr_past_kept_ones(tmp_path):
    kept = range(1, MAX_READS + 1)   # open, off the board, their jobs older than PR 100's
    rig = Rig(tmp_path, {**{n: pr(n) for n in kept}, 100: pr(100, closed_days=30)})
    for n in [*kept, 100]:
        rig.job(n)
    assert await rig.sweep() == []
    assert rig.forge.reads == list(kept)
    assert await rig.sweep(NOW + timedelta(days=1)) == [100]   # never read: first
    assert rig.forge.reads[MAX_READS] == 100

async def test_a_failing_pr_does_not_stop_the_next(tmp_path, caplog):
    rig = Rig(tmp_path, {7: pr(7, closed_days=15), 8: pr(8, closed_days=15), 9: pr(9)})
    failing, swept, later = rig.job(7), rig.job(8), rig.job(9)
    ledger = rig.ledger(7)
    rig.clone.fail.add(7)   # its branch delete raises on every sweep
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.retention"):
        assert await rig.sweep() == [8]
        rig.forge.prs[9] = pr(9, closed_days=15)
        assert await rig.sweep(NOW + timedelta(days=1)) == [9]
    assert rig.forge.reads == [7, 8, 9, 7, 9]   # PR 7 is read before the one swept, both times
    assert rig.copy(failing).is_dir() and ledger.is_dir() and not rig.marked(7)   # nothing of it removed
    assert not rig.copy(swept).exists() and not rig.copy(later).exists()
    assert caplog.text.count("acme/app #7") == 2 and "RuntimeError at " in caplog.text
    assert "boom" not in caplog.text

async def test_sweep_again_after_a_new_job(tmp_path):
    rig = Rig(tmp_path, {7: pr(7, closed_days=15)})
    first = rig.job(7)
    assert await rig.sweep() == [7]
    assert await rig.sweep(NOW + timedelta(days=1)) == []
    assert rig.forge.reads == [7]   # swept, no new job: not read again
    newer = rig.job(7)   # reopened and reviewed (or Review now) after the sweep
    rig.forge.prs[7] = pr(7, closed_days=-5)   # closed again 5 days after NOW
    assert await rig.sweep(NOW + timedelta(days=10)) == []   # closed 5 days ago: kept
    assert rig.copy(newer).is_dir() and not rig.marked(7)
    assert await rig.sweep(NOW + timedelta(days=19)) == [7]   # 14 days after closing again
    assert not rig.copy(newer).exists() and not rig.copy(first).exists()
    assert rig.store.notice_sent(SCOPE, 7, "swept", newer.id)
    assert rig.forge.reads == [7, 7, 7] and rig.clone.deleted == [(7, True), (7, True)]

async def test_branch_delete_waits_for_the_clone_at_most_the_polls_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "_CLONE_WAIT_SECONDS", 0.05)
    rig = Rig(tmp_path, {7: pr(7, closed_days=15)})
    job = rig.job(7)
    ledger = rig.ledger(7)
    await rig.clone.lock.acquire()   # a job or a post holds the clone
    assert await rig.sweep() == []
    assert rig.copy(job).is_dir() and ledger.is_dir() and not rig.marked(7)   # nothing removed
    assert rig.clone.deleted == []
    rig.clone.lock.release()
    assert await rig.sweep(NOW + timedelta(days=1)) == [7]   # tried again on the next sweep
    assert not rig.copy(job).exists() and not ledger.exists()

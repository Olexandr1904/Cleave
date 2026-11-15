"""The retention sweep: what Cleave keeps on disk for a PR closed RETENTION ago or longer (its job copies, its
ledger directory and its pr-review/N branch) is removed. Database rows stay."""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import Job
from orchestrator.reviewer.service import RepoContext, _acquire, _where
from orchestrator.reviewer.store import ReviewerStore

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=14)
MAX_READS = 20  # GitHub reads per repo and sweep
# The notice kind that marks a PR swept, keyed by its newest job then: a PR with a newer job (reopened, or a
# Review now after the sweep) is a candidate again.
_SWEPT = "swept"


async def sweep(
    ctx: RepoContext, store: ReviewerStore, *, jobs_dir: Path, ledgers_dir: Path, now: datetime,
    last_read: dict[int, datetime],
) -> list[int]:
    """The PRs swept. The candidates are the PRs with jobs that are off the board and not marked swept at their
    newest job. At most MAX_READS of them are read on GitHub, the least recently read first (`last_read`, the
    caller's memory between sweeps, which this updates; a PR never read first, then the PR whose newest job is
    oldest), so every candidate is read within a few sweeps. A PR is kept, and is a candidate on the next sweep,
    while a job of it runs or a post of it is under way (then it is not read), when the read fails, when it is
    open or closed less than RETENTION ago, when the clone is busy past the poll's wait, or when anything else
    fails for it (logged; the next PR goes on)."""
    scope = ctx.scope
    board = store.board(scope)
    newest: dict[int, Job] = {}
    for job in store.jobs(scope):  # newest first
        newest.setdefault(job.number, job)
    candidates = [n for n in reversed(newest)
                  if n not in board and not store.notice_sent(scope, n, _SWEPT, newest[n].id)]
    candidates.sort(key=lambda n: (n in last_read, last_read.get(n, now)))  # stable: ties stay oldest first
    swept: list[int] = []
    reads = 0
    for number in candidates:
        if reads == MAX_READS:
            break
        try:
            if _in_use(store, scope, number, store.jobs(scope, number)):
                continue
            reads += 1
            last_read[number] = now
            if await _sweep_pr(ctx, store, number, jobs_dir=jobs_dir, ledgers_dir=ledgers_dir, now=now):
                swept.append(number)
        except Exception as e:
            logger.warning("Reviewer retention sweep of %s #%s failed: %s", scope, number, _where(e))
    return swept


async def _sweep_pr(
    ctx: RepoContext, store: ReviewerStore, number: int, *, jobs_dir: Path, ledgers_dir: Path, now: datetime,
) -> bool:
    """Whether the PR was swept: read on GitHub, closed RETENTION ago or longer, its branch deleted, then (still
    not in use) its copies and ledger directory removed and the PR marked at its newest job."""
    scope = ctx.scope
    try:
        pr = await ctx.forge.pr_full(number)
    except ForgeError:
        return False
    if pr.state == "open" or not pr.closed_at:
        return False
    # GitHub's "Z" suffix, which fromisoformat reads only from Python 3.11.
    if now - datetime.fromisoformat(pr.closed_at.replace("Z", "+00:00")) < RETENTION:
        return False
    # The branch first: a clone busy past the poll's wait leaves everything for the next sweep.
    if not await _acquire(ctx.clone.lock):
        return False
    try:
        await ctx.clone.delete_branch(number)
    finally:
        ctx.clone.lock.release()
    jobs = store.jobs(scope, number)  # again after the awaits; no await from here until marked
    if _in_use(store, scope, number, jobs):
        return False
    for job in jobs:
        _remove(jobs_dir, job.id)
    _remove(ledgers_dir, ctx.project_id, ctx.repo_id, f"PR_{number}")
    store.mark_notice(scope, number, _SWEPT, jobs[0].id, now.isoformat())
    return True


def _in_use(store: ReviewerStore, scope: str, number: int, jobs: list[Job]) -> bool:
    """A job running (an edit or draft session reads its review's copy) or a post under way (it writes the
    ledger and its job's posting files)."""
    return (any(job.state == "running" for job in jobs)
            or any(post.state == "posting" for post in store.posts(scope, number)))


def _remove(root: Path, *parts: str) -> None:
    """Remove root/<parts>: a symlink there is unlinked, never followed; a directory is removed with all in it.
    Nothing when the path is not strictly inside root, or when a symlink on the way would take it elsewhere."""
    root = root.resolve()
    path = root.joinpath(*parts)
    if (root not in path.parents or os.path.normpath(path) != os.fspath(path)
            or path.parent.resolve() != path.parent):
        return
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)

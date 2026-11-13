"""ReviewerService: polls each reviewed repo on its own interval; keeps its board and jobs."""
from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from config.schemas import ReviewerConfig
from orchestrator.reviewer.board import build_board
from orchestrator.reviewer.clone import CloneError
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.jobs import Notice, plan_jobs
from orchestrator.reviewer.lines import doc_missing_line, poll_failed_line, readiness_failed_line
from orchestrator.reviewer.models import BoardRow, Job, RepoState
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches, scope_of

logger = logging.getLogger(__name__)

_MAX_WAIT_SECONDS = 60
_MIN_INTERVAL_SECONDS = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _where(e: BaseException) -> str:
    """The exception's class and the file:line that raised it; never its message (not our text)."""
    frame = traceback.extract_tb(e.__traceback__)[-1]
    return f"{type(e).__name__} at {frame.filename}:{frame.lineno}"


def _interval(cfg: ReviewerConfig) -> int:
    return max(cfg.poll_interval_seconds, _MIN_INTERVAL_SECONDS)


@dataclass
class RepoContext:
    project_id: str
    repo_id: str
    cfg: ReviewerConfig
    clone: Any  # refresh() -> sha, has_file(rel), path
    readiness: Any  # board(clone_dir) -> BoardSnapshot
    forge: Any  # open_prs(), details(number, updated_at), reviewed_by(number, login, updated_at)

    @property
    def scope(self) -> str:
        return scope_of(self.project_id, self.repo_id)


@dataclass
class _Outcome:
    """A recorded poll: when the repo is next due, and what is left once the store is written."""
    next_poll_at: datetime
    events: list[tuple[str, str, dict]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)


class ReviewerService:
    """Polls each due repo in turn; a failing repo backs off on its own and never stops the rest."""

    def __init__(
        self,
        repos: list[RepoContext],
        store: ReviewerStore,
        switches: Switches,
        credential_failing: Callable[[str], bool],
        send_line: Callable[[str], Awaitable[Any]] | None,
        pipeline_prs: Callable[[str, str], set[int]],
        *,
        event_bus=None,
        now: Callable[[], datetime] = utcnow,
        max_backoff_seconds: int = 3600,
    ) -> None:
        self._repos = repos
        self._store = store
        self._switches = switches
        self._credential_failing = credential_failing
        self._send_line = send_line
        self._pipeline_prs = pipeline_prs
        self._event_bus = event_bus
        self._now = now
        self._max_backoff = max_backoff_seconds
        # When each repo is next due, kept here too: a store that cannot be read or written
        # must neither end run() nor have it poll a repo back to back.
        self._next_due: dict[str, datetime] = {}

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every due repo, then wait until the next one is due (at most 60 s) or `stop`."""
        while not stop.is_set():
            for ctx in self._repos:
                if stop.is_set():
                    return
                if self._seconds_to_due(ctx, self._now()) == 0:
                    await self.poll(ctx)
            now = self._now()
            wait = min([_MAX_WAIT_SECONDS, *(self._seconds_to_due(c, now) for c in self._repos)])
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def _seconds_to_due(self, ctx: RepoContext, now: datetime) -> float:
        """0 when due. A time further ahead than any delay a poll sets means the clock went back
        after it was set: the repo is due then, not frozen until the clock catches up."""
        if ctx.scope not in self._next_due:
            self._next_due[ctx.scope] = self._stored_next_poll(ctx, now)
        left = (self._next_due[ctx.scope] - now).total_seconds()
        if left <= 0 or left > max(_interval(ctx.cfg), self._max_backoff):
            return 0
        return left

    def _stored_next_poll(self, ctx: RepoContext, now: datetime) -> datetime:
        try:
            text = self._store.repo_state(ctx.scope).next_poll_at
            return datetime.fromisoformat(text) if text else now  # never polled: due now
        except Exception as e:  # due now: the poll records a time, or backs off if it cannot
            logger.warning("Reviewer state of %s not read: %s", ctx.scope, _where(e))
            return now

    async def poll(self, ctx: RepoContext) -> None:
        """One poll of one repo. Never raises (cancellation aside): failures become its status."""
        now = self._now()
        try:
            outcome = await self._record(ctx, now)
        except Exception as e:  # the store itself failed: nothing is left to record it in
            logger.warning("Reviewer poll of %s not recorded: %s", ctx.scope, _where(e))
            self._next_due[ctx.scope] = now + timedelta(seconds=self._max_backoff)
            return
        self._next_due[ctx.scope] = outcome.next_poll_at
        try:
            await self._finish(ctx, outcome, now.isoformat())
        except Exception as e:  # after the last state write: the poll stays recorded as it is
            logger.warning("Reviewer poll of %s: lines not all sent: %s", ctx.scope, _where(e))

    async def _record(self, ctx: RepoContext, now: datetime) -> _Outcome:
        """The poll up to its state write; a failure is recorded from the state as it was loaded."""
        state = replace(self._store.repo_state(ctx.scope), last_poll_at=now.isoformat())
        try:
            return await self._poll(ctx, state, now)
        except ReadinessError as e:
            status, reason, logged = "readiness_failed", str(e), str(e)
        except (CloneError, ForgeError) as e:
            status, reason, logged = "error", str(e), str(e)
        except Exception as e:  # a bug or an odd response; its text is not ours to show
            status, reason, logged = "error", type(e).__name__, _where(e)
        logger.warning("Reviewer poll of %s failed (%s): %s", ctx.scope, status, logged)
        return self._fail(ctx, state, now, status, reason)

    async def _poll(self, ctx: RepoContext, state: RepoState, now: datetime) -> _Outcome:
        scope, cfg = ctx.scope, ctx.cfg
        stamp = state.last_poll_at
        next_at = now + timedelta(seconds=_interval(cfg))
        if self._credential_failing(f"forge:{scope}"):
            # No line: credential health sends its own.
            self._store.save_repo_state(replace(
                state, status="token_failing", reason="forge token failing",
                next_poll_at=next_at.isoformat(),
            ))
            return _Outcome(next_at)

        await ctx.clone.refresh()
        doc, base = cfg.process.doc, cfg.base_branch
        doc_ok = ctx.clone.has_file(doc)
        notices = []
        if doc_ok:
            self._store.clear_notice(scope, 0, "doc_missing")
        else:
            notices.append(Notice(0, "doc_missing", doc, doc_missing_line(scope, doc, base)))
        snapshot = await ctx.readiness.board(ctx.clone.path)
        open_prs = await ctx.forge.open_prs()
        details, reviewed = {}, {}
        for pr in snapshot.prs:
            if pr.number in open_prs:
                n, updated_at = pr.number, open_prs[pr.number]
                details[n] = await ctx.forge.details(n, updated_at)
                reviewed[n] = await ctx.forge.reviewed_by(n, snapshot.me, updated_at)

        update = build_board(snapshot, details, reviewed, self._store.board(scope),
                             self._pipeline_prs(ctx.project_id, ctx.repo_id), stamp)
        jobs = self._store.jobs(scope)
        jobs_by_pr: dict[int, list[Job]] = {}
        for job in jobs:
            jobs_by_pr.setdefault(job.number, []).append(job)
        running = doc_ok and self._switches.is_running("reviews", ctx.project_id, ctx.repo_id)
        plan = plan_jobs(scope, update, jobs_by_pr,
                         lambda number, head: self._store.is_skipped(scope, number, head),
                         cfg, running, now, not state.first_poll_done)

        # Store changes first, sends after: a failed or slow send never loses a store change.
        by_id = {job.id: job for job in jobs}
        changed: list[tuple[Job, str]] = []
        for job_id, job_state, reason in plan.states:
            self._store.set_job_state(job_id, job_state, reason, stamp)
            changed.append((by_id[job_id], job_state))
        for number, kind, head, key in plan.queue:
            changed.append((self._store.add_job(scope, number, kind, head, key, stamp), "queued"))
        self._store.replace_board(scope, update.rows)
        status, reason = ("ok", "") if doc_ok else ("doc_missing", f"{doc} not on {base}")
        # The poll's last store write. A failure before it is recorded from the state as loaded,
        # so a failure streak and its back-off carry on.
        self._store.save_repo_state(replace(
            state, status=status, reason=reason, first_poll_done=True, failures=0,
            last_ok_at=stamp, next_poll_at=next_at.isoformat(),
        ))

        rows = len(update.rows)
        events = [("reviewer_poll", f"Reviewer poll of {scope}: {rows} PR(s)",
                   {"scope": scope, "prs": rows})]
        events += [(
            "reviewer_turn", f"{scope} #{c.number}: your turn — {c.action}",
            {"scope": scope, "number": c.number, "action": c.action},
        ) for c in update.new_turns]
        events += [(
            "reviewer_job", f"{scope} #{job.number}: {job.kind} job {job_state}",
            {"scope": scope, "number": job.number, "kind": job.kind, "state": job_state},
        ) for job, job_state in changed]
        return _Outcome(next_at, events, plan.lines, notices + plan.notices)

    def _fail(
        self, ctx: RepoContext, state: RepoState, now: datetime, status: str, reason: str,
    ) -> _Outcome:
        """Keep the board; back off exponentially, never below the poll interval; one line on the
        first failure of a streak."""
        failures = state.failures + 1
        interval = _interval(ctx.cfg)
        delay = max(interval, min(interval * 2 ** failures, self._max_backoff))
        next_at = now + timedelta(seconds=delay)
        self._store.save_repo_state(replace(
            state, status=status, reason=reason, failures=failures,
            next_poll_at=next_at.isoformat(),
        ))
        outcome = _Outcome(next_at, [("reviewer_poll_failed",
                                      f"Reviewer poll of {ctx.scope} failed: {reason}",
                                      {"scope": ctx.scope, "reason": reason})])
        if failures == 1:
            line = readiness_failed_line if status == "readiness_failed" else poll_failed_line
            outcome.lines.append(line(ctx.scope, reason, delay // 60))
        return outcome

    async def _finish(self, ctx: RepoContext, outcome: _Outcome, stamp: str) -> None:
        """Events, lines, then the notices not sent yet; a notice is marked once it is sent."""
        for event_type, message, data in outcome.events:
            self._emit(ctx, event_type, message, data)
        for line in outcome.lines:
            await self._send(line)
        for notice in outcome.notices:
            if self._store.notice_sent(ctx.scope, notice.number, notice.kind, notice.key):
                continue
            if await self._send(notice.text):  # unsent: tried again on the next poll
                self._store.mark_notice(ctx.scope, notice.number, notice.kind, notice.key, stamp)

    async def _send(self, line: str) -> bool:
        """Whether the line was delivered. False with no target to send to (nothing logged: that
        is every poll), or when the send failed (logged with the exception class only)."""
        if self._send_line is None:
            return False
        try:
            await self._send_line(line)
        except Exception as e:
            logger.warning("Reviewer line not sent: %s", type(e).__name__)
            return False
        return True

    def _emit(self, ctx: RepoContext, event_type: str, message: str, data: dict) -> None:
        if self._event_bus is not None:
            self._event_bus.emit(event_type, message, project_id=ctx.project_id, data=data)

    def review_now(self, scope: str, number: int) -> Job:
        """Queue a review at the row's head, whatever the quiet window, size, skips or switch."""
        head = self._row(scope, number).head_sha
        stamp = self._now().isoformat()
        at_head = [j for j in self._store.jobs(scope, number)
                   if j.kind == "review" and j.head_sha == head]
        if any(j.state in ("queued", "running") for j in at_head):
            raise ValueError("already queued")
        for job in at_head:
            if job.state in ("done", "stale"):
                self._store.set_job_state(job.id, "superseded", "new review", stamp)
        return self._store.add_job(scope, number, "review", head, head, stamp)

    def skip(self, scope: str, number: int) -> None:
        """Skip the row's head: its queued reviews are dropped and none is queued for it again."""
        head = self._row(scope, number).head_sha
        stamp = self._now().isoformat()
        self._store.skip(scope, number, head, stamp)
        for job in self._store.jobs(scope, number):
            if job.kind == "review" and job.head_sha == head and job.state == "queued":
                self._store.set_job_state(job.id, "skipped", "skipped by operator", stamp)

    def _row(self, scope: str, number: int) -> BoardRow:
        row = self._store.board(scope).get(number)
        if row is None:
            raise LookupError(f"no PR #{number} on the {scope} board")
        return row

    def overview(self) -> list[dict]:
        """Per repo: its state, its board rows, the latest job of each PR and the PRs whose
        current head is skipped."""
        repos = []
        for ctx in self._repos:
            state = self._store.repo_state(ctx.scope)
            latest: dict[int, Job] = {}
            for job in self._store.jobs(ctx.scope):  # newest first
                latest.setdefault(job.number, job)
            rows = list(self._store.board(ctx.scope).values())
            repos.append({
                "scope": ctx.scope, "status": state.status, "reason": state.reason,
                "last_poll_at": state.last_poll_at, "next_poll_at": state.next_poll_at,
                "rows": rows, "jobs": latest,
                "skipped": {r.number for r in rows
                            if self._store.is_skipped(ctx.scope, r.number, r.head_sha)},
            })
        return repos

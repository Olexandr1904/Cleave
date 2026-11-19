"""ReviewerService: polls each reviewed repo on its own interval; keeps its board and jobs."""
from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.schemas import ReviewerConfig
from orchestrator.reviewer.board import ADDRESS_ACTIONS, FIX_KINDS, build_board
from orchestrator.reviewer.clone import CloneError
from orchestrator.reviewer.fix import PATCH_NAME
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.jobs import REVIEW_NOW_SUFFIX, Notice, held_prs, plan_jobs
from orchestrator.reviewer.lines import doc_missing_line, poll_failed_line, readiness_failed_line
from orchestrator.reviewer.models import BoardRow, BoardSnapshot, Job, JobRun, Post, RepoState
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.report import Report, confirmed_findings
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches, scope_of
from orchestrator.tg_i18n import ENGLISH, N_, telegram_language

logger = logging.getLogger(__name__)

_MAX_WAIT_SECONDS = 60
_MIN_INTERVAL_SECONDS = 60
_CLONE_BUSY_RETRY_SECONDS = 5  # a repo whose clone a job prepares or cleans up is polled again this soon
# How long a poll waits for the clone's lock or session lock: a job or a post may hold either for many minutes,
# and the repos are polled in turn.
_CLONE_WAIT_SECONDS = 5
_MAX_REPORT_TEXT = 200_000  # characters of a report the detail view returns


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _where(e: BaseException) -> str:
    """The exception's class and the file:line that raised it; never its message (not our text)."""
    frame = traceback.extract_tb(e.__traceback__)[-1]
    return f"{type(e).__name__} at {frame.filename}:{frame.lineno}"


def _interval(cfg: ReviewerConfig) -> int:
    return max(cfg.poll_interval_seconds, _MIN_INTERVAL_SECONDS)


async def _acquire(lock: asyncio.Lock) -> bool:
    """Whether lock was had within _CLONE_WAIT_SECONDS (then the caller releases it). locked() reading False
    does not mean free: a queued waiter takes it first."""
    try:
        await asyncio.wait_for(lock.acquire(), _CLONE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return False
    return True


def _report_file(job: Job, run: JobRun | None) -> Path | None:
    """The report in a job's copy, whether it parsed or not: Cleave's posting base once it made one (the edits
    and drafts applied, no ticks), else the one named by the job's head, else the first other regular
    `report_*.md` (a report written under another name stays visible)."""
    if run is None or not run.job_dir:
        return None
    job_dir = Path(run.job_dir)
    for path in (job_dir / "posting" / "report.base.md", job_dir / f"report_{job.head_sha[:9]}.md"):
        if path.is_file():
            return path
    return next((p for p in sorted(job_dir.glob("report_*.md")) if p.is_file()), None)


def _has_plan(job: Job, run: JobRun | None) -> bool:
    """Whether a re-check or an address run drafted a replies plan: its copy holds one, and it did not fail reading
    it. A run superseded or cancelled at Prepare, or deferred, has none."""
    return (run is not None and job.state != "failed" and bool(run.job_dir)
            and (Path(run.job_dir) / "replies_plan.json").is_file())


def _duration(run: JobRun) -> int | None:
    if not (run.started_at and run.finished_at):
        return None
    try:
        elapsed = datetime.fromisoformat(run.finished_at) - datetime.fromisoformat(run.started_at)
    except (ValueError, TypeError):  # a malformed stored time: no duration, not a failed page
        return None
    return round(elapsed.total_seconds())


def _job_detail(job: Job, run: JobRun | None) -> dict:
    run = run or JobRun(job.id)
    return {
        "id": job.id, "kind": job.kind, "state": job.state, "reason": job.reason,
        "head": job.head_sha[:9], "created_at": job.created_at, "updated_at": job.updated_at,
        "retry_at": job.retry_at, "started_at": run.started_at, "finished_at": run.finished_at,
        "duration_s": _duration(run), "cost_usd": run.cost_usd, "turns": run.turns,
        "message_ids": run.message_ids, "message_sent": run.message_sent, "notes": run.notes,
    }


# The board's names for the report's finding kinds.
_FINDING_KINDS = {"red": "critical", "orange": "major", "yellow": "minor", "question": "question",
                  "preexisting": "preexisting"}


def _findings(job: Job, run: JobRun) -> dict | None:
    """A review's parsed report as its number of findings of each kind and of dropped candidates; None
    for a re-check or a review without one."""
    if job.kind != "review" or not run.report:
        return None
    counts = dict.fromkeys(_FINDING_KINDS.values(), 0)
    for item in run.report["items"]:
        counts[_FINDING_KINDS[item["kind"]]] += 1
    return {**counts, "dropped": len(run.report["dropped"])}


# A post in one of these states did not land in full: the numbers it named that did not land are "ticked".
_UNLANDED_STATES = ("posting", "failed", "partial", "unverified", "interrupted")


def _ticked(posts: list[Post], job_id: str) -> set[int]:
    """The numbers named in the job's posts that did not land in full."""
    return {n for p in posts if p.job_id == job_id and p.state in _UNLANDED_STATES for n in p.numbers}


def _number_state(posted: bool, ticked: bool) -> str:
    return "posted" if posted else "ticked" if ticked else "open"


def _post_detail(post: Post) -> dict:
    return {
        "id": post.id, "kind": post.kind, "state": post.state, "job_id": post.job_id,
        "head": post.head_sha[:9], "created_at": post.created_at, "numbers": post.numbers,
        "event": post.event, "urls": post.urls, "comments": post.comments, "error": post.error,
    }


def _job_info(job: Job, run: JobRun | None) -> dict:
    """What the board shows of a PR's latest job besides its id, kind, state and head."""
    run = run or JobRun(job.id)
    return {
        "retry_at": job.retry_at, "started_at": run.started_at, "reason": job.reason,
        "message_sent": run.message_sent, "findings": _findings(job, run),
        "fix": bool(run.fix),  # an address job's patch: Fix ready rather than Replies ready
    }


def _review_info(job: Job, run: JobRun | None) -> dict:
    """What the board shows of a PR's newest review, whatever job is newer: whether a report of its head exists,
    its findings, and what its run took and cost (for the confirmation before a review of the same commit)."""
    run = run or JobRun(job.id)
    return {
        "id": job.id, "state": job.state, "head": job.head_sha[:9], "started_at": run.started_at,
        "finished_at": run.finished_at, "duration_s": _duration(run), "cost_usd": run.cost_usd,
        "message_sent": run.message_sent, "findings": _findings(job, run),
    }


def _fix_info(job: Job, run: JobRun | None, pushed: bool, posting: bool) -> dict:
    """What the board shows of a PR's newest fix job (address or findings), whatever job is newer: its kind and state,
    whether it made a fix, whether that fix was pushed and whether a post of it is under way (Fix now, Fix findings…
    and their "again" read these)."""
    run = run or JobRun(job.id)
    return {"id": job.id, "kind": job.kind, "state": job.state, "head": job.head_sha[:9],
            "started_at": run.started_at, "fix": bool(run.fix), "pushed": pushed, "posting": posting}


def _why_not_fixable(cfg: ReviewerConfig, row: BoardRow, held: frozenset[int]) -> str:
    """Why Fix now does not apply to `row`, "" when it does: plan_jobs's rule for an address job."""
    if not cfg.process.address_doc:
        return "no address_doc"
    if "author" not in row.roles:
        return "not your PR"
    if not ADDRESS_ACTIONS & set(row.my_actions):
        return "no thread turn"
    if row.pipeline:
        return "a ticket workspace holds it"
    if row.number in held:
        return "held by a comparison"
    return ""


def _self_review(jobs: list[Job], head: str) -> Job | None:
    """The newest review job at `head` (jobs newest first, as store.jobs lists them), whatever its state: on a PR of
    the operator's every review is one they asked for (an author never has a review turn)."""
    return next((j for j in jobs if j.kind == "review" and j.head_sha == head), None)


def _why_no_findings_fix(
    cfg: ReviewerConfig, row: BoardRow, held: frozenset[int], review: Job | None, run: JobRun | None,
) -> str:
    """Why Fix findings… does not apply to `row`, "" when it does. `review` is the row's _self_review, `run` its run.
    The reasons are N_() literals: a Telegram answer translates them."""
    if not cfg.process.address_doc:
        return N_("no address_doc")
    if "author" not in row.roles:
        return N_("not your PR")
    if row.pipeline:
        return N_("a ticket workspace holds it")
    if row.number in held:
        return N_("held by a comparison")
    if review is None or review.state not in ("done", "posted"):
        return N_("no finished self-review at this head")
    if not confirmed_findings((run or JobRun("")).report):
        return N_("no confirmed findings")
    return ""


def _patch_text(run: JobRun) -> str | None:
    """An address job's patch from its copy, at most _MAX_REPORT_TEXT characters; None without one."""
    path = Path(run.job_dir) / PATCH_NAME
    if not run.job_dir or path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_REPORT_TEXT)
    except OSError as e:  # the file list still shows
        logger.warning("Reviewer patch %s not read: %s", path, type(e).__name__)
        return None


@dataclass
class RepoContext:
    project_id: str
    repo_id: str
    cfg: ReviewerConfig
    clone: Any  # refresh() -> sha, has_file(rel), path, lock, reader(), make_job_clone() (see ReviewClone)
    readiness: Any  # board(clone_dir) -> BoardSnapshot
    forge: Any  # open_prs(), details(number, updated_at), reviewed_by(number, login, updated_at)
    poster: Any = None  # review(clone_dir, values, confirm=), replies(...) (see CommandPoster)
    allowed_links: tuple[str, ...] = ()  # lowercase link prefixes a posted body may use
    commit_author: tuple[str, str] = ("", "")  # name, email of an address job's pushed commit

    @property
    def scope(self) -> str:
        return scope_of(self.project_id, self.repo_id)


@dataclass
class _Outcome:
    """A poll done: when the repo is next due, and what is left once the store is written."""
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
        send_line: Callable[..., Awaitable[Any]] | None,  # (line, scope=, number=), as ReviewerChat.send
        pipeline_prs: Callable[[str, str], set[int]],
        *,
        event_bus=None,
        now: Callable[[], datetime] = utcnow,
        max_backoff_seconds: int = 3600,
        language_of: Callable[[str], str] = lambda project_id: ENGLISH,
        compared_tickets: Callable[[str], frozenset[str]] = lambda scope: frozenset(),
    ) -> None:
        # The Telegram language of a project id: a repo's lines take its project's.
        self._language_of = language_of
        self._repos = repos
        self._store = store
        self._switches = switches
        self._credential_failing = credential_failing
        self._send_line = send_line
        self._pipeline_prs = pipeline_prs
        # The tickets of a scope compared with the skill flow: their two PRs get no job until the verdict.
        self._compared_tickets = compared_tickets
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
        """One poll of one repo. Never raises (cancellation aside): failures become its status. A repo whose
        clone a job prepares or cleans up, or a post holds, is left alone, nothing recorded: it stays due, tried
        again shortly. Its lines are in the repo's project's Telegram language."""
        with telegram_language(self._language_of(ctx.project_id)):
            await self._poll_one(ctx)

    async def _poll_one(self, ctx: RepoContext) -> None:
        now = self._now()
        try:
            outcome = await self._record(ctx, now)
        except Exception as e:  # the store itself failed: nothing is left to record it in
            logger.warning("Reviewer poll of %s not recorded: %s", ctx.scope, _where(e))
            self._next_due[ctx.scope] = now + timedelta(seconds=self._max_backoff)
            return
        if outcome is None:
            logger.debug("Reviewer poll of %s: the clone is busy (a job or a post), polled shortly", ctx.scope)
            self._next_due[ctx.scope] = now + timedelta(seconds=_CLONE_BUSY_RETRY_SECONDS)
            return
        self._next_due[ctx.scope] = outcome.next_poll_at
        try:
            await self._finish(ctx, outcome, now.isoformat())
        except Exception as e:  # after the last state write: the poll stays recorded as it is
            logger.warning("Reviewer poll of %s: lines not all sent: %s", ctx.scope, _where(e))

    async def _record(self, ctx: RepoContext, now: datetime) -> _Outcome | None:
        """The poll up to its state write; a failure is recorded from the state as it was loaded. None: the
        poll was left for later, nothing recorded."""
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

    async def _poll(self, ctx: RepoContext, state: RepoState, now: datetime) -> _Outcome | None:
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

        clone = ctx.clone
        # A job's Prepare holds the clone for seconds, a post for its runs: wait a bound, else poll again shortly.
        if not await _acquire(clone.lock):
            return None
        try:
            await clone.refresh()
            doc_ok, notices, snapshot = await self._read_clone(ctx)
        finally:
            clone.lock.release()
        doc, base = cfg.process.doc, cfg.base_branch
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
        # Read with no await before the state writes below: a post that starts later finds the states they wrote.
        posting = frozenset(post.job_id for post in self._store.posts_in_state("posting"))
        plan = plan_jobs(scope, update, jobs_by_pr,
                         lambda number, head: self._store.is_skipped(scope, number, head),
                         cfg, running, now, not state.first_poll_done, posting=posting,
                         held=held_prs(update.rows, self._compared_tickets(scope)),
                         answered=self._answered_threads, pushed_from=self._pushed_from)

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
            last_ok_at=stamp, next_poll_at=next_at.isoformat(), me=snapshot.me,
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

    def _answered_threads(self, job: Job) -> frozenset[str]:
        """The thread ids of the job's replies that landed (plan_jobs's `answered`)."""
        run = self._store.run(job.id)
        if run is None:
            return frozenset()
        posted = self._store.posted_replies(job)
        return frozenset(e["thread_id"] for e in run.replies
                         if e.get("n") in posted and isinstance(e.get("thread_id"), str))

    def _pushed_from(self, job: Job) -> str:
        """The head the job's landed push started from, "" without one (plan_jobs's `pushed_from`)."""
        push = self._store.push_of(job)
        return push.head_sha if push else ""

    async def _read_clone(self, ctx: RepoContext) -> tuple[bool, list[Notice], BoardSnapshot]:
        """Whether the process doc is on the base (and its notice), then the readiness snapshot; the caller
        holds the clone as a poll may (see ReviewClone)."""
        doc, base = ctx.cfg.process.doc, ctx.cfg.base_branch
        doc_ok = ctx.clone.has_file(doc)
        notices = []
        if doc_ok:
            self._store.clear_notice(ctx.scope, 0, "doc_missing")
        else:
            notices.append(Notice(0, "doc_missing", doc, doc_missing_line(ctx.scope, doc, base)))
        return doc_ok, notices, await ctx.readiness.board(ctx.clone.path)

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
            await self._send(line, scope=ctx.scope)
        for notice in outcome.notices:
            if self._store.notice_sent(ctx.scope, notice.number, notice.kind, notice.key):
                continue
            # unsent: tried again on the next poll
            if await self._send(notice.text, scope=ctx.scope, number=notice.number):
                self._store.mark_notice(ctx.scope, notice.number, notice.kind, notice.key, stamp)

    async def _send(self, line: str, scope: str = "", number: int = 0) -> bool:
        """Whether the line was delivered, a reply anchor to the repo (scope) or to its PR (number, when
        not 0). False with no target to send to (nothing logged: that is every poll), or when the send
        failed (logged with the exception class only)."""
        if self._send_line is None:
            return False
        try:
            await self._send_line(line, scope=scope, number=number)
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
        if any(j.state in ("queued", "running", "deferred") for j in at_head):
            raise ValueError("already queued")
        for job in at_head:
            if job.state in ("done", "stale", "posted"):
                self._store.set_job_state(job.id, "superseded", "new review", stamp)
        # Its key marks it, so the runner settles it without a review turn; at its head it is a review all
        # the same.
        return self._store.add_job(scope, number, "review", head, f"{head}{REVIEW_NOW_SUFFIX}", stamp)

    def address_again(self, scope: str, number: int, instruction: str) -> Job:
        """Queue an address job at the row's head with the operator's instruction ("again: …"), keyed as plan_jobs
        keys one; the PR's address jobs done, posted or stale are superseded. LookupError: the row is gone or holds
        no address turn; ValueError: an address job of the PR is pending."""
        row = self._row(scope, number)
        if not ADDRESS_ACTIONS & set(row.my_actions):
            raise LookupError("no address turn")
        return self._queue_address(scope, row, instruction)

    def address_now(self, scope: str, number: int) -> Job:
        """Fix now: queue an address job at the row's head at once, without the quiet window or an instruction, as
        address_again does. LookupError: the row is gone; ValueError: the PR is not fixable (plan_jobs's address
        rule, "not fixable: <why>"), a post of a fix of it is under way or a fix of it is pending (_pending_fix)."""
        row = self._row(scope, number)
        why = _why_not_fixable(self._repo(scope).cfg, row, held_prs(self._store.board(scope),
                                                                    self._compared_tickets(scope)))
        if why:
            raise ValueError(f"not fixable: {why}")
        return self._queue_address(scope, row, None)

    def fix_findings(
        self, scope: str, number: int, *, review_id: str | None = None, instruction: str | None = None,
    ) -> Job:
        """Fix findings… and "fix": queue a findings job at the row's head for the confirmed findings of its
        self-review (_self_review), after superseding the PR's findings jobs done, posted or stale; its run's request
        names the review, and the instruction when given. LookupError: the row is gone or its repo is no longer
        configured. ValueError: "not fixable: <why>" (_why_no_findings_fix), "not the newest self-review" (`review_id`,
        the review a message is about, is not the self-review), or _pending_fix's reason."""
        row = self._row(scope, number)
        cfg = self._repo(scope).cfg
        review = _self_review(self._store.jobs(scope, number), row.head_sha)
        held = held_prs(self._store.board(scope), self._compared_tickets(scope))
        why = _why_no_findings_fix(cfg, row, held, review, review and self._store.run(review.id))
        if why:
            raise ValueError(f"not fixable: {why}")
        if review_id is not None and review_id != review.id:
            raise ValueError("not the newest self-review")
        if why := self._pending_fix(scope, number):
            raise ValueError(why)
        stamp = self._now().isoformat()
        for job in self._store.jobs(scope, number):
            if job.kind == "findings" and job.state in ("done", "posted", "stale"):
                self._store.set_job_state(job.id, "superseded", "new findings fix", stamp)
        job = self._store.add_job(scope, number, "findings", row.head_sha, f"{row.head_sha}:{review.id}", stamp)
        request = {"review": review.id, **({"instruction": instruction} if instruction else {})}
        self._store.save_run(JobRun(job.id, request=request))
        return job

    def _repo(self, scope: str) -> RepoContext:
        ctx = next((c for c in self._repos if c.scope == scope), None)
        if ctx is None:  # its board outlived its configuration
            raise LookupError(f"no repo {scope} with reviews")
        return ctx

    def _pending_fix(self, scope: str, number: int) -> str:
        """Why no fix of the PR may be queued now, "" when one may: one fix (address or findings) of a PR at a time,
        and none while a post of one is under way (a push moves its job to the new head only once GitHub shows it;
        superseding the job would strand the post)."""
        fixes = [j for j in self._store.jobs(scope, number) if j.kind in FIX_KINDS]
        posting = {post.job_id for post in self._store.posts_in_state("posting")}
        if any(j.id in posting for j in fixes):
            return "a post is under way"
        if any(j.state in ("queued", "running", "deferred") for j in fixes):
            return "already queued"
        return ""

    def _queue_address(self, scope: str, row: BoardRow, instruction: str | None) -> Job:
        """An address job at `row`'s head, keyed as plan_jobs keys one, after superseding the PR's address jobs done,
        posted or stale; with `instruction`, its run carries it. ValueError: _pending_fix's reason."""
        if why := self._pending_fix(scope, row.number):
            raise ValueError(why)
        addresses = [j for j in self._store.jobs(scope, row.number) if j.kind == "address"]
        stamp = self._now().isoformat()
        for job in addresses:
            if job.state in ("done", "posted", "stale"):
                self._store.set_job_state(job.id, "superseded", "new address", stamp)
        threads = sorted({tid for t in row.my_turns if t["action"] in ADDRESS_ACTIONS for tid in t["threads"]})
        job = self._store.add_job(scope, row.number, "address", row.head_sha,
                                  f"{row.head_sha}:{','.join(threads)}", stamp)
        if instruction is not None:
            self._store.save_run(JobRun(job.id, request={"instruction": instruction}))
        return job

    def skip(self, scope: str, number: int) -> None:
        """Skip the row's head: its queued and deferred reviews are dropped and none is queued for
        it again."""
        head = self._row(scope, number).head_sha
        stamp = self._now().isoformat()
        self._store.skip(scope, number, head, stamp)
        for job in self._store.jobs(scope, number):
            if (job.kind == "review" and job.head_sha == head
                    and job.state in ("queued", "deferred")):
                self._store.set_job_state(job.id, "skipped", "skipped by operator", stamp)

    def _row(self, scope: str, number: int) -> BoardRow:
        row = self._store.board(scope).get(number)
        if row is None:
            raise LookupError(f"no PR #{number} on the {scope} board")
        return row

    def overview(self) -> list[dict]:
        """Per repo: its state, its board rows, the latest review, re-check or address job of each PR (an edit or a
        draft is never the row's job) with what the board shows of it (job_info, for the PRs on the board only), the
        PRs whose current head is skipped and the PRs too large to review automatically. For the board also the
        project, the operator's login, each PR's newest review and newest fix job of either kind (address, findings)
        whatever job is newer (reviews, fixes), the PRs Fix now applies to (fixable) and those Fix findings… applies to
        (findings_fixable)."""
        repos = []
        runs: dict[str, JobRun | None] = {}
        posting = frozenset(post.job_id for post in self._store.posts_in_state("posting"))

        def run_of(job: Job) -> JobRun | None:  # read once: the row's job is often its newest review
            if job.id not in runs:
                runs[job.id] = self._store.run(job.id)
            return runs[job.id]

        for ctx in self._repos:
            state = self._store.repo_state(ctx.scope)
            latest: dict[int, Job] = {}
            reviews: dict[int, Job] = {}
            fixes: dict[int, Job] = {}
            by_number: dict[int, list[Job]] = {}
            for job in self._store.jobs(ctx.scope):  # newest first
                by_number.setdefault(job.number, []).append(job)
                if job.kind in ("review", "recheck", "address"):
                    latest.setdefault(job.number, job)
                if job.kind == "review":
                    reviews.setdefault(job.number, job)
                if job.kind in FIX_KINDS:
                    fixes.setdefault(job.number, job)
            board = self._store.board(ctx.scope)
            rows = list(board.values())
            held = held_prs(board, self._compared_tickets(ctx.scope))
            findings_fixable = set()
            for r in rows:
                review = _self_review(by_number.get(r.number, []), r.head_sha)
                if not _why_no_findings_fix(ctx.cfg, r, held, review, review and run_of(review)):
                    findings_fixable.add(r.number)

            repos.append({
                "scope": ctx.scope, "project_id": ctx.project_id, "me": state.me,
                "status": state.status, "reason": state.reason,
                "last_poll_at": state.last_poll_at, "next_poll_at": state.next_poll_at,
                "rows": rows, "jobs": latest,
                "job_info": {r.number: _job_info(latest[r.number], run_of(latest[r.number]))
                             for r in rows if r.number in latest},
                "reviews": {r.number: _review_info(reviews[r.number], run_of(reviews[r.number]))
                            for r in rows if r.number in reviews},
                "fixable": {r.number for r in rows if not _why_not_fixable(ctx.cfg, r, held)},
                "findings_fixable": findings_fixable,
                "fixes": {r.number: _fix_info(job, run_of(job), self._store.push_of(job) is not None, job.id in posting)
                          for r in rows if (job := fixes.get(r.number)) is not None},
                "skipped": {r.number for r in rows
                            if self._store.is_skipped(ctx.scope, r.number, r.head_sha)},
                "large": {r.number for r in rows if r.changed_files >= ctx.cfg.max_files_auto},
            })
        return repos

    def detail(self, scope: str, number: int) -> dict:
        """One PR: its board row, its jobs (newest first), the newest review report, the replies of the newest
        re-check or address job that drafted a plan, the verdicts of the newest findings job that wrote them, each
        number's state (posted, ticked or open) and the PR's posts (newest first). The fix, patch and pushed commit
        are the newer of that address job and that findings job's. Read-only."""
        row = self._store.board(scope).get(number)
        jobs = self._store.jobs(scope, number)
        if row is None and not jobs:
            raise LookupError(f"no PR #{number} in {scope}")
        runs = {job.id: self._store.run(job.id) for job in jobs}
        no_run = JobRun("")
        report_job, report_run, report_text = None, no_run, None
        for job in jobs:
            path = _report_file(job, runs[job.id]) if job.kind == "review" else None
            if path is not None:
                report_job, report_run = job, runs[job.id]
                try:
                    with path.open(encoding="utf-8", errors="replace") as f:
                        report_text = f.read(_MAX_REPORT_TEXT)
                except OSError as e:  # the parsed report still shows
                    logger.warning("Reviewer report %s not read: %s", path, type(e).__name__)
                break
        recheck = next(  # a re-check or an address job
            (j for j in jobs if j.kind in ("recheck", "address") and _has_plan(j, runs[j.id])), None
        )
        verdicts = next(  # a failed one is left out, as _has_plan leaves out a failed address job
            (j for j in jobs if j.kind == "findings" and j.state != "failed" and (runs[j.id] or no_run).verdicts), None
        )
        # The newest fix job of either kind: jobs are newest first.
        fix_job = next((j for j in jobs if j in (verdicts, recheck) and j.kind in FIX_KINDS), None)
        pushed = self._store.push_of(fix_job) if fix_job else None
        posts = self._store.posts(scope, number)
        number_states, reply_states = {}, {}
        if report_job is not None and report_run.report:
            landed = self._store.posted_numbers(report_job, Report.from_dict(report_run.report), posts)
            ticked = _ticked(posts, report_job.id)
            number_states = {
                str(item["number"]): _number_state(item["number"] in landed, item["number"] in ticked)
                for item in report_run.report["items"]
            }
        if recheck is not None:
            replied, ticked = self._store.posted_numbers(recheck, None, posts), _ticked(posts, recheck.id)
            reply_states = {str(entry["n"]): _number_state(entry["n"] in replied, entry["n"] in ticked)
                            for entry in runs[recheck.id].replies}
        return {
            "row": None if row is None else asdict(row),
            "jobs": [_job_detail(job, runs[job.id]) for job in jobs],
            "report_job": report_job.id if report_job else None,
            "report": report_run.report or None,  # {} when the report did not parse
            "replies_job": recheck.id if recheck else None,
            "replies": runs[recheck.id].replies if recheck else [],
            "tg_lines": report_run.tg_lines,
            "report_text": report_text,
            "number_states": number_states,
            "reply_states": reply_states,
            "posts": [_post_detail(p) for p in posts],
            "verdicts_job": verdicts.id if verdicts else None,
            "verdicts": runs[verdicts.id].verdicts if verdicts else [],
            "fix": (runs[fix_job.id].fix or None) if fix_job else None,
            "fix_patch": _patch_text(runs[fix_job.id]) if fix_job else None,
            "pushed_sha": pushed.sha if pushed else None,
        }

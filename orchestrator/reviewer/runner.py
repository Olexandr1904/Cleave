"""The job runner: runs the queued review and re-check jobs, several at once (one per PR), each from Prepare in a
job clone of its own through the model session to the job's state, its copy in the jobs directory and its
Telegram message."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.schemas import ReviewerJobConfig
from orchestrator.reviewer.board import TICKET_KEY
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.clone import ReviewClone, git_file_changed, guarded_git_files
from orchestrator.reviewer.jobs import REVIEW_NOW_SUFFIX
from orchestrator.reviewer.lines import (
    drift_line,
    job_deferred_line,
    job_failed_line,
    login_line,
)
from orchestrator.reviewer.messages import (
    MESSAGE_LIMIT,
    message_parts,
    recheck_message,
    review_message,
)
from orchestrator.reviewer.models import POSTABLE_STATES, BoardRow, Job, JobRun
from orchestrator.reviewer.prepare import JobPaths, Prepared, PrepareError, job_paths, prepare
from orchestrator.reviewer.report import (
    Report,
    ReportError,
    parse_replies_plan,
    parse_report,
    parse_tg_lines,
)
from orchestrator.reviewer.retention import sweep
from orchestrator.reviewer.service import RepoContext, _where, utcnow
from orchestrator.reviewer.session import SessionOutcome, recheck_prompt, review_prompt, run_session
from orchestrator.reviewer.store import ReviewerStore

logger = logging.getLogger(__name__)

LOGIN_BACKOFF = timedelta(minutes=15)
_QUOTA_RETRY = timedelta(hours=1)  # when the CLI gives no reset time, or one already past at the session's end
_NEWER_STATES = ("queued", "running", "deferred", "done", "posted")
_PENDING_STATES = ("queued", "deferred", "running")
_PREPARE_STATES = {"failed": "failed", "superseded": "superseded", "cancelled": "cancelled", "requeue": "queued"}
_CONFIG_CHANGED = "the session changed its job clone's git config"
# The third usage-limit or login deferral of a job fails it: session output that fakes either cannot loop.
_MAX_DEFERRALS = 3
_DEFERRED_TOO_OFTEN = f"deferred {_MAX_DEFERRALS} times (usage limit or login)"
_NO_ME = "waiting for the first poll"
_PLAN_COPY = "replies_plan.json"
_PREPARED_THREADS = "threads.prepared.json"  # Prepare's threads.json in the job copy (see _run)
# Names in job_dir that are Cleave's, never copied from run_dir; matched after NFC and casefold, as a
# case-insensitive filesystem (the host's APFS) would match them. "posting" holds Cleave's posting files.
_RESERVED = ("session", _PLAN_COPY, "posting", _PREPARED_THREADS)
# The wait before the next delivery attempt, by the failed attempts so far; the fifth failure ends them.
_RESEND_WAITS = (timedelta(0), timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15),
                 timedelta(minutes=60))
_MAX_SEND_ATTEMPTS = len(_RESEND_WAITS)
# The wait before a job GitHub was unavailable for runs again, by its requeues in a row (the last one after
# that); the sixth in a row fails it.
_UNAVAILABLE_WAITS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60))
_MAX_UNAVAILABLE = 6
_SWEEP_EVERY = timedelta(hours=24)


@dataclass
class RunnerConfig:
    jobs_dir: Path
    secret_paths: list[Path]
    jobs: dict[str, ReviewerJobConfig]
    ledgers_dir: Path | None = None  # None: no retention sweep
    work_dir: Path | None = None  # the job clones' root; None: `work` beside jobs_dir
    max_parallel: int = 6  # heavy jobs running at once
    min_free_bytes: int = 0  # no job starts while the work dir's volume has less free

    @property
    def work_root(self) -> Path:
        return self.work_dir or self.jobs_dir.parent / "work"

    def job_root(self, project_id: str, repo_id: str, job_id: str) -> Path:
        """The directory a job owns while it runs: its job clone and the clone's worktree."""
        return self.work_root / project_id / repo_id / job_id


def settle(job: Job, row: BoardRow | None, jobs: list[Job], skipped: bool = False) -> tuple[str, str]:
    """The state of a job whose session ended well, against the board and the PR's jobs as they are now.
    `skipped`: the operator skipped the job's head after it was queued. A review the operator asked for
    (Review now) needs no review turn."""
    if row is None:
        return "stale", "PR gone"
    if row.head_sha != job.head_sha:
        return "superseded", "new head"
    if skipped:
        return "skipped", "skipped by operator"
    review_now = job.kind == "review" and job.key.endswith(REVIEW_NOW_SUFFIX)
    if job.kind not in row.my_actions and not review_now:
        return "stale", "turn gone"
    if any(j.kind == job.kind and j.state in _NEWER_STATES for j in _created_after(job, jobs)):
        return "superseded", "newer job"
    return "done", ""


def _created_after(job: Job, jobs: list[Job]) -> list[Job]:
    """The jobs created after `job`. At an equal created_at, those listed before it: store.jobs lists
    newest first, the insertion order breaking the tie."""
    ids = [j.id for j in jobs]
    mine = ids.index(job.id) if job.id in ids else len(ids)
    return [j for i, j in enumerate(jobs) if j.id != job.id and (
        j.created_at > job.created_at or (j.created_at == job.created_at and i < mine))]


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


class _ArchiveRefused(Exception):
    pass


def _archive(run_dir: Path, job_dir: Path) -> list[str]:
    """Copy every regular file under run_dir into job_dir, keeping the layout, and return the names it kept
    out. The session wrote run_dir: a symlink there is neither copied nor followed, and the names the job copy
    keeps for Cleave's own files (the session directory, the replies plan, the posting files, Prepare's
    threads) are not taken from it."""
    if run_dir.resolve() != run_dir:  # job_paths resolved it: a symlink appeared at it or on the way
        raise _ArchiveRefused("run directory is a symlink")
    kept_out: list[str] = []
    for top, dirs, names in os.walk(run_dir):  # never descends into a symlinked directory
        if top == os.fspath(run_dir):
            kept_out = sorted(n for n in (*dirs, *names) if _reserved(n))
            dirs[:] = [d for d in dirs if not _reserved(d)]
            names = [n for n in names if not _reserved(n)]
        for name in names:
            src = Path(top) / name
            if not _regular(src):
                continue
            dst = job_dir / src.relative_to(run_dir)
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    return kept_out


def _reserved(name: str) -> bool:
    return unicodedata.normalize("NFC", name).casefold() in _RESERVED


def may_still_save(job: Job, run: JobRun) -> bool:
    """Whether the runner may still save the job's run: while the job is done or posted, its message not all out,
    and the attempts not used up, the runner sends the message's unsent parts as it splits the run's report, and
    holds the run across its sends. Past that, it never saves the run again. The resend follows this rule, and an
    edit or a draft waits for it to end."""
    return job.state in POSTABLE_STATES and not run.message_sent and run.send_attempts < _MAX_SEND_ATTEMPTS


def _resend_due(run: JobRun, now: datetime) -> bool:
    """Whether the wait since the last attempt of a run's message has passed (may_still_save holds)."""
    if not run.last_attempt_at:
        return True
    return now >= datetime.fromisoformat(run.last_attempt_at) + _RESEND_WAITS[run.send_attempts]


def _message(job: Job, run: JobRun) -> str:
    tg_lines = {int(n): line for n, line in run.tg_lines.items()}
    if job.kind == "review":
        return review_message(job.scope, job.number, run.title, run.ticket_key, run.url, run.files,
                              Report.from_dict(run.report), tg_lines, posted_at_head=run.posted_at_head,
                              summary_posted=run.summary_posted)
    return recheck_message(job.scope, job.number, run.title, run.url, job.head_sha, run.replies, tg_lines)


class JobRunner:
    """Runs heavy jobs as tasks, up to `max_parallel` at once and one per PR. Each job runs in a job clone of its
    own under `work_root`, removed when it ends; a job holds the review clone's lock only for Prepare's git there
    (see prepare)."""

    def __init__(
        self,
        repos: list[RepoContext],
        store: ReviewerStore,
        switches,
        credential_failing: Callable[[str], bool],
        report_auth_failure: Callable[[str, int], None],
        chat: ReviewerChat | None,
        llm,
        trackers: dict[str, Any],
        config: RunnerConfig,
        *,
        event_bus=None,
        now: Callable[[], datetime] = utcnow,
        environ: Mapping[str, str] = os.environ,
        idle_seconds: float = 30,
    ) -> None:
        self._contexts = {ctx.scope: ctx for ctx in repos}
        self._store = store
        self._switches = switches
        self._credential_failing = credential_failing
        self._report_auth_failure = report_auth_failure
        self._chat = chat
        self._llm = llm
        self._trackers = trackers
        self._config = config
        self._event_bus = event_bus
        self._now = now
        self._environ = environ
        self._idle = idle_seconds
        # The last login failure of the current streak; None once a session has ended otherwise.
        self._login_failed_at: datetime | None = None
        # The end of the current usage-limit wait (no job starts before it), and whether the streak's deferred
        # line is out; both reset once a session has ended otherwise.
        self._quota_until: datetime | None = None
        self._quota_since: datetime | None = None  # when the current usage-limit wait began
        self._quota_line_sent = False
        self._in_flight: dict[str, tuple[Job, asyncio.Task]] = {}  # job id -> (job, its task)
        self._wait_a_cycle: set[str] = set()  # requeued jobs: not started again before an idle wait has passed
        self._disk_held = False  # whether the last check found too little free space
        self._swept_at: datetime | None = None  # the last retention sweep; None: due
        self._last_read: dict[str, dict[int, datetime]] = {}  # per repo: when the sweep last read each PR

    async def run(self, stop: asyncio.Event) -> None:
        """start(), then loop(stop). Never raises (cancellation aside)."""
        await self.start()
        await self.loop(stop)

    async def start(self) -> None:
        """recover() once, at start-up, then the job directories a dead process left, removed in a thread (a job
        clone holds a whole checkout). Never raises."""
        try:
            self.recover()
        except Exception as e:
            logger.warning("Reviewer jobs not recovered: %s", _where(e))
        try:
            await asyncio.to_thread(self._clear_work)
        except Exception as e:
            logger.warning("Reviewer work directory not cleared: %s", _where(e))

    async def loop(self, stop: asyncio.Event) -> None:
        """Jobs as tasks, up to max_parallel at once, until `stop`; then it waits for the jobs in flight. Cancelled,
        it cancels and awaits them first: no job task outlives it. Never raises (cancellation aside)."""
        try:
            while not stop.is_set():
                try:
                    await self.resend_unsent()
                    if not self._start_jobs() and not self._in_flight:
                        await self._sweep()
                except Exception as e:
                    logger.warning("Reviewer job cycle failed: %s", _where(e))
                if await self._wait(stop):
                    # A requeued job (forge token failing) waits a cycle: the forge gate may not have caught up
                    # yet, and preparing it again at once would loop on the forge.
                    self._wait_a_cycle.clear()
            if self._in_flight:
                await asyncio.wait([task for _, task in self._in_flight.values()])
        finally:
            tasks = [task for _, task in self._in_flight.values()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _start_jobs(self) -> int:
        """Start next_job() as a task while there is room; the number started."""
        started = 0
        while len(self._in_flight) < self._config.max_parallel and (job := self.next_job()) is not None:
            self._in_flight[job.id] = (job, asyncio.create_task(self._run_one(job)))
            started += 1
        return started

    async def _run_one(self, job: Job) -> None:
        try:
            await self.run_job(job)
            if self._store.job(job.id).state == "queued":
                self._wait_a_cycle.add(job.id)
        except Exception as e:  # run_job's own handling failed: logged here, the job held for a cycle
            logger.warning("Reviewer job %s (%s #%s) failed: %s", job.id, job.scope, job.number, _where(e))
            self._wait_a_cycle.add(job.id)
        finally:
            self._in_flight.pop(job.id, None)

    async def _wait(self, stop: asyncio.Event) -> bool:
        """Until a job in flight ends, `stop`, or the idle wait: True when the idle wait ran out."""
        stopping = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait({stopping, *(task for _, task in self._in_flight.values())},
                                         timeout=self._idle, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        return not done

    def recover(self) -> None:
        """A review or re-check still running at start-up was interrupted: it fails, so plan 3's rules retry it
        once. Edit and draft jobs are not this runner's."""
        for job in self._store.jobs_in_state("running", kinds=("review", "recheck")):
            self._change(job, "failed", "interrupted (daemon restarted)")

    def _clear_work(self) -> None:
        """Remove every job directory under the work root: at start-up none belongs to a running job."""
        work = self._config.work_root
        if work.is_dir() and not work.is_symlink():
            for entry in work.iterdir():
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError as e:
                    logger.warning("Reviewer work directory %s not removed: %s", entry, type(e).__name__)

    async def _sweep(self) -> None:
        """The retention sweep of every repo, when a ledgers directory is set and 24 hours have passed since the
        last one (due at start); one line per repo with the PRs it swept. A repo whose sweep fails is logged; the
        next one is swept all the same."""
        ledgers_dir = self._config.ledgers_dir
        if ledgers_dir is None or (self._swept_at is not None and self._now() - self._swept_at < _SWEEP_EVERY):
            return
        self._swept_at = self._now()
        for ctx in self._contexts.values():
            try:
                swept = await sweep(ctx, self._store, jobs_dir=self._config.jobs_dir, ledgers_dir=ledgers_dir,
                                    now=self._now(), last_read=self._last_read.setdefault(ctx.scope, {}))
            except Exception as e:
                logger.warning("Reviewer retention sweep of %s failed: %s", ctx.scope, _where(e))
            else:
                logger.info("Reviewer retention sweep of %s: swept %s", ctx.scope,
                            ", ".join(f"#{n}" for n in swept) or "none")

    def next_job(self) -> Job | None:
        """The first runnable job whose repo is configured and passes the gates: reviews running there, its
        forge token and the claude login not failing, no login failure in the last 15 minutes, no usage-limit
        wait, enough free space for a job clone; not one in flight, nor one of a PR with a job in flight; a
        re-check also waits for its PR's pending review and for the repo's recorded login."""
        now = self._now()
        if self._credential_failing("claude_login"):
            return None
        if self._login_failed_at is not None and now - self._login_failed_at < LOGIN_BACKOFF:
            return None
        if self._quota_until is not None and now < self._quota_until:
            return None
        if self._low_disk():
            return None
        busy = {(j.scope, j.number) for j, _ in self._in_flight.values()}
        for job in self._store.runnable_jobs(now.isoformat()):
            if job.id in self._in_flight or job.id in self._wait_a_cycle or (job.scope, job.number) in busy:
                continue
            ctx = self._contexts.get(job.scope)
            if (ctx is not None
                    and self._switches.is_running("reviews", ctx.project_id, ctx.repo_id)
                    and not self._credential_failing(f"forge:{job.scope}")
                    and (job.kind != "recheck" or self._recheck_ready(job))):
                return job
        return None

    def _low_disk(self) -> bool:
        """Whether the work root's volume has less free space than min_free_bytes; logged once per streak."""
        need = self._config.min_free_bytes
        if need <= 0:
            return False
        path = self._config.work_root
        while not path.exists() and path != path.parent:
            path = path.parent
        free = shutil.disk_usage(path).free
        low = free < need
        if low and not self._disk_held:
            logger.warning("Reviewer jobs held: %.1f GB free, under %.1f GB", free / 1024**3, need / 1024**3)
        self._disk_held = low
        return low

    def _recheck_ready(self, job: Job) -> bool:
        """A re-check runs after its PR's pending review (review first), and once a poll has recorded the
        repo's login (the prompt keeps the threads whose turn it is): until then it is requeued, saying so."""
        if any(j.kind == "review" and j.state in _PENDING_STATES for j in self._store.jobs(job.scope, job.number)):
            return False
        if not self._store.repo_state(job.scope).me:
            if (job.state, job.reason) != ("queued", _NO_ME):
                self._change(job, "queued", _NO_ME)
            return False
        return True

    async def run_job(self, job: Job) -> None:
        """Prepare, run the session, keep its output, settle the state and send the message. The job never
        stays running: anything unexpected fails it (cancellation aside: recover() fails it then)."""
        ctx = self._contexts[job.scope]
        try:
            await self._run(ctx, job)
        except PrepareError as e:
            if e.outcome == "unavailable":
                await self._github_unavailable(job, e.reason)
            elif (state := _PREPARE_STATES[e.outcome]) == "failed":
                await self._fail(job, e.reason)
            else:
                self._change(job, state, e.reason)
            if e.drift:
                await self._send_drift(ctx)
        except Exception as e:
            logger.warning("Reviewer job %s (%s #%s) failed: %s", job.id, job.scope, job.number, _where(e))
            await self._fail(job, f"internal error: {type(e).__name__}")

    async def _github_unavailable(self, job: Job, reason: str) -> None:
        """Queue the job again once its wait is over; its sixth time in a row fails it."""
        run = self._store.run(job.id)  # saved before Prepare
        run.requeues += 1
        self._store.save_run(run)
        if run.requeues >= _MAX_UNAVAILABLE:
            await self._fail(job, f"{reason} ({run.requeues} attempts)")
            return
        wait = _UNAVAILABLE_WAITS[min(run.requeues, len(_UNAVAILABLE_WAITS)) - 1]
        self._change(job, "queued", reason, (self._now() + wait).isoformat())

    async def resend_unsent(self) -> None:
        """Send the rest of the message of every done or posted job whose message is not out and whose wait
        since its last attempt has passed; a done job after settling it against the board as it is now. A
        posted job is not settled: after a post its turn is normally gone, and it would turn stale. The pass
        ends at the first failed send: the others wait for the next one."""
        if self._chat is None:
            return
        now = self._now()
        for job, run in self._store.done_unsent():
            if job.id in self._in_flight:  # its own task is delivering it
                continue
            try:
                if not (may_still_save(job, run) and _resend_due(run, now)):
                    continue
                if job.state != "posted":
                    state, reason = await self._settle(job)
                    if state != "done":
                        self._change(job, state, reason)
                        continue
                if not await self._deliver(job, run):
                    return
            except Exception as e:  # one bad saved run must not hold back the others, nor the jobs
                logger.warning("Reviewer message of job %s not resent: %s", job.id, _where(e))
                self._attempt_failed(run)  # a run that cannot be rebuilt reaches the cap, not a loop

    async def _run(self, ctx: RepoContext, job: Job) -> None:
        root = self._config.job_root(ctx.project_id, ctx.repo_id, job.id)
        paths = job_paths(root / "clone", ctx.cfg.process, job.number, job.head_sha,
                          self._config.jobs_dir, job.id)
        previous = self._store.run(job.id) or JobRun(job.id)  # a deferred or requeued job's earlier run
        started = self._now()
        self._change(job, "running", "")
        # The counts, notes, cost and turns are the job's, over all its runs.
        run = JobRun(job.id, started_at=self._stamp(), job_dir=str(paths.job_dir), deferrals=previous.deferrals,
                     requeues=previous.requeues, notes=list(previous.notes), cost_usd=previous.cost_usd,
                     turns=previous.turns)
        self._store.save_run(run)
        paths.job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        await self._remove_root(root, job, run)  # an earlier run's, if its removal failed
        try:
            prepared = await prepare(ctx, job, paths, store=self._store, tracker=self._trackers.get(ctx.project_id),
                                     now=self._stamp(), job_clone_dir=root / "clone")
            # Prepare's snapshot, kept before the session: the session may rewrite the run directory's.
            if _regular(threads := paths.run_dir / "threads.json"):
                shutil.copyfile(threads, paths.job_dir / _PREPARED_THREADS)
            if run.requeues:  # GitHub answered: the next "unavailable" starts a new count
                run.requeues = 0
                self._store.save_run(run)
            if prepared.drift:  # at once: prepare has stored the new hash, a later failure must not lose it
                await self._send_drift(ctx)
            outcome, changed_config = await self._session(ctx, job, paths, prepared.clone)
            await self._settle_outcome(ctx, job, paths, run, prepared, outcome, changed_config, started)
        finally:  # the job clone and its worktree, whatever happened: the job's copy keeps what is kept
            # The run as stored now: _fail and the chat may have written its error and notes since.
            await self._remove_root(root, job, self._store.run(job.id) or run)

    async def _settle_outcome(
        self, ctx: RepoContext, job: Job, paths: JobPaths, run: JobRun, prepared: Prepared,
        outcome: SessionOutcome, changed_config: bool, started: datetime,
    ) -> None:
        run.finished_at = self._stamp()
        run.cost_usd += outcome.cost_usd
        run.turns += outcome.turns
        self._store.save_run(run)
        # A session that ended otherwise ends a streak (its next usage-limit or login failure sends its line),
        # but only one that started once the wait had begun: one already running then says nothing of it.
        if outcome.kind != "quota" and (self._quota_since is None or started >= self._quota_since):
            self._quota_until, self._quota_since, self._quota_line_sent = None, None, False
        if outcome.kind != "login" and (self._login_failed_at is None or started >= self._login_failed_at):
            self._login_failed_at = None
        if changed_config:
            await self._fail(job, _CONFIG_CHANGED)
        elif outcome.kind == "quota":
            end = self._now()
            retry_at = outcome.retry_at
            if retry_at is None or retry_at <= end:  # never runnable at once
                retry_at = end + _QUOTA_RETRY
            retry_at = retry_at.astimezone(timezone.utc)
            self._quota_since = end  # the latest hit: only a session started after it lifts the wait
            # No other job starts before it either; the latest reset of the jobs that hit the limit.
            self._quota_until = max(self._quota_until or retry_at, retry_at)
            if not await self._deferred_too_often(job, run):
                self._change(job, "deferred", "usage limit reached", retry_at.isoformat())
                if not self._quota_line_sent:  # one line per streak
                    self._quota_line_sent = True
                    await self._send(job_deferred_line(job.scope, job.number, job.kind, retry_at),
                                     job_id=job.id)
        elif outcome.kind == "login":
            end = self._now()
            first = self._login_failed_at is None
            self._login_failed_at = end
            if not await self._deferred_too_often(job, run):
                self._change(job, "deferred", "claude login failing", (end + LOGIN_BACKOFF).isoformat())
            self._report_auth_failure("claude_login", 401)
            if first:
                await self._send(login_line())
        elif outcome.kind == "failed":
            await self._fail(job, outcome.error or "the session failed")
        else:
            await self._finish(ctx, job, paths, run, prepared)

    async def _remove_root(self, root: Path, job: Job, run: JobRun) -> None:
        """Remove the job's directory (its job clone and worktree) in a thread: a checkout's many files would hold
        the event loop. One left behind is noted and logged; the next start removes it."""
        if not root.exists() and not root.is_symlink():
            return
        try:
            if root.is_symlink():
                root.unlink()
            else:
                await asyncio.to_thread(shutil.rmtree, root)
        except OSError as e:
            logger.warning("Reviewer job %s: its directory was not removed: %s", job.id, type(e).__name__)
            run.notes.append(f"job directory not removed: {type(e).__name__}")
            self._store.save_run(run)

    async def _deferred_too_often(self, job: Job, run: JobRun) -> bool:
        """Count the job's deferral; its third fails the job instead (with its line)."""
        run.deferrals += 1
        self._store.save_run(run)
        if run.deferrals < _MAX_DEFERRALS:
            return False
        await self._fail(job, _DEFERRED_TOO_OFTEN)
        return True

    async def _session(
        self, ctx: RepoContext, job: Job, paths: JobPaths, clone: ReviewClone,
    ) -> tuple[SessionOutcome, bool]:
        """The session's outcome, in the job clone, and whether it changed the job clone's .git/config,
        .git/info/attributes or .git/commondir. Nothing is written back: no git runs in the job clone after its
        session, and the job clone is removed with the job."""
        process = ctx.cfg.process
        if job.kind == "review":
            prompt = review_prompt(process.doc, process.prepare_section, job.number, paths)
        else:
            me = self._store.repo_state(ctx.scope).me
            prompt = recheck_prompt(process.doc, process.recheck_section, job.number, paths, job.head_sha, me)
        saved = guarded_git_files(clone.path)
        outcome = await run_session(
            self._llm, kind=job.kind, clone_dir=clone.path, paths=paths, prompt=prompt,
            job_cfg=self._config.jobs[job.kind], helper_scripts=process.helper_scripts,
            secret_paths=self._config.secret_paths, environ=self._environ,
        )
        changed = [path for path, data in saved.items() if git_file_changed(path, data)]
        if changed:
            logger.warning("Reviewer job %s: the session changed %s", job.id, ", ".join(map(str, changed)))
        return outcome, bool(changed)

    async def _finish(
        self, ctx: RepoContext, job: Job, paths: JobPaths, run: JobRun, prepared: Prepared,
    ) -> None:
        """Keep the run directory in the job's copy, parse from the copy, settle and send."""
        review = job.kind == "review"
        try:
            kept_out = _archive(paths.run_dir, paths.job_dir)
        except _ArchiveRefused as e:
            await self._fail(job, str(e))
            return
        if kept_out:
            run.notes += [f"not kept from the run directory: {name} (a name the job copy reserves)"
                          for name in kept_out]
            self._store.save_run(run)
        if not review and _regular(paths.replies_plan):
            shutil.copyfile(paths.replies_plan, paths.job_dir / _PLAN_COPY)
        output = paths.job_dir / (paths.report.name if review else _PLAN_COPY)
        if not output.is_file():
            await self._fail(job, "no report written" if review else "no replies plan written")
            return
        text = output.read_text(encoding="utf-8", errors="replace")
        try:
            if review:
                run.report = parse_report(text, job.head_sha).to_dict()
            else:
                run.replies = parse_replies_plan(text)
        except ReportError as e:
            await self._fail(job, f"report unreadable: {e}")
            return
        tg_file = paths.job_dir / "tg_lines.json"
        tg_lines = parse_tg_lines(tg_file.read_text(encoding="utf-8", errors="replace")) if tg_file.is_file() else {}
        run.tg_lines = {str(n): line for n, line in tg_lines.items()}
        row = self._store.board(ctx.scope).get(job.number)
        if row is not None:
            run.title, run.url, run.ticket_key, run.files = row.title, row.url, row.ticket_key, row.changed_files
        else:
            pr = prepared.pr
            key = m.group(0) if (m := TICKET_KEY.search(pr.title)) else ""
            run.title, run.url, run.ticket_key, run.files = pr.title, pr.url, key, pr.changed_files
        # Saved with the run: the message's parts come from it alone, the same on every attempt.
        run.posted_at_head, run.summary_posted = self._store.posted_count(job.scope, job.number, job.head_sha)
        self._store.save_run(run)
        state, reason = await self._settle(job)
        self._change(job, state, reason)
        if state == "done":
            await self._deliver(job, run)

    async def _settle(self, job: Job) -> tuple[str, str]:
        """settle(), after one read of the PR on GitHub: the board may not have seen a new head or a close
        yet. A failed read leaves it to the board."""
        ctx = self._contexts.get(job.scope)
        if ctx is not None:
            try:
                pr = await ctx.forge.pr_full(job.number)
            except Exception as e:
                logger.warning("Reviewer job %s: PR not read before settling: %s", job.id, _where(e))
            else:
                if pr.state != "open":
                    return "stale", "PR gone"
                if pr.head_sha != job.head_sha:
                    return "superseded", "new head"
        skipped = (job.kind == "review"
                   and self._store.skipped_at(job.scope, job.number, job.head_sha) > job.created_at)
        return settle(job, self._store.board(job.scope).get(job.number),
                      self._store.jobs(job.scope, job.number), skipped=skipped)

    async def _deliver(self, job: Job, run: JobRun) -> bool:
        """Whether the job's whole message is out. Sent part by part, each part a reply anchor to the job,
        starting at the first part not delivered yet: the parts come from the saved run alone, so they are the
        same on every attempt, and run.message_ids holds those already out. A failed part ends the attempt."""
        if self._chat is None:
            return False
        run.last_attempt_at = self._stamp()
        parts = message_parts(_message(job, run), limit=MESSAGE_LIMIT)
        for part in parts[len(run.message_ids):]:
            message_id = await self._send(part, job_id=job.id)
            if message_id is None:
                self._attempt_failed(run)
                return False
            run.message_ids.append(message_id)
            run.notes = self._store.run(job.id).notes  # with a note the chat made on a failed anchor write
            self._store.save_run(run)  # a part out is never sent again, even after a restart
        run.message_sent = True
        self._store.save_run(run)
        return True

    def _attempt_failed(self, run: JobRun) -> None:
        run.send_attempts += 1
        run.last_attempt_at = self._stamp()
        if run.send_attempts == _MAX_SEND_ATTEMPTS:
            run.notes.append(f"message not delivered after {_MAX_SEND_ATTEMPTS} attempts")
        self._store.save_run(run)

    async def _fail(self, job: Job, reason: str) -> None:
        self._change(job, "failed", reason)
        await self._send(job_failed_line(job.scope, job.number, job.kind, reason), job_id=job.id)

    async def _send_drift(self, ctx: RepoContext) -> None:
        process = ctx.cfg.process
        await self._send(drift_line(ctx.scope, process.doc, process.prepare_section), scope=ctx.scope)

    async def _send(self, text: str, job_id: str = "", scope: str = "") -> int | None:
        """The Telegram message id, the message a reply anchor to the job or the repo; None without a chat,
        or when the send failed (logged with the exception class only)."""
        if self._chat is None:
            return None
        try:
            return await self._chat.send(text, job_id=job_id, scope=scope)
        except Exception as e:
            logger.warning("Reviewer line not sent: %s", type(e).__name__)
            return None

    def _change(self, job: Job, state: str, reason: str, retry_at: str = "") -> None:
        self._store.set_job_state(job.id, state, reason, self._stamp(), retry_at)
        if state == "failed" and (run := self._store.run(job.id)) is not None:
            run.error = reason
            self._store.save_run(run)
        if self._event_bus is not None:
            ctx = self._contexts.get(job.scope)
            self._event_bus.emit(
                "reviewer_job", f"{job.scope} #{job.number}: {job.kind} job {state}",
                project_id=ctx.project_id if ctx else None,
                data={"scope": job.scope, "number": job.number, "kind": job.kind, "state": state},
            )

    def _stamp(self) -> str:
        return self._now().isoformat()

"""From a board update: the review, re-check and address jobs to queue or retire, the findings jobs to retire (only
the operator queues one), and the lines to send."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from config.schemas import ReviewerConfig
from orchestrator.pr_creation import COMPARED_MARKS
from orchestrator.reviewer.board import ADDRESS_ACTIONS, FIX_KINDS, BoardUpdate, job_kind
from orchestrator.reviewer.lines import large_line, turn_line
from orchestrator.reviewer.models import BoardRow, Job

_GONE_STATES = {"queued": "cancelled", "deferred": "cancelled", "done": "stale"}
# A new re-check supersedes the PR's older re-checks in these states: a reply plan read from older threads
# must not stay postable.
_RECHECK_SUPERSEDED = ("queued", "deferred", "done", "posted")
_PENDING = ("queued", "running", "deferred")
_MAX_FAILURES = 2  # spec, Failure handling: two failures at one head stop auto-retries
# The key of a review the operator asked for ("Review now") ends with it; an automatic review's key is its head.
REVIEW_NOW_SUFFIX = ":now"


@dataclass(frozen=True)
class Notice:
    number: int
    kind: str
    key: str
    text: str


@dataclass
class Plan:
    queue: list[tuple[int, str, str, str]]  # (number, kind, head_sha, key)
    states: list[tuple[str, str, str]]  # (job_id, state, reason)
    lines: list[str]
    notices: list[Notice]


def _blocked(same: list[Job]) -> bool:
    """Whether the jobs of one kind at one key keep a new one from being queued automatically:
    any job but a cancelled one does, a failed one only once _MAX_FAILURES have failed there."""
    failures = sum(j.state == "failed" for j in same)
    return any(j.state != "cancelled" and (j.state != "failed" or failures >= _MAX_FAILURES)
               for j in same)


def _turn_back(same: list[Job], states: dict[str, tuple[str, str]]) -> bool:
    """A turn that comes back makes the newest report current again, when it went stale; whether it did."""
    if same and not any(j.state in ("queued", "running", "deferred", "done") for j in same):
        newest = max(same, key=lambda j: j.created_at)
        if newest.state == "stale":
            states.setdefault(newest.id, ("done", "turn back"))
            return True
    return False


def _threads_of(job: Job) -> set[str]:
    """The thread ids an address job's key names (after its head)."""
    return {t for t in job.key.partition(":")[2].split(",") if t}


def held_prs(rows: dict[int, BoardRow], compared_tickets: frozenset[str]) -> frozenset[int]:
    """The two PRs of each ticket compared with the skill flow, by the mark their titles end with: until the
    verdict (which retitles the winner and closes the comparison) a review would change one side mid-comparison."""
    return frozenset(n for n, row in rows.items()
                     if row.ticket_key in compared_tickets and row.title.endswith(COMPARED_MARKS))


def plan_jobs(
    scope: str,
    update: BoardUpdate,
    jobs_by_pr: dict[int, list[Job]],
    is_skipped: Callable[[int, str], bool],
    cfg: ReviewerConfig,
    reviews_running: bool,
    now: datetime,
    first_poll: bool,
    posting: frozenset[str] = frozenset(),
    held: frozenset[int] = frozenset(),
    answered: Callable[[Job], frozenset[str]] = lambda job: frozenset(),
    pushed_from: Callable[[Job], str] = lambda job: "",
) -> Plan:
    """Rules in order: review (new head, turn back), re-check (turn back, new threads), address (turn back, new
    threads or head), findings (new head: never queued here), gone turns, turn lines.

    A job matched by more than one rule gets one state change: the first rule in that order wins. A job in
    `posting` (the ids of the jobs with a Post `posting`) is left to its post by the gone-turn rule: the post
    itself takes the turn away; and while a fix job of either kind has one, no address job is queued for its PR. A PR in
    `held` (see held_prs) gets no review, no re-check and no address job.
    `answered(job)`: the thread ids of an address job's replies that landed. `pushed_from(job)`: the head its
    landed push started from, "" without one.
    """
    plan = Plan([], [], [], [])
    states: dict[str, tuple[str, str]] = {}  # job id -> (state, reason)
    quiet = timedelta(minutes=cfg.quiet_minutes)
    for number, row in update.rows.items():
        if number in held:
            continue
        jobs = jobs_by_pr.get(number, [])
        head = row.head_sha
        review_settled = True  # the re-check waits while this head's review is still to come
        if "review" in row.my_actions:
            reviews = [j for j in jobs if j.kind == "review"]
            for j in reviews:
                if j.state in ("queued", "deferred") and j.head_sha != head:
                    states.setdefault(j.id, ("superseded", "new head"))
            at_head = [j for j in reviews if j.head_sha == head]
            _turn_back(at_head, states)
            blocked = _blocked(at_head)
            skipped = is_skipped(number, head)
            large = row.changed_files >= cfg.max_files_auto
            pending = any(j.state in ("queued", "running", "deferred") for j in at_head)
            waited = now - datetime.fromisoformat(row.head_first_seen_at) >= quiet
            if not (blocked or skipped) and waited:
                if large:
                    plan.notices.append(Notice(number, "large", head, large_line(scope, row)))
                elif reviews_running:
                    plan.queue.append((number, "review", head, head))
                    pending = True
                    for j in reviews:
                        if j.state in ("done", "posted") and j.head_sha != head:
                            states.setdefault(j.id, ("superseded", "new head"))
            review_settled = not pending and (skipped or large or blocked)
        if "recheck" in row.my_actions:
            rechecks = [j for j in jobs if j.kind == "recheck"]
            threads = next(t["threads"] for t in row.my_turns if t["action"] == "recheck")
            key = f"{head}:{','.join(sorted(threads))}"
            same_key = [j for j in rechecks if j.key == key]
            _turn_back(same_key, states)
            # A posted re-check blocks its key until the board saw my own thread turns change after the post:
            # another reviewer's turn, a role or the draft flag does not unblock it.
            changed = datetime.fromisoformat(row.thread_turns_change or row.last_change)
            blocking = [j for j in same_key if j.state != "posted" or changed <= datetime.fromisoformat(j.updated_at)]
            if review_settled and reviews_running and not _blocked(blocking):
                for j in rechecks:
                    if j.state in _RECHECK_SUPERSEDED:
                        states.setdefault(j.id, ("superseded", "new threads"))
                plan.queue.append((number, "recheck", head, key))
        # Address: the operator's own PR, and no ticket workspace holds it (its push would overwrite a fix).
        mine = [t for t in row.my_turns if t["action"] in ADDRESS_ACTIONS]
        if cfg.process.address_doc and not row.pipeline and "author" in row.roles and mine:
            addresses = [j for j in jobs if j.kind == "address"]
            current = {tid for t in mine for tid in t["threads"]}
            key = f"{head}:{','.join(sorted(current))}"
            same_key = [j for j in addresses if j.key == key]
            revived = _turn_back(same_key, states)
            # A job at this head covers the turn while it is pending, and once done or posted while every thread
            # of the turn is one of its threads whose reply has not landed. A job Cleave pushed for is at the new
            # head: it covers the head it was pushed from too, which the row shows until the board catches up with
            # GitHub (else this poll would queue a job at the old head and supersede the pushed one).
            covered = any((j.head_sha == head or pushed_from(j) == head) and (j.state in _PENDING or (
                j.state in ("done", "posted") and current <= _threads_of(j) - answered(j))) for j in addresses)
            # A post under way, of a fix of either kind: a push moves its job to the new head only once GitHub shows
            # that head, and no new fix of the PR starts while one is being pushed.
            busy = any(j.id in posting for j in jobs if j.kind in FIX_KINDS)
            failures = sum(j.state == "failed" for j in same_key)
            settled = datetime.fromisoformat(row.thread_turns_change or row.last_change)
            if (reviews_running and now - settled >= quiet
                    and not (covered or revived or busy or failures >= _MAX_FAILURES)):
                for j in addresses:
                    if j.state in _RECHECK_SUPERSEDED:
                        states.setdefault(j.id, ("superseded", "new threads"))
                plan.queue.append((number, "address", head, key))
        # Findings: only the operator queues one (ReviewerService.fix_findings). One at an old head is retired, unless
        # its landed push started from this head (the board is behind GitHub) or its push is under way.
        for j in jobs:
            if (j.kind == "findings" and j.state in ("queued", "deferred", "done") and j.head_sha != head
                    and pushed_from(j) != head and j.id not in posting):
                states.setdefault(j.id, ("superseded", "new head"))
    for change in update.gone_turns:
        kind = job_kind(change.action, True)
        if kind is None:
            continue
        gone_row = update.rows.get(change.number)
        if kind == "address" and gone_row is not None and ADDRESS_ACTIONS & set(gone_row.my_actions):
            continue  # another of the three actions is left: the job keeps its turn
        # A PR off the board (merged or closed, or no longer listed): "PR gone", as settle() says.
        reason = "turn gone" if gone_row is not None else "PR gone"
        for j in jobs_by_pr.get(change.number, []):
            if j.kind != kind or j.state not in _GONE_STATES or j.id in posting:
                continue
            state = _GONE_STATES[j.state]
            if state == "cancelled" and j.key.endswith(REVIEW_NOW_SUFFIX):
                continue  # the operator asked for it: it needs no turn, and only a skip drops it
            states.setdefault(j.id, (state, reason))
    plan.states = [(job_id, state, reason) for job_id, (state, reason) in states.items()]
    if not first_poll:
        for change in update.new_turns:
            row = update.rows.get(change.number)
            if row is None:
                continue
            # An address turn the rule above does not take (a ticket workspace holds the row, it is held, or the
            # PR is not mine) keeps its line, as in a repo without address_doc.
            address = (bool(cfg.process.address_doc) and not row.pipeline and "author" in row.roles
                       and change.number not in held)
            if job_kind(change.action, address) is None:
                plan.lines.append(turn_line(scope, row, change))
    return plan

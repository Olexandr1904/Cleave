"""From a board update: the review and re-check jobs to queue or retire, and the lines to send."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from config.schemas import ReviewerConfig
from orchestrator.reviewer.board import JOB_ACTIONS, BoardUpdate
from orchestrator.reviewer.lines import large_line, turn_line
from orchestrator.reviewer.models import Job

_GONE_STATES = {"queued": "cancelled", "deferred": "cancelled", "done": "stale"}
# A new re-check supersedes the PR's older re-checks in these states: a reply plan read from older threads
# must not stay postable.
_RECHECK_SUPERSEDED = ("queued", "deferred", "done", "posted")
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


def _turn_back(same: list[Job], states: dict[str, tuple[str, str]]) -> None:
    """A turn that comes back makes the newest report current again, when it went stale."""
    if same and not any(j.state in ("queued", "running", "deferred", "done") for j in same):
        newest = max(same, key=lambda j: j.created_at)
        if newest.state == "stale":
            states.setdefault(newest.id, ("done", "turn back"))


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
) -> Plan:
    """Rules in order: review (new head, turn back), re-check (turn back, new threads), gone turns,
    turn lines.

    A job matched by more than one rule gets one state change: the first rule in that order wins. A job in
    `posting` (the ids of the jobs with a Post `posting`) is left to its post by the gone-turn rule: the post
    itself takes the turn away.
    """
    plan = Plan([], [], [], [])
    states: dict[str, tuple[str, str]] = {}  # job id -> (state, reason)
    quiet = timedelta(minutes=cfg.quiet_minutes)
    for number, row in update.rows.items():
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
    for change in update.gone_turns:
        if change.action not in JOB_ACTIONS:
            continue
        for j in jobs_by_pr.get(change.number, []):
            if j.kind != change.action or j.state not in _GONE_STATES or j.id in posting:
                continue
            state = _GONE_STATES[j.state]
            if state == "cancelled" and j.key.endswith(REVIEW_NOW_SUFFIX):
                continue  # the operator asked for it: it needs no turn, and only a skip drops it
            states.setdefault(j.id, (state, "turn gone"))
    plan.states = [(job_id, state, reason) for job_id, (state, reason) in states.items()]
    if not first_poll:
        for change in update.new_turns:
            row = update.rows.get(change.number)
            if change.action not in JOB_ACTIONS and row is not None:
                plan.lines.append(turn_line(scope, row, change))
    return plan

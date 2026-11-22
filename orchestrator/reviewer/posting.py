"""Posting from Telegram: the only module that posts.

A review post runs the spec's "Posting a review" in order, one post at a time: the job's state, GitHub's head and
the operator's own reviews, the named items' text, the job copy ticked on exactly the named numbers, the repo's
poster tool in a dry run checked against them, GitHub's head again, the confirm run, and a post-condition read on
GitHub before anything is called posted. A re-check's replies run the spec's "Posting re-check replies" by the
same rules: the plan checked against Prepare's threads snapshot, a plan file of exactly the named entries, the
replies tool in a dry run checked against them, the confirm run, and a post-condition read with the threads
command. An address job's push runs the address spec's "Push": its patch committed on the PR head in a worktree of
the review clone and pushed without force, then GitHub's head read back. Nothing here holds the forge token: the
poster, the threads command and the review clone's git run with it, and their output comes back redacted.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import shutil
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from config.schemas import POSTER_PLACEHOLDERS, REPLIES_PLACEHOLDERS
from orchestrator.reviewer.board import FIX_KINDS
from orchestrator.reviewer.clone import CloneError
from orchestrator.reviewer.commands import CommandResult, placeholder_values, stderr_tail
from orchestrator.reviewer.fix import PATCH_NAME
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.grammar import ReplyCommand, check_command, retry_text
from orchestrator.reviewer.lines import (
    address_body_problem_line,
    already_approved_line,
    already_reviewed_line,
    approve_unknown_line,
    approved_line,
    body_problem_line,
    github_unreadable_line,
    head_moved_line,
    internal_error_line,
    left_out_line,
    not_posted_line,
    not_pushed_line,
    not_shown_line,
    nothing_left_line,
    outdated_line,
    partial_line,
    posted_line,
    pr_closed_line,
    pushed_line,
    recheck_body_problem_line,
    replies_partial_line,
    replies_posted_line,
    summary_landed_line,
    unverified_line,
)
from orchestrator.reviewer.models import POSTABLE_STATES, ForgeReview, Job, Post, PRFull
from orchestrator.reviewer.poster_output import (
    PosterOutputError,
    ReviewDryRun,
    parse_replies_results,
    parse_replies_steps,
    parse_review_confirm,
    parse_review_dry_run,
)
from orchestrator.reviewer.readiness import ReadinessError, parse_threads_snapshot
from orchestrator.reviewer.report import Report, ReportError, ReportItem, parse_report
from orchestrator.reviewer.report_edit import (
    body_problems,
    drop_summary,
    ensure_base,
    tick,
    write_atomic,
)
from orchestrator.reviewer.service import RepoContext, _where, utcnow
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import N_, detail, tr

logger = logging.getLogger(__name__)

EVENT_STATE = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}
_MAX_PATH = 200  # characters of a payload comment's path shown in a reply
_FINDINGS = "### Findings without a diff anchor"  # where a payload body's Summary ends and its findings start
# The texts below and the reasons marked N_ are kept in English (a Post's error) and translated where Telegram gets
# them (lines.detail, tr).
_REPORT_CHANGED = N_("The report changed while posting (an edit landed): look at it again and reply. Nothing was done.")
_PREPARED_THREADS = "threads.prepared.json"  # Prepare's threads snapshot, kept in a re-check's copy by the runner
_NO_SNAPSHOT = N_("Prepare's threads snapshot is not in this job's copy: nothing posted.")
_PLAN_KEYS = ("n", "thread_id", "comment_id", "review_id", "body", "resolve")  # what the written plan keeps
_RESULT_STEP = {"replied": "reply", "commented": "comment", "resolved": "resolve"}  # a confirm result's step
_STEP_ORDER = ("reply", "resolve", "comment")
_RESOLVED = N_(" · resolved (reply not seen on GitHub: resolved threads are not listed)")
_PLAIN_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")  # the branch names Cleave pushes to (and never one starting "-")
_T = TypeVar("_T")


@dataclass(frozen=True)
class PostOutcome:
    state: str  # posted, partial, refused, failed, unverified, nothing
    text: str


@dataclass(frozen=True)
class DryRunPlan:
    """What the confirm run will land, from a dry run that matched the command."""
    reviews: int
    comments: int
    first_body: str
    summary: bool
    inline: tuple[int, ...]  # the moved ones included
    moved: tuple[int, ...]
    in_body: tuple[int, ...]
    skipped: tuple[int, ...]
    batches: tuple[tuple[int, ...], ...]  # the numbers in each payload; the body items in the first


# An approve without a body: one review, no comments, an empty body.
_APPROVE_PLAN = DryRunPlan(1, 0, "", False, (), (), (), (), ((),))


class _Refused(Exception):
    """The post stops before the confirm run (or the approve): nothing was posted."""


def check_dry_run(dry: ReviewDryRun, report: Report, numbers: tuple[int, ...], event: str, head: str,
                  allowed: tuple[str, ...], *, no_summary: bool = False) -> DryRunPlan | str:
    """The plan of what the confirm run lands, or why the dry run does not post exactly what was named.

    A comment is item n's when its path is the item's, its body starts with `**<title>**` and holds the item's
    own text (`_item_words`). A payload body is the Summary up to its last `### Findings without a diff anchor`
    heading, then the findings: a named number that is not inline is in the review body when `**<title>**` and its
    text are in the findings, else the ledger skips it. An unnamed item's `**<title>**` may be in neither part,
    but in the Summary as the report's own Summary has it; with `no_summary` there is no Summary at all. The
    dry run's Summary line is not trusted: a payload body holds the report's Summary exactly when it says yes."""
    named = sorted(numbers)
    listed = sorted(item.number for item in dry.items)
    if listed != named:
        return f"the dry run lists {_numbered(listed)} instead of {_numbered(named)}"
    if len(dry.head) < 7 or not head.startswith(dry.head):
        return f"the dry run's HEAD is {dry.head[:9]}, not {head[:9]}"
    if dry.event != event:
        return f"the dry run's event is {dry.event}, not {event}"
    bodies = [payload.get("body", "") for payload in dry.payloads]
    parts = [_body_parts(body) for body in bodies]  # (the Summary, the findings) of each payload body
    if no_summary and (dry.summary or any(summary.strip() for summary, _ in parts)):
        return "the dry run posts the Summary, which you left out"
    if len(dry.payloads) != dry.reviews:
        return f"the dry run plans {dry.reviews} review(s) but shows {len(dry.payloads)}"
    shown = sum(len(payload["comments"]) for payload in dry.payloads)
    if shown != dry.comments:
        return f"the dry run plans {dry.comments} comment(s) but shows {shown}"
    if (other := next((p["commit_id"] for p in dry.payloads if p["commit_id"] != head), None)) is not None:
        return f"a payload is for commit {other[:9]}"
    if (other := next((p["event"] for p in dry.payloads if p["event"] != event), None)) is not None:
        return f"a payload's event is {other[:60]}"
    items = {item.number: item for item in report.items}
    wanted = [items[n] for n in named]
    inline: dict[int, int] = {}  # a number that goes inline: its comment's line
    batches: list[list[int]] = []
    for payload in dry.payloads:
        batch: list[int] = []
        for comment in payload["comments"]:
            words = _words(comment["body"])
            matches = [item for item in wanted if comment["path"] == item.path
                       and comment["body"].startswith(f"**{item.title}**") and _item_words(item) in words]
            if not matches:
                return f"the comment on {_place(comment)} matches no named number"
            free = [item for item in matches if item.number not in inline]
            if not free:
                return f"the comment on {_place(comment)} matches #{matches[0].number} twice"
            inline[free[0].number] = comment["line"]
            batch.append(free[0].number)
        batches.append(batch)
    rest = [item for item in wanted if item.number not in inline]
    in_body = [item.number for item in rest if any(
        f"**{item.title}**" in findings and _item_words(item) in _words(findings) for _, findings in parts)]
    skipped = [item.number for item in rest if item.number not in in_body]
    if len(skipped) > dry.skipped:
        return f"#{skipped[0]} is neither inline, in the review body nor skipped"
    if len(skipped) < dry.skipped:
        return f"the dry run skips {dry.skipped} already posted, not {len(skipped)}"
    unnamed = [(item.number, f"**{item.title}**") for item in report.items if item.number not in numbers]
    for n, bold in unnamed:
        if any(bold in findings or (bold in summary and bold not in report.summary) for summary, findings in parts):
            return f"the review body holds #{n}, which you did not name"
    for payload in dry.payloads:
        for comment in payload["comments"]:
            if (n := next((n for n, bold in unnamed if bold in comment["body"]), None)) is not None:
                return f"the comment on {_place(comment)} holds #{n}, which you did not name"
    for payload, body in zip(dry.payloads, bodies):
        for comment in payload["comments"]:
            if problems := body_problems(comment["body"], allowed):
                return f"the comment on {_place(comment)} {problems[0]}"
        if problems := body_problems(body, allowed):
            return f"the review body {problems[0]}"
    if summary_words := _words(report.summary):
        carried = any(summary_words in _words(body) for body in bodies)
        if carried and not dry.summary:
            return "the dry run says Summary: no, but a review body holds the Summary"
        if dry.summary and bodies and not carried:
            return "the dry run says Summary: yes, but no review body holds the Summary"
    if batches:
        batches[0] += in_body
    return DryRunPlan(
        reviews=dry.reviews,
        comments=dry.comments,
        first_body=bodies[0] if bodies else "",
        summary=dry.summary,
        inline=tuple(sorted(inline)),
        moved=tuple(sorted(n for n, line in inline.items() if line != items[n].line)),
        in_body=tuple(in_body),
        skipped=tuple(skipped),
        batches=tuple(tuple(sorted(batch)) for batch in batches),
    )


def validate_plan(plan: list[dict], threads: dict, me: str) -> tuple[dict[int, dict], dict[int, str]]:
    """A re-check's replies plan checked against a threads snapshot (Prepare's): the valid entries by `n`, and why
    each other one is not postable. The plan is model output written after reading untrusted text, so an id
    matches only as the same JSON type."""
    unresolved, bodies = _listed(threads, "threads"), _listed(threads, "review_bodies")
    valid: dict[int, dict] = {}
    invalid: dict[int, str] = {}
    for entry in plan:
        if (why := _not_postable(entry, unresolved, bodies, me)) is None:
            valid[entry["n"]] = entry
        else:
            invalid[entry["n"]] = why
    return valid, invalid


def _not_postable(entry: dict, threads: list[dict], bodies: list[dict], me: str) -> str | None:
    if "fix_sha" in entry:
        return "carries fix_sha"
    if ("thread_id" in entry) == ("review_id" in entry):
        return "names neither a thread nor a review"
    if "thread_id" in entry:
        thread_id = _shown(entry["thread_id"])
        thread = _find(threads, entry["thread_id"])
        if thread is None:
            return f"thread {thread_id} is not an unresolved thread of this PR"
        if not _is_me(thread.get("turn"), me):
            return f"thread {thread_id} is not your turn"
        if "comment_id" in entry and not _same_id(entry["comment_id"], _first_comment_id(thread)):
            return f"comment {_shown(entry['comment_id'])} is not the first comment of thread {thread_id}"
        return None
    review_id = _shown(entry["review_id"])
    body = _find(bodies, entry["review_id"])
    if body is None:
        return f"review {review_id} is not a review body of this PR"
    if body.get("answered") is not False:
        return f"review {review_id} is answered already"
    return None


def _check_steps(steps: list[dict], entries: list[dict], threads: dict, allowed: tuple[str, ...],
                 pr_url: str) -> dict[int, list[str]] | str:
    """The steps the confirm run takes, by entry number, or why the dry run does not post exactly the named
    entries: a thread entry gets a reply (and a resolve when its `resolve` is true), a review entry a comment, and
    a named entry with no step is already posted. A resolve alone passes here: whether its reply is in the thread
    is GitHub's to say, right before the confirm run. A reply carries its entry's text, a comment the same after a
    line naming the review it answers (`_shown_text`); a resolve posts none, and shows none or its entry's text.
    Every text passes the body check. Where each step aims is checked once GitHub's threads are read (`_off_aim`)."""
    by_n = {entry["n"]: entry for entry in entries}
    planned: dict[int, list[str]] = {}
    for step in steps:
        planned.setdefault(step["n"], []).append(step["step"])
    for n, kinds in planned.items():
        if n not in by_n or sorted(kinds) not in _allowed_steps(by_n[n]):
            return f"the dry run plans {' + '.join(kinds)} for #{n}"
    for step in steps:
        entry, what, body = by_n[step["n"]], f"the {step['step']} for #{step['n']}", step.get("body", "")
        if step["step"] == "resolve":
            if _words(body) not in ("", _words(entry["body"])):
                return f"{what} carries a text"
        elif _words(body) != _words(_shown_text(step["step"], entry, threads, pr_url)):
            return f"{what} is not its planned text"
        if problems := body_problems(body, allowed):
            return f"{what} {problems[0]}"
    return {n: [kind for kind in _STEP_ORDER if kind in kinds] for n, kinds in planned.items()}


def _off_aim(steps: list[dict], entries: list[dict], prepared: dict, current: dict) -> str | None:
    """Why a step of the dry run does not aim at its entry's thread or review body, None when each does. A thread
    is named by its id or by its `path:line` as GitHub shows it now (`current`: the threads command right before
    the confirm run) or as Prepare's snapshot (`prepared`) had it: Cleave's own push of a fix moves a thread's line
    when the fix added lines above it."""
    by_n = {entry["n"]: entry for entry in entries}
    for step in steps:
        entry = by_n[step["n"]]
        if not any(_aims_at(step["target"], entry, threads) for threads in (current, prepared)):
            return f"the {step['step']} for #{step['n']} is aimed at {step['target']}"
    return None


class Posting:
    """Posts a job's review or a re-check's replies, one post at a time (one lock for every repo and both kinds).
    Each post is a Post in the store from right before the confirm run (or the approve): `posting` until it ends,
    as a restart leaves it."""

    def __init__(self, store: ReviewerStore, *, ledgers_dir: Path, now: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, verify_reads: int = 3,
                 verify_wait: float = 2.0, reader_wait: float = 600.0, work_dir: Path | None = None):
        self._store = store
        self._ledgers_dir = Path(ledgers_dir)
        # A push's worktree of the review clone goes under work_dir/push; by default beside the ledgers.
        self._work_dir = Path(work_dir) if work_dir is not None else self._ledgers_dir.parent / "work"
        self._now = now
        self._sleep = sleep
        self._verify_reads = verify_reads
        self._verify_wait = verify_wait
        self._reader_wait = reader_wait
        self._lock = asyncio.Lock()
        self._post: Post | None = None  # the post's Post as last saved, once it started (the confirm run may land)

    def ledger_path(self, ctx: RepoContext, number: int, head: str) -> Path:
        return self._ledgers_dir / ctx.project_id / ctx.repo_id / f"PR_{number}" / f"posted_{head[:9]}.json"

    def recover(self) -> list[Post]:
        """Every post a restart left `posting`, now `interrupted` (at start, before any post runs)."""
        stamp = self._stamp()
        recovered = []
        for post in self._store.posts_in_state("posting"):
            post = dataclasses.replace(post, state="interrupted", updated_at=stamp)
            self._store.save_post(post)
            recovered.append(post)
        return recovered

    async def post_review(self, ctx: RepoContext, job: Job, cmd: ReplyCommand) -> PostOutcome:
        """Never raises but a cancellation, which leaves the Post under way `posting`: the outcome is unknown,
        neither failed nor "Not posted". A cancel is a shutdown as a rule, and the restart's recover() marks it
        `interrupted` for the line the operator gets."""
        async with self._lock:
            try:
                return await self._post_review(ctx, job, cmd)
            except _Refused as e:
                return PostOutcome("refused", str(e))
            except Exception as e:
                return self._internal_error(job, e)
            finally:
                self._post = None

    async def _post_review(self, ctx: RepoContext, job: Job, cmd: ReplyCommand) -> PostOutcome:
        if cmd.action != "post":  # the caller routes by action; this is the module that posts
            raise _Refused(tr("Not a post command. Nothing was done."))
        # 1. The job as it is now; me; the named numbers checked again, since an identical post may have landed
        # after the reply was checked.
        job = self._current(job.id)
        number, head = job.number, job.head_sha
        me = self._store.repo_state(job.scope).me
        if not me:
            raise _Refused(tr("Waiting for the first poll: nothing posted."))
        run = self._store.run(job.id)
        report = Report.from_dict(run.report)
        posted = self._store.posted_numbers(job, report)
        checked = check_command(cmd, job.kind, report, run.replies, posted, set(run.drafted))
        if isinstance(checked, str):
            raise _Refused(checked)
        cmd = checked
        # 2. GitHub's head; 3. an approval at the head ("anyway" or not: never a second one), and a review of the
        # operator's own since the run started. An APPROVE is refused here, so "nothing left" never offers one.
        await self._pr_at_head(ctx, number, head)
        reviews = await self._read(number, ctx.forge.reviews(number))
        self._refuse_second_approve(job, me, cmd, reviews)
        if not cmd.anyway and (own := self._reviewed_since(job, me, run.started_at, reviews)):
            raise _Refused(already_reviewed_line(own.submitted_at))
        # 4. The named items' text.
        items = {item.number: item for item in report.items}
        for n in cmd.numbers:
            if problems := body_problems(f"**{items[n].title}**\n{items[n].body}", ctx.allowed_links):
                raise _Refused(body_problem_line(n, problems[0]))
        # 5. The job copy ticked on exactly the named numbers.
        if cmd.event == "APPROVE" and not cmd.numbers and cmd.no_summary:
            return await self._approve(ctx, job, me, cmd)
        job_dir = Path(run.job_dir)
        head9 = head[:9]
        report_path = job_dir / f"report_{head9}.md"
        try:
            base = ensure_base(job_dir, report_path.name)
        except ReportError as e:
            raise _Refused(not_posted_line(number, str(e))) from None
        except FileNotFoundError:
            raise _Refused(not_posted_line(number, N_("the job's report file is missing"))) from None
        text = base.read_bytes().decode("utf-8")
        self._refuse_changed(job, text, items, cmd.numbers)
        ticked = tick(text, set(cmd.numbers))
        write_atomic(report_path, drop_summary(ticked) if cmd.no_summary else ticked)
        ledger = self.ledger_path(ctx, number, head)
        _make_dirs(ledger.parent)
        values = placeholder_values(
            POSTER_PLACEHOLDERS, repo=ctx.readiness.repo, pr=str(number), head9=head9, report=str(report_path),
            plan=str(job_dir / "posting" / f"replies_{head9}.json"), job_dir=str(job_dir), ledger=str(ledger),
            event=cmd.event)
        # 6-8 in one lease: the clone's checkout cannot move between the dry run and the confirm run.
        async with AsyncExitStack() as lease:
            try:
                await lease.enter_async_context(ctx.clone.reader(wait=self._reader_wait))
            except CloneError as e:
                raise _Refused(not_posted_line(number, str(e))) from None
            plan = await self._dry_run(ctx, values, number, report, cmd, head)
            if plan.reviews == 0:
                return PostOutcome("nothing", _nothing_left(number, head, cmd))
            pr = await self._pr_at_head(ctx, number, head)
            before_reviews = await self._read(number, ctx.forge.reviews(number))
            before = {r.id for r in before_reviews}
            self._refuse_second_approve(job, me, cmd, before_reviews)
            self._current(job.id)
            # An edit may have landed during the dry run: the text the operator now has is not the one checked.
            self._refuse_changed(job, base.read_bytes().decode("utf-8"), items, cmd.numbers)
            post = self._start(job, cmd, "review")
            result = await ctx.poster.review(ctx.clone.path, values, confirm=True)
            try:
                batches: list[tuple[int, int, str]] | None = parse_review_confirm(result.stdout)
                unknown = ""
            except PosterOutputError as e:  # what the tool reports is not known: GitHub alone says what landed
                batches, unknown = None, str(e)
        # 9. The post-condition, on GitHub only, whatever the tool printed and however its run ended.
        try:
            new, why = await self._verify(ctx, number, me, before, plan, cmd.event, head)
        except ForgeError as e:
            self._end(post, "unverified", error=str(e))
            return PostOutcome("unverified", unverified_line(number, str(e), pr.url))
        # 10. GitHub is the truth, whatever the tool's exit or output.
        if why is None:
            landed = sorted(plan.inline + plan.in_body)
            urls = [r.url for r in new]
            stamp = self._end(post, "posted", landed=_landed(items, plan, landed), summary_posted=plan.summary,
                              comments=plan.comments, review_ids=[r.id for r in new], urls=urls)
            self._mark_posted(post, stamp)
            text = posted_line(number, head, cmd.event, urls, plan.inline, plan.moved, plan.in_body, plan.skipped)
            return PostOutcome("posted", text + _held_anyway(result, unknown))
        failed = result.timed_out or result.returncode != 0
        if failed:
            why = f"{why}; {_tool_failure(result)}"
        if unknown and not result.timed_out:  # a run cut short leaves no output to understand
            why = f"{why}; {_not_understood(unknown)}"
        # A batch landed only when GitHub shows it: a new review by me at the head in the planned state, the
        # batches in the order the tool posts them. The tool's own lines alone never count.
        state = EVENT_STATE[cmd.event]
        shown = [r for r in new if r.commit_id == head and r.state == state][:plan.reviews]
        if not shown:
            if new or batches or unknown or result.timed_out or not failed:
                # It may have landed: GitHub shows something else new, or the tool reports it posted (or cannot
                # say, or says what Cleave does not understand). Never "Not posted"; no review id is recorded, so
                # the next post needs "anyway".
                self._end(post, "unverified", error=why)
                return PostOutcome("unverified", not_shown_line(number, why, pr.url))
            self._end(post, "failed", error=why)
            return PostOutcome("failed", not_posted_line(number, why))
        landed_batches = [(i, r.url) for i, r in enumerate(shown, 1)]
        landed = sorted({n for i, _ in landed_batches for n in plan.batches[i - 1]})
        missing = sorted(set(plan.inline + plan.in_body) - set(landed))
        # The tool reports them, GitHub does not show them.
        unconfirmed = [i for i, _, _ in batches or () if i > len(shown)]
        stamp = self._end(post, "partial", landed=_landed(items, plan, landed), summary_posted=plan.summary,
                          comments=sum(n in plan.inline for n in landed), review_ids=[r.id for r in shown],
                          urls=[r.url for r in shown], error=why)
        self._mark_posted(post, stamp)
        # The rest as the command was, its flags kept; without "approve": batch 1 approved, and a second approve is
        # refused; with "no summary" when batch 1 carried the Summary: it landed.
        event = "COMMENT" if cmd.event == "APPROVE" else cmd.event
        no_summary = cmd.no_summary or plan.summary
        retry = retry_text(missing, event, no_summary=no_summary, anyway=cmd.anyway) if missing else ""
        return PostOutcome("partial", partial_line(number, head, landed_batches, plan.reviews, landed, missing, why,
                                                   retry, unconfirmed))

    async def _approve(self, ctx: RepoContext, job: Job, me: str, cmd: ReplyCommand) -> PostOutcome:
        """An APPROVE without a body: one POST through the forge reader, never retried, then the post-condition."""
        number, head = job.number, job.head_sha
        pr = await self._pr_at_head(ctx, number, head)
        before_reviews = await self._read(number, ctx.forge.reviews(number))
        before = {r.id for r in before_reviews}
        self._refuse_second_approve(job, me, cmd, before_reviews)
        self._current(job.id)
        post = self._start(job, cmd, "approve")
        unknown: ForgeError | None = None  # the approve may have landed
        try:
            await ctx.forge.approve(number, head)
        except ForgeError as e:
            if not e.unavailable:
                self._end(post, "failed", error=f"GitHub said {e}")
                return PostOutcome("failed", not_posted_line(number, f"GitHub said {e}"))
            unknown = e
        try:
            new, why = await self._verify(ctx, number, me, before, _APPROVE_PLAN, "APPROVE", head)
        except ForgeError as e:
            self._end(post, "unverified", error=str(e))
            return PostOutcome("unverified", unverified_line(number, str(e), pr.url))
        if why is None:
            stamp = self._end(post, "posted", review_ids=[new[0].id], urls=[new[0].url])
            self._mark_posted(post, stamp)
            return PostOutcome("posted", approved_line(number, head, new[0].url))
        if unknown is not None and not new:
            why = f"GitHub said {unknown}, and shows no new review"
            self._end(post, "failed", error=why)
            return PostOutcome("failed", not_posted_line(number, why))
        # GitHub accepted the approve, or shows something else new: it may have landed, so never "Not posted". No
        # review id is recorded, so the next post needs "anyway".
        self._end(post, "unverified", error=why)
        return PostOutcome("unverified", not_shown_line(number, why, pr.url))

    async def _dry_run(self, ctx: RepoContext, values: dict[str, str], number: int, report: Report,
                       cmd: ReplyCommand, head: str) -> DryRunPlan:
        result = await ctx.poster.review(ctx.clone.path, values, confirm=False)
        if result.timed_out or result.returncode != 0:
            raise _Refused(not_posted_line(number, f"the dry run failed: {_dry_run_failure(result)}"))
        try:
            dry = parse_review_dry_run(result.stdout)
        except PosterOutputError as e:
            raise _Refused(not_posted_line(number, f"the dry run's output was not understood: {e}")) from None
        plan = check_dry_run(dry, report, cmd.numbers, cmd.event, head, ctx.allowed_links, no_summary=cmd.no_summary)
        if isinstance(plan, str):
            raise _Refused(not_posted_line(number, plan))
        # Never twice: the tool's ledger is not the Summary's only guard. An event-only post has nothing else to
        # post, and "no summary" would leave its review empty.
        if plan.summary and self._store.posted_count(ctx.scope, number, head)[1]:
            raise _Refused(summary_landed_line(number, head) if cmd.numbers else _nothing_left(number, head, cmd))
        return plan

    async def _verify(self, ctx: RepoContext, number: int, me: str, before: set[int], plan: DryRunPlan,
                      event: str, head: str) -> tuple[list[ForgeReview], str | None]:
        """The operator's reviews GitHub lists that were not there before the confirm run, oldest first, and what
        differs from the plan (None: the post-condition holds); read up to verify_reads times, verify_wait apart.
        A ForgeError is raised."""
        for read in range(self._verify_reads):
            if read:
                await self._sleep(self._verify_wait)
            new = sorted((r for r in await ctx.forge.reviews(number) if _same_login(r.login, me)
                          and r.id not in before), key=lambda r: r.id)
            why = await self._differs(ctx, number, new, plan, event, head)
            if why is None:
                break
        return new, why

    async def _differs(self, ctx: RepoContext, number: int, new: list[ForgeReview], plan: DryRunPlan, event: str,
                       head: str) -> str | None:
        if not new:
            return "GitHub shows no new review"
        if len(new) != plan.reviews:
            return f"GitHub shows {len(new)} new review(s), not {plan.reviews}"
        state = EVENT_STATE[event]
        for r in new:
            if r.state != state:
                return f"a new review is {r.state[:60]}, not {state}"
            if r.commit_id != head:
                return f"a new review is for commit {r.commit_id[:9]}"
        comments = 0
        for r in new:  # read only once the rest holds
            comments += len(await ctx.forge.review_comments(number, r.id))
        if comments != plan.comments:
            return f"GitHub shows {comments} comment(s), not {plan.comments}"
        if _body(new[0].body) != _body(plan.first_body):
            return "the first new review's body is not the planned one"
        return None

    async def post_replies(self, ctx: RepoContext, job: Job, cmd: ReplyCommand) -> PostOutcome:
        """A re-check's drafted replies, under the same lock and rules as post_review: never raises but a
        cancellation, which leaves the Post under way `posting` for the restart's recover()."""
        async with self._lock:
            try:
                return await self._post_replies(ctx, job, cmd)
            except _Refused as e:
                return PostOutcome("refused", str(e))
            except Exception as e:
                return self._internal_error(job, e)
            finally:
                self._post = None

    async def _post_replies(self, ctx: RepoContext, job: Job, cmd: ReplyCommand) -> PostOutcome:
        if cmd.action != "post_replies":  # the caller routes by action; this is the module that posts
            raise _Refused(tr("Not a post command. Nothing was done."))
        # 1. The job as it is now; me; the named numbers checked again, since an identical post may have landed
        # after the reply was checked.
        job = self._current(job.id)
        number, head = job.number, job.head_sha
        me = self._store.repo_state(job.scope).me
        if not me:
            raise _Refused(tr("Waiting for the first poll: nothing posted."))
        run = self._store.run(job.id)
        checked = check_command(cmd, job.kind, None, run.replies, self._store.posted_numbers(job, None),
                                set(run.drafted))
        if isinstance(checked, str):
            raise _Refused(checked)
        cmd = checked
        # 2. GitHub's head.
        await self._pr_at_head(ctx, number, head)
        # 3. The plan checked against Prepare's snapshot of this PR at this head, never the copy's threads.json,
        # which the session may have rewritten; then the named replies' text. An address job Cleave pushed for
        # was prepared at the head before its push.
        address = job.kind == "address"
        push = self._store.push_of(job) if address else None
        prepared = _prepared_threads(run.job_dir, number)
        if (other := _other_pr(prepared, number, push.head_sha if push else head)) is not None:
            raise _Refused(not_posted_line(number, f"Prepare's threads snapshot is of {other}"))
        valid, invalid = validate_plan(run.replies, prepared, me)
        if (bad := next((n for n in cmd.numbers if n in invalid), None)) is not None:
            raise _Refused(tr("#{n} cannot be posted: {why}. Nothing was done.", n=bad, why=detail(invalid[bad])))
        entries = [valid[n] for n in cmd.numbers]
        fix_shas: dict[int, str] = {}
        if address:
            # A reply that reports a fix names the job's head: the pushed commit, or the head the fix was already
            # on when the job changed no code. Cleave writes the SHA; the model's entry never carries one.
            unpushed = next((e["n"] for e in entries if e.get("fixed") is True), None) if run.fix and not push else None
            if unpushed is not None and run.fix.get("protected"):
                raise _Refused(tr('#{n} reports a fix that is not pushed: reply "push anyway" first. Nothing was done.',
                                  n=unpushed))
            if unpushed is not None:
                raise _Refused(tr('#{n} reports a fix that is not pushed: reply "push" first. Nothing was done.',
                                  n=unpushed))
            entries = [{**e, "body": e["body"].replace("{fix_sha}", head[:9])} if e.get("fixed") is True else e
                       for e in entries]
            fix_shas = {e["n"]: head for e in entries if e.get("fixed") is True}
        for entry in entries:
            if problems := body_problems(entry["body"], ctx.allowed_links):
                line = address_body_problem_line if address else recheck_body_problem_line
                raise _Refused(line(entry["n"], problems[0]))
        # 4. A plan of exactly the named entries.
        job_dir = Path(run.job_dir)
        head9 = head[:9]
        plan = _write_plan(job_dir, head9, entries, number, fix_shas)
        values = placeholder_values(REPLIES_PLACEHOLDERS, repo=ctx.readiness.repo, pr=str(number), head9=head9,
                                    plan=str(plan), job_dir=str(job_dir))
        # 5-8 in one lease, the post-condition included: the threads command runs from the clone too.
        async with AsyncExitStack() as lease:
            try:
                await lease.enter_async_context(ctx.clone.reader(wait=self._reader_wait))
            except CloneError as e:
                raise _Refused(not_posted_line(number, str(e))) from None
            planned, steps = await self._replies_dry_run(ctx, values, number, entries, prepared)
            pr = await self._pr_at_head(ctx, number, head)
            before = await self._threads_before(ctx, number, head)
            if (off := _off_aim(steps, entries, prepared, before)) is not None:
                raise _Refused(not_posted_line(number, off))
            # An entry the dry run leaves out is already posted only when GitHub agrees.
            by_n = {entry["n"]: entry for entry in entries}
            left_out = {n: why for n in cmd.numbers
                        if n not in planned and (why := _left_out_unseen(by_n[n], before, prepared, me)) is not None}
            if not planned:
                if left_out:
                    # No Post and no confirm run: nothing was posted here. "unverified" is the tool's word that
                    # GitHub does not bear out, not a post that may have landed (Task 14: no interrupted line).
                    return PostOutcome("unverified", left_out_line(number, "; ".join(left_out.values()), pr.url))
                return PostOutcome("nothing", nothing_left_line(number, head))
            for n in sorted(planned):  # GitHub, right before the confirm run, must still allow each step
                if (refused := _not_postable_now(by_n[n], planned[n], before, prepared, me, number)) is not None:
                    raise _Refused(refused)
            self._current(job.id)
            post = self._start(job, dataclasses.replace(cmd, event=""), "replies")
            result = await ctx.poster.replies(ctx.clone.path, values, confirm=True)
            try:
                results: list[dict] | None = parse_replies_results(result.stdout)
                unknown = ""
            except PosterOutputError as e:  # what the tool reports is not known: GitHub alone says what landed
                results, unknown = None, str(e)
            return await self._replies_verified(ctx, job, post, pr, me, cmd.numbers, entries, planned, before,
                                                left_out, results, result, unknown)

    async def _replies_dry_run(self, ctx: RepoContext, values: dict[str, str], number: int, entries: list[dict],
                               prepared: dict) -> tuple[dict[int, list[str]], list[dict]]:
        result = await ctx.poster.replies(ctx.clone.path, values, confirm=False)
        if result.timed_out or result.returncode != 0:
            raise _Refused(not_posted_line(number, f"the dry run failed: {_dry_run_failure(result)}"))
        try:
            steps = parse_replies_steps(result.stdout)
        except PosterOutputError as e:
            raise _Refused(not_posted_line(number, f"the dry run's output was not understood: {e}")) from None
        planned = _check_steps(steps, entries, prepared, ctx.allowed_links,
                               f"https://github.com/{ctx.readiness.repo}/pull/{number}")
        if isinstance(planned, str):
            raise _Refused(not_posted_line(number, planned))
        return planned, steps

    @staticmethod
    async def _threads_before(ctx: RepoContext, number: int, head: str) -> dict:
        """The threads command's snapshot of this PR at this head, right before the confirm run: when it fails,
        nothing is posted."""
        try:
            before = await ctx.readiness.threads(ctx.clone.path, number)
        except ReadinessError as e:
            raise _Refused(github_unreadable_line(number, f"the threads command failed: {e}")) from None
        if (other := _other_pr(before, number, head)) is not None:
            if _same_id(before.get("number"), number) and isinstance(before.get("head_sha"), str):
                raise _Refused(head_moved_line(head, before["head_sha"]))
            raise _Refused(not_posted_line(number, f"the threads command shows {other}"))
        return before

    async def _replies_verified(self, ctx: RepoContext, job: Job, post: Post, pr: PRFull, me: str,
                                numbers: tuple[int, ...], entries: list[dict], planned: dict[int, list[str]],
                                before: dict, left_out: dict[int, str], results: list[dict] | None,
                                result: CommandResult, unknown: str) -> PostOutcome:
        """The post-condition, on the threads command, read up to verify_reads times, verify_wait apart: each reply
        (its text, by me) new in its thread, each resolved thread gone, each review-body answer `answered`. A
        resolved thread is not listed, so its reply is the tool's report. Every step GitHub shows is recorded as
        landed. Once anything landed the post is partly posted, never "Not posted"; a step the tool reports that
        GitHub does not show, an entry left out that GitHub does not show posted, or a report of the tool's that is
        not known (`results` None: its output, `unknown`, was not understood, or the run timed out) leaves it
        unverified."""
        number = job.number
        by_n = {entry["n"]: entry for entry in entries}
        steps = [(n, kind) for n in sorted(planned) for kind in planned[n]]
        known = results is not None
        results = results or []
        reported = Counter((r["n"], _RESULT_STEP[key]) for r in results for key in r if key != "n")
        urls = {(r["n"], _RESULT_STEP[key]): r[key] for r in results for key in r if key in ("replied", "commented")}
        unexpected = sorted((reported - Counter(steps)).elements())
        seen: set[tuple[int, str]] = set()  # as the last good read shows them
        after, failure = before, ""
        for read in range(self._verify_reads):
            if read:
                await self._sleep(self._verify_wait)
            try:
                snapshot = await ctx.readiness.threads(ctx.clone.path, number)
            except ReadinessError as e:
                failure = str(e)
                break
            if (other := _other_pr(snapshot, number, job.head_sha)) is not None:
                failure = f"the threads command shows {other}"
                break
            after = snapshot
            seen = {step for step in steps if _seen(by_n[step[0]], step[1], before, after, me, reported)}
            if len(seen) == len(steps):
                break
        if failure and not seen:
            self._end(post, "unverified", error=failure)
            return PostOutcome("unverified", unverified_line(number, failure, pr.url))
        links = {n: urls.get((n, kind), pr.url) for n, kind in steps if (n, kind) in seen and kind != "resolve"}
        fields = {"landed": [{"n": n, "path": "", "line": None, "title": "", "where": kind}
                             for n, kind in steps if (n, kind) in seen],
                  "comments": len(links), "urls": list(links.values())}
        missing: dict[int, list[str]] = {}
        for n, kind in steps:
            if (n, kind) not in seen:
                missing.setdefault(n, []).append(kind)
        if not (failure or missing or unexpected or left_out):
            stamp = self._end(post, "posted", **fields)
            self._mark_posted(post, stamp)
            text = replies_posted_line(number, [_landed_text(n, by_n[n], planned[n], links) if n in planned
                                                else tr("{n}: already posted", n=n) for n in numbers])
            return PostOutcome("posted", text + _held_anyway(result, unknown))
        problems = [f"#{n}: " + ", ".join(_unseen_text(by_n[n], kind, after, known) for kind in kinds)
                    for n, kinds in missing.items()]
        problems += [f"the tool reports a {kind} for #{n} that was not planned" for n, kind in unexpected]
        problems += [*left_out.values(), *([failure] if failure else [])]
        why = "; ".join(problems)
        if result.timed_out or result.returncode != 0:
            why += f"; {_tool_failure(result)}"
        if unknown and not result.timed_out:  # a run cut short leaves no output to understand
            why += f"; {_not_understood(unknown)}"
        uncertain = bool(failure or unexpected or left_out or result.timed_out or not known
                         or any(reported[(n, kind)] for n, kinds in missing.items() for kind in kinds))
        if not seen:
            state = "unverified" if uncertain else "failed"
            self._end(post, state, error=why)
            text = not_shown_line(number, why, pr.url) if uncertain else not_posted_line(number, why)
            return PostOutcome(state, text)
        state = "unverified" if uncertain else "partial"
        stamp = self._end(post, state, error=why, **fields)
        if state == "partial":
            self._mark_posted(post, stamp)
        landed = {n for n, _ in seen}
        results_text = [_landed_text(n, by_n[n], [kind for kind in planned[n] if (n, kind) in seen], links)
                        if n in landed else tr("{n}: already posted", n=n)
                        for n in numbers if n in landed or (n not in planned and n not in left_out)]
        by_hand = [_by_hand_text(by_n[n], kinds, known) for n, kinds in missing.items() if n in landed]
        rest = [n for n in missing if n not in landed]
        retry = "post replies " + " ".join(map(str, rest)) if rest else ""
        return PostOutcome(state, replies_partial_line(number, why, results_text, by_hand, retry,
                                                       pr.url if uncertain else ""))

    async def push_fix(self, ctx: RepoContext, job: Job, *, anyway: bool = False,
                       then_post: bool = False) -> PostOutcome:
        """A fix job's patch (an address or a findings job's) committed on the PR head and pushed, under the posting
        lock; never raises but a cancellation, which leaves the Post `posting` for the restart's recover().
        `anyway`: the operator's "push anyway", which pushes a fix with protected files too; `then_post`: a post of
        the replies follows, so the answer names no "post replies" to send."""
        async with self._lock:
            try:
                return await self._push_fix(ctx, job, anyway, then_post)
            except _Refused as e:
                return PostOutcome("refused", str(e))
            except Exception as e:
                return self._internal_error(job, e)
            finally:
                self._post = None

    async def _push_fix(self, ctx: RepoContext, job: Job, anyway: bool, then_post: bool) -> PostOutcome:
        # 1. The job as it is now, with a patch Cleave may push and no push of it landed; no ticket workspace
        # holding the PR (the ticket pipeline force-pushes its branch), read from the board before GitHub is.
        job = self._current(job.id)
        number = job.number
        run = self._store.run(job.id)
        if job.kind not in FIX_KINDS or not run.fix:
            raise _Refused(tr("This job changed no code: nothing to push."))
        if run.fix.get("protected") and not anyway:
            raise _Refused(tr('The fix touches protected files ({paths}): Cleave pushes it only on "push anyway". '
                              "Nothing was done.", paths=", ".join(run.fix["protected"])))
        if (landed := self._store.push_of(job)) is not None:
            raise _Refused(tr("Already pushed as {sha}. Nothing was done.", sha=landed.sha[:9]))
        row = self._store.board(job.scope).get(number)
        if row is not None and row.pipeline:
            raise _Refused(tr("A ticket of the pipeline still holds #{number}'s branch and may overwrite it: nothing "
                              'pushed. Reply "push" again when the ticket is done.', number=number))
        patch = Path(run.job_dir) / PATCH_NAME
        if not run.job_dir or patch.is_symlink() or not patch.is_file():
            raise _Refused(not_pushed_line(number, N_("the patch is not in this job's copy")))
        # 2. GitHub: open, at the job's head, its branch in this repo, plain and not the base branch.
        pr = await self._read(number, ctx.forge.pr_full(number))
        if pr.state != "open":
            raise _Refused(pr_closed_line(number))
        if pr.head_sha != job.head_sha:
            raise _Refused(tr('Head moved ({old} → {new}): nothing pushed. Reply "again" for a new run on the new '
                              "head.", old=job.head_sha[:9], new=pr.head_sha[:9]))
        if pr.head_repo.lower() != ctx.readiness.repo.lower():  # GitHub's owner and repo names ignore case
            raise _Refused(tr("The PR's branch is in a fork ({repo}): Cleave does not push there. Nothing was done.",
                              repo=pr.head_repo[:_MAX_PATH] or "?"))
        branch = pr.head_ref
        if branch == ctx.cfg.base_branch or not _PLAIN_BRANCH.fullmatch(branch) or branch.startswith("-"):
            raise _Refused(tr("Cleave does not push to the branch {branch}. Nothing was done.",
                              branch=branch[:_MAX_PATH]))
        what = "address review comments" if job.kind == "address" else "self-review findings"
        message = f"fix({run.ticket_key}): {what} on #{number}" if run.ticket_key else f"fix: {what} on #{number}"
        # 3-4 in one lease of the review clone: the post recorded as under way, then the commit and its push.
        async with AsyncExitStack() as lease:
            try:
                await lease.enter_async_context(ctx.clone.reader(wait=self._reader_wait))
            except CloneError as e:
                raise _Refused(not_pushed_line(number, str(e))) from None
            self._current(job.id)
            post = self._start(job, ReplyCommand("push", event=""), "push")
            root = self._work_dir / "push" / post.id
            try:
                sha = await ctx.clone.push_patch(number=number, head=job.head_sha, branch=branch, patch=patch,
                                                 message=message, author=ctx.commit_author, worktree=root / "wt")
            except CloneError as e:
                self._end(post, "failed", error=str(e))
                return PostOutcome("failed", not_pushed_line(number, str(e)))
            finally:
                await asyncio.to_thread(shutil.rmtree, root, True)
        # 5. GitHub must show the new commit as the head; 6. then the job moves to it, before its post ends: a poll
        # leaves the address jobs of a PR with a Post `posting` alone, so none sees the new head with the job left
        # behind.
        if not await self._head_is(ctx, number, sha):
            self._end(post, "unverified", sha=sha, error=f"GitHub does not show {sha[:9]} as the head")
            return PostOutcome("unverified", tr(
                "The push to #{number} ran, but GitHub does not show {sha} as the head. Check the PR's commits before "
                "pushing again: {url}", number=number, sha=sha[:9], url=pr.url))
        stamp = self._stamp()
        self._store.rehead_job(job.id, sha, f"{sha}:{job.key.partition(':')[2]}", stamp)
        url = f"https://github.com/{ctx.readiness.repo}/commit/{sha}"
        self._end(post, "posted", sha=sha, urls=[url])
        # A findings job has no replies: no "post replies" hint follows its push.
        posted = self._store.posted_replies(job)
        fixed = [e["n"] for e in run.replies if e.get("fixed") is True and e["n"] not in posted]
        return PostOutcome("posted", pushed_line(number, sha, url, [] if then_post else fixed))

    async def _head_is(self, ctx: RepoContext, number: int, sha: str) -> bool:
        """Whether GitHub shows `sha` as the PR's head, read up to verify_reads times, verify_wait apart (its PR
        head follows a push with a lag); a failed read is one of them."""
        for read in range(self._verify_reads):
            if read:
                await self._sleep(self._verify_wait)
            try:
                if (await ctx.forge.pr_full(number)).head_sha == sha:
                    return True
            except ForgeError:
                continue
        return False

    async def _pr_at_head(self, ctx: RepoContext, number: int, head: str) -> PRFull:
        pr = await self._read(number, ctx.forge.pr_full(number))
        if pr.state != "open":
            raise _Refused(pr_closed_line(number))
        if pr.head_sha != head:
            raise _Refused(head_moved_line(head, pr.head_sha))
        return pr

    @staticmethod
    async def _read(number: int, read: Awaitable[_T]) -> _T:
        """A GitHub read before the confirm run (or the approve): when it fails, nothing is posted."""
        try:
            return await read
        except ForgeError as e:
            raise _Refused(github_unreadable_line(number, str(e))) from None

    def _current(self, job_id: str) -> Job:
        """The job as the store has it now; posting needs it done or posted (not superseded, not stale)."""
        job = self._store.job(job_id)
        if job.state not in POSTABLE_STATES:
            raise _Refused(outdated_line(job.state, job.reason))
        return job

    def _mark_posted(self, post: Post, stamp: str) -> None:
        """The job `posted` after its `post` landed, while it is still done or posted, or stale because its turn
        went once the post had started (the post itself takes the turn away; a poll leaves a job with a Post
        `posting` alone, so this is a second guard). One superseded since (a new head polled during the
        post-condition, say), or stale otherwise, keeps that state."""
        job = self._store.job(post.job_id)
        turn_gone_since = (job.state, job.reason) == ("stale", "turn gone") and (
            _time(job.updated_at) >= _time(post.created_at))
        if job.state in POSTABLE_STATES or turn_gone_since:
            self._store.set_job_state(job.id, "posted", "", stamp)
        else:
            logger.info("Job %s is %s now: left as it is after the post", job.id, job.state)

    def _refuse_changed(self, job: Job, text: str, items: dict[int, ReportItem], numbers: tuple[int, ...]) -> None:
        """Refuse when an edit landed after step 1 read and checked the report: a named item in the copy's base
        report `text` is not the one checked. The drafted numbers are read now: a draft that landed adds an item,
        an unnamed one."""
        drafted = frozenset(self._store.run(job.id).drafted)
        try:
            now = {item.number: item for item in parse_report(text, job.head_sha, drafted).items}
        except ReportError as e:
            raise _Refused(not_posted_line(job.number, f"the report is unreadable: {e}")) from None
        if any(now.get(n) != items[n] for n in numbers):
            raise _Refused(tr(_REPORT_CHANGED))

    def _refuse_second_approve(self, job: Job, me: str, cmd: ReplyCommand, reviews: list[ForgeReview]) -> None:
        """An APPROVE at a head already approved: by a review of mine GitHub lists there (Cleave's own included),
        or by an approve Cleave recorded as posted there; "anyway" never overrides that. Unless the reply says
        "anyway", also at a head where an approving Post of Cleave's ended unknown (unverified or interrupted): it
        may have landed, but a truly failed one can be retried."""
        if cmd.event != "APPROVE":
            return
        head = job.head_sha
        posts = [p for p in self._store.posts(job.scope, job.number) if p.head_sha == head and p.event == "APPROVE"]
        if any(_same_login(r.login, me) and r.state == "APPROVED" and r.commit_id == head for r in reviews) or any(
                p.kind == "approve" and p.state == "posted" for p in posts):
            raise _Refused(already_approved_line(job.number, head))
        if not cmd.anyway and any(p.state in ("unverified", "interrupted") for p in posts):
            raise _Refused(approve_unknown_line(job.number, head))

    def _reviewed_since(self, job: Job, me: str, started_at: str, reviews: list[ForgeReview]) -> ForgeReview | None:
        """A review the operator submitted after the run started that is not one of Cleave's posts."""
        started = _time(started_at)
        cleave = self._store.cleave_review_ids(job.scope, job.number)
        return next((r for r in reviews if _same_login(r.login, me) and r.state != "PENDING"
                     and r.id not in cleave and _time(r.submitted_at) > started), None)

    def _start(self, job: Job, cmd: ReplyCommand, kind: str) -> Post:
        stamp = self._stamp()
        post = Post(uuid4().hex[:12], job.id, job.scope, job.number, job.head_sha, kind, "posting", stamp, stamp,
                    numbers=list(cmd.numbers), event=cmd.event)
        self._store.save_post(post)
        self._post = post
        return post

    def _end(self, post: Post, state: str, **fields) -> str:
        """The Post saved in its final state; the time it ended."""
        stamp = self._stamp()
        ended = dataclasses.replace(post, state=state, updated_at=stamp, **fields)
        self._store.save_post(ended)
        self._post = ended
        return stamp

    def _internal_error(self, job: Job, e: Exception) -> PostOutcome:
        """Before the confirm run (or the approve) nothing was posted: "failed". Once it ran, it may have landed:
        the Post, if still `posting`, ends unverified (a final state it reached stays), and the answer says to
        check the PR."""
        name = type(e).__name__
        logger.warning("Posting on %s #%d stopped: %s", job.scope, job.number, _where(e))
        post = self._post
        if post is None:
            return PostOutcome("failed", tr(
                "Posting stopped on an internal error ({name}); check the PR before posting again.", name=name))
        if post.state == "posting":
            try:
                self._end(post, "unverified", error=f"internal error ({name})")
            except Exception as again:
                logger.warning("Post %s not marked unverified: %s", post.id, _where(again))
        return PostOutcome("unverified", internal_error_line(job.number, name))

    def _stamp(self) -> str:
        return self._now().isoformat()


def _landed(items: dict[int, ReportItem], plan: DryRunPlan, numbers: list[int]) -> list[dict]:
    """The landed numbers as the report has them (a moved anchor keeps its report line), and where each went."""
    return [{"n": n, "path": items[n].path, "line": items[n].line, "title": items[n].title,
             "where": "moved" if n in plan.moved else "inline" if n in plan.inline else "body"} for n in numbers]


def _make_dirs(path: Path) -> None:
    """`path` and its missing parents, each made with mode 0700."""
    missing = []
    while not path.is_dir():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def _prepared_threads(job_dir: str, number: int) -> dict:
    """Prepare's threads snapshot, which the runner keeps in the job copy before the session."""
    path = Path(job_dir) / _PREPARED_THREADS
    if not job_dir or path.is_symlink() or not path.is_file():
        raise _Refused(tr(_NO_SNAPSHOT))
    try:
        return parse_threads_snapshot(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ReadinessError):
        raise _Refused(not_posted_line(number, N_("Prepare's threads snapshot in this job's copy is unreadable"))) from None


def _write_plan(job_dir: Path, head9: str, entries: list[dict], number: int,
                fix_shas: dict[int, str] | None = None) -> Path:
    """`job_dir/posting/replies_<head9>.json`: the named entries with their known keys only, and `fix_sha` from
    Cleave (`fix_shas`, by number) on an address job's replies that report a fix. The replies tool keeps its own
    ledger beside it."""
    posting = job_dir / "posting"
    if posting.is_symlink() or (posting.exists() and not posting.is_dir()):
        raise _Refused(not_posted_line(number, N_("the job copy's posting directory is not Cleave's")))
    _make_dirs(posting)
    path = posting / f"replies_{head9}.json"
    fix_shas = fix_shas or {}
    plan = [{**{key: entry[key] for key in _PLAN_KEYS if key in entry},
             **({"fix_sha": fix_shas[entry["n"]]} if entry["n"] in fix_shas else {})} for entry in entries]
    write_atomic(path, json.dumps(plan, indent=2) + "\n")
    return path


def _wanted_steps(entry: dict) -> list[str]:
    if "thread_id" in entry:
        return ["reply", "resolve"] if entry.get("resolve") is True else ["reply"]
    return ["comment"]


def _shown_text(kind: str, entry: dict, threads: dict, pr_url: str) -> str:
    """The text the replies tool posts for an entry's reply or comment: a comment first names the review body it
    answers, `Re: [review by <author>](<PR url>#pullrequestreview-<id>)` and a blank line, its author as Prepare's
    snapshot (`threads`) gives it."""
    if kind != "comment":
        return entry["body"]
    review = _find(_listed(threads, "review_bodies"), entry["review_id"]) or {}
    link = f"{pr_url}#pullrequestreview-{entry['review_id']}"
    return f"Re: [review by {review.get('author')}]({link})\n\n{entry['body']}"


def _allowed_steps(entry: dict) -> list[list[str]]:
    """The dry run's steps for an entry, sorted: what it wants, or a resolve alone (its reply already in)."""
    wanted = _wanted_steps(entry)
    return [sorted(wanted), ["resolve"]] if "resolve" in wanted else [wanted]


def _aims_at(target: str, entry: dict, threads: dict) -> bool:
    """Whether a step's `target` ("path:line @author" or "review <id> @author") names the entry's review body, or
    its thread by id or by the exact `path:line` Prepare's snapshot (`threads`) gives it."""
    where = target.rpartition(" @")[0] or target
    if "review_id" in entry:
        return where == f"review {entry['review_id']}"
    if where == entry["thread_id"]:
        return True
    thread = _find(_listed(threads, "threads"), entry["thread_id"]) or {}
    path, line = thread.get("path"), thread.get("line")
    return isinstance(path, str) and path != "" and type(line) is int and where == f"{path}:{line}"


def _other_pr(snapshot: dict, number: int, head: str) -> str | None:
    """None when a threads snapshot is of PR `number` at `head`, else what it is of."""
    shown, at = snapshot.get("number"), snapshot.get("head_sha")
    if _same_id(shown, number) and at == head:
        return None
    return f"#{_shown(shown)} at {_shown(at)[:9]}"


def _comment_ids(snapshot: dict, thread_id: object) -> set:
    """The ids of the comments the snapshot lists in a thread."""
    return {c.get("id") for c in _listed(_find(_listed(snapshot, "threads"), thread_id) or {}, "comments")}


def _my_reply_in(thread: dict, entry: dict, me: str, old: set) -> bool:
    """Whether the thread holds a comment of mine, not among `old`, with the entry's text."""
    return any(type(c.get("id")) is int and c["id"] not in old and _is_me(c.get("author"), me)
               and isinstance(c.get("body"), str) and _words(c["body"]) == _words(entry["body"])
               for c in _listed(thread, "comments"))


def _left_out_unseen(entry: dict, before: dict, prepared: dict, me: str) -> str | None:
    """None when GitHub agrees that an entry the dry run leaves out is posted: its review body answered, or its
    thread gone, no longer my turn or holding my reply (a comment of mine with its text that Prepare's snapshot
    does not list: short replies repeat across rounds); else what it shows."""
    if "review_id" in entry:
        body = _find(_listed(before, "review_bodies"), entry["review_id"])
        if body is not None and body.get("answered") is True:
            return None
        return f"#{entry['n']}: left out as already posted, but review {_shown(entry['review_id'])} is not answered"
    thread = _find(_listed(before, "threads"), entry["thread_id"])
    if thread is None or not _is_me(thread.get("turn"), me) or _my_reply_in(
            thread, entry, me, _comment_ids(prepared, entry["thread_id"])):
        return None
    return f"#{entry['n']}: left out as already posted, but thread {_shown(entry['thread_id'])} is still your turn"


def _not_postable_now(entry: dict, kinds: list[str], before: dict, prepared: dict, me: str,
                      number: int) -> str | None:
    """Why GitHub, right before the confirm run, no longer allows an entry's planned steps (the refusal), or
    None. A reply already in its thread (a comment of mine with its text that Prepare's snapshot does not list)
    is never posted again."""
    n = entry["n"]
    if "thread_id" in entry:
        thread_id = _shown(entry["thread_id"])
        thread = _find(_listed(before, "threads"), entry["thread_id"])
        if thread is not None and _my_reply_in(thread, entry, me, _comment_ids(prepared, entry["thread_id"])):
            if "resolve" in _wanted_steps(entry):
                return f"#{n}: your reply is in thread {thread_id}; resolve it on GitHub yourself. Nothing was done."
            return f"#{n}: your reply is in thread {thread_id} already. Nothing was done."
        if kinds == ["resolve"]:  # a resolve without its reply
            return not_posted_line(number, f"the dry run plans resolve for #{n}")
    why = validate_plan([entry], before, me)[1].get(n)
    return None if why is None else f"#{n} cannot be posted: {why}. Nothing was done."


def _seen(entry: dict, kind: str, before: dict, after: dict, me: str, reported: Counter) -> bool:
    """Whether the threads snapshot `after` shows the entry's step `kind` done."""
    if kind == "comment":
        body = _find(_listed(after, "review_bodies"), entry["review_id"])
        return body is not None and body.get("answered") is True
    thread = _find(_listed(after, "threads"), entry["thread_id"])
    if kind == "resolve":
        return thread is None
    if thread is None:  # resolved: its reply is the tool's report
        return "resolve" in _wanted_steps(entry) and reported[(entry["n"], "reply")] > 0
    return _my_reply_in(thread, entry, me, _comment_ids(before, entry["thread_id"]))


def _landed_text(n: int, entry: dict, kinds: list[str], links: dict[int, str]) -> str:
    """What landed of an entry: `kinds`, its steps GitHub shows (a Telegram line)."""
    if "thread_id" not in entry:
        return tr("{n}: answered {url}", n=n, url=links[n])
    if "reply" not in kinds:
        return tr("{n}: resolved", n=n)
    return tr("{n}: replied {url}", n=n, url=links[n]) + (tr(_RESOLVED) if "resolve" in kinds else "")


def _by_hand_text(entry: dict, missing: list[str], known: bool) -> str:
    """What only the operator can finish of an entry that partly landed (one of its two steps). `known`: whether
    the tool's report was read. A Telegram line."""
    thread_id = _shown(entry["thread_id"])
    if missing == ["resolve"]:
        return tr("#{n}: your reply is in thread {thread}; resolve it on GitHub yourself.", n=entry["n"],
                  thread=thread_id)
    if known:
        return tr("#{n}: thread {thread} was resolved, but the tool reports no reply in it; reply on GitHub yourself"
                  " if it is missing.", n=entry["n"], thread=thread_id)
    return tr("#{n}: thread {thread} was resolved, but the reply in it is not seen; reply on GitHub yourself if it"
              " is missing.", n=entry["n"], thread=thread_id)


def _unseen_text(entry: dict, kind: str, after: dict, known: bool) -> str:
    if kind == "comment":
        return f"review {_shown(entry['review_id'])} is not answered"
    thread_id = _shown(entry["thread_id"])
    if kind == "resolve":
        return f"thread {thread_id} is still unresolved"
    if _find(_listed(after, "threads"), entry["thread_id"]) is not None:
        return f"no new reply of yours in thread {thread_id}"
    if "resolve" in _wanted_steps(entry):
        unseen = "the tool reports no reply" if known else "the reply is not seen"
        return f"{unseen} (a resolved thread's replies are not listed)"
    return f"thread {thread_id} is not listed any more"


def _listed(snapshot: dict, key: str) -> list[dict]:
    """The objects in the snapshot's list `key`: the tool's output is not trusted to be well formed."""
    items = snapshot.get(key)
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _find(items: list[dict], item_id: object) -> dict | None:
    return next((item for item in items if _same_id(item.get("id"), item_id)), None)


def _same_id(a: object, b: object) -> bool:
    return a is not None and type(a) is type(b) and a == b  # JSON 101.0 or true is not the id 101


def _first_comment_id(thread: dict) -> object:
    comments = thread.get("comments")
    if isinstance(comments, list) and comments and isinstance(comments[0], dict):
        return comments[0].get("id")
    return None


def _is_me(login: object, me: str) -> bool:
    return isinstance(login, str) and _same_login(login, me)


def _shown(value: object) -> str:
    return str(value)[:60]  # a plan's id is model output


def _body_parts(body: str) -> tuple[str, str]:
    """A payload body's Summary and its findings: what comes before and after its last findings heading."""
    summary, heading, findings = body.rpartition(_FINDINGS)
    return (summary, findings) if heading else (body, "")


def _item_words(item: ReportItem) -> str:
    """The item's own text as the posting tool posts it, as `_words`: every line that starts (after its indent)
    with `**File:**` or `**Ticket:**` left out (spec: a Ticket line is never posted)."""
    return _words("\n".join(line for line in item.body.split("\n")
                            if not line.lstrip().startswith(("**File:**", "**Ticket:**"))))


def _words(text: str) -> str:
    return " ".join(text.split())  # the tool may wrap or space the text differently


def _numbered(numbers: list[int]) -> str:
    return "#" + " ".join(map(str, numbers)) if numbers else "nothing"


def _place(comment: dict) -> str:
    return f"{comment['path'][:_MAX_PATH]}:{comment['line']}"


def _tool_failure(result: CommandResult) -> str:
    if result.timed_out:
        return "the tool timed out"
    tail = stderr_tail(result.stderr)
    return f"the tool exited {result.returncode}" + (f": {tail}" if tail else "")


def _dry_run_failure(result: CommandResult) -> str:
    if result.timed_out:
        return f"timed out after {int(result.timeout)} s"
    return stderr_tail(result.stderr) or f"exit {result.returncode}"


def _not_understood(unknown: str) -> str:
    return f"the tool's output was not understood ({unknown})"


def _held_anyway(result: CommandResult, unknown: str) -> str:
    """The lines a post GitHub shows in full adds about a confirm run that did not end well."""
    text = ""
    if result.returncode != 0:
        text += "\n" + tr("The tool exited {code}, but GitHub shows everything posted.", code=result.returncode)
    if unknown and not result.timed_out:
        text += "\n" + tr("The tool's output was not understood ({unknown}), but GitHub shows everything posted.",
                          unknown=unknown)
    return text


def _nothing_left(number: int, head: str, cmd: ReplyCommand) -> str:
    """Nothing of the command is left to post at the head; an event-only APPROVE (step 3 found no approval
    there) can still go without a body."""
    text = nothing_left_line(number, head)
    if cmd.event == "APPROVE" and not cmd.numbers:
        text += " " + tr('Reply "approve no summary" to approve without a body.')
    return text


def _same_login(login: str, me: str) -> bool:
    return login.lower() == me.lower()  # GitHub logins are case-insensitive


def _time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))  # GitHub's Z form; 3.10 reads no Z


def _body(text: str) -> str:
    return text.replace("\r\n", "\n").strip()

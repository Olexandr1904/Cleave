"""Prepare: Cleave's own code, not the model, gets a PR ready for the review session — the clone at the
base, the PR head in a worktree, the run directory's artifacts, the repo's offline steps, the drift alarm."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from config.schemas import JOB_PATH_PLACEHOLDERS, PREPARE_STEP_PLACEHOLDERS, ProcessPartConfig
from orchestrator.reviewer.board import TICKET_KEY
from orchestrator.reviewer.clone import CloneError, ReviewClone
from orchestrator.reviewer.commands import (
    NO_HOST_GIT_ENV, TEMPLATE_ERRORS, base_env, expand, placeholder_values, run_command, stderr_tail,
)
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.markdown import fenced
from orchestrator.reviewer.models import Job, PRComments, PRFull
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore

_STEP_TIMEOUT = 300
_TOKEN_FAILING = frozenset({"GitHub 401", "GitHub 403"})
_FORGE_ERROR_LIMIT = 200
_HEADING = re.compile(r"(#{1,6})(?:[ \t]+(.*))?$")
_CLOSING = re.compile(r"(?:^|[ \t]+)#+$")


class PrepareError(Exception):
    """Why a job cannot start; outcome is "failed", "superseded", "cancelled", "requeue" or "unavailable"
    (GitHub is, so the job waits and tries again). drift: the preparation section changed in this run, so its
    line is due even though the job does not start."""

    def __init__(self, outcome: str, reason: str, drift: bool = False) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.drift = drift


@dataclass(frozen=True)
class JobPaths:
    run_dir: Path
    worktree: Path
    report: Path
    replies_plan: Path
    job_dir: Path


@dataclass(frozen=True)
class Prepared:
    pr: PRFull
    drift: bool
    clone: ReviewClone  # the job clone the session runs in


def job_paths(
    clone_dir: Path, cfg: ProcessPartConfig, number: int, head: str, jobs_dir: Path, job_id: str,
) -> JobPaths:
    """The job's paths, clone_dir being its job clone: the worktree must lie inside the job's own directory
    (clone_dir's parent), which the runner removes when the job ends."""
    root = clone_dir.resolve()
    values = placeholder_values(JOB_PATH_PLACEHOLDERS, pr=str(number), head9=head[:9])

    def resolved(name: str, template: str) -> Path:
        try:
            value = expand([template], values)[0]
        except TEMPLATE_ERRORS as e:
            raise PrepareError("failed", f"{name}: {e}") from None
        return (root / value).resolve()

    run_dir = resolved("run_dir", cfg.run_dir)
    replies_plan = resolved("replies_plan", cfg.replies_plan)
    worktree = resolved("worktree_dir", cfg.worktree_dir)
    _check_inside(root, run_dir, replies_plan)
    if root.parent not in worktree.parents:
        raise PrepareError("failed", "worktree_dir is outside the job's directory")
    return JobPaths(
        run_dir=run_dir,
        worktree=worktree,
        report=run_dir / f"report_{head[:9]}.md",
        replies_plan=replies_plan,
        job_dir=jobs_dir / job_id,
    )


def _check_inside(root: Path, run_dir: Path, replies_plan: Path) -> None:
    """Prepare empties run_dir and writes into the plan's directory: both strictly inside the clone
    (so run_dir is never the clone itself), with no symlink on the way."""
    for name, path, directory in (("run_dir", run_dir, run_dir),
                                  ("replies_plan", replies_plan, replies_plan.parent)):
        if root not in path.parents or directory.resolve() != directory:
            raise PrepareError("failed", f"{name} is outside the review clone")


def section_text(doc: str, heading: str) -> str | None:
    """The section whose heading line's text is `heading`, from that line through the line before the
    next heading of the same or a higher level; None when absent. A line in a fenced code block (a shell
    comment, say) is never a heading."""
    lines = doc.splitlines(keepends=True)
    in_fence = fenced([line.rstrip("\r\n") for line in lines])
    start, level = None, 0
    for i, line in enumerate(lines):
        if in_fence[i] or not (m := _HEADING.match(line.rstrip("\r\n"))):
            continue
        if start is not None and len(m.group(1)) <= level:
            return "".join(lines[start:i])
        if start is None and _CLOSING.sub("", (m.group(2) or "").strip()).strip() == heading:
            start, level = i, len(m.group(1))
    return None if start is None else "".join(lines[start:])


def section_hash(text: str) -> str:
    return hashlib.sha256("\n".join(line.rstrip() for line in text.splitlines()).encode()).hexdigest()


def meta_env(head: str, base: str, files: int, changed: int, merge_base: str, worktree: Path) -> str:
    return f"HEAD={head}\nBASE={base}\nFILES={files}\nCHANGED={changed}\nMB={merge_base}\nWT={worktree}\n"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def existing_comments_md(c: PRComments) -> str:
    lines = []
    for x in c.inline:
        where = x["path"] if x["line"] is None else f'{x["path"]}:{x["line"]}'
        lines.append(f'- {x["login"]} `{where}` [id {x["id"]}]: {_one_line(x["body"])}')
    lines += [f'- review by {r["login"]} [{r["state"]}]: {_one_line(r["body"])}' for r in c.reviews]
    lines += [f'- conversation, {x["login"]}: {_one_line(x["body"])}' for x in c.conversation]
    return "".join(f"{line}\n" for line in lines) or "(none)\n"


async def ticket_json(tracker, key: str) -> dict:
    """The ticket for ticket.json, or {"error": ...}: a failure is never fatal, and its text (not ours,
    it may hold a secret) is never written."""
    if not key:
        return {"error": "no ticket key in the PR title"}
    if tracker is None:
        return {"error": "no tracker for this project"}
    try:
        ticket = await tracker.get_ticket(key)
        comments = await tracker.get_comments(key)
    except Exception as e:
        return {"error": f"tracker read failed: {type(e).__name__}"}
    return {
        "summary": ticket.summary,
        "description": ticket.description,
        "comments": [{"author": c.author, "created": c.created, "body": c.body} for c in comments],
    }


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def prepare(
    ctx: RepoContext, job: Job, paths: JobPaths, *, store: ReviewerStore, tracker, now: str, job_clone_dir: Path,
) -> Prepared:
    """Get the job's PR ready for the review session in a job clone of its own at job_clone_dir (paths are that
    clone's). It holds ctx.clone.lock only for the review clone's own git: refresh, the PR head, the drift check
    and the job clone. A job that cannot start raises PrepareError with its outcome."""
    try:
        return await _prepare(ctx, job, paths, store, tracker, now, job_clone_dir)
    except (ForgeError, CloneError, ReadinessError) as e:
        raise _prepare_error(e) from None


def _prepare_error(e: Exception, drift: bool = False) -> PrepareError:
    if isinstance(e, ForgeError):
        if str(e) in _TOKEN_FAILING:
            return PrepareError("requeue", "forge token failing", drift)
        if e.unavailable:
            return PrepareError("unavailable", "GitHub unavailable", drift)
        return PrepareError("failed", str(e)[:_FORGE_ERROR_LIMIT], drift)
    return PrepareError("failed", str(e), drift)  # a CloneError's or ReadinessError's text is already redacted


def _drift(ctx: RepoContext, store: ReviewerStore, now: str) -> bool:
    """Whether the preparation section changed since the last job's (its new hash stored); read from the review
    clone under its lock, so parallel Prepares of one repo report a change once."""
    process = ctx.cfg.process
    heading = process.prepare_section
    section = section_text((ctx.clone.path / process.doc).read_text(encoding="utf-8"), heading)
    if section is None:
        raise PrepareError("failed", f'section "{heading}" not found in {process.doc}')
    new, old = section_hash(section), store.prepare_hash(ctx.scope)
    if new != old:
        store.set_prepare_hash(ctx.scope, new, now)
    return bool(old) and new != old


async def _prepare(
    ctx: RepoContext, job: Job, paths: JobPaths, store: ReviewerStore, tracker, now: str, job_clone_dir: Path,
) -> Prepared:
    process = ctx.cfg.process
    doc, base, n, head = process.doc, ctx.cfg.base_branch, job.number, job.head_sha

    pr = await ctx.forge.pr_full(n)
    if pr.state != "open":
        raise PrepareError("cancelled", "PR closed")
    if pr.head_sha != head:
        raise PrepareError("superseded", "head moved")
    if pr.base != base:  # the clone has the base branch only: MB and the diff would be wrong
        raise PrepareError("failed", f"PR base is {pr.base}, not {base}")
    async with ctx.clone.lock:
        await ctx.clone.refresh()
        if not ctx.clone.has_file(doc):
            raise PrepareError("failed", f"{doc} not on {base}")
        if await ctx.clone.fetch_pr_head(n) != head:
            raise PrepareError("superseded", "head moved")
        # The drift check comes before everything that may fail: a changed section is reported even then.
        drift = _drift(ctx, store, now)
        try:
            # The readiness command needs the forge token: it runs from the review clone, never a job clone.
            threads = await ctx.readiness.threads(ctx.clone.path, n)
            if threads["head_sha"] != head:
                raise PrepareError("superseded", "head moved", drift)
            clone = await ctx.clone.make_job_clone(job_clone_dir, n)
        except (ForgeError, CloneError, ReadinessError) as e:
            raise _prepare_error(e, drift) from None
    try:
        return await _prepare_job_clone(ctx, clone, pr, paths, tracker, drift, threads)
    except (ForgeError, CloneError, ReadinessError) as e:
        raise _prepare_error(e, drift) from None


async def _prepare_job_clone(
    ctx: RepoContext, clone: ReviewClone, pr: PRFull, paths: JobPaths, tracker, drift: bool, threads: dict,
) -> Prepared:
    """The rest of Prepare, in the job clone, the review clone's lock not held."""
    process, base, n, head = ctx.cfg.process, ctx.cfg.base_branch, pr.number, pr.head_sha
    await clone.detach_at_base()
    # Again on the job clone's checkout: the base branch may since have put a symlink on the way.
    _check_inside(clone.path.resolve(), paths.run_dir, paths.replies_plan)
    merge_base = await clone.merge_base(head)

    # Emptied: a run directory the base branch tracks would otherwise be archived with this job
    # (the job keeps its own copies in job_dir).
    if paths.run_dir.exists():
        shutil.rmtree(paths.run_dir)
    paths.run_dir.mkdir(parents=True)
    paths.replies_plan.parent.mkdir(parents=True, exist_ok=True)
    paths.replies_plan.unlink(missing_ok=True)
    await clone.add_worktree(paths.worktree, head)

    run = paths.run_dir
    changed = pr.additions + pr.deletions
    (run / "meta.env").write_text(
        meta_env(head, base, pr.changed_files, changed, merge_base, paths.worktree), encoding="utf-8")
    (run / "pr_body.md").write_text(f"{pr.title}\n\n{pr.body}\n", encoding="utf-8")
    (run / "existing_comments.md").write_text(existing_comments_md(await ctx.forge.comments(n)), encoding="utf-8")
    _write_json(run / "threads.json", threads)
    key = m.group(0) if (m := TICKET_KEY.search(pr.title)) else ""
    _write_json(run / "ticket.json", await ticket_json(tracker, key))

    values = placeholder_values(PREPARE_STEP_PLACEHOLDERS, pr=str(n), worktree=str(paths.worktree),
                                merge_base=merge_base, head9=head[:9], repo=ctx.readiness.repo)
    for i, step in enumerate(process.prepare_steps, 1):
        try:
            argv = expand(step, values)
        except TEMPLATE_ERRORS as e:
            raise PrepareError("failed", f"prepare step {i}: {e}", drift) from None
        # No token. The steps are offline and run from the job clone's base checkout; a step's own git reads
        # no host config.
        env = {**base_env(), **NO_HOST_GIT_ENV}
        result = await run_command(argv, cwd=clone.path, env=env, timeout=_STEP_TIMEOUT)
        if result.timed_out:
            raise PrepareError("failed", f"prepare step {i} failed: timed out after {_STEP_TIMEOUT} s", drift)
        if result.returncode != 0:
            tail = stderr_tail(ctx.clone.redact(result.stderr))  # the review clone's redact knows the token
            raise PrepareError("failed", f"prepare step {i} failed: {tail}", drift)
    return Prepared(pr, drift, clone)

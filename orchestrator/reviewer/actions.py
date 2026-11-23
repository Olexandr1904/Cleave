"""ReviewerActions: a command on a reviewer job checked against the job as the store has it now, then run.

Telegram's replies (ReviewerReplies) and the dashboard call the same layer. It returns what it did and the texts that
say so, in the caller's language (tr() reads the current one), and never sends anything itself. A post or a push goes
through the one Posting both channels share, the only module that posts.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.reviewer.board import FIX_KINDS
from orchestrator.reviewer.fix import PATCH_NAME
from orchestrator.reviewer.grammar import ReplyCommand, check_command, describe
from orchestrator.reviewer.lines import github_note, not_understood_line, outdated_line
from orchestrator.reviewer.messages import MESSAGE_LIMIT, split_message
from orchestrator.reviewer.models import POSTABLE_STATES, Job, JobRun
from orchestrator.reviewer.posting import Posting
from orchestrator.reviewer.reply_llm import ReplyParser, reply_context
from orchestrator.reviewer.report import Report
from orchestrator.reviewer.report_edit import item_text
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.small_jobs import SmallJobRunner
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import N_, model_text, tr

_OUTDATED = ("superseded", "stale")  # the job states "review again" and "skip" refuse
_PR_ACTIONS = ("review_again", "skip")  # the commands about the PR's head, not the report
_POSTS = ("post", "post_replies", "push", "push_post")  # the commands that go through Posting
_MAX_PARTS = 3  # of one answer: an item's text, a posting result
# Answers, translated where they are sent (tr).
_GONE = N_("This repo is no longer reviewed; nothing was done.")
_NO_LOGIN = N_("claude is not logged in on the host: nothing was started.")
_NO_LOGIN_PARSER = N_("claude is not logged in on the host: only exact replies work now; nothing was done.")
_FORGE_FAILING = N_("The forge token is failing (see the dashboard): nothing posted.")

LONG_ACTIONS = ("post", "post_replies", "push", "push_post", "text")  # Telegram runs these in a tracked task


@dataclass(frozen=True)
class ActionResult:
    state: str  # "refused", "shown", "queued" (a review, a fix or an edit queued, a skip recorded), or the PostOutcome
                # state ("posted", "partial", "refused", "failed", "unverified", "nothing"); push_post: the post's when
                # it ran
    texts: list[str]  # what was said, in the current language, in order
    command: ReplyCommand | None = None  # the checked command; None when refused before it was checked


class ReviewerActions:
    """Checks and runs the commands on reviewer jobs; the replies' and the dashboard's one implementation."""

    def __init__(
        self,
        contexts: list[RepoContext],
        store: ReviewerStore,
        service: ReviewerService | None,
        posting: Posting,
        small: SmallJobRunner | None,
        parser: ReplyParser | None,
        *,
        credential_failing: Callable[[str], bool],
    ) -> None:
        self._contexts = {ctx.scope: ctx for ctx in contexts}
        self._store = store
        self._service = service
        self.posting = posting  # the one Posting both channels use: one post or push at a time
        self._small = small
        self._parser = parser
        self._credential_failing = credential_failing

    def check(self, job: Job, cmd: ReplyCommand) -> ReplyCommand | str:
        """The command as it runs against the job as the store has it now ("all confirmed" expanded), or why nothing
        is done: the repo is no longer reviewed, the job or its report refuses it, or a credential it needs is
        failing. Reviews paused blocks none of it."""
        if job.scope not in self._contexts:
            return tr(_GONE)
        checked = self._check(job, cmd)
        if isinstance(checked, str):
            return checked
        if checked.action in ("again", "fix", "edit", "draft") and self._credential_failing("claude_login"):
            return tr(_NO_LOGIN)
        if checked.action in _POSTS and self._credential_failing(f"forge:{job.scope}"):
            return tr(_FORGE_FAILING)
        return checked

    def progress_line(self, job: Job, cmd: ReplyCommand) -> str:
        """What is said before a checked post or push starts: "Checking before pushing." etc."""
        if cmd.action == "push":
            return tr("Checking before pushing.")
        if cmd.action == "push_post":
            return tr("Checking before pushing; once the push lands, every drafted reply is posted.")
        return tr("Checking before posting: {command}.", command=describe(cmd, job.head_sha))

    def free_text_refusal(self, job: Job) -> str | None:
        """Why a reply the grammar does not know cannot go to the model, or None: only a done or posted review or
        re-check takes one, while there is a parser and claude is logged in on the host."""
        if self._parser is None or job.kind not in ("review", "recheck") or job.state not in POSTABLE_STATES:
            return not_understood_line(job.kind)
        if self._credential_failing("claude_login"):
            return tr(_NO_LOGIN_PARSER)
        return None

    async def understand(self, job: Job, text: str) -> ReplyCommand | str:
        """The LLM fallback: the parser's command, checked against the job as it is once the parser has answered, or
        why nothing is done (free_text_refusal first). Nothing runs from here: the caller asks Yes / No first."""
        if (refusal := self.free_text_refusal(job)) is not None:
            return refusal
        run = self._store.run(job.id)
        context = reply_context(job.kind, _report(run), run.replies, set(run.drafted))
        cmd = await self._parser.parse(text, context)
        if cmd is None:
            return not_understood_line(job.kind)
        return self._check(self._store.job(job.id), cmd)

    async def run(self, job: Job, cmd: ReplyCommand,
                  say: Callable[[str], Awaitable[None]] | None = None) -> ActionResult:
        """Run a command `check` passed. Each text goes to `say` as soon as it exists (a push's outcome before the post
        that follows it) and into the result's texts."""
        texts: list[str] = []

        async def said(text: str) -> None:
            texts.append(text)
            if say is not None:
                await say(text)

        if cmd.action in _PR_ACTIONS:
            state, text = self._pr_action(cmd.action, job.scope, job.number)
            await said(text)
        elif cmd.action in ("text", "diff"):
            state = "shown"
            for text in self._shown(job, cmd):
                await said(text)
        elif cmd.action in ("again", "fix"):
            if cmd.action == "fix":  # on a self-review's message: that review must be the PR's self-review
                state, text = self._fix_findings(job, review_id=job.id)
            elif job.kind == "findings":  # on the PR's self-review at its head, whichever that is now
                state, text = self._fix_findings(job, instruction=cmd.instruction)
            else:
                state, text = self._address_again(job, cmd.instruction)
            await said(text)
        elif cmd.action in ("edit", "draft"):
            [n] = cmd.numbers  # check_command passes one number only
            self._small.queue(job, cmd.action, n, cmd.instruction)
            state = "queued"
            if cmd.action == "edit":
                await said(tr("Editing {n}: the new text comes in a few minutes; nothing is posted.", n=n))
            else:
                await said(tr("Drafting {n}: the comment comes in a few minutes; nothing is posted.", n=n))
        else:  # post, post_replies, push, push_post
            state = await self._post(self._contexts[job.scope], job, cmd, said)
        return ActionResult(state, texts, cmd)

    def pr_action(self, action: str, scope: str, number: int) -> ActionResult:
        """"review again" or "skip" on a line about a PR rather than a job's message."""
        state, text = self._pr_action(action, scope, number)
        return ActionResult(state, [text], ReplyCommand(action))

    def _shown(self, job: Job, cmd: ReplyCommand) -> list[str]:
        """What "text N" or "diff" shows, one text per message."""
        run = self._store.run(job.id)
        if cmd.action == "diff":
            return _patch_parts(run)
        if job.kind in ("recheck", "address"):
            texts = {entry["n"]: _reply_text(entry) for entry in run.replies}
        else:
            texts = {item.number: item_text(item) for item in _report(run).items}
        return [texts[n] + github_note() for n in cmd.numbers]

    async def _post(self, ctx: RepoContext, job: Job, cmd: ReplyCommand,
                    say: Callable[[str], Awaitable[None]]) -> str:
        if cmd.action in ("push", "push_post"):
            then_post = cmd.action == "push_post"
            outcome = await self.posting.push_fix(ctx, job, anyway=cmd.anyway, then_post=then_post)
            await say(outcome.text)
            if then_post and outcome.state == "posted":
                return await self._post_all(ctx, job.id, say) or outcome.state
            return outcome.state
        else:
            post = self.posting.post_review if cmd.action == "post" else self.posting.post_replies
            outcome = await post(ctx, job, cmd)
        await say(outcome.text)
        return outcome.state

    async def _post_all(self, ctx: RepoContext, job_id: str, say: Callable[[str], Awaitable[None]]) -> str | None:
        """After "push and post" pushed the fix: every drafted reply not posted at the job's new head. The post's
        state; None when nothing was left to post."""
        job = self._store.job(job_id)
        checked = self._check(job, ReplyCommand("post_replies", all_replies=True))
        if isinstance(checked, str):
            await say(checked)
            return None
        await say(self.progress_line(job, checked))
        outcome = await self.posting.post_replies(ctx, job, checked)
        await say(outcome.text)
        return outcome.state

    def _check(self, job: Job, cmd: ReplyCommand) -> ReplyCommand | str:
        """The command as it runs against the job ("all confirmed" expanded), or why nothing is done."""
        if cmd.action in _PR_ACTIONS:
            return outdated_line(job.state, job.reason) if job.state in _OUTDATED else cmd
        if cmd.action == "again":  # as "review again": a failed fix job's line takes it too
            if job.kind not in FIX_KINDS:
                return check_command(cmd, job.kind, None, [], set(), set())
            return outdated_line(job.state, job.reason) if job.state in _OUTDATED else cmd
        if job.state not in POSTABLE_STATES:
            return outdated_line(job.state, job.reason)
        run = self._store.run(job.id)
        report = _report(run)
        posted = self._store.posted_numbers(job, report)
        return check_command(cmd, job.kind, report, run.replies, posted, set(run.drafted))

    def _pr_action(self, action: str, scope: str, number: int) -> tuple[str, str]:
        return self._review_again(scope, number) if action == "review_again" else self._skip(scope, number)

    def _review_again(self, scope: str, number: int) -> tuple[str, str]:
        try:
            job = self._service.review_now(scope, number)
        except ValueError:
            return "refused", tr("A review of #{number} at this head is already queued.", number=number)
        except LookupError:
            return "refused", tr("#{number} is not on the board any more; nothing was done.", number=number)
        return "queued", tr("Queued a review of #{number} at {head}.", number=number, head=job.head_sha[:9])

    def _address_again(self, job: Job, instruction: str) -> tuple[str, str]:
        try:
            new = self._service.address_again(job.scope, job.number, instruction)
        except ValueError as e:
            return "refused", _fix_refused(job.number, str(e))
        except LookupError:
            return "refused", tr("#{number} has no threads waiting on you any more; nothing was done.",
                                 number=job.number)
        return "queued", tr("Queued a new run on #{number} at {head}.", number=job.number, head=new.head_sha[:9])

    def _fix_findings(self, job: Job, *, review_id: str | None = None,
                      instruction: str | None = None) -> tuple[str, str]:
        """"fix" on a self-review's message (review_id: that review) or "again" on a findings message: a findings job
        queued for the PR's self-review at its head, or why not."""
        try:
            new = self._service.fix_findings(job.scope, job.number, review_id=review_id, instruction=instruction)
        except ValueError as e:
            return "refused", _fix_refused(job.number, str(e))
        except LookupError:
            return "refused", tr("#{number} is not on the board any more; nothing was done.", number=job.number)
        return "queued", tr('Fixing the findings of #{number} at {head}: the result comes here; nothing is pushed until'
                            ' you reply "push".', number=job.number, head=new.head_sha[:9])

    def _skip(self, scope: str, number: int) -> tuple[str, str]:
        try:
            self._service.skip(scope, number)
        except LookupError:
            return "refused", tr("#{number} is not on the board any more; nothing was done.", number=number)
        head = self._store.board(scope)[number].head_sha  # the head skip() read: nothing ran in between
        return "queued", tr("Skipped #{number} at {head}: no automatic review of this head.", number=number,
                            head=head[:9])


def _fix_refused(number: int, error: str) -> str:
    """Why a fix of either kind was not queued, from the service's ValueError text (ReviewerService._pending_fix,
    fix_findings): a "not fixable: <why>" reason is an N_() literal, translated here."""
    if error == "a post is under way":
        return tr("A push or a post of a fix of #{number} is under way; try again when it ends. Nothing was done.",
                  number=number)
    if error == "not the newest self-review":
        return tr("This is not the newest self-review of #{number} at its head. Nothing was done.", number=number)
    if error.startswith("not fixable: "):
        why = error.removeprefix("not fixable: ")
        return tr("Cannot fix the findings of #{number}: {why}. Nothing was done.", number=number, why=tr(why))
    return tr("A run on #{number} is already queued.", number=number)


def _reply_text(entry: dict) -> str:
    """A drafted re-check reply as "text N" sends it: where posting it goes, whether a resolve follows, and its full
    body (marked for translation into the Telegram language). The plan is model output: an id is shown cut."""
    if "thread_id" in entry and "review_id" not in entry:
        thread = str(entry["thread_id"])[:60]
        if entry.get("resolve") is True:
            target = tr("reply in thread {thread}, then resolve it", thread=thread)
        else:
            target = tr("reply in thread {thread}, left unresolved", thread=thread)
    elif "review_id" in entry and "thread_id" not in entry:
        target = tr("answer to review {review}", review=str(entry["review_id"])[:60])
    else:
        target = tr("names no single thread or review: it cannot be posted")
    return f"#{entry['n']} · {target}\n\n{model_text(entry['body'])}"


def _patch_parts(run: JobRun) -> list[str]:
    """An address job's patch as "diff" sends it: at most _MAX_PARTS parts, the last one ending with a note when it
    is cut; one line when the job's copy holds no patch."""
    patch = Path(run.job_dir) / PATCH_NAME
    if not run.job_dir or not patch.is_file() or patch.is_symlink():
        return [tr("This job changed no code: there is no patch.")]
    parts = split_message(patch.read_text(encoding="utf-8", errors="replace"), MESSAGE_LIMIT)
    if len(parts) <= _MAX_PARTS:
        return parts
    note = tr("Patch cut: the dashboard shows all of it.")
    last = split_message(parts[_MAX_PARTS - 1], MESSAGE_LIMIT - len(note) - 1)[0]
    return [*parts[:_MAX_PARTS - 1], f"{last}\n{note}"]


def _report(run: JobRun) -> Report | None:
    """The run's parsed report; None for a re-check's run, which has none."""
    return Report.from_dict(run.report) if run.report else None

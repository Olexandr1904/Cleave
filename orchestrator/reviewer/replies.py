"""ReviewerReplies: a reply to a reviewer message, or a tap on an echo's button, turned into an action.

A reply the grammar knows word for word is checked and runs at once. Any other reply to a review or a re-check goes
to the LLM fallback: its command is checked like the grammar's, echoed with Yes / No buttons, and runs only after
Yes, within CONFIRM_TTL; an address message takes exact replies only. Every command runs against the job as the store
has it at that moment, and a post or a push goes through Posting, the only module that posts. Every answer is a reply
to the anchored message, anchored as it is.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from integrations.base.notifier import Button
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.fix import PATCH_NAME
from orchestrator.reviewer.grammar import ReplyCommand, check_command, describe, parse_reply
from orchestrator.reviewer.lines import github_note, interrupted_line, not_understood_line, outdated_line
from orchestrator.reviewer.messages import MESSAGE_LIMIT, message_parts, split_message
from orchestrator.reviewer.models import POSTABLE_STATES, Anchor, Confirmation, Job, JobRun
from orchestrator.reviewer.posting import Posting
from orchestrator.reviewer.reply_llm import ReplyParser, reply_context
from orchestrator.reviewer.report import Report
from orchestrator.reviewer.report_edit import item_text
from orchestrator.reviewer.service import RepoContext, ReviewerService, _where, utcnow
from orchestrator.reviewer.small_jobs import SmallJobRunner
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import (
    ENGLISH, N_, UKRAINIAN, current_language, marked_texts, model_text, telegram_language, tr,
)

logger = logging.getLogger(__name__)

CONFIRM_TTL = timedelta(minutes=15)
# TelegramAdapter.start_polling() keeps pending updates: a reply older than this (one sent while the daemon was down,
# say) runs nothing.
_REPLY_MAX_AGE = timedelta(minutes=15)
_OUTDATED = ("superseded", "stale")  # the job states "review again" and "skip" refuse
_PR_ACTIONS = ("review_again", "skip")  # the commands about the PR's head, not the report
_MAX_PARTS = 3  # of one answer: an item's text, a posting result
# Answers, translated where they are sent (tr).
_OFF = N_("Reviewer mode is off on this daemon: nothing was done.")
_GONE = N_("This repo is no longer reviewed; nothing was done.")
_NO_REPLIES = N_("This line takes no replies; nothing was done.")
_LINE_ONLY = N_('Only "review again" and "skip" work on this line; nothing was done.')
_NO_LOGIN_PARSER = N_("claude is not logged in on the host: only exact replies work now; nothing was done.")
_NO_LOGIN = N_("claude is not logged in on the host: nothing was started.")
_FORGE_FAILING = N_("The forge token is failing (see the dashboard): nothing posted.")
_HANDLED = N_("Already handled.")
_CANCELLED = N_("Cancelled: nothing was done.")
_EXPIRED = N_("Expired: reply again; nothing was done.")


@dataclass(frozen=True)
class _To:
    """Where an answer goes: a reply to `reply_to`, anchored to the job, else to the line's PR (scope and number)."""
    reply_to: int | None
    job_id: str = ""
    scope: str = ""
    number: int = 0


class ReviewerReplies:
    """Routes the replies to reviewer messages and the taps on its echoes. Long work (the LLM fallback, a post, a text to
    translate) runs in tracked tasks, so the Telegram handler is never held."""

    def __init__(
        self,
        contexts: list[RepoContext],
        store: ReviewerStore,
        service: ReviewerService,
        chat: ReviewerChat | None,
        posting: Posting,
        small: SmallJobRunner | None,
        parser: ReplyParser | None,
        *,
        credential_failing: Callable[[str], bool],
        now: Callable[[], datetime] = utcnow,
        language_of: Callable[[str], str] = lambda project_id: ENGLISH,
    ) -> None:
        self._contexts = {ctx.scope: ctx for ctx in contexts}
        # The Telegram language of a project id ("": the global one). An answer takes its anchored job's project's.
        self._language_of = language_of
        self._store = store
        self._service = service
        self._chat = chat
        self._posting = posting
        self._small = small
        self._parser = parser
        self._credential_failing = credential_failing
        self._now = now
        self._tasks: set[asyncio.Task] = set()
        self._closed = False  # aclose() ran: no task is started any more

    def has_repos(self) -> bool:
        """Whether any repo is in reviewer mode on this daemon."""
        return bool(self._contexts)

    def is_anchor(self, chat_id: str, message_id: int) -> bool:
        """Whether the message is the reviewer's; a store error propagates (the caller answers it)."""
        return self._store.anchor(chat_id, message_id) is not None

    def anchor_answer(self, chat_id: str, message_id: int) -> None:
        """Make a message the command handler sent itself ("Reply not handled") an anchor that takes no replies, so
        a reply to it is the reviewer's too; a store error propagates."""
        self._store.add_anchor(chat_id, message_id, "", self._now().isoformat())

    async def handle_reply(self, chat_id: str, message_id: int, text: str, sent_at: datetime | None = None) -> None:
        """Act on a reply to an anchored message, or answer why nothing is done. `sent_at` is the reply's Telegram
        date: a reply older than _REPLY_MAX_AGE runs nothing. Never raises (cancellation aside)."""
        to = _To(message_id)
        language = self._language_of("")  # until the anchor names a project
        try:
            if not self._contexts:
                with telegram_language(language):
                    await self._answer(to, tr(_OFF))
                return
            # Called for anchors only (is_anchor): one gone since reads as a line that takes no replies.
            anchor = self._store.anchor(chat_id, message_id) or Anchor("", "", 0)
            to = _To(message_id, anchor.job_id, anchor.scope, anchor.number)
            language = self._anchor_language(anchor.job_id, anchor.scope)
            with telegram_language(language):
                if sent_at is not None and (age := self._now() - sent_at) > _REPLY_MAX_AGE:
                    await self._answer(to, _too_old_line(sent_at, age))
                elif anchor.job_id:
                    await self._job_reply(chat_id, self._store.job(anchor.job_id), text, to)
                else:
                    await self._line_reply(anchor, text, to)
        except Exception as e:
            with telegram_language(language):
                await self._internal_error(to, e)

    async def handle_callback(self, action: str, conf_id: str, chat_id: str, message_id: int) -> None:
        """A tap on an echo's Yes (rv_ok) or No (rv_no). The store hands a confirmation out once, so a second tap or
        a redelivered Yes runs nothing. A tap in another chat than the echo's is logged only: it neither runs nor
        spends the echo. Never raises (cancellation aside)."""
        to = _To(message_id)
        language = self._language_of("")  # until the echo's job names a project
        try:
            if not self._contexts:
                with telegram_language(language):
                    await self._answer(to, tr(_OFF))
                return
            if (conf := self._store.confirmation(conf_id)) is not None and conf.chat_id != chat_id:
                logger.warning("Reviewer confirmation tapped in another chat: nothing was done")
                return
            if anchor := self._store.anchor(chat_id, message_id):
                to = _To(message_id, anchor.job_id, anchor.scope, anchor.number)
            language = self._anchor_language(conf.job_id if conf is not None else to.job_id, to.scope)
            with telegram_language(language):
                if action == "rv_no":
                    declined = self._store.claim_confirmation(conf_id, "declined")
                    await self._answer(to, tr(_CANCELLED if declined else _HANDLED))
                elif action == "rv_ok":
                    await self._confirmed(self._store.claim_confirmation(conf_id, "confirmed"), to)
                else:
                    logger.warning("Reviewer callback not known: nothing was done")
        except Exception as e:
            with telegram_language(language):
                await self._internal_error(to, e)

    async def start(self) -> None:
        """The posts a restart left `posting`, now `interrupted`, each announced with a line anchored to its job;
        before any reply is handled. Never raises (cancellation aside)."""
        try:
            posts = self._posting.recover()
        except Exception as e:
            logger.warning("Reviewer posts not recovered: %s", _where(e))
            return
        for post in posts:
            with telegram_language(self._anchor_language("", post.scope)):
                await self._answer(_To(None, post.job_id),
                                   interrupted_line(post.scope, post.number, post.kind, post.numbers))

    async def wait_idle(self) -> None:
        """Until every tracked task has ended, those they started included."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel the tracked tasks and wait for them (shutdown): a post cut short stays `posting` for start(). A
        reply that arrives after this starts nothing."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _job_reply(self, chat_id: str, job: Job, text: str, to: _To) -> None:
        ctx = self._contexts.get(job.scope)
        if ctx is None:
            await self._answer(to, tr(_GONE))
        elif (cmd := parse_reply(text)) is not None:
            await self._execute(ctx, job, cmd, to)
        elif self._parser is not None and job.kind in ("review", "recheck") and job.state in POSTABLE_STATES:
            if self._credential_failing("claude_login"):
                await self._answer(to, tr(_NO_LOGIN_PARSER))
            else:
                self._track(to, self._understand(job, chat_id, text, to))
        else:
            await self._answer(to, not_understood_line(job.kind))

    async def _line_reply(self, anchor: Anchor, text: str, to: _To) -> None:
        """A line about one PR takes "review again" and "skip", in the grammar's words; any other line (number 0:
        about a repo, or the login line, which has no scope either) takes no replies."""
        if anchor.number == 0:
            await self._answer(to, tr(_NO_REPLIES))
        elif anchor.scope not in self._contexts:
            await self._answer(to, tr(_GONE))
        elif (cmd := parse_reply(text)) is None or cmd.action not in _PR_ACTIONS:
            await self._answer(to, tr(_LINE_ONLY))
        else:
            await self._answer(to, self._pr_action(cmd.action, anchor.scope, anchor.number))

    async def _understand(self, job: Job, chat_id: str, text: str, to: _To) -> None:
        """The LLM fallback: the parser's command, checked against the job as it is once the parser has answered,
        then echoed with Yes / No. Nothing runs from here."""
        run = self._store.run(job.id)
        context = reply_context(job.kind, _report(run), run.replies, set(run.drafted))
        cmd = await self._parser.parse(text, context)
        if cmd is None:
            await self._answer(to, not_understood_line(job.kind))
            return
        job = self._store.job(job.id)
        checked = self._check(job, cmd)
        if isinstance(checked, str):
            await self._answer(to, checked)
            return
        conf = self._store.add_confirmation(job.id, chat_id, checked.to_dict(), self._now().isoformat())
        await self._answer(to, tr("Understood: {command}. Correct?", command=describe(checked, job.head_sha)),
                           [Button(tr("Yes"), f"rv_ok:{conf.id}"), Button(tr("No"), f"rv_no:{conf.id}")])

    async def _confirmed(self, conf: Confirmation | None, to: _To) -> None:
        """A Yes: the stored command run against its job as it is now, unless it was handled or has expired. The
        answers are anchored to the confirmation's job."""
        if conf is None:
            await self._answer(to, tr(_HANDLED))
            return
        to = _To(to.reply_to, conf.job_id)
        if self._now() - datetime.fromisoformat(conf.created_at) > CONFIRM_TTL:
            await self._answer(to, tr(_EXPIRED))
            return
        job = self._store.job(conf.job_id)
        ctx = self._contexts.get(job.scope)
        if ctx is None:
            await self._answer(to, tr(_GONE))
        else:
            await self._execute(ctx, job, ReplyCommand.from_dict(conf.command), to)

    async def _execute(self, ctx: RepoContext, job: Job, cmd: ReplyCommand, to: _To) -> None:
        """Run `cmd` against `job` as the store has it now, or answer why nothing is done. Reviews paused blocks
        none of it."""
        checked = self._check(job, cmd)
        if isinstance(checked, str):
            await self._answer(to, checked)
        elif checked.action in _PR_ACTIONS:
            await self._answer(to, self._pr_action(checked.action, job.scope, job.number))
        elif checked.action == "text":
            run = self._store.run(job.id)
            if job.kind in ("recheck", "address"):
                texts = {entry["n"]: _reply_text(entry) for entry in run.replies}
            else:
                texts = {item.number: item_text(item) for item in _report(run).items}
            answers = [texts[n] + github_note() for n in checked.numbers]
            if any(marked_texts(answer) for answer in answers):  # a model turn translates them: not in the handler
                self._track(to, self._answer_each(to, answers))
            else:
                await self._answer_each(to, answers)
        elif checked.action == "diff":
            await self._answer_each(to, _patch_parts(self._store.run(job.id)))
        elif checked.action == "again":
            if self._credential_failing("claude_login"):
                await self._answer(to, tr(_NO_LOGIN))
            else:
                await self._answer(to, self._address_again(job, checked.instruction))
        elif checked.action in ("edit", "draft"):
            if self._credential_failing("claude_login"):
                await self._answer(to, tr(_NO_LOGIN))
                return
            [n] = checked.numbers  # check_command passes one number only
            self._small.queue(job, checked.action, n, checked.instruction)
            if checked.action == "edit":
                await self._answer(to, tr("Editing {n}: the new text comes in a few minutes; nothing is posted.", n=n))
            else:
                await self._answer(to, tr("Drafting {n}: the comment comes in a few minutes; nothing is posted.",
                                          n=n))
        elif self._credential_failing(f"forge:{job.scope}"):
            await self._answer(to, tr(_FORGE_FAILING))
        else:  # post, post_replies, push
            if checked.action == "push":
                await self._answer(to, tr("Checking before pushing."))
            else:
                await self._answer(to, tr("Checking before posting: {command}.",
                                          command=describe(checked, job.head_sha)))
            self._track(to, self._post(ctx, job, checked, to))

    async def _answer_each(self, to: _To, texts: list[str]) -> None:
        for text in texts:
            await self._answer(to, text)

    async def _post(self, ctx: RepoContext, job: Job, cmd: ReplyCommand, to: _To) -> None:
        if cmd.action == "push":
            outcome = await self._posting.push_fix(ctx, job)
        else:
            post = self._posting.post_review if cmd.action == "post" else self._posting.post_replies
            outcome = await post(ctx, job, cmd)
        await self._answer(to, outcome.text)

    def _check(self, job: Job, cmd: ReplyCommand) -> ReplyCommand | str:
        """The command as it runs against the job ("all confirmed" expanded), or why nothing is done."""
        if cmd.action in _PR_ACTIONS:
            return outdated_line(job.state, job.reason) if job.state in _OUTDATED else cmd
        if cmd.action == "again":  # as "review again": a failed address job's line takes it too
            if job.kind != "address":
                return check_command(cmd, job.kind, None, [], set(), set())
            return outdated_line(job.state, job.reason) if job.state in _OUTDATED else cmd
        if job.state not in POSTABLE_STATES:
            return outdated_line(job.state, job.reason)
        run = self._store.run(job.id)
        report = _report(run)
        posted = self._store.posted_numbers(job, report)
        return check_command(cmd, job.kind, report, run.replies, posted, set(run.drafted))

    def _pr_action(self, action: str, scope: str, number: int) -> str:
        return self._review_again(scope, number) if action == "review_again" else self._skip(scope, number)

    def _review_again(self, scope: str, number: int) -> str:
        try:
            job = self._service.review_now(scope, number)
        except ValueError:
            return tr("A review of #{number} at this head is already queued.", number=number)
        except LookupError:
            return tr("#{number} is not on the board any more; nothing was done.", number=number)
        return tr("Queued a review of #{number} at {head}.", number=number, head=job.head_sha[:9])

    def _address_again(self, job: Job, instruction: str) -> str:
        try:
            new = self._service.address_again(job.scope, job.number, instruction)
        except ValueError:
            return tr("A run on #{number} is already queued.", number=job.number)
        except LookupError:
            return tr("#{number} has no threads waiting on you any more; nothing was done.", number=job.number)
        return tr("Queued a new run on #{number} at {head}.", number=job.number, head=new.head_sha[:9])

    def _skip(self, scope: str, number: int) -> str:
        try:
            self._service.skip(scope, number)
        except LookupError:
            return tr("#{number} is not on the board any more; nothing was done.", number=number)
        head = self._store.board(scope)[number].head_sha  # the head skip() read: nothing ran in between
        return tr("Skipped #{number} at {head}: no automatic review of this head.", number=number, head=head[:9])

    def _anchor_language(self, job_id: str, scope: str) -> str:
        """The Telegram language of an answer about a job (else about a PR or repo `scope`): its repo's project's;
        the global one when the repo is not reviewed here."""
        if job_id and (job := self._store.job(job_id)) is not None:
            scope = job.scope
        ctx = self._contexts.get(scope)
        return self._language_of(ctx.project_id if ctx is not None else "")

    def _track(self, to: _To, work: Coroutine[Any, Any, None]) -> None:
        if self._closed:  # shutting down: nothing would wait for the task or cancel it
            work.close()
            logger.warning("Reviewer reply not acted on: shutting down")
            return
        task = asyncio.create_task(self._guarded(to, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, to: _To, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception as e:
            await self._internal_error(to, e)

    async def _internal_error(self, to: _To, e: Exception) -> None:
        logger.warning("Reviewer reply stopped: %s", _where(e))
        await self._answer(to, tr("Internal error ({name}); nothing was done.", name=type(e).__name__))

    async def _answer(self, to: _To, text: str, buttons: list[Button] | None = None) -> None:
        """`text` as a reply to the anchored message, its model-written text translated, in parts that fit Telegram,
        each anchored as that one is (the buttons on the last); a failed send is logged with the exception class only,
        and the rest is not sent."""
        if self._chat is None:
            logger.warning("Reviewer answer not sent: no reviewer chat")
            return
        anchor = {"job_id": to.job_id} if to.job_id else {"scope": to.scope, "number": to.number}
        parts = message_parts(await self._chat.localize(text), limit=MESSAGE_LIMIT, max_parts=_MAX_PARTS)
        for i, part in enumerate(parts, 1):
            last = {"buttons": buttons} if buttons and i == len(parts) else {}
            try:
                await self._chat.send(part, **anchor, **last, reply_to=to.reply_to)
            except Exception as e:
                logger.warning("Reviewer answer not sent: %s", type(e).__name__)
                return


def _too_old_line(sent_at: datetime, age: timedelta) -> str:
    """The answer to a reply `age` old: its time in UTC, with the day once it is over a day old."""
    when = sent_at.astimezone(timezone.utc)
    if age <= timedelta(days=1):
        at = f"{when:%H:%M} UTC"
    elif current_language() == UKRAINIAN:  # no English month name in a Ukrainian line
        at = f"{when:%d.%m %H:%M} UTC"
    else:
        at = f"{when.day} {when:%b %H:%M} UTC"
    return tr("This reply is from {at}; send it again to act on it. Nothing was done.", at=at)


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

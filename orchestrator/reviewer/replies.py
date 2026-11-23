"""ReviewerReplies: a reply to a reviewer message, or a tap on an echo's button, turned into an action.

A reply the grammar knows word for word is checked and runs at once. Any other reply to a review or a re-check goes
to the LLM fallback: its command is checked like the grammar's, echoed with Yes / No buttons, and runs only after
Yes, within CONFIRM_TTL; an address or a findings message takes exact replies only, and so does "fix". Every command
is checked and run by ReviewerActions, the layer the dashboard calls too, against the job as the store has it at that
moment. Every answer is a reply to the anchored message, anchored as it is.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from integrations.base.notifier import Button
from orchestrator.reviewer.actions import _GONE, _MAX_PARTS, _PR_ACTIONS, LONG_ACTIONS, ReviewerActions
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.grammar import ReplyCommand, describe, parse_reply
from orchestrator.reviewer.lines import interrupted_line, not_understood_line
from orchestrator.reviewer.messages import MESSAGE_LIMIT, message_parts
from orchestrator.reviewer.models import POSTABLE_STATES, Anchor, Confirmation, Job
from orchestrator.reviewer.posting import Posting
from orchestrator.reviewer.reply_llm import ReplyParser
from orchestrator.reviewer.service import RepoContext, ReviewerService, _where, utcnow
from orchestrator.reviewer.small_jobs import SmallJobRunner
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import ENGLISH, N_, UKRAINIAN, current_language, telegram_language, tr

logger = logging.getLogger(__name__)

CONFIRM_TTL = timedelta(minutes=15)
# TelegramAdapter.start_polling() keeps pending updates: a reply older than this (one sent while the daemon was down,
# say) runs nothing.
_REPLY_MAX_AGE = timedelta(minutes=15)
# Answers, translated where they are sent (tr).
_OFF = N_("Reviewer mode is off on this daemon: nothing was done.")
_NO_REPLIES = N_("This line takes no replies; nothing was done.")
_LINE_ONLY = N_('Only "review again" and "skip" work on this line; nothing was done.')
_NO_LOGIN_PARSER = N_("claude is not logged in on the host: only exact replies work now; nothing was done.")
_HANDLED = N_("Already handled.")
_CANCELLED = N_("Cancelled: nothing was done.")
_EXPIRED = N_("Expired: reply again; nothing was done.")
# A fix message's buttons (messages.fix_buttons), each the command the typed reply gives; rv_pushx asks first.
_TAPS = {
    "rv_push": ReplyCommand("push"),
    "rv_pushpost": ReplyCommand("push_post"),
    "rv_post": ReplyCommand("post_replies", all_replies=True),
    "rv_diff": ReplyCommand("diff"),
}
_PUSH_ANYWAY_TAP = "rv_pushx"


@dataclass(frozen=True)
class _To:
    """Where an answer goes: a reply to `reply_to`, anchored to the job, else to the line's PR (scope and number)."""
    reply_to: int | None
    job_id: str = ""
    scope: str = ""
    number: int = 0


class ReviewerReplies:
    """Routes the replies to reviewer messages and the taps on its echoes. Long work (the LLM fallback, a post, a text to
    translate) runs in tracked tasks, so the Telegram handler is never held. The commands run through `actions`, built
    from the arguments unless given (the daemon shares one with the dashboard, and so one Posting)."""

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
        actions: ReviewerActions | None = None,
    ) -> None:
        self._contexts = {ctx.scope: ctx for ctx in contexts}
        # The Telegram language of a project id ("": the global one). An answer takes its anchored job's project's.
        self._language_of = language_of
        self._store = store
        self._chat = chat
        if actions is None:
            actions = ReviewerActions(contexts, store, service, posting, small, parser,
                                      credential_failing=credential_failing)
        self.actions = actions
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
        """A tap on an echo's Yes (rv_ok) or No (rv_no), or on a fix message's button (then `conf_id` is the job's id,
        see _tap). The store hands a confirmation out once, so a second tap or a redelivered Yes runs nothing. A tap
        in another chat than the echo's is logged only: it neither runs nor spends the echo. Never raises
        (cancellation aside)."""
        to = _To(message_id)
        language = self._language_of("")  # until the echo's job names a project
        try:
            if not self._contexts:
                with telegram_language(language):
                    await self._answer(to, tr(_OFF))
                return
            if action in _TAPS or action == _PUSH_ANYWAY_TAP:
                await self._tap(action, conf_id, chat_id, message_id)
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

    async def _tap(self, action: str, job_id: str, chat_id: str, message_id: int) -> None:
        """A tap on a fix message's button: its command run as the typed reply to that message runs, answered as a
        reply to it. The tapped message must be the job's own, else nothing runs (logged only). "Push anyway…"
        echoes what it would push with Yes / No first."""
        to = _To(message_id, job_id)
        anchor = self._store.anchor(chat_id, message_id)
        if anchor is None or anchor.job_id != job_id:
            logger.warning("Reviewer button tapped on a message that is not its job's: nothing was done")
            return
        language = self._anchor_language(job_id, anchor.scope)
        try:
            with telegram_language(language):
                job = self._store.job(job_id)
                ctx = self._contexts.get(job.scope)
                if ctx is None:
                    await self._answer(to, tr(_GONE))
                elif action == _PUSH_ANYWAY_TAP:
                    await self._ask_push_anyway(job, chat_id, to)
                else:
                    await self._execute(job, _TAPS[action], to)
        except Exception as e:
            with telegram_language(language):
                await self._internal_error(to, e)

    async def _ask_push_anyway(self, job: Job, chat_id: str, to: _To) -> None:
        """The echo of "push anyway" (then a post of every drafted reply, on a threads message with replies), with
        Yes / No: Yes runs it as the stored command (_confirmed)."""
        run = self._store.run(job.id)
        then_post = job.kind == "address" and bool(run.replies)
        checked = self.actions.check(job, ReplyCommand("push_post" if then_post else "push", anyway=True))
        if isinstance(checked, str):
            await self._answer(to, checked)
            return
        paths = ", ".join((run.fix or {}).get("protected", []))
        if then_post:
            text = tr("Push the fix with protected files ({paths}) to #{number}, then post every drafted reply?",
                      paths=paths, number=job.number)
        else:
            text = tr("Push the fix with protected files ({paths}) to #{number}?", paths=paths, number=job.number)
        conf = self._store.add_confirmation(job.id, chat_id, checked.to_dict(), self._now().isoformat())
        await self._answer(to, text, [Button(tr("Yes"), f"rv_ok:{conf.id}"), Button(tr("No"), f"rv_no:{conf.id}")])

    async def start(self) -> None:
        """The posts a restart left `posting`, now `interrupted`, each announced with a line anchored to its job;
        before any reply is handled. Never raises (cancellation aside)."""
        try:
            posts = self.actions.posting.recover()
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
            await self._execute(job, cmd, to)
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
            await self._answer_each(to, self.actions.pr_action(cmd.action, anchor.scope, anchor.number).texts)

    async def _understand(self, job: Job, chat_id: str, text: str, to: _To) -> None:
        """The LLM fallback (ReviewerActions.understand): the parser's command, checked, then echoed with Yes / No.
        Nothing runs from here."""
        checked = await self.actions.understand(job, text)
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
            await self._execute(job, ReplyCommand.from_dict(conf.command), to)

    async def _execute(self, job: Job, cmd: ReplyCommand, to: _To) -> None:
        """Run `cmd` against `job` as the store has it now, or answer why nothing is done. A post or a push answers its
        progress line first; it and a text (whose translation may take a model turn) run in a tracked task."""
        checked = self.actions.check(job, cmd)
        if isinstance(checked, str):
            await self._answer(to, checked)
            return
        say = functools.partial(self._answer, to)
        if checked.action in LONG_ACTIONS:
            if checked.action != "text":
                await self._answer(to, self.actions.progress_line(job, checked))
            self._track(to, self.actions.run(job, checked, say=say))
        else:
            await self.actions.run(job, checked, say=say)

    async def _answer_each(self, to: _To, texts: list[str]) -> None:
        for text in texts:
            await self._answer(to, text)

    def _anchor_language(self, job_id: str, scope: str) -> str:
        """The Telegram language of an answer about a job (else about a PR or repo `scope`): its repo's project's;
        the global one when the repo is not reviewed here."""
        if job_id and (job := self._store.job(job_id)) is not None:
            scope = job.scope
        ctx = self._contexts.get(scope)
        return self._language_of(ctx.project_id if ctx is not None else "")

    def _track(self, to: _To, work: Coroutine[Any, Any, Any]) -> None:
        if self._closed:  # shutting down: nothing would wait for the task or cancel it
            work.close()
            logger.warning("Reviewer reply not acted on: shutting down")
            return
        task = asyncio.create_task(self._guarded(to, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, to: _To, work: Awaitable[Any]) -> None:
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

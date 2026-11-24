# tests/unit/test_reviewer_replies.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from config.schemas import ReviewerConfig
from integrations.base.notifier import Button
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.grammar import ReplyCommand
from orchestrator.reviewer.lines import interrupted_line, not_understood_line, replies_posted_line
from orchestrator.reviewer.messages import MESSAGE_LIMIT, message_parts
from orchestrator.reviewer.models import Job, JobRun, Post
from orchestrator.reviewer.posting import Posting, PostOutcome
from orchestrator.reviewer.replies import CONFIRM_TTL, ReviewerReplies
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.report_edit import item_text
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.tg_i18n import fill
from tests.unit.test_reviewer_reply_llm import FULL
from tests.unit.test_reviewer_report import HEAD, REPORT
from tests.unit.test_reviewer_runner import board_row

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)  # the job's run
T1 = T0 + timedelta(hours=2)  # Cleave's clock while the operator replies
CHAT = "1"
SCOPE = "acme/app"
N = 1712
NEW_HEAD = "8f01a2b3c" + "b" * 31  # the board's head once the PR moved on
POSTED = PostOutcome("posted", "Posted …")
REPLIES = [{"n": 1, "thread_id": "T1", "comment_id": 101, "body": "Fixed.", "resolve": True},
           {"n": 3, "review_id": 456, "body": "Agreed."}]
EVERY_BUT_3 = ReplyCommand("post", (1, 2, 4, 5, 6))
VERDICTS = [{"n": 1, "verdict": "FIXED", "why": "null check added"}]


class FakeChat:
    """Records (text, kwargs) per message; anchors each one in the store, as ReviewerChat does."""

    def __init__(self, store):
        self.store, self.sent = store, []

    async def send(self, text, **kwargs):
        self.sent.append((text, kwargs))
        message_id = 1000 + len(self.sent)
        self.store.add_anchor(CHAT, message_id, kwargs.get("job_id", ""), T0.isoformat(),
                              scope=kwargs.get("scope", ""), number=kwargs.get("number", 0))
        return message_id

    async def translations(self, texts):  # ReviewerChat without a translator
        return {}

    async def localize(self, text):
        return fill(text, {})


class FakePosting:
    """Records (method, scope, job id, command) per post; returns `outcome` (POSTED) or raises `error`."""

    def __init__(self):
        self.calls, self.error, self.outcome, self.outcomes = [], None, POSTED, {}

    async def post_review(self, ctx, job, cmd):
        return self._post("post_review", ctx, job, cmd)

    async def post_replies(self, ctx, job, cmd):
        return self._post("post_replies", ctx, job, cmd)

    async def push_fix(self, ctx, job, *, anyway=False, then_post=False):
        """A plain push records None; "anyway" or a post to follow records both flags."""
        flags = {"anyway": anyway, "then_post": then_post} if anyway or then_post else None
        return self._post("push_fix", ctx, job, flags)

    def _post(self, name, ctx, job, cmd):
        self.calls.append((name, ctx.scope, job.id, cmd))
        if self.error is not None:
            raise self.error
        return self.outcomes.get(name, self.outcome)


class FakeSmall:
    """Records (review job, kind, number, instruction) per queue call."""

    def __init__(self):
        self.calls = []

    def queue(self, review_job, kind, number, instruction=""):
        self.calls.append((review_job, kind, number, instruction))


class FakeParser:
    """Returns the scripted commands in order; records (text, context) per call."""

    def __init__(self, results):
        self.results, self.calls = list(results), []

    async def parse(self, text, context):
        self.calls.append((text, context))
        return self.results.pop(0)


class FakeService:
    """Records (method, scope, number); review_now returns a job at NEW_HEAD; either raises its scripted error."""

    def __init__(self):
        self.calls, self.review_error, self.skip_error = [], None, None

    def review_now(self, scope, number):
        self.calls.append(("review_now", scope, number))
        if self.review_error is not None:
            raise self.review_error
        return Job("j-new", scope, number, "review", NEW_HEAD, NEW_HEAD, "queued", "", T1.isoformat(),
                   T1.isoformat())

    def skip(self, scope, number):
        self.calls.append(("skip", scope, number))
        if self.skip_error is not None:
            raise self.skip_error

    def fix_findings(self, scope, number, *, review_id=None, instruction=None):
        self.calls.append(("fix_findings", scope, number, review_id, instruction))
        if self.fix_error is not None:
            raise self.fix_error
        return Job("j-fix", scope, number, "findings", HEAD, f"{HEAD}:r1", "queued", "", T1.isoformat(),
                   T1.isoformat())

    fix_error = None

    def address_again(self, scope, number, instruction):
        self.calls.append(("address_again", scope, number, instruction))
        if self.again_error is not None:
            raise self.again_error
        return Job("j-again", scope, number, "address", HEAD, f"{HEAD}:T1", "queued", "", T1.isoformat(),
                   T1.isoformat())

    again_error = None


class Rig:
    """A done review job of PR 1712 at HEAD in a real store (its run with plan 4's report, started at T0), its
    message anchored at (CHAT, 100), the PR on the board at NEW_HEAD; the router on fakes, on a clock tests move,
    answering in `language`."""

    def __init__(self, tmp_path, *, parser=(), failing=(), contexts=None, language="en"):
        self.tmp = tmp_path
        self.language = language
        self.store = ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
        self.store.replace_board(SCOPE, {N: board_row(NEW_HEAD), 1730: board_row(NEW_HEAD, number=1730)})
        self.job = self.add_job("review", 100)
        self.ctx = RepoContext("acme", "app", ReviewerConfig(), None, None, None)
        self.clock = [T1]
        self.failing = set(failing)
        self.chat, self.posting = FakeChat(self.store), FakePosting()
        self.small, self.service = FakeSmall(), FakeService()
        self.parser = None if parser is None else FakeParser(parser)
        self.replies = self.router(self.posting, [self.ctx] if contexts is None else contexts)

    def router(self, posting, contexts):
        return ReviewerReplies(contexts, self.store, self.service, self.chat, posting, self.small, self.parser,
                               credential_failing=self.failing.__contains__, now=lambda: self.clock[0],
                               language_of=lambda project_id: self.language)

    def add_job(self, kind, message_id, scope=SCOPE):
        key = {"review": HEAD, "address": f"{HEAD}:T1", "findings": f"{HEAD}:r1"}.get(kind, f"recheck:{HEAD}")
        job = self.store.add_job(scope, N, kind, HEAD, key, T0.isoformat())
        self.store.set_job_state(job.id, "done", "", T0.isoformat())
        job_dir = self.tmp / "rv" / "jobs" / job.id
        job_dir.mkdir(parents=True)
        if kind == "review":
            (job_dir / f"report_{HEAD[:9]}.md").write_text(REPORT, encoding="utf-8")
            run = JobRun(job.id, started_at=T0.isoformat(), job_dir=str(job_dir),
                         report=parse_report(REPORT, HEAD).to_dict(), message_ids=[message_id], message_sent=True)
        elif kind == "findings":
            run = JobRun(job.id, started_at=T0.isoformat(), job_dir=str(job_dir), verdicts=VERDICTS,
                         request={"review": "r1"}, message_ids=[message_id], message_sent=True)
        else:
            run = JobRun(job.id, started_at=T0.isoformat(), job_dir=str(job_dir), replies=REPLIES,
                         message_ids=[message_id], message_sent=True)
        self.store.save_run(run)
        self.store.add_anchor(CHAT, message_id, job.id, T0.isoformat())
        return self.store.job(job.id)

    async def reply(self, text, message_id=100, sent_at=None):
        await self.replies.handle_reply(CHAT, message_id, text, sent_at=sent_at)
        await self.replies.wait_idle()

    async def tap(self, action, conf_id, message_id):
        await self.replies.handle_callback(action, conf_id, CHAT, message_id)
        await self.replies.wait_idle()

    def texts(self):
        return [text for text, _ in self.chat.sent]

    def last(self):
        return self.chat.sent[-1][0]

    def last_id(self):
        return 1000 + len(self.chat.sent)

    def at(self, reply_to, job=None):
        """The kwargs of an answer anchored to the job (the rig's by default)."""
        return {"job_id": (job or self.job).id, "reply_to": reply_to}


def echo_id(kwargs):
    """The confirmation id the echo's buttons carry, checking they are Yes then No on it."""
    conf_id = kwargs["buttons"][0].action.removeprefix("rv_ok:")
    assert kwargs["buttons"] == [Button("Yes", f"rv_ok:{conf_id}"), Button("No", f"rv_no:{conf_id}")]
    return conf_id


def landed_post(job, numbers):
    """A posted review Post at HEAD with the report items `numbers` landed inline."""
    items = [i for i in parse_report(REPORT, HEAD).items if i.number in numbers]
    landed = [{"n": i.number, "path": i.path, "line": i.line, "title": i.title, "where": "inline"} for i in items]
    return Post("p-landed", job.id, SCOPE, N, HEAD, "review", "posted", T0.isoformat(), T0.isoformat(),
                numbers=list(numbers), event="COMMENT", landed=landed, comments=len(landed))


async def test_grammar_post_runs_at_once(tmp_path):
    rig = Rig(tmp_path)
    await rig.reply("пости 1 2 4 апрув")
    assert rig.posting.calls == [("post_review", SCOPE, rig.job.id, ReplyCommand("post", (1, 2, 4), "APPROVE"))]
    assert rig.chat.sent == [
        ("Checking before posting: post 1 2 4 + APPROVE at 26c4929c5.", rig.at(100)),
        ("Posted …", rig.at(100)),
    ]
    assert rig.parser.calls == []


@pytest.mark.parametrize("tap, event", [
    ("rv_comment", "COMMENT"), ("rv_approve", "APPROVE"), ("rv_changes", "REQUEST_CHANGES"),
])
async def test_an_event_button_posts_every_confirmed_finding_with_it(tmp_path, tap, event):
    rig = Rig(tmp_path)
    await rig.tap(tap, rig.job.id, 100)
    [(kind, scope, job_id, cmd)] = rig.posting.calls
    assert (kind, scope, job_id, cmd.action, cmd.event) == ("post_review", SCOPE, rig.job.id, "post", event)
    assert cmd.numbers == (1, 2, 3)  # the report's confirmed findings, as the dashboard's Post review… posts them
    assert rig.texts()[-1] == "Posted …"


async def test_superseded_report_answers_outdated(tmp_path):
    rig = Rig(tmp_path)
    rig.store.set_job_state(rig.job.id, "superseded", "new head", T1.isoformat())
    for text in ("post 1 2", "review again", "skip", "text 2"):
        await rig.reply(text)
    assert rig.chat.sent == [("Outdated: there is a review of a newer head. Nothing was done.", rig.at(100))] * 4
    assert (rig.posting.calls, rig.service.calls, rig.small.calls) == ([], [], [])


async def test_unknown_number_refused(tmp_path):
    rig = Rig(tmp_path)
    await rig.reply("post 12")
    assert rig.chat.sent == [("Unknown number: 12. Nothing was done.", rig.at(100))]
    assert rig.posting.calls == []


async def test_redelivered_post_is_refused(tmp_path):
    rig = Rig(tmp_path, parser=[ReplyCommand("post", (1, 2))])
    await rig.reply("пости первые два")  # an echo from before the post landed
    old_echo, old_id = echo_id(rig.chat.sent[-1][1]), rig.last_id()
    rig.store.save_post(landed_post(rig.job, (1, 2)))
    rig.store.set_job_state(rig.job.id, "posted", "", T1.isoformat())

    await rig.reply("post 1 2")
    assert rig.chat.sent[-1] == ("#1 is already posted at this head. Nothing was done.", rig.at(100))
    await rig.tap("rv_ok", old_echo, old_id)  # the Yes runs against the job as it is now
    assert rig.chat.sent[-1] == ("#1 is already posted at this head. Nothing was done.", rig.at(old_id))
    assert rig.posting.calls == []


async def test_llm_path_needs_yes(tmp_path):
    rig = Rig(tmp_path, parser=[EVERY_BUT_3] * 3)
    await rig.reply("пости всё кроме 3")
    assert rig.parser.calls == [("пости всё кроме 3", FULL)]
    [(echo, kwargs)] = rig.chat.sent
    assert echo == "Understood: post 1 2 4 5 6 + COMMENT at 26c4929c5. Correct?"
    first, first_at = echo_id(kwargs), rig.last_id()
    assert {k: v for k, v in kwargs.items() if k != "buttons"} == rig.at(100)
    assert rig.posting.calls == []

    await rig.tap("rv_ok", first, first_at)
    assert rig.posting.calls == [("post_review", SCOPE, rig.job.id, EVERY_BUT_3)]
    assert rig.chat.sent[1:] == [
        ("Checking before posting: post 1 2 4 5 6 + COMMENT at 26c4929c5.", rig.at(first_at)),
        ("Posted …", rig.at(first_at)),
    ]
    await rig.tap("rv_ok", first, first_at)
    assert rig.last() == "Already handled."
    await rig.tap("rv_no", first, first_at)  # never "Cancelled" for a command that ran
    assert rig.last() == "Already handled."
    assert len(rig.posting.calls) == 1

    await rig.reply("пости всё кроме 3")
    second, second_at = echo_id(rig.chat.sent[-1][1]), rig.last_id()
    await rig.tap("rv_no", second, second_at)
    assert rig.chat.sent[-1] == ("Cancelled: nothing was done.", rig.at(second_at))
    await rig.tap("rv_ok", second, second_at)
    assert rig.last() == "Already handled."

    await rig.reply("пости всё кроме 3")
    third, third_at = echo_id(rig.chat.sent[-1][1]), rig.last_id()
    rig.clock[0] += timedelta(minutes=16)
    await rig.tap("rv_ok", third, third_at)
    assert rig.chat.sent[-1] == ("Expired: reply again; nothing was done.", rig.at(third_at))
    assert len(rig.posting.calls) == 1
    assert CONFIRM_TTL == timedelta(minutes=15)


def confirmations(rig):
    return rig.store._conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0]


async def test_a_job_superseded_during_the_llm_call_is_refused(tmp_path):
    """The job is read again once the parser has answered: superseded meanwhile, the command is refused, and no
    echo or confirmation is left to tap (Task 14, pinned by the final review M9)."""
    rig = Rig(tmp_path, parser=[EVERY_BUT_3])
    parse = rig.parser.parse

    async def superseding(text, context):
        rig.store.set_job_state(rig.job.id, "superseded", "new head", T1.isoformat())  # a new head polled meanwhile
        return await parse(text, context)

    rig.parser.parse = superseding
    await rig.reply("пости всё кроме 3")
    assert rig.chat.sent == [("Outdated: there is a review of a newer head. Nothing was done.", rig.at(100))]
    assert confirmations(rig) == 0 and rig.posting.calls == []


async def test_a_yes_on_a_job_superseded_since_the_echo_is_refused(tmp_path):
    """A Yes runs the stored command against the job as it is at the tap (Task 14, pinned by the final review M9)."""
    rig = Rig(tmp_path, parser=[EVERY_BUT_3])
    await rig.reply("пости всё кроме 3")
    conf_id, echo_at = echo_id(rig.chat.sent[-1][1]), rig.last_id()
    assert confirmations(rig) == 1
    rig.store.set_job_state(rig.job.id, "superseded", "new head", T1.isoformat())
    await rig.tap("rv_ok", conf_id, echo_at)
    assert rig.chat.sent[-1] == ("Outdated: there is a review of a newer head. Nothing was done.", rig.at(echo_at))
    assert rig.posting.calls == []
    await rig.tap("rv_ok", conf_id, echo_at)  # spent: a second tap runs nothing either
    assert rig.last() == "Already handled." and rig.posting.calls == []


async def test_a_tap_is_bound_to_its_chat(tmp_path, caplog):
    rig = Rig(tmp_path, parser=[EVERY_BUT_3] * 2)
    await rig.reply("пости всё кроме 3")
    first, first_at = echo_id(rig.chat.sent[-1][1]), rig.last_id()
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.replies"):
        for action in ("rv_no", "rv_ok"):  # taps from another allowlisted chat
            await rig.replies.handle_callback(action, first, "2", first_at)
            await rig.replies.wait_idle()
    assert rig.posting.calls == []
    assert len(rig.chat.sent) == 1  # no answer in the reviewer chat
    assert caplog.text.count("Reviewer confirmation tapped in another chat: nothing was done") == 2
    await rig.tap("rv_ok", first, first_at)  # neither spent the echo
    assert rig.posting.calls == [("post_review", SCOPE, rig.job.id, EVERY_BUT_3)]
    assert rig.last() == "Posted …"

    await rig.reply("пости всё кроме 3")
    second = echo_id(rig.chat.sent[-1][1])
    await rig.tap("rv_ok", second, 999)  # no anchor at 999: the answers go to the confirmation's job
    assert rig.posting.calls == [("post_review", SCOPE, rig.job.id, EVERY_BUT_3)] * 2
    assert rig.chat.sent[-1] == ("Posted …", rig.at(999))


async def test_an_old_reply_runs_nothing(tmp_path):
    """TelegramAdapter.start_polling() keeps pending updates: a "post …" sent while the daemon was down arrives at
    its start, and must not run then. The answer is anchored like the message replied to."""
    rig = Rig(tmp_path)
    await rig.reply("post 1 2", sent_at=T1 - timedelta(minutes=15, seconds=1))
    assert rig.chat.sent == [
        ("This reply is from 11:44 UTC; send it again to act on it. Nothing was done.", rig.at(100))]
    age_line = rig.last_id()
    await rig.reply("review again", sent_at=T1 - timedelta(days=1, minutes=1))  # over a day: with the date
    assert rig.chat.sent[-1] == (
        "This reply is from 19 Nov 11:59 UTC; send it again to act on it. Nothing was done.", rig.at(100))
    rig.store.add_anchor(CHAT, 203, "", T0.isoformat())  # the login line
    await rig.reply("review again", 203, sent_at=T1 - timedelta(hours=1))
    assert rig.chat.sent[-1] == ("This reply is from 11:00 UTC; send it again to act on it. Nothing was done.",
                                 {"scope": "", "number": 0, "reply_to": 203})
    assert (rig.posting.calls, rig.service.calls) == ([], [])

    await rig.reply("post 1 2", sent_at=T1 - timedelta(minutes=15))  # exactly 15:00 old: runs
    assert [(name, job_id) for name, _, job_id, _ in rig.posting.calls] == [("post_review", rig.job.id)]
    await rig.reply("post 4", age_line, sent_at=T1)  # a reply to the age line is a reply to the review
    assert [cmd.numbers for *_, cmd in rig.posting.calls] == [(1, 2), (4,)]


async def test_an_answer_anchor_takes_no_replies(tmp_path):
    """The command handler's "Reply not handled" answer, anchored: a reply to it is the reviewer's."""
    rig = Rig(tmp_path)
    rig.replies.anchor_answer(CHAT, 500)
    assert rig.replies.is_anchor(CHAT, 500)
    await rig.reply("post 1 2", 500)
    assert rig.chat.sent == [("This line takes no replies; nothing was done.",
                              {"scope": "", "number": 0, "reply_to": 500})]
    assert rig.posting.calls == []


async def test_llm_command_still_checked(tmp_path):
    rig = Rig(tmp_path, parser=[ReplyCommand("post", (12,)), None])
    await rig.reply("пости двенадцатый")
    assert rig.chat.sent == [("Unknown number: 12. Nothing was done.", rig.at(100))]  # no buttons
    await rig.reply("что это?")
    assert rig.chat.sent[-1] == (not_understood_line("review"), rig.at(100))
    assert not_understood_line("review") == (
        'Not understood; nothing was done. Reply like "post 1 2 4 approve", "text 2", "edit 2: …", "draft 7"'
        ' or "review again".')

    rig.failing.add("claude_login")
    await rig.reply("пости всё кроме 3")
    assert rig.last() == "claude is not logged in on the host: only exact replies work now; nothing was done."
    rig.failing.clear()
    rig.store.set_job_state(rig.job.id, "failed", "the session failed", T1.isoformat())
    await rig.reply("пости всё кроме 3")  # only a done or posted job's reply goes to the parser
    assert rig.last() == not_understood_line("review")
    assert len(rig.parser.calls) == 2


async def test_no_parser_answers_not_understood(tmp_path):
    rig = Rig(tmp_path, parser=None)
    recheck = rig.add_job("recheck", 300)
    await rig.reply("пости всё кроме 3")
    await rig.reply("ответь на всё", 300)
    assert rig.chat.sent == [(not_understood_line("review"), rig.at(100)),
                             (not_understood_line("recheck"), rig.at(300, recheck))]
    assert not_understood_line("recheck") == (
        'Not understood; nothing was done. Reply like "post replies 1 3", "text 1" or "review again".')


async def test_text_sends_item_text(tmp_path):
    rig = Rig(tmp_path)
    items = {i.number: i for i in parse_report(REPORT, HEAD).items}
    await rig.reply("text 2")
    assert rig.chat.sent == [(item_text(items[2]), rig.at(100))]
    await rig.reply("text 1 4")
    assert rig.texts()[1:] == [item_text(items[1]), item_text(items[4])]


async def test_text_on_a_recheck_sends_the_drafted_reply(tmp_path):
    """A re-check message shows each reply's gist (the session's verdict line): "text N" sends what posting it does,
    where it goes and whether a resolve follows, and its full text, in parts when long."""
    rig = Rig(tmp_path)
    recheck = rig.add_job("recheck", 300)
    for text in ("text 1", "текст 3", "text 9"):
        await rig.reply(text, 300)
    assert rig.chat.sent == [(text, rig.at(300, recheck)) for text in (
        "#1 · reply in thread T1, then resolve it\n\nFixed.",
        "#3 · answer to review 456\n\nAgreed.",
        "Unknown number: 9. Nothing was done.",
    )]
    run = rig.store.run(recheck.id)
    long_body = "\n".join(f"Line {i} of a long reply." for i in range(1000))
    run.replies = [{"n": 2, "thread_id": "T2", "body": long_body, "resolve": False}]
    rig.store.save_run(run)
    await rig.reply("text 2", 300)
    parts = message_parts(f"#2 · reply in thread T2, left unresolved\n\n{long_body}", MESSAGE_LIMIT, max_parts=3)
    assert len(parts) == 3 and rig.chat.sent[3:] == [(part, rig.at(300, recheck)) for part in parts]
    assert rig.posting.calls == [] and rig.parser.calls == []


async def test_edit_queues_small_job(tmp_path):
    rig = Rig(tmp_path)
    await rig.reply("измени 2: shorter")
    assert rig.small.calls == [(rig.store.job(rig.job.id), "edit", 2, "shorter")]
    assert rig.chat.sent == [("Editing 2: the new text comes in a few minutes; nothing is posted.", rig.at(100))]
    await rig.reply("draft 7")
    assert rig.small.calls[1:] == [(rig.store.job(rig.job.id), "draft", 7, "")]
    assert rig.last() == "Drafting 7: the comment comes in a few minutes; nothing is posted."

    rig.failing.add("claude_login")
    await rig.reply("edit 3: shorter")
    assert rig.chat.sent[-1] == ("claude is not logged in on the host: nothing was started.", rig.at(100))
    assert len(rig.small.calls) == 2


async def test_review_again_and_skip(tmp_path):
    rig = Rig(tmp_path)
    await rig.reply("review again")
    rig.service.review_error = ValueError("already queued")
    await rig.reply("review again")
    rig.service.review_error = LookupError("no PR #1712 on the acme/app board")
    await rig.reply("review again")
    await rig.reply("skip")
    rig.service.skip_error = LookupError("no PR #1712 on the acme/app board")
    await rig.reply("skip")
    assert rig.service.calls == [("review_now", SCOPE, N)] * 3 + [("skip", SCOPE, N)] * 2
    assert rig.chat.sent == [(text, rig.at(100)) for text in (
        "Queued a review of #1712 at 8f01a2b3c.",
        "A review of #1712 at this head is already queued.",
        "#1712 is not on the board any more; nothing was done.",
        "Skipped #1712 at 8f01a2b3c: no automatic review of this head.",
        "#1712 is not on the board any more; nothing was done.",
    )]


async def test_failed_job_takes_review_again_and_skip(tmp_path):
    """The line a failed job's command about the report gets names the two commands that still run there."""
    rig = Rig(tmp_path)
    rig.store.set_job_state(rig.job.id, "failed", "the session failed", T1.isoformat())
    await rig.reply("post 1 2")
    await rig.reply("review again")
    await rig.reply("skip")
    assert rig.service.calls == [("review_now", SCOPE, N), ("skip", SCOPE, N)]
    assert [text for text, _ in rig.chat.sent] == [
        'This job ended failed: only "review again" and "skip" work here. Nothing was done.',
        "Queued a review of #1712 at 8f01a2b3c.",
        "Skipped #1712 at 8f01a2b3c: no automatic review of this head.",
    ]


async def test_line_anchors(tmp_path):
    rig = Rig(tmp_path)
    rig.store.add_anchor(CHAT, 200, "", T0.isoformat(), scope=SCOPE, number=1730)  # a large-PR line
    rig.store.add_anchor(CHAT, 201, "", T0.isoformat(), scope=SCOPE, number=0)  # a repo line
    rig.store.add_anchor(CHAT, 202, "", T0.isoformat(), scope="gone/repo", number=7)
    rig.store.add_anchor(CHAT, 203, "", T0.isoformat())  # the login line
    assert rig.replies.is_anchor(CHAT, 200) and not rig.replies.is_anchor(CHAT, 999)

    await rig.reply("review again", 200)
    await rig.reply("skip", 200)
    for text in ("post 1", "что тут?"):
        await rig.reply(text, 200)
    pr_line = {"scope": SCOPE, "number": 1730, "reply_to": 200}
    assert rig.chat.sent == [
        ("Queued a review of #1730 at 8f01a2b3c.", pr_line),
        ("Skipped #1730 at 8f01a2b3c: no automatic review of this head.", pr_line),
        ('Only "review again" and "skip" work on this line; nothing was done.', pr_line),
        ('Only "review again" and "skip" work on this line; nothing was done.', pr_line),
    ]
    assert rig.service.calls == [("review_now", SCOPE, 1730), ("skip", SCOPE, 1730)]

    for message_id in (201, 202, 203):
        await rig.reply("review again", message_id)
    assert rig.chat.sent[4:] == [
        ("This line takes no replies; nothing was done.", {"scope": SCOPE, "number": 0, "reply_to": 201}),
        ("This repo is no longer reviewed; nothing was done.", {"scope": "gone/repo", "number": 7, "reply_to": 202}),
        ("This line takes no replies; nothing was done.", {"scope": "", "number": 0, "reply_to": 203}),
    ]
    assert len(rig.service.calls) == 2
    assert rig.parser.calls == []


async def test_off_and_gone_repos(tmp_path):
    rig = Rig(tmp_path, contexts=[])
    await rig.reply("post 1 2")
    await rig.tap("rv_ok", "abc123", 100)
    assert rig.texts() == ["Reviewer mode is off on this daemon: nothing was done."] * 2

    gone = Rig(tmp_path / "gone")
    job = gone.add_job("review", 400, scope="gone/repo")
    await gone.reply("post 1 2", 400)
    assert gone.chat.sent == [("This repo is no longer reviewed; nothing was done.", gone.at(400, job))]
    assert (rig.posting.calls, gone.posting.calls) == ([], [])


async def test_posting_rules(tmp_path):
    rig = Rig(tmp_path, failing={f"forge:{SCOPE}"})
    await rig.reply("post 1 2")
    assert rig.chat.sent == [("The forge token is failing (see the dashboard): nothing posted.", rig.at(100))]
    assert rig.posting.calls == []

    rig.failing.clear()
    recheck = rig.add_job("recheck", 300)
    await rig.reply("post replies 1 3", 300)
    assert rig.posting.calls == [("post_replies", SCOPE, recheck.id, ReplyCommand("post_replies", (1, 3)))]
    assert rig.chat.sent[1:] == [("Checking before posting: post replies 1 3.", rig.at(300, recheck)),
                                 ("Posted …", rig.at(300, recheck))]


async def test_a_long_posting_result_arrives_in_parts(tmp_path):
    """A result too long for one Telegram message (30 replies, each resolved) comes in parts, every one anchored like
    the message replied to (final review M4)."""
    rig = Rig(tmp_path)
    recheck = rig.add_job("recheck", 300)
    resolved = " · resolved (reply not seen on GitHub: resolved threads are not listed)"
    url = "https://github.com/acme-org/acme-app/pull/1712#discussion_r"
    text = replies_posted_line(N, [f"{n}: replied {url}{1900000 + n}{resolved}" for n in range(1, 31)])
    rig.posting.outcome = PostOutcome("posted", text)
    await rig.reply("post replies 1 3", 300)
    parts = message_parts(text, MESSAGE_LIMIT, max_parts=3)
    assert len(parts) == 2 and "\n".join(parts) == text
    assert rig.chat.sent[1:] == [(part, rig.at(300, recheck)) for part in parts]
    await rig.reply("review again", rig.last_id())  # a reply to a part is a reply to the re-check
    assert rig.service.calls == [("review_now", SCOPE, N)]


async def test_start_marks_interrupted_posts(tmp_path):
    rig = Rig(tmp_path)
    replies = rig.router(Posting(rig.store, ledgers_dir=tmp_path / "ledgers", now=lambda: T1), [rig.ctx])
    recheck = rig.add_job("recheck", 300)
    rig.store.save_post(Post("p-cut", rig.job.id, SCOPE, N, HEAD, "review", "posting", T0.isoformat(),
                             T0.isoformat(), numbers=[1, 2], event="COMMENT"))
    rig.store.save_post(Post("p-replies", recheck.id, SCOPE, N, HEAD, "replies", "posting", T0.isoformat(),
                             T0.isoformat(), numbers=[1, 3]))
    rig.store.save_post(landed_post(rig.job, (4,)))

    await replies.start()
    assert rig.store.post("p-cut").state == "interrupted"
    assert rig.store.post("p-replies").state == "interrupted"
    assert rig.store.post("p-landed").state == "posted"
    # A re-check post takes no "anyway": the replies GitHub does not show are named again (final review M2).
    assert rig.chat.sent == [
        (interrupted_line(SCOPE, N), {"job_id": rig.job.id, "reply_to": None}),
        ('[acme/app] #1712: a post of replies 1 3 was interrupted by a restart. Check the PR, then reply'
         ' "post replies …" with the ones GitHub does not show.', {"job_id": recheck.id, "reply_to": None}),
    ]
    assert interrupted_line(SCOPE, N, "replies", [1, 3]) == rig.chat.sent[1][0]
    assert '"anyway"' in interrupted_line(SCOPE, N, "approve", []) and '"anyway"' in interrupted_line(SCOPE, N)
    await replies.start()
    assert len(rig.chat.sent) == 2


async def test_a_reply_during_shutdown_starts_nothing(tmp_path, caplog):
    """Once aclose() ran, a reply that still arrives (Telegram polling stops after it) starts no task: nothing would
    wait for it or cancel it (/code-review C12)."""
    rig = Rig(tmp_path, parser=[EVERY_BUT_3])
    await rig.replies.aclose()
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.replies"):
        await rig.reply("post 1 2")
        await rig.reply("everything but 3")  # the LLM fallback runs in a task too
    assert rig.posting.calls == [] and rig.parser.calls == []
    assert caplog.text.count("Reviewer reply not acted on: shutting down") == 2


async def test_handle_reply_never_raises(tmp_path):
    rig = Rig(tmp_path)
    rig.posting.error = RuntimeError("ghp_not_real leaked")
    await rig.reply("post 1 2")
    assert rig.chat.sent == [("Checking before posting: post 1 2 + COMMENT at 26c4929c5.", rig.at(100)),
                             ("Internal error (RuntimeError); nothing was done.", rig.at(100))]
    rig.service.review_error = RuntimeError("ghp_not_real leaked")
    await rig.reply("review again")
    assert rig.chat.sent[-1] == ("Internal error (RuntimeError); nothing was done.", rig.at(100))
    assert not any("ghp_not_real" in text for text in rig.texts())


# --- Address jobs ---

PATCH = "diff --git a/a.kt b/a.kt\n--- a/a.kt\n+++ b/a.kt\n@@ -1 +1 @@\n-old\n+new"

def address_rig(tmp_path, patch=PATCH, **kw):
    """The rig plus a done address job of PR 1712 at HEAD, its message anchored at (CHAT, 200), its copy holding
    `patch` as fix.patch (None: the session changed no code)."""
    rig = Rig(tmp_path, **kw)
    rig.review, rig.job = rig.job, rig.add_job("address", 200)
    run = rig.store.run(rig.job.id)
    if patch is not None:
        (tmp_path / "rv" / "jobs" / rig.job.id / "fix.patch").write_text(patch, encoding="utf-8")
        run.fix = {"files": [{"path": "a.kt", "added": 1, "deleted": 1}], "protected": [], "bytes": len(patch)}
        rig.store.save_run(run)
    return rig

async def test_diff_sends_the_patch(tmp_path):
    rig = address_rig(tmp_path)
    await rig.reply("diff", 200)
    assert rig.chat.sent == [(PATCH, rig.at(200))]

async def test_diff_is_cut_after_three_parts(tmp_path):
    patch = "".join(f"+line {i:04d} " + "x" * 88 + "\n" for i in range(200))   # 20 000 characters
    rig = address_rig(tmp_path, patch=patch)
    await rig.reply("патч", 200)
    assert len(rig.chat.sent) == 3 and all(len(text) <= MESSAGE_LIMIT for text in rig.texts())
    assert rig.texts()[0].startswith("+line 0000 ")
    assert rig.last().endswith("\nPatch cut: the dashboard shows all of it.")

async def test_diff_without_a_patch(tmp_path):
    rig = address_rig(tmp_path, patch=None)
    await rig.reply("diff", 200)
    assert rig.chat.sent == [("This job changed no code: there is no patch.", rig.at(200))]

async def test_text_shows_an_address_reply(tmp_path):
    rig = address_rig(tmp_path)
    await rig.reply("text 1", 200)
    assert rig.last() == "#1 · reply in thread T1, then resolve it\n\nFixed."

async def test_again_queues_with_the_instruction(tmp_path):
    rig = address_rig(tmp_path)
    await rig.reply("again: leave 2 as it is", 200)
    assert rig.service.calls == [("address_again", SCOPE, N, "leave 2 as it is")]
    assert rig.chat.sent == [(f"Queued a new run on #{N} at {HEAD[:9]}.", rig.at(200))]

async def test_again_answers_why_nothing_was_queued(tmp_path):
    rig = address_rig(tmp_path)
    for error, text in ((ValueError("already queued"), f"A run on #{N} is already queued."),
                        (LookupError("no address turn"),
                         f"#{N} has no threads waiting on you any more; nothing was done.")):
        rig.service.again_error = error
        await rig.reply("заново", 200)
        assert rig.last() == text
    rig = address_rig(tmp_path / "login", failing=("claude_login",))
    await rig.reply("again", 200)
    assert rig.last() == "claude is not logged in on the host: nothing was started." and rig.service.calls == []

async def test_push_runs_the_push_and_answers_its_outcome(tmp_path):
    rig = address_rig(tmp_path)
    rig.posting.outcome = PostOutcome("posted", "Pushed …")
    await rig.reply("push", 200)
    assert rig.posting.calls == [("push_fix", SCOPE, rig.job.id, None)]
    assert rig.chat.sent == [("Checking before pushing.", rig.at(200)), ("Pushed …", rig.at(200))]

def protect(rig):
    run = rig.store.run(rig.job.id)
    run.fix = {**run.fix, "protected": [".github/workflows/ci.yml"]}
    rig.store.save_run(run)

async def test_push_anyway_pushes_a_protected_fix(tmp_path):
    rig = address_rig(tmp_path)
    protect(rig)
    rig.posting.outcome = PostOutcome("posted", "Pushed …")
    await rig.reply("push anyway", 200)
    assert rig.posting.calls == [("push_fix", SCOPE, rig.job.id, {"anyway": True, "then_post": False})]
    assert rig.chat.sent == [("Checking before pushing.", rig.at(200)), ("Pushed …", rig.at(200))]

PUSH_THEN_POST = "Checking before pushing; once the push lands, every drafted reply is posted."

async def test_push_and_post_pushes_then_posts_every_drafted_reply(tmp_path):
    rig = address_rig(tmp_path)
    rig.posting.outcomes = {"push_fix": PostOutcome("posted", "Pushed …")}
    await rig.reply("push and post", 200)
    assert rig.posting.calls == [("push_fix", SCOPE, rig.job.id, {"anyway": False, "then_post": True}),
                                 ("post_replies", SCOPE, rig.job.id, ReplyCommand("post_replies", (1, 3)))]
    assert rig.chat.sent == [(PUSH_THEN_POST, rig.at(200)), ("Pushed …", rig.at(200)),
                             ("Checking before posting: post replies 1 3.", rig.at(200)), ("Posted …", rig.at(200))]

async def test_push_and_post_posts_nothing_when_the_push_does_not_land(tmp_path):
    rig = address_rig(tmp_path)
    protect(rig)
    for outcome in (PostOutcome("refused", "The fix touches protected files …"), PostOutcome("unverified", "Check …")):
        rig.posting.calls, rig.chat.sent = [], []
        rig.posting.outcomes = {"push_fix": outcome}
        await rig.reply("push and post", 200)
        assert [call[0] for call in rig.posting.calls] == ["push_fix"]
        assert rig.texts() == [PUSH_THEN_POST, outcome.text]

async def test_push_and_post_after_the_push_names_what_is_left(tmp_path):
    rig = address_rig(tmp_path)
    rig.store.save_post(Post("r1", rig.job.id, SCOPE, N, HEAD, "replies", "posted", T0.isoformat(), T0.isoformat(),
                             numbers=[1, 3], landed=[{"n": n, "path": "", "line": None, "title": "", "where": "reply"}
                                                     for n in (1, 3)]))
    rig.posting.outcomes = {"push_fix": PostOutcome("posted", "Pushed …")}
    await rig.reply("push and post anyway", 200)
    assert [call[0] for call in rig.posting.calls] == ["push_fix"]
    assert rig.last() == "Nothing left to post: every drafted reply is already posted at this head. Nothing was done."

async def test_post_all_posts_every_drafted_reply_not_posted(tmp_path):
    rig = address_rig(tmp_path)
    rig.store.save_post(Post("r1", rig.job.id, SCOPE, N, HEAD, "replies", "posted", T0.isoformat(), T0.isoformat(),
                             numbers=[1], landed=[{"n": 1, "path": "", "line": None, "title": "", "where": "reply"}]))
    await rig.reply("post all", 200)
    assert rig.posting.calls == [("post_replies", SCOPE, rig.job.id, ReplyCommand("post_replies", (3,)))]
    assert rig.chat.sent[0] == ("Checking before posting: post replies 3.", rig.at(200))

# --- Buttons of a fix message ---

async def test_push_button_runs_the_push_on_its_message(tmp_path):
    rig = address_rig(tmp_path)
    rig.posting.outcome = PostOutcome("posted", "Pushed …")
    await rig.tap("rv_push", rig.job.id, 200)
    assert rig.posting.calls == [("push_fix", SCOPE, rig.job.id, None)]
    assert rig.chat.sent == [("Checking before pushing.", rig.at(200)), ("Pushed …", rig.at(200))]

async def test_push_and_reply_all_button_pushes_then_posts(tmp_path):
    rig = address_rig(tmp_path)
    rig.posting.outcomes = {"push_fix": PostOutcome("posted", "Pushed …")}
    await rig.tap("rv_pushpost", rig.job.id, 200)
    assert [(call[0], call[3]) for call in rig.posting.calls] == [
        ("push_fix", {"anyway": False, "then_post": True}), ("post_replies", ReplyCommand("post_replies", (1, 3)))]
    assert rig.texts() == [PUSH_THEN_POST, "Pushed …", "Checking before posting: post replies 1 3.", "Posted …"]

async def test_reply_all_and_diff_buttons(tmp_path):
    rig = address_rig(tmp_path)
    await rig.tap("rv_post", rig.job.id, 200)
    assert rig.posting.calls == [("post_replies", SCOPE, rig.job.id, ReplyCommand("post_replies", (1, 3)))]
    await rig.tap("rv_diff", rig.job.id, 200)
    assert rig.chat.sent[-1] == (PATCH, rig.at(200))

async def test_a_button_runs_nothing_on_another_jobs_message(tmp_path, caplog):
    rig = address_rig(tmp_path)
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.replies"):
        await rig.tap("rv_push", rig.job.id, 100)     # the review's message, not the threads message
        await rig.tap("rv_push", rig.job.id, 999)     # no message of the reviewer's
    assert rig.chat.sent == [] and rig.posting.calls == []
    assert caplog.text.count("not its job's: nothing was done") == 2

PUSH_ANYWAY_ECHO = (f"Push the fix with protected files (.github/workflows/ci.yml) to #{N}, then post every drafted "
                    "reply?")

async def test_push_anyway_button_asks_first_and_yes_pushes_and_posts(tmp_path):
    rig = address_rig(tmp_path)
    protect(rig)
    rig.posting.outcomes = {"push_fix": PostOutcome("posted", "Pushed …")}
    await rig.tap("rv_pushx", rig.job.id, 200)
    assert rig.posting.calls == [] and rig.chat.sent[0][0] == PUSH_ANYWAY_ECHO
    conf_id = echo_id(rig.chat.sent[0][1])
    await rig.tap("rv_ok", conf_id, rig.last_id())
    assert [(call[0], call[3]) for call in rig.posting.calls] == [
        ("push_fix", {"anyway": True, "then_post": True}), ("post_replies", ReplyCommand("post_replies", (1, 3)))]

async def test_push_anyway_button_no_pushes_nothing(tmp_path):
    rig = address_rig(tmp_path)
    protect(rig)
    await rig.tap("rv_pushx", rig.job.id, 200)
    await rig.tap("rv_no", echo_id(rig.chat.sent[0][1]), rig.last_id())
    assert rig.posting.calls == [] and rig.last() == "Cancelled: nothing was done."

async def test_push_anyway_button_on_a_findings_message_asks_for_the_push_alone(tmp_path):
    rig = findings_rig(tmp_path)
    protect(rig)
    await rig.tap("rv_pushx", rig.job.id, 300)
    assert rig.chat.sent[0][0] == f"Push the fix with protected files (.github/workflows/ci.yml) to #{N}?"
    await rig.tap("rv_ok", echo_id(rig.chat.sent[0][1]), rig.last_id())
    assert rig.posting.calls == [("push_fix", SCOPE, rig.job.id, {"anyway": True, "then_post": False})]

async def test_push_refused_while_the_forge_token_fails(tmp_path):
    rig = address_rig(tmp_path, failing=("forge:acme/app",))
    await rig.reply("push", 200)
    assert rig.chat.sent == [("The forge token is failing (see the dashboard): nothing posted.", rig.at(200))]
    assert rig.posting.calls == []

async def test_free_text_on_an_address_message_is_not_parsed_by_the_model(tmp_path):
    rig = address_rig(tmp_path, parser=(ReplyCommand("push"),))
    await rig.reply("давай пуш уже", 200)
    assert rig.chat.sent == [(not_understood_line("address"), rig.at(200))]
    assert rig.parser.calls == [] and rig.posting.calls == []

async def test_again_works_on_a_failed_address_job_and_not_on_a_review(tmp_path):
    rig = address_rig(tmp_path)
    rig.store.set_job_state(rig.job.id, "failed", "the session failed", T1.isoformat())
    await rig.reply("again", 200)
    assert rig.service.calls == [("address_again", SCOPE, N, "")]
    rig.store.set_job_state(rig.job.id, "superseded", "new address", T1.isoformat())
    await rig.reply("again", 200)
    assert rig.last() == "Outdated: there is a newer review. Nothing was done." and len(rig.service.calls) == 1
    await rig.reply("again", 100)   # the review's message
    assert rig.chat.sent[-1] == ('"again" works on a fix message of your own PR (threads or self-review findings). Nothing was done.',
                                 rig.at(100, rig.review))
    for text in ("push", "diff"):
        await rig.reply(text, 100)
        assert rig.last() == f'"{text}" works on a fix message of your own PR (threads or self-review findings). Nothing was done.'
    assert len(rig.service.calls) == 1 and rig.posting.calls == []

async def test_push_and_diff_need_a_done_address_job(tmp_path):
    rig = address_rig(tmp_path)
    rig.store.set_job_state(rig.job.id, "failed", "the session failed", T1.isoformat())
    await rig.reply("push", 200)
    assert rig.posting.calls == [] and rig.last().startswith("This job ended failed")


async def test_again_on_a_threads_message_while_a_post_is_under_way(tmp_path):
    rig = address_rig(tmp_path)
    rig.service.again_error = ValueError("a post is under way")
    await rig.reply("again", 200)
    assert rig.last() == f"A push or a post of a fix of #{N} is under way; try again when it ends. Nothing was done."


# --- Findings jobs ---

def findings_rig(tmp_path, patch=PATCH, **kw):
    """The rig plus a done findings job of PR 1712 at HEAD, its message anchored at (CHAT, 300), its copy holding
    `patch` as fix.patch (None: the session changed no code)."""
    rig = Rig(tmp_path, **kw)
    rig.review, rig.job = rig.job, rig.add_job("findings", 300)
    if patch is not None:
        (tmp_path / "rv" / "jobs" / rig.job.id / "fix.patch").write_text(patch, encoding="utf-8")
        run = rig.store.run(rig.job.id)
        run.fix = {"files": [{"path": "a.kt", "added": 1, "deleted": 1}], "protected": [], "bytes": len(patch)}
        rig.store.save_run(run)
    return rig

FIXING = f'Fixing the findings of #{N} at {HEAD[:9]}: the result comes here; nothing is pushed until you reply "push".'

async def test_fix_on_a_self_review_message_queues_a_findings_job(tmp_path):
    rig = Rig(tmp_path)
    for text in ("fix", "пофикси"):
        await rig.reply(text, 100)
        assert rig.chat.sent[-1] == (FIXING, rig.at(100))
    assert rig.service.calls == [("fix_findings", SCOPE, N, rig.job.id, None)] * 2

async def test_fix_answers_each_refusal(tmp_path):
    rig = Rig(tmp_path)
    for error, text in (
        (ValueError("already queued"), f"A run on #{N} is already queued."),
        (ValueError("a post is under way"),
         f"A push or a post of a fix of #{N} is under way; try again when it ends. Nothing was done."),
        (ValueError("not the newest self-review"),
         f"This is not the newest self-review of #{N} at its head. Nothing was done."),
        (ValueError("not fixable: no confirmed findings"),
         f"Cannot fix the findings of #{N}: no confirmed findings. Nothing was done."),
        (ValueError("not fixable: not your PR"), f"Cannot fix the findings of #{N}: not your PR. Nothing was done."),
        (LookupError("no PR"), f"#{N} is not on the board any more; nothing was done."),
    ):
        rig.service.fix_error = error
        await rig.reply("fix", 100)
        assert rig.last() == text

async def test_fix_needs_the_claude_login(tmp_path):
    rig = Rig(tmp_path, failing=("claude_login",))
    await rig.reply("fix", 100)
    assert rig.last() == "claude is not logged in on the host: nothing was started." and rig.service.calls == []

async def test_fix_on_an_outdated_review_message(tmp_path):
    rig = Rig(tmp_path)
    rig.store.set_job_state(rig.job.id, "superseded", "new review", T1.isoformat())
    await rig.reply("fix", 100)
    assert rig.last().startswith("Outdated") and rig.service.calls == []

async def test_fix_is_refused_on_other_messages(tmp_path):
    rig = findings_rig(tmp_path)
    rig.add_job("address", 200)
    for message_id in (200, 300):
        await rig.reply("fix", message_id)
        assert rig.last() == '"fix" works on the message of a self-review of your own PR. Nothing was done.'
    assert rig.service.calls == []

async def test_again_on_a_findings_message_queues_with_the_instruction(tmp_path):
    rig = findings_rig(tmp_path)
    await rig.reply("again: leave 3", 300)
    assert rig.chat.sent[-1] == (FIXING, rig.at(300))
    rig.store.set_job_state(rig.job.id, "failed", "no verdicts written", T1.isoformat())
    await rig.reply("заново", 300)
    assert rig.service.calls == [("fix_findings", SCOPE, N, None, "leave 3"), ("fix_findings", SCOPE, N, None, "")]
    rig.store.set_job_state(rig.job.id, "superseded", "new findings fix", T1.isoformat())
    await rig.reply("again", 300)
    assert rig.last().startswith("Outdated") and len(rig.service.calls) == 2

async def test_again_on_a_findings_message_needs_the_claude_login(tmp_path):
    rig = findings_rig(tmp_path, failing=("claude_login",))
    await rig.reply("again", 300)
    assert rig.last() == "claude is not logged in on the host: nothing was started." and rig.service.calls == []

async def test_push_and_diff_work_on_a_findings_job(tmp_path):
    rig = findings_rig(tmp_path)
    await rig.reply("diff", 300)
    assert rig.chat.sent == [(PATCH, rig.at(300))]
    rig.posting.outcome = PostOutcome("posted", "Pushed …")
    await rig.reply("push", 300)
    assert rig.posting.calls == [("push_fix", SCOPE, rig.job.id, None)]
    assert rig.chat.sent[-2:] == [("Checking before pushing.", rig.at(300)), ("Pushed …", rig.at(300))]

async def test_a_findings_message_refuses_post_text_edit_and_draft(tmp_path):
    rig = findings_rig(tmp_path)
    for text in ("post 1", "approve", "post replies 1", "text 1", "edit 1: shorter", "draft 1"):
        await rig.reply(text, 300)
        assert rig.last() == 'This is a findings message: reply "push", "diff" or "again: …". Nothing was done.'
    assert rig.posting.calls == [] and rig.small.calls == []

async def test_free_text_on_a_findings_message_is_not_parsed_by_the_model(tmp_path):
    rig = findings_rig(tmp_path, parser=(ReplyCommand("push"),))
    await rig.reply("давай пуш уже", 300)
    assert rig.chat.sent == [(not_understood_line("findings"), rig.at(300))]
    assert rig.parser.calls == [] and rig.posting.calls == []


# --- What the dashboard did, told in Telegram ---

COMMIT_URL = "https://github.com/acme/app/commit/abc"


def recorded_refreshes(rig):
    """The job ids whose buttons announce_dashboard refreshes (refresh_buttons has its own tests)."""
    refreshed = []

    async def refresh(job_id):
        refreshed.append(job_id)

    rig.replies.refresh_buttons = refresh
    return refreshed


def save_post(rig, post_id, at, *, job=None, state="posted", urls=(COMMIT_URL,)):
    rig.store.save_post(Post(post_id, (job or rig.job).id, SCOPE, N, HEAD, "push", state, at.isoformat(),
                             urls=list(urls)))


async def test_announce_dashboard_replies_to_the_job_message_in_its_language(tmp_path):
    rig = address_rig(tmp_path, language="uk")
    refreshed = recorded_refreshes(rig)
    save_post(rig, "p-push", T1)
    await rig.replies.announce_dashboard(rig.job.id, ReplyCommand("push"), "posted")
    assert rig.chat.sent == [("🖥 Дашборд: push — виконано https://github.com/acme/app/commit/abc", rig.at(200))]
    assert refreshed == [rig.job.id]


@pytest.mark.parametrize("state, outcome", [("posted", "done"), ("partial", "partly done")])
async def test_announce_dashboard_links_the_post_that_landed(tmp_path, state, outcome):
    """A post that landed fully or partly links the Post Posting saved for it (posted or partial): the job's newest
    of either state, whatever an earlier post of the job or a newer one of another job links."""
    rig = address_rig(tmp_path)
    recorded_refreshes(rig)
    run = rig.store.run(rig.job.id)
    run.message_ids = [200, 201]
    rig.store.save_run(run)
    save_post(rig, "p-push", T0, urls=(COMMIT_URL,))
    save_post(rig, "p-replies", T1, state=state,
              urls=("https://github.com/acme/app/pull/1712#discussion_r1", "https://x.test/second"))
    save_post(rig, "p-other-job", T1 + timedelta(minutes=2), job=rig.review, urls=("https://x.test/review",))
    await rig.replies.announce_dashboard(rig.job.id, ReplyCommand("post_replies", (1, 3)), state)
    assert rig.chat.sent == [(f"🖥 Dashboard: post replies 1 3 — {outcome} "
                              "https://github.com/acme/app/pull/1712#discussion_r1", rig.at(201))]


@pytest.mark.parametrize("state, outcome", [
    ("failed", "failed"), ("unverified", "not verified"), ("nothing", "nothing to do"), ("queued", "queued"),
])
async def test_announce_dashboard_links_nothing_when_nothing_landed(tmp_path, state, outcome):
    """No link from an earlier post of the job, nor from the failed or unverified Post this one saved."""
    rig = address_rig(tmp_path)
    recorded_refreshes(rig)
    save_post(rig, "p-push", T0, urls=(COMMIT_URL,))
    save_post(rig, "p-this", T1, state=state if state in ("failed", "unverified") else "failed",
              urls=("https://x.test/shown",))
    await rig.replies.announce_dashboard(rig.job.id, ReplyCommand("push"), state)
    assert rig.chat.sent == [(f"🖥 Dashboard: push — {outcome}", rig.at(200))]


@pytest.mark.parametrize("state, outcome", [("posted", "done"), ("partial", "partly done")])
async def test_announce_dashboard_links_nothing_when_the_post_has_no_link(tmp_path, state, outcome):
    """The newest landed Post of the job has no url: no link, and no earlier post's either."""
    rig = address_rig(tmp_path)
    recorded_refreshes(rig)
    save_post(rig, "p-push", T0, urls=(COMMIT_URL,))
    save_post(rig, "p-this", T1, state=state, urls=())
    await rig.replies.announce_dashboard(rig.job.id, ReplyCommand("push"), state)
    assert rig.chat.sent == [(f"🖥 Dashboard: push — {outcome}", rig.at(200))]


async def test_announce_dashboard_without_a_message_still_refreshes(tmp_path):
    rig = address_rig(tmp_path)
    refreshed = recorded_refreshes(rig)
    run = rig.store.run(rig.job.id)
    run.message_ids = []
    rig.store.save_run(run)
    await rig.replies.announce_dashboard(rig.job.id, ReplyCommand("push"), "posted")
    assert rig.chat.sent == [] and refreshed == [rig.job.id]

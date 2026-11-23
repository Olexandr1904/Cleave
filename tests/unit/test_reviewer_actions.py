# tests/unit/test_reviewer_actions.py
from __future__ import annotations

from datetime import timedelta

from orchestrator.reviewer.actions import CONFIRM_TTL, ReviewerActions
from orchestrator.reviewer.grammar import ReplyCommand
from orchestrator.reviewer.lines import not_understood_line
from orchestrator.reviewer.models import Post
from orchestrator.reviewer.posting import PostOutcome
from orchestrator.reviewer.replies import ReviewerReplies
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.report_edit import item_text
from tests.unit.test_reviewer_replies import (
    CHAT, EVERY_BUT_3, NEW_HEAD, SCOPE, T1, FakePosting, Rig, address_rig, confirmations, echo_id, findings_rig,
    protect,
)
from tests.unit.test_reviewer_report import HEAD, REPORT


async def _append(said, text):
    said.append(text)


def actions_of(rig):
    return rig.replies.actions


def supersede(rig, job):
    """The job superseded by a review of a newer head, and read again as the rig's job."""
    rig.store.set_job_state(job.id, "superseded", "new head", T1.isoformat())
    rig.job = rig.store.job(job.id)


def outdated_prefix():
    return "Outdated: "


async def test_run_says_each_text_in_order_and_returns_them(tmp_path):
    rig = Rig(tmp_path)
    said = []
    actions = rig.replies.actions
    cmd = actions.check(rig.job, ReplyCommand("post", numbers=[1]))
    result = await actions.run(rig.job, cmd, say=lambda t: _append(said, t))
    assert result.state == "posted" and result.texts == said == [rig.posting.outcome.text]
    assert result.command == cmd and rig.posting.calls == [("post_review", SCOPE, rig.job.id, cmd)]
    assert rig.chat.sent == []   # the layer sends nothing itself


async def test_a_refusal_is_a_string_from_check(tmp_path):
    rig = Rig(tmp_path)
    supersede(rig, rig.job)
    assert actions_of(rig).check(rig.job, ReplyCommand("post", numbers=[1])).startswith(outdated_prefix())


async def test_check_refuses_what_a_failing_credential_stops(tmp_path):
    """The credential a command needs is checked with it, so a caller refuses before any progress line."""
    rig = Rig(tmp_path, failing={f"forge:{SCOPE}", "claude_login"})
    actions = actions_of(rig)
    assert actions.check(rig.job, ReplyCommand("post", (1,))) == (
        "The forge token is failing (see the dashboard): nothing posted.")
    assert actions.check(rig.job, ReplyCommand("draft", (7,))) == (
        "claude is not logged in on the host: nothing was started.")
    assert actions.check(rig.job, ReplyCommand("text", (2,))) == ReplyCommand("text", (2,))


async def test_check_refuses_a_job_of_a_repo_no_longer_reviewed(tmp_path):
    rig = Rig(tmp_path)
    job = rig.add_job("review", 400, scope="gone/repo")
    assert actions_of(rig).check(job, ReplyCommand("post", (1,))) == (
        "This repo is no longer reviewed; nothing was done.")


async def test_push_and_post_says_the_push_outcome_before_the_post(tmp_path):
    """The post's state when it ran; the push's when it did not."""
    rig = address_rig(tmp_path)
    rig.posting.outcomes = {"push_fix": PostOutcome("posted", "Pushed …"),
                            "post_replies": PostOutcome("partial", "Posted 1, not 3 …")}
    actions, said = actions_of(rig), []
    cmd = actions.check(rig.job, ReplyCommand("push_post"))
    result = await actions.run(rig.job, cmd, say=lambda t: _append(said, t))
    assert said == result.texts == ["Pushed …", "Checking before posting: post replies 1 3.", "Posted 1, not 3 …"]
    assert result.state == "partial" and [call[0] for call in rig.posting.calls] == ["push_fix", "post_replies"]

    rig.posting.calls, rig.posting.outcomes = [], {"push_fix": PostOutcome("refused", "The fix touches …")}
    result = await actions.run(rig.job, cmd)   # no say: the texts are only collected
    assert (result.state, result.texts) == ("refused", ["The fix touches …"])
    assert [call[0] for call in rig.posting.calls] == ["push_fix"]


async def test_the_states_of_what_shows_or_queues(tmp_path):
    rig = Rig(tmp_path)
    actions = actions_of(rig)
    item = {i.number: i for i in parse_report(REPORT, HEAD).items}[2]
    shown = await actions.run(rig.job, actions.check(rig.job, ReplyCommand("text", (2,))))
    assert (shown.state, shown.texts) == ("shown", [item_text(item)])
    queued = await actions.run(rig.job, actions.check(rig.job, ReplyCommand("review_again")))
    assert (queued.state, queued.texts) == ("queued", [f"Queued a review of #1712 at {NEW_HEAD[:9]}."])
    rig.service.review_error = ValueError("already queued")
    refused = await actions.run(rig.job, actions.check(rig.job, ReplyCommand("review_again")))
    assert (refused.state, refused.texts) == ("refused", ["A review of #1712 at this head is already queued."])
    drafted = await actions.run(rig.job, actions.check(rig.job, ReplyCommand("draft", (7,))))
    assert drafted.state == "queued" and len(rig.small.calls) == 1


async def test_understand_returns_the_checked_command_and_stores_no_confirmation(tmp_path):
    rig = Rig(tmp_path, parser=[EVERY_BUT_3, None])
    actions = actions_of(rig)
    assert await actions.understand(rig.job, "пости всё кроме 3") == EVERY_BUT_3
    assert await actions.understand(rig.job, "что это?") == not_understood_line("review")
    assert confirmations(rig) == 0 and rig.chat.sent == [] and rig.posting.calls == []


async def test_replies_given_actions_use_them(tmp_path):                        # Review Focus 1
    rig = Rig(tmp_path)
    shared = ReviewerActions([rig.ctx], rig.store, rig.service, rig.posting, rig.small, rig.parser,
                             credential_failing=rig.failing.__contains__)
    other = FakePosting()   # never used: the replies post through the shared layer's Posting
    replies = ReviewerReplies([rig.ctx], rig.store, rig.service, rig.chat, other, rig.small, rig.parser,
                              credential_failing=rig.failing.__contains__, actions=shared)
    assert replies.actions is shared and replies.actions.posting is rig.posting
    await replies.handle_reply(CHAT, 100, "post 1 2")
    await replies.wait_idle()
    assert [call[0] for call in rig.posting.calls] == ["post_review"] and other.calls == []


FORGE_FAILING = "The forge token is failing (see the dashboard): nothing posted."
NO_LOGIN_PARSER = "claude is not logged in on the host: only exact replies work now; nothing was done."


async def test_push_anyway_tap_is_refused_at_once_while_the_forge_token_fails(tmp_path):   # Ruling 8
    """No Yes / No echo for a push the forge token cannot make: the refusal comes at the tap."""
    rig = address_rig(tmp_path, failing={f"forge:{SCOPE}"})
    protect(rig)
    await rig.tap("rv_pushx", rig.job.id, 200)
    assert rig.chat.sent == [(FORGE_FAILING, rig.at(200))]   # no buttons
    assert confirmations(rig) == 0 and rig.posting.calls == []


async def test_free_text_goes_to_the_model_only_on_a_done_review_or_recheck(tmp_path):   # Ruling 9
    rig = Rig(tmp_path, parser=[])
    actions = actions_of(rig)
    assert actions.free_text_refusal(rig.job) is None
    recheck = rig.add_job("recheck", 300)
    assert actions.free_text_refusal(recheck) is None
    rig.store.set_job_state(rig.job.id, "failed", "the session failed", T1.isoformat())
    assert actions.free_text_refusal(rig.store.job(rig.job.id)) == not_understood_line("review")
    for fix in (address_rig(tmp_path / "address"), findings_rig(tmp_path / "findings")):
        assert actions_of(fix).free_text_refusal(fix.job) == not_understood_line(fix.job.kind)


async def test_free_text_refusals_keep_their_order(tmp_path):
    """No parser, then the job, then the claude login: the first that refuses is answered."""
    no_parser = Rig(tmp_path / "none", parser=None, failing={"claude_login"})
    assert actions_of(no_parser).free_text_refusal(no_parser.job) == not_understood_line("review")
    address = address_rig(tmp_path / "address", failing={"claude_login"})
    assert actions_of(address).free_text_refusal(address.job) == not_understood_line("address")
    login = Rig(tmp_path / "login", failing={"claude_login"})
    assert actions_of(login).free_text_refusal(login.job) == NO_LOGIN_PARSER


async def test_understand_answers_the_refusal_without_a_parser(tmp_path):
    rig = Rig(tmp_path, parser=None)
    assert await actions_of(rig).understand(rig.job, "пости всё кроме 3") == not_understood_line("review")
    login = Rig(tmp_path / "login", parser=[EVERY_BUT_3], failing={"claude_login"})
    assert await actions_of(login).understand(login.job, "пости всё кроме 3") == NO_LOGIN_PARSER
    assert login.parser.calls == []


class FakeNotifier:
    """Records (chat id, message id, labels) per set_buttons; raises `error` when set."""

    def __init__(self):
        self.buttons_set, self.error = [], None

    async def set_buttons(self, chat_id, message_id, buttons):
        if self.error:
            raise self.error
        self.buttons_set.append((chat_id, message_id, [b.label for b in buttons]))


def with_notifier(rig):
    rig.chat.notifier, rig.chat.chat_id = FakeNotifier(), CHAT
    return rig.chat.notifier


def land_push_on_run(rig, job):
    """The fake posting records the push in the store, as the real one does."""
    original = rig.posting.push_fix

    async def push_fix(ctx, job_, anyway=False, then_post=False):
        rig.store.save_post(Post("p-push", job_.id, SCOPE, job_.number, job_.head_sha, "push", "posted",
                                 T1.isoformat()))
        return await original(ctx, job_, anyway=anyway, then_post=then_post)

    rig.posting.push_fix = push_fix


async def test_a_telegram_push_refreshes_the_fix_message_buttons(tmp_path):
    rig = address_rig(tmp_path)
    notifier = with_notifier(rig)
    land_push_on_run(rig, rig.job)
    await rig.tap("rv_push", rig.job.id, 200)
    assert notifier.buttons_set == [(CHAT, 200, ["Reply all", "Diff"])]


async def test_a_refused_push_refreshes_nothing(tmp_path):
    rig = address_rig(tmp_path)
    notifier = with_notifier(rig)
    rig.posting.outcome = PostOutcome("refused", "Nope")
    await rig.tap("rv_push", rig.job.id, 200)
    assert notifier.buttons_set == []


async def test_diff_refreshes_nothing(tmp_path):
    rig = address_rig(tmp_path)
    notifier = with_notifier(rig)
    await rig.tap("rv_diff", rig.job.id, 200)
    assert notifier.buttons_set == []


async def test_every_reply_posted_removes_reply_all(tmp_path):
    rig = address_rig(tmp_path)
    notifier = with_notifier(rig)
    for n in (1, 3):
        rig.store.save_post(Post(f"p{n}", rig.job.id, SCOPE, rig.job.number, HEAD, "replies", "posted", T1.isoformat(),
                                 numbers=[n], landed=[{"n": n, "path": "", "line": None, "title": "",
                                                       "where": "reply"}]))
    await rig.replies.refresh_buttons(rig.job.id)
    assert notifier.buttons_set == [(CHAT, 200, ["Push", "Diff"])]


async def test_refresh_is_for_the_last_part_and_never_raises(tmp_path):
    rig = address_rig(tmp_path)
    notifier = with_notifier(rig)
    run = rig.store.run(rig.job.id)
    run.message_ids = [200, 201]
    rig.store.save_run(run)
    await rig.replies.refresh_buttons(rig.job.id)
    assert notifier.buttons_set[-1][1] == 201
    notifier.error = RuntimeError("telegram down")
    await rig.replies.refresh_buttons(rig.job.id)           # logged, not raised
    await rig.replies.refresh_buttons("no-such-job")
    assert rig.replies._chat is rig.chat
    rig.replies._chat = None
    await rig.replies.refresh_buttons(rig.job.id)


async def test_a_job_no_longer_postable_has_its_buttons_removed(tmp_path):
    """A superseded or stale fix's message stops offering Push: none of its buttons would run."""
    for state in ("superseded", "stale"):
        rig = address_rig(tmp_path / state)
        notifier = with_notifier(rig)
        rig.store.set_job_state(rig.job.id, state, "new head", T1.isoformat())
        await rig.replies.refresh_buttons(rig.job.id)
        assert notifier.buttons_set == [(CHAT, 200, [])], state


async def test_a_review_job_has_no_buttons_to_refresh(tmp_path):
    rig = Rig(tmp_path)
    notifier = with_notifier(rig)
    await rig.replies.refresh_buttons(rig.job.id)
    assert notifier.buttons_set == []


# --- The Yes or No on a confirmation: one gate for both channels ---

HANDLED = "Already handled."


def ask(rig, cmd=EVERY_BUT_3):
    """A pending confirmation of `cmd` on the rig's job, asked at T1 in the rig's chat."""
    return rig.store.add_confirmation(rig.job.id, CHAT, cmd.to_dict(), T1.isoformat())


async def test_claim_answers_a_no_a_second_answer_and_an_expired_yes(tmp_path):
    rig = Rig(tmp_path)
    actions = actions_of(rig)
    declined = ask(rig)
    assert actions.claim(declined.id, False, T1) == "Cancelled: nothing was done."
    assert actions.claim(declined.id, False, T1) == HANDLED
    assert actions.claim(declined.id, True, T1) == HANDLED
    assert actions.claim("no-such-confirmation", True, T1) == HANDLED
    expired = ask(rig)
    assert actions.claim(expired.id, True, T1 + CONFIRM_TTL + timedelta(seconds=1)) == (
        "Expired: reply again; nothing was done.")
    assert actions.claim(expired.id, False, T1) == HANDLED   # spent: never "Cancelled" after the Yes
    assert rig.posting.calls == [] and CONFIRM_TTL == timedelta(minutes=15)


async def test_claim_returns_the_command_checked_against_the_job_as_it_is_now(tmp_path):
    rig = Rig(tmp_path)
    actions = actions_of(rig)
    assert actions.claim(ask(rig).id, True, T1 + CONFIRM_TTL) == EVERY_BUT_3   # nothing runs from here
    superseded = ask(rig)
    supersede(rig, rig.job)
    assert actions.claim(superseded.id, True, T1).startswith(outdated_prefix())
    assert actions.claim(superseded.id, True, T1) == HANDLED   # a refused Yes is spent too
    rig.store.set_job_state(rig.job.id, "done", "", T1.isoformat())
    rig.failing.add(f"forge:{SCOPE}")
    assert actions.claim(ask(rig).id, True, T1) == FORGE_FAILING
    assert rig.posting.calls == []


async def test_a_telegram_yes_and_no_go_through_claim(tmp_path):
    rig = Rig(tmp_path, parser=[EVERY_BUT_3] * 2)
    actions, claims = actions_of(rig), []
    claim = actions.claim
    actions.claim = lambda *args: claims.append(args) or claim(*args)
    for action in ("rv_ok", "rv_no"):
        await rig.reply("пости всё кроме 3")
        conf_id = echo_id(rig.chat.sent[-1][1])
        await rig.tap(action, conf_id, rig.last_id())
        assert claims[-1] == (conf_id, action == "rv_ok", T1)
    assert len(claims) == 2 and len(rig.posting.calls) == 1

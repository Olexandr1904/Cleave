# tests/unit/test_reviewer_actions.py
from __future__ import annotations

from orchestrator.reviewer.actions import ReviewerActions
from orchestrator.reviewer.grammar import ReplyCommand
from orchestrator.reviewer.lines import not_understood_line
from orchestrator.reviewer.posting import PostOutcome
from orchestrator.reviewer.replies import ReviewerReplies
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.report_edit import item_text
from tests.unit.test_reviewer_replies import (
    CHAT, EVERY_BUT_3, NEW_HEAD, SCOPE, T1, FakePosting, Rig, address_rig, confirmations,
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

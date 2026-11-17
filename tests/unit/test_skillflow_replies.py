# tests/unit/test_skillflow_replies.py
from __future__ import annotations

import dataclasses
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.reviewer.db import connect
from orchestrator.skillflow.chat import SkillChat
from orchestrator.skillflow.messages import (
    cleave_stop_message, interrupted_message, no_change_message, parked_buttons, stop_message, waiting_message,
)
from orchestrator.skillflow.replies import OK_WORDS, SkillReplies
from orchestrator.skillflow.store import SkillStore

T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
NOW = T0.isoformat()
CHAT = "-100"
NOT_LATEST = "ACME-1 · That is not this run's latest message; nothing was done. Use the newest one."


@dataclass
class Sent:
    chat_id: str
    text: str
    buttons: list | None
    reply_to: int | None
    message_id: int


@dataclass
class FakeNotifier:
    """Records each send and returns 500, 501, …"""
    sent: list[Sent] = field(default_factory=list)

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        self.sent.append(Sent(chat_id, text, buttons, reply_to_message_id, 500 + len(self.sent)))
        return self.sent[-1].message_id

    async def delete_message(self, chat_id, message_id):
        pass


class Rig:
    """The real SkillReplies, store and chat over a fake notifier, at `now`. `answers` are the texts sent through
    SkillChat.answer (the replies), `woken` the calls of wake."""

    def __init__(self, tmp_path):
        self.store = SkillStore(connect(tmp_path / "reviewer.db"))
        self.notifier = FakeNotifier()
        self.chat = SkillChat(self.notifier, self.store, now=lambda: T0)
        self.now, self.woken, self.message_id, self._next_id = T0, 0, 0, 100
        self.replies = SkillReplies(self.store, self.chat, self._wake, now=lambda: self.now)

    def _wake(self):
        self.woken += 1

    @property
    def answers(self) -> list[str]:
        return [m.text for m in self.notifier.sent if m.reply_to is not None]

    def parked(self, state, phase="work", **data):
        """A run of ACME-1 in `state` at `phase`, its message with buttons gone out and anchored: `message_id`, the
        run's one ask message id."""
        run = self.store.add_run("acme/app", "ACME-1", NOW, skill="fix-this", chat_id=CHAT, phase=phase, **data)
        run.state = state
        self.store.save(run, NOW)
        self.message_id = self.shown(run.id)
        return self.store.run(run.id)

    def shown(self, run_id, buttons=True) -> int:
        """A message of the run gone out and anchored; one with buttons recorded as the runner does: the run's newest
        message that asks."""
        message_id, self._next_id = self._next_id, self._next_id + 1
        self.store.add_anchor(CHAT, message_id, run_id, NOW)
        if buttons:
            self.store.drop_sent(run_id, 0, NOW, ask_message_ids=[message_id])
        return message_id


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def boom(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize("text", ["ok", "OK.", " Ок ", "так", "approve!"])
async def test_ok_approves_a_waiting_run(rig, text):
    run = rig.parked("waiting")
    await rig.replies.handle_reply("-100", rig.message_id, text)
    got = rig.store.run(run.id)
    assert (got.state, got.approved, got.pending) == ("queued", True, "") and rig.woken == 1
    assert rig.answers[-1] == "ACME-1 · Approved — the session goes on."


async def test_a_text_reply_goes_to_the_session(rig):
    run = rig.parked("stopped", phase="gates")
    await rig.replies.handle_reply("-100", rig.message_id, "the test is flaky, skip LoginTest")
    got = rig.store.run(run.id)
    assert (got.state, got.pending, got.phase, got.approved) == ("queued", "the test is flaky, skip LoginTest", "work", False)


async def test_a_reply_reopens_a_no_change_run_but_ok_does_not(rig):
    run = rig.parked("no_change")
    await rig.replies.handle_reply("-100", rig.message_id, "ok")
    assert rig.store.run(run.id).state == "no_change" and "finished (no_change)" in rig.answers[-1]
    await rig.replies.handle_reply("-100", rig.message_id, "it is an app bug: see the stack trace")
    assert rig.store.run(run.id).state == "queued"


@pytest.mark.parametrize("state, phase", [("stopped", "push"), ("interrupted", "commit")])
async def test_approve_continues_from_the_step(rig, state, phase):
    run = rig.parked(state, phase=phase)
    await rig.replies.handle_callback("sk_ok", run.id, "-100", rig.message_id)
    got = rig.store.run(run.id)
    assert (got.state, got.phase) == ("queued", phase) and rig.answers[-1] == f"ACME-1 · Continuing from the {phase} step."


@pytest.mark.parametrize("state", ["queued", "deferred", "waiting", "stopped", "interrupted"])
async def test_drop(rig, state):
    run = rig.parked(state)
    await rig.replies.handle_callback("sk_drop", run.id, "-100", rig.message_id)
    assert rig.store.run(run.id).state == "dropped" and rig.answers[-1] == "ACME-1 · Dropped. Nothing was pushed."


@pytest.mark.parametrize("state, words", [("running", "working (running)"), ("gates", "working (gates)"),
                                          ("pr_open", "finished (pr_open)"), ("dropped", "finished (dropped)")])
async def test_nothing_is_done_while_working_or_finished(rig, state, words):
    run = rig.parked(state)
    for act in (lambda: rig.replies.handle_reply("-100", rig.message_id, "ok"),
                lambda: rig.replies.handle_callback("sk_ok", run.id, "-100", rig.message_id),
                lambda: rig.replies.handle_callback("sk_drop", run.id, "-100", rig.message_id)):
        await act()
        assert rig.store.run(run.id).state == state and words in rig.answers[-1]
    assert rig.woken == 0


async def test_a_second_tap_queues_nothing_twice(rig):
    run = rig.parked("waiting")
    await rig.replies.handle_callback("sk_ok", run.id, "-100", rig.message_id)
    await rig.replies.handle_callback("sk_ok", run.id, "-100", rig.message_id)
    assert rig.woken == 1 and "working (queued)" in rig.answers[-1]


async def test_an_unknown_run_is_answered(rig):
    await rig.replies.handle_callback("sk_ok", "nope", "-100", 1)
    assert rig.answers[-1] == "This skill run is not known; nothing was done."


# The controller's additions (task-13-addendum.md).

def _tap(action):
    return lambda rig, run, message_id: rig.replies.handle_callback(action, run.id, CHAT, message_id)


def _say(text):
    return lambda rig, run, message_id: rig.replies.handle_reply(CHAT, message_id, text)


@pytest.mark.parametrize("act, taken", [(_tap("sk_ok"), "queued"), (_say("ok"), "queued"),
                                        (_tap("sk_drop"), "dropped")], ids=["approve", "ok-word", "drop"])
async def test_approve_and_drop_count_only_on_the_newest_message_that_asked(rig, act, taken):
    run = rig.parked("waiting")
    old = rig.message_id
    newest = rig.shown(run.id)                # a newer question, with its buttons
    rig.shown(run.id, buttons=False)          # and a line after it
    await act(rig, run, old)
    assert rig.store.run(run.id).state == "waiting" and rig.woken == 0
    assert rig.answers[-1] == NOT_LATEST
    await act(rig, run, newest)
    assert rig.store.run(run.id).state == taken and rig.woken == 1


async def test_a_text_reply_counts_on_any_message_of_the_run(rig):
    run = rig.parked("waiting")
    old = rig.message_id
    rig.shown(run.id)
    await rig.replies.handle_reply(CHAT, old, "use the staging login")
    got = rig.store.run(run.id)
    assert (got.state, got.pending) == ("queued", "use the staging login") and rig.woken == 1


async def test_nothing_is_approved_or_dropped_before_a_message_asked(rig):
    """No ask message ids: no message with buttons has gone out (or the row is an older version's)."""
    run = rig.parked("interrupted")
    rig.store.save(dataclasses.replace(run, ask_message_ids=[]), NOW)
    for act in (_tap("sk_ok"), _say("ok"), _tap("sk_drop")):
        await act(rig, run, rig.message_id)
        assert rig.store.run(run.id).state == "interrupted" and rig.answers[-1] == NOT_LATEST
    await rig.replies.handle_reply(CHAT, rig.message_id, "rebase on develop first")
    assert rig.store.run(run.id).state == "queued" and rig.woken == 1


async def test_a_reply_more_than_15_minutes_old_changes_nothing(rig):
    run = rig.parked("waiting")
    for text in ("ok", "use the staging login"):
        await rig.replies.handle_reply(CHAT, rig.message_id, text, sent_at=T0 - timedelta(minutes=16))
        assert rig.store.run(run.id).state == "waiting"
        assert rig.answers[-1] == ("ACME-1 · Your reply from 09:44 UTC is more than 15 minutes old; nothing was done. "
                                   "Reply again if it still holds.")
    assert rig.woken == 0


@pytest.mark.parametrize("now, sent_at, when", [
    (datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 15, 30, tzinfo=timezone.utc),
     "16 Nov 15:30 UTC"),
    (datetime(2026, 11, 17, 0, 5, tzinfo=timezone.utc), datetime(2026, 11, 16, 23, 40, tzinfo=timezone.utc),
     "16 Nov 23:40 UTC"),     # yesterday's, though less than a day old
])
async def test_an_old_reply_from_another_day_names_its_day(rig, now, sent_at, when):
    run = rig.parked("waiting")
    rig.now = now
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok", sent_at=sent_at)
    assert rig.store.run(run.id).state == "waiting"
    assert rig.answers[-1] == (f"ACME-1 · Your reply from {when} is more than 15 minutes old; nothing was done. "
                               "Reply again if it still holds.")


async def test_a_reply_14_minutes_old_is_taken(rig):
    run = rig.parked("waiting")
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok", sent_at=T0 - timedelta(minutes=14))
    assert rig.store.run(run.id).state == "queued" and rig.woken == 1


async def test_a_text_reply_starts_from_the_work_step_and_clears_the_approval(rig):
    run = rig.parked("interrupted", phase="push", approved=True)
    await rig.replies.handle_reply(CHAT, rig.message_id, "rebase on develop first")
    got = rig.store.run(run.id)
    assert (got.state, got.phase, got.pending, got.approved) == ("queued", "work", "rebase on develop first", False)


@pytest.mark.parametrize("act", [_tap("sk_ok"), _say("ok")], ids=["approve", "ok-word"])
@pytest.mark.parametrize("state", ["stopped", "interrupted"])
async def test_approve_gives_a_fresh_deferral_count(rig, act, state):
    run = rig.parked(state, deferrals=3)
    await act(rig, run, rig.message_id)
    got = rig.store.run(run.id)
    assert (got.state, got.deferrals) == ("queued", 0)


@pytest.mark.parametrize("data", [{"pending": "the test is flaky, skip LoginTest"}, {"approved": True}])
async def test_approve_keeps_what_the_next_turn_carries(rig, data):
    """A reply not yet used (a run stopped before its session started) and an approval not yet used stay."""
    run = rig.parked("stopped", **data)
    await rig.replies.handle_callback("sk_ok", run.id, CHAT, rig.message_id)
    got = rig.store.run(run.id)
    assert (got.state, got.pending, got.approved) == ("queued", data.get("pending", ""), data.get("approved", False))


ON_THE_FORGE = "ACME-1 · Dropped. The branch fix/ACME-1-login is on the forge; Cleave pushes nothing more."


@pytest.mark.parametrize("phase, pushed, answer", [
    ("work", True, ON_THE_FORGE),       # a reply sent it back to work after the push landed
    ("push", True, ON_THE_FORGE),
    ("push", False, "ACME-1 · Dropped. Nothing was pushed."),   # stopped in the push step before any push landed
])
async def test_drops_answer_rests_on_a_landed_push_not_on_the_phase(rig, phase, pushed, answer):
    run = rig.parked("stopped", phase=phase, branch="fix/ACME-1-login", pushed=pushed)
    await rig.replies.handle_callback("sk_drop", run.id, CHAT, rig.message_id)
    got = rig.store.run(run.id)
    assert (got.state, got.reason, got.pushed) == ("dropped", "dropped by the operator", pushed)
    assert rig.answers[-1] == answer


MAY_HAVE_LANDED = ("ACME-1 · Dropped. A push of fix/ACME-1-login was started and may have reached the forge; "
                   "Cleave pushes nothing more.")


@pytest.mark.parametrize("pushed, push_branch, answer", [
    (True, "fix/ACME-1-login", ON_THE_FORGE),                       # a push that returned
    (False, "fix/ACME-1-login", MAY_HAVE_LANDED),                   # one that raised, or was cut by a restart
    (False, "", "ACME-1 · Dropped. Nothing was pushed."),
])
async def test_drops_answer_tells_a_push_that_was_started(rig, pushed, push_branch, answer):
    run = rig.parked("stopped", phase="push", branch="fix/ACME-1-login", pushed=pushed, push_branch=push_branch)
    await rig.replies.handle_callback("sk_drop", run.id, CHAT, rig.message_id)
    assert (rig.store.run(run.id).state, rig.answers[-1]) == ("dropped", answer)


@pytest.mark.parametrize("word", sorted(OK_WORDS))
async def test_every_ok_word_approves(rig, word):
    run = rig.parked("waiting")
    await rig.replies.handle_reply(CHAT, rig.message_id, word)
    assert rig.store.run(run.id).approved is True


@pytest.mark.parametrize("text", ["ok, but skip the UI tests", "ok..", "ok !", "not ok", "okk"])
async def test_only_a_whole_ok_word_approves(rig, text):
    run = rig.parked("waiting")
    await rig.replies.handle_reply(CHAT, rig.message_id, text)
    got = rig.store.run(run.id)
    assert (got.state, got.approved, got.pending) == ("queued", False, text)


NOTES = Path("/repo/ai_instructions/ACME-1")
# The messages a parked run gets, built and sent as the runner sends them (with buttons or without), and what each
# offers: its closing line, its buttons and the reply that always reaches the session; for no_change, what it does not.
OFFERS = [
    ("waiting", lambda run: waiting_message(run, "Which login flow?", NOTES), True,
     ["text", "ok", "approve", "drop"], []),
    ("stopped", lambda run: stop_message(run, "The crash is in the vendor SDK.", NOTES), True,
     ["text", "approve", "drop"], []),
    ("stopped", lambda run: cleave_stop_message(run, "lint exit 1"), True, ["text", "approve", "drop"], []),
    ("interrupted", interrupted_message, True, ["text", "approve", "drop"], []),
    ("no_change", lambda run: no_change_message(run, "Already fixed on develop.", NOTES), False,
     ["text"], ["ok", "approve", "drop"]),
]


@pytest.mark.parametrize("state, build, buttons, offered, refused", OFFERS,
                         ids=["waiting", "stopped-by-skill", "stopped-by-cleave", "interrupted", "no_change"])
async def test_every_action_a_message_offers_is_accepted(rig, state, build, buttons, offered, refused):
    for kind in (*offered, *refused):
        run = rig.parked(state)            # each on a fresh run; its earlier message's buttons are rig.message_id's
        ids = await rig.chat.send(run, build(run), parked_buttons(run.id) if buttons else None)
        if buttons:
            rig.store.drop_sent(run.id, 0, NOW, ask_message_ids=ids)
        message = rig.notifier.sent[-1]
        if kind in ("text", "ok"):
            await rig.replies.handle_reply(CHAT, message.message_id, "ok" if kind == "ok" else "the SSO login")
        else:
            # The message's own button, else (no_change has none) the earlier message's; its data split as the
            # Telegram adapter splits it.
            tapped = message.message_id if message.buttons else rig.message_id
            [button] = [b for b in message.buttons or parked_buttons(run.id) if b.label.lower() == kind]
            action, run_id = button.action.split(":", 1)
            await rig.replies.handle_callback(action, run_id, CHAT, tapped)
        got = rig.store.run(run.id)
        if kind in offered:
            assert got.state == ("dropped" if kind == "drop" else "queued"), (state, kind)
        else:
            assert got.state == state and f"finished ({state})" in rig.answers[-1], (state, kind)


async def test_a_reply_whose_run_cannot_be_read_is_answered(rig):
    run = rig.parked("waiting")
    rig.store._conn.execute("UPDATE skill_runs SET data = 'not json' WHERE id = ?", (run.id,))
    rig.store.add_anchor(CHAT, 99, "gone", NOW)       # an anchor whose run the store no longer has
    for message_id in (rig.message_id, 99):
        await rig.replies.handle_reply(CHAT, message_id, "ok")
        assert rig.answers[-1] == "This skill run cannot be read; nothing was done."
    assert rig.woken == 0


async def test_an_answer_is_a_reply_to_the_message_anchored_to_the_run(rig):
    run = rig.parked("running")
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok")
    answer = rig.notifier.sent[-1]
    assert (answer.chat_id, answer.reply_to) == (CHAT, rig.message_id)
    assert rig.store.anchor(CHAT, answer.message_id) == run.id     # a reply to the answer is the run's too


async def test_a_run_moved_since_the_read_is_answered_by_its_state(rig, monkeypatch):
    run = rig.parked("waiting")
    real = rig.store.claim

    def raced(run_id, *args, **kwargs):
        real(run_id, ("waiting",), NOW, "dropped")    # another tap got there first
        return real(run_id, *args, **kwargs)

    monkeypatch.setattr(rig.store, "claim", raced)
    await rig.replies.handle_callback("sk_ok", run.id, CHAT, rig.message_id)
    got = rig.store.run(run.id)
    assert (got.state, got.approved) == ("dropped", False) and rig.woken == 0
    assert rig.answers[-1] == "ACME-1 · The run is finished (dropped); nothing was done."


async def test_a_store_error_is_answered_by_its_class_only(rig, monkeypatch, caplog):
    run = rig.parked("waiting")
    monkeypatch.setattr(rig.store, "claim", boom)
    with caplog.at_level(logging.WARNING):
        await rig.replies.handle_reply(CHAT, rig.message_id, "ok")
        await rig.replies.handle_callback("sk_drop", run.id, CHAT, rig.message_id)
    assert rig.answers[-2:] == ["Internal error (OperationalError); nothing was done."] * 2
    assert "database is locked" not in caplog.text
    # The log says where, as the runner's do: the class and the file:line that raised.
    assert caplog.text.count("OperationalError at ") == 2 and f"{Path(__file__).name}:" in caplog.text
    assert rig.store.run(run.id).state == "waiting" and rig.woken == 0


async def test_a_button_not_known_is_answered_and_changes_nothing(rig, caplog):
    run = rig.parked("waiting")
    with caplog.at_level(logging.WARNING, logger="orchestrator.skillflow.replies"):
        await rig.replies.handle_callback("sk_maybe", run.id, CHAT, rig.message_id)
    assert rig.store.run(run.id).state == "waiting" and rig.woken == 0
    assert rig.answers == ["Unknown action; nothing was done."]
    assert rig.store.anchor(CHAT, rig.notifier.sent[-1].message_id) == ""    # a dead end
    assert "sk_maybe" in caplog.text


# The controller's follow-up rulings: a message still in the outbox, and answers about no run.

UNSENT = "ACME-1 · This run's newest message has not gone out yet; nothing was done. Wait for it."
DEAD_END = "Reply to a skill run's own message to act on it; nothing was done."


def _queue(rig, run_id, buttons):
    """A message of the run waiting in its outbox, as the runner's park leaves it until the send."""
    run = rig.store.run(run_id)
    entry = {"text": "ACME-1 · skill run (fix-this)", "buttons": buttons}
    rig.store.save(dataclasses.replace(run, outbox=[*run.outbox, entry]), NOW)


@pytest.mark.parametrize("act", [_tap("sk_ok"), _say("ok"), _tap("sk_drop")], ids=["approve", "ok-word", "drop"])
async def test_approve_and_drop_wait_while_a_message_with_buttons_is_unsent(rig, act):
    run = rig.parked("waiting")
    oldest = rig.message_id
    asked = rig.shown(run.id)              # ask_message_ids name it: the newest that went out
    _queue(rig, run.id, buttons=True)      # the newest question, not gone out yet
    for message_id in (asked, oldest):
        await act(rig, run, message_id)
        assert rig.store.run(run.id).state == "waiting" and rig.answers[-1] == UNSENT
    assert rig.woken == 0


async def test_a_text_reply_is_taken_while_a_message_with_buttons_is_unsent(rig):
    run = rig.parked("stopped")
    _queue(rig, run.id, buttons=True)
    await rig.replies.handle_reply(CHAT, rig.message_id, "use the staging login")
    got = rig.store.run(run.id)
    assert (got.state, got.pending) == ("queued", "use the staging login") and rig.woken == 1


async def test_an_unsent_line_without_buttons_holds_no_approval(rig):
    run = rig.parked("waiting")
    _queue(rig, run.id, buttons=False)
    await rig.replies.handle_callback("sk_ok", run.id, CHAT, rig.message_id)
    assert rig.store.run(run.id).state == "queued"


async def test_a_reply_to_an_answer_about_no_run_is_a_dead_end(rig, monkeypatch):
    run = rig.parked("waiting")
    await rig.replies.handle_callback("sk_ok", "nope", CHAT, rig.message_id)        # not known
    rig.store.add_anchor(CHAT, 99, "gone", NOW)
    await rig.replies.handle_reply(CHAT, 99, "ok")                                   # cannot be read
    monkeypatch.setattr(rig.store, "claim", boom)
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok")                       # internal error
    answers = [m for m in rig.notifier.sent if m.reply_to is not None]
    assert [m.text for m in answers] == ["This skill run is not known; nothing was done.",
                                         "This skill run cannot be read; nothing was done.",
                                         "Internal error (OperationalError); nothing was done."]
    for answer in answers:
        assert rig.store.anchor(CHAT, answer.message_id) == "" and rig.replies.is_anchor(CHAT, answer.message_id)
        await rig.replies.handle_reply(CHAT, answer.message_id, "ok")
        assert rig.answers[-1] == DEAD_END
    dead_end = rig.notifier.sent[-1]
    assert (dead_end.reply_to, rig.store.anchor(CHAT, dead_end.message_id)) == (answers[-1].message_id, "")
    await rig.replies.handle_reply(CHAT, dead_end.message_id, "approve")             # itself a dead end
    assert rig.answers[-1] == DEAD_END
    assert rig.store.run(run.id).state == "waiting" and rig.woken == 0


async def test_answer_sends_an_answer_about_no_run(rig):
    """For the command handler's own answer: a reply to the message it answers, a dead end."""
    await rig.replies.answer(CHAT, 7, "Reply not handled: the skill-run store could not be read; nothing was done.")
    sent = rig.notifier.sent[-1]
    assert (sent.chat_id, sent.reply_to) == (CHAT, 7) and rig.store.anchor(CHAT, sent.message_id) == ""


# Task 13 review round 1.

async def test_ok_on_any_part_of_the_newest_message_with_buttons_approves(rig):
    """The operator often replies to the first part of a long question."""
    run = rig.parked("waiting")
    older = rig.message_id
    ids = await rig.chat.send(run, "step\n" * 3000, parked_buttons(run.id))
    rig.store.drop_sent(run.id, 0, NOW, ask_message_ids=ids)      # as the runner's flush records it
    assert len(ids) == 4
    await rig.replies.handle_reply(CHAT, older, "ok")
    assert rig.store.run(run.id).state == "waiting" and rig.answers[-1] == NOT_LATEST
    await rig.replies.handle_reply(CHAT, ids[0], "ok")
    got = rig.store.run(run.id)
    assert (got.state, got.approved) == ("queued", True) and rig.woken == 1


@pytest.mark.parametrize("act, taken", [(_tap("sk_drop"), "dropped"), (_tap("sk_ok"), "queued")],
                         ids=["drop", "approve"])
async def test_a_tap_on_any_part_of_the_newest_message_with_buttons_counts(rig, act, taken):
    run = rig.parked("stopped")
    ids = await rig.chat.send(run, "step\n" * 3000, parked_buttons(run.id))
    rig.store.drop_sent(run.id, 0, NOW, ask_message_ids=ids)
    await act(rig, run, ids[1])
    assert rig.store.run(run.id).state == taken and rig.woken == 1


async def test_a_text_reply_gives_a_fresh_deferral_count(rig):
    run = rig.parked("stopped", deferrals=3)
    await rig.replies.handle_reply(CHAT, rig.message_id, "the limit resets at noon")
    got = rig.store.run(run.id)
    assert (got.state, got.deferrals) == ("queued", 0)


async def test_a_waiting_approve_clears_an_earlier_reply(rig):
    run = rig.parked("waiting", pending="use the staging login")
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok")
    got = rig.store.run(run.id)
    assert (got.state, got.approved, got.pending) == ("queued", True, "")


@pytest.mark.parametrize("age, taken", [(timedelta(minutes=15), True), (timedelta(minutes=15, seconds=1), False)])
async def test_the_age_limit_is_exactly_15_minutes(rig, age, taken):
    run = rig.parked("waiting")
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok", sent_at=T0 - age)
    assert rig.store.run(run.id).state == ("queued" if taken else "waiting")


async def test_the_claim_is_made_from_the_state_that_was_read(rig, monkeypatch):
    """Changes chosen for a waiting run never land on a run that is in another state by the time of the claim, even
    one the same input would also take."""
    run = rig.parked("waiting")
    real = rig.store.claim

    def raced(run_id, *args, **kwargs):
        real(run_id, ("waiting",), NOW, "interrupted")
        return real(run_id, *args, **kwargs)

    monkeypatch.setattr(rig.store, "claim", raced)
    await rig.replies.handle_reply(CHAT, rig.message_id, "ok")
    got = rig.store.run(run.id)
    assert (got.state, got.approved) == ("interrupted", False) and rig.woken == 0
    assert "nothing was done" in rig.answers[-1]


def test_is_anchor(rig, monkeypatch):
    rig.parked("waiting")
    assert rig.replies.is_anchor(CHAT, rig.message_id) is True
    assert rig.replies.is_anchor(CHAT, rig.message_id + 1) is False
    assert rig.replies.is_anchor("-999", rig.message_id) is False
    monkeypatch.setattr(rig.store, "anchor", boom)
    with pytest.raises(sqlite3.OperationalError):    # the command handler answers it
        rig.replies.is_anchor(CHAT, rig.message_id)

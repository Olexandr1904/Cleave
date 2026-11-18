# tests/unit/test_skillflow_replies_compare.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.reviewer.db import connect
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.replies import SkillOff, SkillReplies
from orchestrator.skillflow.sides import SideView
from orchestrator.skillflow.store import SkillStore

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
CHAT = "-100"


class FakeChat:
    """Records each answer: `answers` its text, `targets` the id it is anchored to ("" for none)."""

    def __init__(self):
        self.answers: list[str] = []
        self.targets: list[str] = []

    async def answer(self, target, chat_id, reply_to, text):
        self.answers.append(text)
        self.targets.append("" if target is None else target.id)


def _side(pr_number: int) -> dict:
    return SideView("pr_open" if pr_number else "stopped", True, pr_number,
                    f"https://github.com/acme/app/pull/{pr_number}" if pr_number else "", "", 30, 100, 20,
                    "fp").as_dict()


class Rig:
    """The real SkillReplies over real stores: a stopped skill run of ACME-1 (its message `run_message`) and its
    comparison `cid`, open, its newest message [50], staged PR 12 and skill PR 13, messages=1."""

    def __init__(self, tmp_path, language_of=None):
        conn = connect(tmp_path / "reviewer.db")
        self.runs = SkillStore(conn)
        self.comparisons = ComparisonStore(conn)
        self.chat = FakeChat()
        self.woken = 0
        extra = {} if language_of is None else {"language_of": language_of}
        self.replies = SkillReplies(self.runs, self.chat, lambda: None, now=lambda: NOW,
                                    comparisons=self.comparisons, wake_keeper=self._wake, **extra)
        run = self.runs.add_run("acme/app", "ACME-1", STAMP, skill="fix-this", chat_id=CHAT)
        run.state = "stopped"
        self.runs.save(run, STAMP)
        self.run_id, self.run_message = run.id, 40
        self.runs.add_anchor(CHAT, self.run_message, run.id, STAMP)
        self.runs.drop_sent(run.id, 0, STAMP, ask_message_ids=[self.run_message])
        c = self.comparisons.add("acme/app", "ACME-1", run.id, STAMP, chat_id=CHAT, summary="Fix login")
        self.cid = c.id
        self.comparisons.claim(self.cid, ("open",), STAMP, "open", messages=1, ask_message_ids=[50],
                               fingerprint="pair-1", sides={"staged": _side(12), "skill": _side(13)})
        self.runs.add_anchor(CHAT, 50, self.cid, STAMP)

    def _wake(self):
        self.woken += 1

    def set_sides(self, staged_pr=12, skill_pr=13):
        self.comparisons.claim(self.cid, ("open",), STAMP, "open",
                               sides={"staged": _side(staged_pr), "skill": _side(skill_pr)})

    def stuck(self, message_ids):
        """The verdict stuck, its Retry message `message_ids` gone out."""
        c = self.comparisons.get(self.cid)
        self.comparisons.claim(self.cid, ("open",), STAMP, "stuck", verdict="skill", reason="502",
                               messages=c.messages + 1, ask_message_ids=list(message_ids))
        for message_id in message_ids:
            self.runs.add_anchor(CHAT, message_id, self.cid, STAMP)

    def replies_reads_stale(self, cid):
        """The handler's next read of the comparison sees it as it was before the last claim."""
        stale = self.comparisons.get(cid)
        stale.messages -= 1
        real = self.comparisons.get

        def get_once(wanted):
            self.comparisons.get = real   # claim's own read inside its lock sees the row as it is
            return stale if wanted == cid else real(wanted)

        self.comparisons.get = get_once


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


class OffRig:
    def __init__(self, tmp_path, language_of=None):
        self.store = SkillStore(connect(tmp_path / "reviewer.db"))
        self.store.add_anchor(CHAT, 50, "cmp:abc", STAMP)
        self.chat = FakeChat()
        extra = {} if language_of is None else {"language_of": language_of}
        self.off = SkillOff(self.store, self.chat, **extra)


@pytest.fixture
def off_rig(tmp_path):
    return OffRig(tmp_path)


async def test_verdict_tap_claims_and_wakes(rig):
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 50)
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.verdict) == ("deciding", "skill") and rig.woken == 1
    assert rig.chat.answers[-1] == "ACME-1 · Verdict: skill. Cleave marks PR #13 ready and makes the tracker writes for it, and closes PR #12."
    assert rig.chat.targets[-1] == rig.cid


async def test_an_older_message_is_refused(rig):
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 49)
    assert rig.comparisons.get(rig.cid).state == "open" and "not the comparison's newest" in rig.chat.answers[-1]


async def test_a_side_without_a_pr_is_refused(rig):
    rig.set_sides(skill_pr=0)
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 50)
    assert rig.comparisons.get(rig.cid).state == "open" and rig.chat.answers[-1].endswith("That side has no PR; nothing was done.")


async def test_a_tap_racing_a_new_message_loses(rig):
    rig.comparisons.claim(rig.cid, ("open",), STAMP, "open", messages=2)   # the keeper's new message, after the tap's read
    rig.replies_reads_stale(rig.cid)                                      # the handler read messages=1
    await rig.replies.handle_callback("sk_win_staged", rig.cid, "-100", 50)
    assert rig.comparisons.get(rig.cid).state == "open"


async def test_second_tap_is_answered_not_done_twice(rig):
    await rig.replies.handle_callback("sk_win_none", rig.cid, "-100", 50)
    await rig.replies.handle_callback("sk_win_none", rig.cid, "-100", 50)
    assert rig.woken == 1 and "being carried out" in rig.chat.answers[-1]


async def test_a_verdict_tap_on_a_stuck_comparison_points_to_retry(rig):
    rig.stuck(message_ids=[60])
    await rig.replies.handle_callback("sk_win_staged", rig.cid, "-100", 60)
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.verdict) == ("stuck", "skill") and rig.woken == 0
    assert rig.chat.answers[-1] == ("ACME-1 · The verdict (skill) is stuck; nothing was done. The Retry button on its"
                                    " newest message continues it.")


async def test_a_verdict_tap_on_a_stuck_comparison_is_answered_in_ukrainian(tmp_path):
    rig = Rig(tmp_path, language_of=lambda scope, chat_id: "uk")
    rig.stuck(message_ids=[60])
    await rig.replies.handle_callback("sk_win_staged", rig.cid, "-100", 60)
    assert rig.chat.answers[-1] == ("ACME-1 · Вердикт (skill) застряг; нічого не зроблено. Кнопка «Повторити» на"
                                    " його найновішому повідомленні продовжить його.")


async def test_reply_is_a_note(rig):
    await rig.replies.handle_reply("-100", 50, "ok", sent_at=NOW - timedelta(hours=3))
    assert rig.comparisons.get(rig.cid).note == "ok" and rig.comparisons.get(rig.cid).state == "open"
    assert rig.chat.answers[-1].endswith("Kept as the comparison's note.")


async def test_retry_only_when_stuck(rig):
    await rig.replies.handle_callback("sk_retry", rig.cid, "-100", 50)
    assert rig.comparisons.get(rig.cid).state == "open"
    rig.stuck(message_ids=[60])
    await rig.replies.handle_callback("sk_retry", rig.cid, "-100", 60)
    assert rig.comparisons.get(rig.cid).state == "deciding" and rig.woken == 1


async def test_a_decided_runs_messages_are_refused(rig):
    rig.comparisons.claim(rig.cid, ("open",), STAMP, "deciding", verdict="staged")
    await rig.replies.handle_reply("-100", rig.run_message, "try again", sent_at=NOW)
    assert rig.runs.run(rig.run_id).state == "stopped"
    assert rig.chat.answers[-1].endswith("This run's comparison has its verdict; nothing was done.")


async def test_skill_off_answers_and_keeps_the_parser_out(off_rig):
    assert off_rig.off.is_anchor("-100", 50) is True
    await off_rig.off.handle_reply("-100", 50, "ok")
    await off_rig.off.handle_callback("sk_win_skill", "cmp:abc", "-100", 50)
    assert off_rig.chat.answers == ["The skill flow is off on this daemon; nothing was done."] * 2


async def test_answers_are_in_the_language_of_the_comparison(tmp_path):
    asked = []
    rig = Rig(tmp_path, language_of=lambda scope, chat_id: asked.append((scope, chat_id)) or "uk")
    await rig.replies.handle_reply("-100", 50, "the skill side is cleaner")
    assert asked == [("acme/app", "-100")]
    assert rig.chat.answers[-1] == "ACME-1 · Збережено як примітку до порівняння."


# --- carried rulings: the chat a tap counts in, the pair a verdict binds to, SkillOff's language ---

@pytest.mark.parametrize("action", ["sk_win_skill", "sk_retry"])
async def test_a_tap_from_another_chat_writes_nothing(rig, action):
    if action == "sk_retry":
        rig.stuck(message_ids=[60])
    before = rig.comparisons.get(rig.cid)
    await rig.replies.handle_callback(action, rig.cid, "-200", 60 if action == "sk_retry" else 50)
    after = rig.comparisons.get(rig.cid)
    assert (after.state, after.verdict, after.updated_at) == (before.state, before.verdict, before.updated_at)
    assert rig.woken == 0
    assert rig.chat.answers[-1] == "This comparison is not known; nothing was done." and rig.chat.targets[-1] == ""


async def test_a_tap_on_a_pair_that_changed_under_the_same_message_loses(rig):
    stale = rig.comparisons.get(rig.cid)           # the handler's read: fingerprint pair-1
    rig.comparisons.claim(rig.cid, ("open",), STAMP, "open", fingerprint="pair-2")   # a side moved, same messages
    real = rig.comparisons.get

    def get_once(wanted):
        rig.comparisons.get = real
        return stale if wanted == rig.cid else real(wanted)

    rig.comparisons.get = get_once
    await rig.replies.handle_callback("sk_win_skill", rig.cid, "-100", 50)
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.verdict, c.messages) == ("open", "", stale.messages) and rig.woken == 0
    assert rig.chat.answers[-1].endswith(
        "That is not the comparison's newest message; nothing was done. Use the newest one.")


async def test_skill_off_answers_in_the_chats_language(tmp_path):
    asked = []
    off_rig = OffRig(tmp_path, language_of=lambda chat_id: asked.append(chat_id) or "uk")
    await off_rig.off.handle_reply("-100", 50, "ok")
    await off_rig.off.handle_callback("sk_win_skill", "cmp:abc", "-100", 50)
    assert asked == ["-100", "-100"]
    off = "The skill flow is off on this daemon; nothing was done."
    assert off_rig.chat.answers[0] == off_rig.chat.answers[1] != off

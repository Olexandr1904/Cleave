# tests/unit/test_skillflow_chat.py
from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
import pytest
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.messages import MESSAGE_LIMIT, split_message
from orchestrator.skillflow.chat import SendError, SkillChat
from orchestrator.skillflow.messages import parked_buttons
from orchestrator.skillflow.models import SkillRun
from orchestrator.skillflow.store import SkillStore

T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
TOKEN_TEXT = "Unauthorized: bot0000:not-a-real-token"


@dataclass
class Sent:
    chat_id: str
    text: str
    buttons: list | None
    reply_to: int | None
    message_id: int


@dataclass
class FakeNotifier:
    """Records each send and returns 500, 501, …, and each delete; with `fail` (`fail_delete`) set a send (a
    delete) raises an error whose text holds a token."""
    fail: bool = False
    fail_delete: bool = False
    sent: list[Sent] = field(default_factory=list)
    deleted: list[tuple[str, int]] = field(default_factory=list)

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        if self.fail:
            raise RuntimeError(TOKEN_TEXT)
        self.sent.append(Sent(chat_id, text, buttons, reply_to_message_id, 500 + len(self.sent)))
        return self.sent[-1].message_id

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        if self.fail_delete:
            raise RuntimeError(TOKEN_TEXT)


@dataclass
class Rig:
    chat: SkillChat
    notifier: FakeNotifier
    store: SkillStore
    run: SkillRun
    sleeps: list[float]


@pytest.fixture
def rig(tmp_path):
    store = SkillStore(connect(tmp_path / "reviewer.db"))
    run = store.add_run("acme/app", "ACME-1", T0.isoformat(), skill="fix-this", chat_id="-100")
    notifier, sleeps = FakeNotifier(), []

    async def sleep(seconds):
        sleeps.append(seconds)

    return Rig(SkillChat(notifier, store, now=lambda: T0, sleep=sleep), notifier, store, run, sleeps)


def boom(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


async def test_send_anchors_every_part_and_buttons_go_on_the_last(rig):
    ids = await rig.chat.send(rig.run, "a\n" * 5000, parked_buttons(rig.run.id))
    assert len(rig.notifier.sent) >= 2
    assert [m.buttons is not None for m in rig.notifier.sent] == [False] * (len(rig.notifier.sent) - 1) + [True]
    assert all(rig.store.anchor("-100", m.message_id) == rig.run.id for m in rig.notifier.sent)
    assert ids == [m.message_id for m in rig.notifier.sent]
    assert {m.chat_id for m in rig.notifier.sent} == {"-100"}

async def test_a_failed_send_raises(rig):
    rig.notifier.fail = True
    with pytest.raises(SendError) as raised:
        await rig.chat.send(rig.run, "hi")
    assert "RuntimeError" in str(raised.value) and "not-a-real-token" not in str(raised.value)

async def test_a_message_that_is_not_anchored_raises(rig, monkeypatch):
    monkeypatch.setattr(rig.store, "add_anchor", boom)
    with pytest.raises(SendError, match="not anchored") as raised:
        await rig.chat.send(rig.run, "hi")
    assert rig.sleeps == [0.2, 0.2]                    # three tries
    assert "OperationalError" in str(raised.value) and "locked" not in str(raised.value)

async def test_an_anchor_write_that_fails_once_is_tried_again(rig, monkeypatch):
    real, failures = rig.store.add_anchor, [sqlite3.OperationalError("database is locked")]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.store, "add_anchor", flaky)
    [message_id] = await rig.chat.send(rig.run, "hi")
    assert rig.sleeps == [0.2] and rig.store.anchor("-100", message_id) == rig.run.id
    assert rig.notifier.deleted == []

async def test_a_part_that_is_not_anchored_is_deleted_and_the_anchored_ones_stay(rig, monkeypatch):
    real = rig.store.add_anchor

    def only_the_first(*args, **kwargs):
        if rig.notifier.sent[-1].message_id != 500:
            boom()
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.store, "add_anchor", only_the_first)
    with pytest.raises(SendError, match="not anchored"):
        await rig.chat.send(rig.run, "a\n" * 5000)
    first, second = rig.notifier.sent                  # the third part is not sent
    assert rig.notifier.deleted == [("-100", second.message_id)]
    assert rig.store.anchor("-100", first.message_id) == rig.run.id

async def test_a_failed_delete_is_logged_and_the_send_still_raises(rig, monkeypatch, caplog):
    monkeypatch.setattr(rig.store, "add_anchor", boom)
    rig.notifier.fail_delete = True
    with caplog.at_level(logging.WARNING, logger="orchestrator.skillflow.chat"):
        with pytest.raises(SendError, match="not anchored"):
            await rig.chat.send(rig.run, "hi")
    [sent] = rig.notifier.sent
    assert rig.notifier.deleted == [("-100", sent.message_id)]
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and str(sent.message_id) in errors[0] and "RuntimeError" in errors[0]
    # What is true of a reply to it, and what to do.
    assert ("a reply to it is not the skill flow's and reaches the ticket pipeline's commands: delete the message "
            "by hand") in errors[0] and "not recognised" not in errors[0]
    assert "not-a-real-token" not in caplog.text

async def test_a_blank_part_is_not_sent(rig):
    ids = await rig.chat.send(rig.run, "a" + "\n" * 8000 + "b", parked_buttons(rig.run.id))
    assert [m.text.strip() for m in rig.notifier.sent] == ["a", "b"]
    assert [m.buttons is not None for m in rig.notifier.sent] == [False, True]
    assert ids == [m.message_id for m in rig.notifier.sent]

async def test_a_text_of_blank_parts_sends_nothing(rig):
    assert await rig.chat.send(rig.run, "\n" * 8000 + " " * 5000, parked_buttons(rig.run.id)) == []
    assert rig.notifier.sent == []

THREE_PARTS = "a\n" * 5000

async def test_send_skips_the_parts_already_sent(rig):
    parts = split_message(THREE_PARTS, MESSAGE_LIMIT)
    assert len(parts) == 3
    ids = await rig.chat.send(rig.run, THREE_PARTS, parked_buttons(rig.run.id), skip=1)
    assert [m.text for m in rig.notifier.sent] == parts[1:] and ids == [m.message_id for m in rig.notifier.sent]
    assert [m.buttons is not None for m in rig.notifier.sent] == [False, True]

async def test_a_failure_on_a_later_part_names_the_parts_this_send_sent(rig, monkeypatch):
    real = rig.notifier.send_message

    async def the_third_fails(chat_id, text, **kwargs):
        if len(rig.notifier.sent) == 2:
            raise RuntimeError(TOKEN_TEXT)
        return await real(chat_id, text, **kwargs)

    monkeypatch.setattr(rig.notifier, "send_message", the_third_fails)
    with pytest.raises(SendError) as raised:
        await rig.chat.send(rig.run, THREE_PARTS, skip=0)
    assert raised.value.ids == [500, 501]

async def test_a_part_not_anchored_is_not_among_the_parts_sent(rig, monkeypatch):
    real = rig.store.add_anchor

    def only_the_first(*args, **kwargs):
        if rig.notifier.sent[-1].message_id != 500:
            boom()
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.store, "add_anchor", only_the_first)
    with pytest.raises(SendError, match="not anchored") as raised:
        await rig.chat.send(rig.run, THREE_PARTS)
    assert raised.value.ids == [500] and rig.notifier.deleted == [("-100", 501)]

async def test_answer_replies_and_anchors(rig):
    await rig.chat.answer(rig.run, "-100", 7, "Dropped. Nothing was pushed.")
    sent = rig.notifier.sent[0]
    assert sent.reply_to == 7 and rig.store.anchor("-100", sent.message_id) == rig.run.id

async def test_answer_without_a_run_is_a_dead_end_anchor(rig):
    """A reply to it is the skill flow's, never the intent parser's: its anchor names no run."""
    await rig.chat.answer(None, "-100", 7, "This skill run is not known; nothing was done.")
    sent = rig.notifier.sent[0]
    assert (sent.chat_id, sent.reply_to, sent.buttons) == ("-100", 7, None)
    assert rig.store.anchor("-100", sent.message_id) == "" and rig.notifier.deleted == []


async def test_a_dead_end_answer_whose_anchor_fails_stays(rig, monkeypatch, caplog):
    """The operator needs to read it: it is kept, and the failed write is one line with the class only."""
    monkeypatch.setattr(rig.store, "add_anchor", boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator.skillflow.chat"):
        await rig.chat.answer(None, "-100", 7, "This skill run is not known; nothing was done.")
    sent = rig.notifier.sent[0]
    assert rig.notifier.deleted == [] and rig.store.anchor("-100", sent.message_id) is None
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "OperationalError" in lines[0] and str(sent.message_id) in lines[0]
    assert "locked" not in caplog.text

async def test_an_answer_that_is_not_anchored_is_deleted(rig, monkeypatch, caplog):
    monkeypatch.setattr(rig.store, "add_anchor", boom)
    await rig.chat.answer(rig.run, "-100", 7, "Approved — the session goes on.")
    assert rig.notifier.deleted == [("-100", rig.notifier.sent[0].message_id)]
    rig.notifier.fail_delete = True
    with caplog.at_level(logging.WARNING, logger="orchestrator.skillflow.chat"):
        await rig.chat.answer(rig.run, "-100", 7, "Approved — the session goes on.")
    second = rig.notifier.sent[1]
    assert rig.notifier.deleted[1:] == [("-100", second.message_id)]
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and str(second.message_id) in errors[0] and "RuntimeError" in errors[0]
    assert "not-a-real-token" not in caplog.text and "locked" not in caplog.text

async def test_answer_logs_its_failures_and_never_raises(rig, monkeypatch, caplog):
    monkeypatch.setattr(rig.store, "add_anchor", boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator.skillflow.chat"):
        await rig.chat.answer(rig.run, "-100", 7, "Approved — the session goes on.")
        rig.notifier.fail = True
        await rig.chat.answer(rig.run, "-100", 7, "Approved — the session goes on.")
    assert "not anchored: OperationalError" in caplog.text and "RuntimeError" in caplog.text
    assert "locked" not in caplog.text and "not-a-real-token" not in caplog.text

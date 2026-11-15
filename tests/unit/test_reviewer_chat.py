# tests/unit/test_reviewer_chat.py
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from integrations.base.notifier import Button
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.models import Anchor, JobRun
from orchestrator.reviewer.store import ReviewerStore

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
CHAT_ID = "-1001"


class Notifier:
    """Returns the given message ids in turn; records each send's chat, text, buttons and reply target."""
    def __init__(self, *ids):
        self.ids, self.sent = list(ids), []

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        self.sent.append((chat_id, text, buttons, reply_to_message_id))
        return self.ids.pop(0)


def store_at(tmp_path):
    return ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))


async def test_chat_send_anchors(tmp_path, monkeypatch, caplog):
    store = store_at(tmp_path)
    notifier = Notifier(501, 502)
    chat = ReviewerChat(notifier, CHAT_ID, store=store, now=lambda: T0)
    assert await chat.send("a", job_id="j1", reply_to=7) == 501
    assert store.anchor(CHAT_ID, 501) == Anchor("j1", "", 0)
    buttons = [Button("Yes", "rv:yes:c1"), Button("No", "rv:no:c1")]
    assert await chat.send("b", scope="acme/app", number=1712, buttons=buttons) == 502
    assert store.anchor(CHAT_ID, 502) == Anchor("", "acme/app", 1712)
    assert notifier.sent == [(CHAT_ID, "a", None, 7), (CHAT_ID, "b", buttons, None)]

    plain = Notifier(501)
    assert await ReviewerChat(plain, CHAT_ID).send("a", job_id="j1") == 501   # no store: no anchor
    assert plain.sent == [(CHAT_ID, "a", None, None)]

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    failing = store_at(tmp_path / "other")
    monkeypatch.setattr(failing, "add_anchor", locked)
    chat = ReviewerChat(Notifier(501, 502), CHAT_ID, store=failing, now=lambda: T0, sleep=no_wait)
    await chat.send("a")
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.chat"):
        assert await chat.send("b", job_id="j1") == 502   # the message is out: the failed write does not raise
    assert "OperationalError" in caplog.text and "database is locked" not in caplog.text


async def no_wait(seconds):
    pass


async def test_a_failed_anchor_write_is_tried_again(tmp_path, monkeypatch, caplog):
    """The anchor write is tried 3 times, 0.2 s apart. When it still fails the message is out but unanchored: an
    ERROR names its ids and the exception class, and the job's run gets a note (/code-review C5)."""
    store = store_at(tmp_path)
    job = store.add_job("acme/app", 1712, "review", "a" * 40, "a" * 40, T0.isoformat())
    store.save_run(JobRun(job.id, notes=["earlier note"]))
    real, failures = store.add_anchor, []

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "add_anchor", flaky)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    chat = ReviewerChat(Notifier(501, 502, 503), CHAT_ID, store=store, now=lambda: T0, sleep=sleep)
    failures[:] = [sqlite3.OperationalError("database is locked")] * 2
    assert await chat.send("a", job_id=job.id) == 501
    assert store.anchor(CHAT_ID, 501) == Anchor(job.id, "", 0) and waits == [0.2, 0.2]

    failures[:] = [sqlite3.OperationalError("database is locked")] * 3
    with caplog.at_level(logging.WARNING, logger="orchestrator.reviewer.chat"):
        assert await chat.send("b", job_id=job.id) == 502
    assert store.anchor(CHAT_ID, 502) is None and waits == [0.2] * 4
    assert store.run(job.id).notes == ["earlier note",
                                       "message 502 is not registered; replies to it are not recognised"]
    [error] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "502" in error.getMessage() and job.id in error.getMessage() and "OperationalError" in error.getMessage()
    assert "database is locked" not in caplog.text

    # A message about a repo or a PR has no run: the ERROR alone.
    failures[:] = [sqlite3.OperationalError("database is locked")] * 3
    assert await chat.send("c", scope="acme/app", number=1712) == 503
    assert len(store.run(job.id).notes) == 2

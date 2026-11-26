# tests/unit/test_skillflow_store.py
from __future__ import annotations
import json
import logging
import pytest
from orchestrator.reviewer.db import connect
from orchestrator.skillflow.store import SkillStore

NOW, LATER = "2026-11-16T10:00:00+00:00", "2026-11-16T10:05:00+00:00"

@pytest.fixture
def store(tmp_path):
    return SkillStore(connect(tmp_path / "reviewer.db"))

def test_add_and_read_back(store):
    run = store.add_run("acme/app", "ACME-1", "2026-11-16T10:00:00+00:00", skill="fix-this", model="claude-opus-5-5")
    got = store.run(run.id)
    assert (got.state, got.phase, got.skill, got.model, got.unsent) == ("queued", "work", "fix-this", "claude-opus-5-5", 0)
    assert store.runs_for_ticket("acme/app", "ACME-1") == [got] and store.run("nope") is None

def test_save_keeps_data_and_counts_the_outbox(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    run.state, run.branch, run.outbox = "waiting", "ACME-1-fix", [{"text": "hi", "buttons": True}]
    store.save(run, LATER)
    got = store.run(run.id)
    assert (got.state, got.branch, got.unsent, got.updated_at) == ("waiting", "ACME-1-fix", 1, LATER)
    assert store.unsent_runs() == [got] and store.in_states(("waiting",)) == [got]

def test_claim_changes_only_from_the_given_states(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    assert store.claim(run.id, ("waiting",), LATER, "queued", pending="x") is None
    assert store.run(run.id).pending == ""
    run.state = "waiting"; store.save(run, NOW)
    claimed = store.claim(run.id, ("waiting", "stopped"), LATER, "queued", pending="ok then", phase="work")
    assert (claimed.state, claimed.pending) == ("queued", "ok then")
    assert store.claim(run.id, ("waiting",), LATER, "queued") is None      # a second tap finds it gone

def test_claim_rejects_an_unknown_field(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    with pytest.raises(AttributeError):
        store.claim(run.id, ("queued",), LATER, "dropped", nonsense=1)

def test_claim_refuses_a_state_that_is_none_of_the_run_states(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    with pytest.raises(ValueError, match="Queued"):
        store.claim(run.id, ("queued",), LATER, "Queued")
    assert store.run(run.id).state == "queued"

def test_a_row_that_still_holds_notes_loads(store):
    """`notes` was a field of an earlier version; its key is skipped on load."""
    run = store.add_run("acme/app", "ACME-1", NOW, skill="fix-this")
    store._conn.execute("UPDATE skill_runs SET data = ? WHERE id = ?",
                        (json.dumps({"skill": "fix-this", "notes": ["an old note"]}), run.id))
    got = store.run(run.id)
    assert got is not None and got.skill == "fix-this" and not hasattr(got, "notes")

def test_anchor_round_trip(store):
    store.add_anchor("-100", 7, "r1", NOW)
    assert store.anchor("-100", 7) == "r1" and store.anchor("-100", 8) is None

def test_in_states_is_oldest_first(store):
    a = store.add_run("acme/app", "ACME-1", "2026-11-16T10:00:00+00:00")
    b = store.add_run("acme/app", "ACME-2", "2026-11-16T09:00:00+00:00")
    assert [r.id for r in store.in_states(("queued",))] == [b.id, a.id]

def test_a_row_from_an_older_version_loads_the_defaults(store):
    run = store.add_run("acme/app", "ACME-1", NOW, skill="fix-this")
    store._conn.execute("UPDATE skill_runs SET data = ? WHERE id = ?",
                        (json.dumps({"skill": "fix-this", "future_field": 1}), run.id))
    got = store.run(run.id)
    assert (got.skill, got.phase, got.outbox, got.facts, got.cleaned) == ("fix-this", "work", [], {}, False)

def test_the_ask_message_ids_pushed_and_the_tokens_are_kept(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    assert (run.ask_message_ids, run.pushed, run.tokens_in, run.tokens_out) == ([], False, 0, 0)
    run.ask_message_ids, run.pushed, run.tokens_in, run.tokens_out = [501, 502], True, 12000, 3400
    store.save(run, LATER)
    got = store.run(run.id)
    assert (got.ask_message_ids, got.pushed, got.tokens_in, got.tokens_out) == ([501, 502], True, 12000, 3400)

def test_a_row_with_an_older_versions_single_ask_message_id_loads_with_none(store):
    run = store.add_run("acme/app", "ACME-1", NOW, skill="fix-this")
    store._conn.execute("UPDATE skill_runs SET data = ? WHERE id = ?",
                        (json.dumps({"skill": "fix-this", "ask_message_id": 501}), run.id))
    got = store.run(run.id)
    assert (got.skill, got.ask_message_ids, got.pushed) == ("fix-this", [], False)

def test_drop_sent_trims_the_stored_outbox_and_keeps_a_reply(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    run.state, run.outbox = "waiting", [{"text": "a", "buttons": True}, {"text": "b", "buttons": False}]
    store.save(run, NOW)
    store.claim(run.id, ("waiting",), NOW, "queued", pending="the login screen")   # a reply in between
    store.drop_sent(run.id, 1, LATER, ask_message_ids=[500, 501])
    got = store.run(run.id)
    assert (got.state, got.pending, got.outbox, got.unsent) == ("queued", "the login screen",
                                                                [{"text": "b", "buttons": False}], 1)
    assert (got.ask_message_ids, got.updated_at) == ([500, 501], LATER)
    store.drop_sent(run.id, 1, LATER)
    got = store.run(run.id)
    assert (got.outbox, got.unsent, got.ask_message_ids) == ([], 0, [500, 501])
    store.drop_sent("nope", 1, LATER)                     # a run that is gone: nothing to trim

def test_mark_parts_adds_to_the_first_entrys_parts_on_the_row_as_it_is_now(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    run.state, run.outbox = "waiting", [{"text": "a", "buttons": True}, {"text": "b", "buttons": False}]
    store.save(run, NOW)
    store.claim(run.id, ("waiting",), NOW, "queued", pending="the login screen")   # a reply in between
    store.mark_parts(run.id, [500], LATER)
    store.mark_parts(run.id, [501], LATER)
    got = store.run(run.id)
    assert got.outbox == [{"text": "a", "buttons": True, "ids": [500, 501]}, {"text": "b", "buttons": False}]
    assert (got.state, got.pending, got.unsent, got.updated_at) == ("queued", "the login screen", 2, LATER)
    store.mark_parts("nope", [1], LATER)                  # a run that is gone: nothing to mark

def test_keep_translation_replaces_the_first_entrys_text_only_while_it_is_that_text_with_no_part_out(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    run.state, run.outbox = "waiting", [{"text": "a", "buttons": True}, {"text": "b", "buttons": False}]
    store.save(run, NOW)
    store.claim(run.id, ("waiting",), NOW, "queued", pending="the login screen")   # a reply in between
    store.keep_translation(run.id, "a", "а", LATER)
    got = store.run(run.id)
    assert got.outbox == [{"text": "а", "buttons": True}, {"text": "b", "buttons": False}]
    assert (got.state, got.pending, got.updated_at) == ("queued", "the login screen", LATER)
    store.keep_translation(run.id, "a", "x", LATER)       # the first entry is no longer that text
    store.mark_parts(run.id, [500], LATER)
    store.keep_translation(run.id, "а", "y", LATER)       # a part of it is out: its parts are counted on it
    assert store.run(run.id).outbox[0] == {"text": "а", "buttons": True, "ids": [500]}
    store.keep_translation("nope", "a", "b", LATER)       # a run that is gone: nothing to keep

def _damage(store, run_id, data):
    store._conn.execute(
        "INSERT INTO skill_runs (id, scope, ticket_id, state, retry_at, unsent, created_at, updated_at, data) "
        "VALUES (?, 'acme/app', 'ACME-1', 'queued', '', 1, ?, ?, ?)", (run_id, NOW, NOW, data))

@pytest.mark.parametrize("data, error", [("{not json", "JSONDecodeError"), ("[1, 2]", "AttributeError"),
                                         ("null", "AttributeError")])
def test_a_damaged_row_is_skipped_with_a_warning(store, caplog, data, error):
    good = store.add_run("acme/app", "ACME-1", NOW)
    good.outbox = [{"text": "hi", "buttons": False}]
    store.save(good, NOW)
    _damage(store, "damaged1", data)
    with caplog.at_level(logging.WARNING, logger="orchestrator.skillflow.store"):
        assert [r.id for r in store.in_states(("queued",))] == [good.id]
        assert [r.id for r in store.unsent_runs()] == [good.id]
        assert [r.id for r in store.runs_for_ticket("acme/app", "ACME-1")] == [good.id]
        assert store.run("damaged1") is None
        assert store.claim("damaged1", ("queued",), LATER, "dropped") is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 5 and all("damaged1" in w and error in w for w in warnings)

def test_the_gated_commit_is_kept(store):
    run = store.add_run("acme/app", "ACME-1", NOW)
    assert run.gated_sha == ""
    run.gated_sha = "b" * 40
    store.save(run, LATER)
    assert store.run(run.id).gated_sha == "b" * 40

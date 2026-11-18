# tests/unit/test_skillflow_compare_store.py
from __future__ import annotations
import pytest
from orchestrator.reviewer.db import connect
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.store import SkillStore

NOW = "2026-11-17T10:00:00+00:00"

def store(tmp_path):
    return ComparisonStore(connect(tmp_path / "reviewer.db"))

def test_add_and_read(tmp_path):
    s = store(tmp_path)
    c = s.add("acme/app", "ACME-1", "run1", NOW, chat_id="-100", workspace_started_at="T0")
    assert c.id.startswith("cmp:") and c.state == "open"
    assert s.get(c.id).chat_id == "-100"
    assert s.for_run("run1").id == c.id
    assert s.for_workspace("acme/app", "ACME-1", "T0").id == c.id
    assert s.for_workspace("acme/app", "ACME-1", "T1") is None

def test_claim_expects_fields(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    s.claim(c.id, ("open",), NOW, "open", messages=2)
    assert s.claim(c.id, ("open",), NOW, "deciding", expect={"messages": 1}, verdict="skill") is None
    taken = s.claim(c.id, ("open",), NOW, "deciding", expect={"messages": 2}, verdict="skill")
    assert taken.state == "deciding" and s.get(c.id).verdict == "skill"

def test_claim_appends_a_message_inside_the_lock(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    s.claim(c.id, ("open",), NOW, "open", message={"text": "a", "buttons": []})
    s.claim(c.id, ("open",), NOW, "open", message={"text": "b", "buttons": []})
    assert [e["text"] for e in s.get(c.id).outbox] == ["a", "b"] and s.get(c.id).unsent == 2

def test_a_note_survives_a_later_claim(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    s.add_note(c.id, "skill is cleaner", NOW)
    s.claim(c.id, ("open",), NOW, "open", fingerprint="x")
    s.add_note(c.id, "but slower", NOW)
    assert s.get(c.id).note == "skill is cleaner\n\nbut slower"

def test_unknown_change_or_state_is_refused(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    with pytest.raises(AttributeError):
        s.claim(c.id, ("open",), NOW, "open", nonsense=1)
    with pytest.raises(ValueError):
        s.claim(c.id, ("open",), NOW, "maybe")

def test_a_damaged_row_is_skipped(tmp_path, caplog):
    s = store(tmp_path); s.add("acme/app", "ACME-1", "run1", NOW); bad = s.add("acme/app", "ACME-2", "run2", NOW)
    s._conn.execute("UPDATE comparisons SET data = '{\"rest_since\": 3, \"x' WHERE id = ?", (bad.id,))
    assert [c.ticket_id for c in s.in_states(("open",))] == ["ACME-1"]
    assert bad.id in caplog.text

def test_a_claim_from_another_state_writes_nothing(tmp_path):
    s = store(tmp_path); c = s.add("acme/app", "ACME-1", "run1", NOW)
    before = s._conn.execute("SELECT * FROM comparisons").fetchall()
    assert s.claim(c.id, ("deciding", "stuck"), "2026-11-17T11:00:00+00:00", "closed", verdict="skill",
                   message={"text": "a", "buttons": []}) is None
    assert s._conn.execute("SELECT * FROM comparisons").fetchall() == before

def test_tally_counts_every_row_by_its_verdict(tmp_path, caplog):
    s = store(tmp_path)
    for state, verdict in (("closed", "skill"), ("closed", "skill"), ("closed", "neither"), ("stuck", "staged"),
                           ("deciding", "staged")):
        c = s.add("acme/app", "ACME-1", "run1", NOW)
        s.claim(c.id, ("open",), NOW, state, verdict=verdict)
    s.add("acme/app", "ACME-2", "run2", NOW)
    bad = s.add("acme/app", "ACME-3", "run3", NOW)
    s.claim(bad.id, ("open",), NOW, "closed", verdict="skill")
    s._conn.execute("UPDATE comparisons SET data = '{\"verdict\": \"sk' WHERE id = ?", (bad.id,))
    assert s.tally() == {"staged": 2, "skill": 2, "neither": 1, "undecided": 1}
    assert bad.id in caplog.text

def test_skill_store_has_anchors(tmp_path):
    runs = SkillStore(connect(tmp_path / "reviewer.db"))
    assert runs.has_anchors() is False
    runs.add_anchor("-100", 5, "run1", NOW)
    assert runs.has_anchors() is True

def test_released_defaults_false_for_stored_runs(tmp_path):
    runs = SkillStore(connect(tmp_path / "reviewer.db"))
    run = runs.add_run("acme/app", "ACME-1", NOW)
    assert runs.run(run.id).released is False


def test_open_tickets_are_the_scopes_comparisons_not_closed(tmp_path):
    store = ComparisonStore(connect(tmp_path / "reviewer.db"))
    now = "2026-11-18T10:00:00+00:00"
    for ticket, state in (("ACME-1", "open"), ("ACME-2", "deciding"), ("ACME-3", "stuck"), ("ACME-4", "closed")):
        c = store.add("acme/app", ticket, f"run-{ticket}", now)
        if state != "open":
            store.claim(c.id, ("open",), now, state, verdict="neither")
    store.add("acme/web", "ACME-5", "run-5", now)
    assert store.open_tickets("acme/app") == frozenset({"ACME-1", "ACME-2", "ACME-3"})
    assert store.open_tickets("acme/none") == frozenset()

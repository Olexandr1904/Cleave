# tests/unit/test_reviewer_store.py
from __future__ import annotations
import dataclasses
import json
import pytest
from orchestrator.reviewer.db import MIGRATIONS, connect
from orchestrator.reviewer.models import BoardRow, JobRun, RepoState
from orchestrator.reviewer.store import ReviewerStore

T0, T1 = "2026-11-20T10:00:00+00:00", "2026-11-20T10:05:00+00:00"

def row(n=1712, head="a" * 40, actions=("review",)):
    return BoardRow(n, "Fix cart sync ACME-1234", f"https://github.com/acme-org/acme-app/pull/{n}", "octo-author",
                    ["reviewer"], head, T0, False, 14, [{"action": a, "threads": []} for a in actions], [], False,
                    False, "ACME-1234", T0, T0)

@pytest.fixture
def store(tmp_path):
    return ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))

def test_migrations_applied(tmp_path):
    conn = connect(tmp_path / "r.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 3

def test_board_roundtrip_and_replace(store):
    store.replace_board("acme/app", {1712: row(), 1720: row(1720)})
    assert store.board("acme/app")[1712] == row()
    assert store.board("acme/app")[1712].my_actions == ["review"]
    store.replace_board("acme/app", {1720: row(1720)})
    assert list(store.board("acme/app")) == [1720]
    assert store.board("other/repo") == {}

def test_repo_state_default_and_save(store):
    assert store.repo_state("acme/app") == RepoState("acme/app")
    store.save_repo_state(RepoState("acme/app", True, "ok", "", 0, T1, T1, T1))
    assert store.repo_state("acme/app").first_poll_done

def test_jobs(store):
    j = store.add_job("acme/app", 1712, "review", "a" * 40, "a" * 40, T0)
    assert (j.state, j.reason, len(j.id)) == ("queued", "", 12)
    store.add_job("acme/app", 1712, "recheck", "a" * 40, "k", T1)
    assert [x.kind for x in store.jobs("acme/app", 1712)] == ["recheck", "review"]
    store.set_job_state(j.id, "cancelled", "turn gone", T1)
    assert store.jobs("acme/app", 1712)[1].state == "cancelled"
    with pytest.raises(ValueError):
        store.set_job_state(j.id, "exploded", "", T1)

def test_skips_and_notices(store):
    store.skip("acme/app", 1712, "a" * 40, T0)
    assert store.is_skipped("acme/app", 1712, "a" * 40) and not store.is_skipped("acme/app", 1712, "b" * 40)
    assert not store.notice_sent("acme/app", 1712, "large", "a" * 40)
    store.mark_notice("acme/app", 1712, "large", "a" * 40, T0)
    assert store.notice_sent("acme/app", 1712, "large", "a" * 40)
    store.clear_notice("acme/app", 1712, "large")
    assert not store.notice_sent("acme/app", 1712, "large", "a" * 40)

def test_skipped_at_is_the_last_skip(store):
    assert store.skipped_at("acme/app", 1712, "a" * 40) == ""
    store.skip("acme/app", 1712, "a" * 40, T0)
    assert store.skipped_at("acme/app", 1712, "a" * 40) == T0
    store.skip("acme/app", 1712, "a" * 40, T1)   # skipped again: a job queued in between is skipped too
    assert store.skipped_at("acme/app", 1712, "a" * 40) == T1
    assert store.skipped_at("acme/app", 1712, "b" * 40) == ""

def test_board_row_ignores_unknown_stored_keys():
    data = json.loads(row().to_json()) | {"future_field": 1}
    assert BoardRow.from_json(json.dumps(data)) == row()

def test_repo_state_ignores_unknown_stored_keys(tmp_path):
    conn = connect(tmp_path / "r.db")
    data = dataclasses.asdict(RepoState("acme/app", True, "ok")) | {"future_field": 1}
    conn.execute("INSERT INTO repo_state (scope, data) VALUES (?, ?)", ("acme/app", json.dumps(data)))
    assert ReviewerStore(conn).repo_state("acme/app") == RepoState("acme/app", True, "ok")

def test_retry_at_and_runnable(store):
    a = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    b = store.add_job("acme/app", 2, "recheck", "b" * 40, "k", T1)
    store.add_job("acme/app", 3, "edit", "c" * 40, "x", T1)
    store.set_job_state(b.id, "deferred", "usage limit", T1, retry_at="2026-11-20T11:00:00+00:00")
    assert store.job(b.id).retry_at == "2026-11-20T11:00:00+00:00"
    assert [j.id for j in store.runnable_jobs("2026-11-20T10:30:00+00:00")] == [a.id]
    assert [j.id for j in store.runnable_jobs("2026-11-20T11:00:00+00:00")] == [a.id, b.id]
    store.set_job_state(a.id, "running", "", T1)
    assert [j.id for j in store.jobs_in_state("running")] == [a.id]
    assert store.job("nope") is None

def test_job_run_roundtrip_and_unsent(store):
    j = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    run = JobRun(j.id, started_at=T0, cost_usd=2.5, report={"items": []}, tg_lines={"1": "x"})
    store.save_run(run)
    assert store.run(j.id) == run
    assert store.done_unsent() == []
    store.set_job_state(j.id, "done", "", T1)
    assert store.done_unsent() == [(store.job(j.id), run)]
    run.message_sent = True
    store.save_run(run)
    assert store.done_unsent() == []

def test_job_run_send_attempts_default_for_old_rows(store):
    j = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    store._conn.execute("INSERT INTO job_runs (job_id, data) VALUES (?, ?)",
                        (j.id, json.dumps({"job_id": j.id, "message_sent": False})))
    run = store.run(j.id)
    assert (run.send_attempts, run.last_attempt_at) == (0, "")
    run.send_attempts, run.last_attempt_at = 3, T1
    store.save_run(run)
    assert (store.run(j.id).send_attempts, store.run(j.id).last_attempt_at) == (3, T1)

def test_anchors_and_prepare_hash(store):
    store.add_anchor("42", 1001, "j1", T0)
    assert store.anchor_job("42", 1001) == "j1" and store.anchor_job("42", 1002) is None
    assert store.prepare_hash("acme/app") == ""
    store.set_prepare_hash("acme/app", "h1", T0)
    store.set_prepare_hash("acme/app", "h2", T1)
    assert store.prepare_hash("acme/app") == "h2"

def test_repo_state_me_defaults_for_old_rows(store):
    store._conn.execute("INSERT INTO repo_state (scope, data) VALUES (?, ?)", ("acme/app", '{"scope": "acme/app"}'))
    assert store.repo_state("acme/app").me == ""

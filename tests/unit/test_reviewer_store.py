# tests/unit/test_reviewer_store.py
from __future__ import annotations
import dataclasses
import json
import pytest
from orchestrator.reviewer.db import MIGRATIONS, connect
from orchestrator.reviewer.models import BoardRow, RepoState
from orchestrator.reviewer.store import ReviewerStore

T0, T1 = "2026-11-20T10:00:00+00:00", "2026-11-20T10:05:00+00:00"

def row(n=1712, head="a" * 40, actions=("review",)):
    return BoardRow(n, "Fix cart sync ACME-1234", f"https://github.com/acme-org/acme-app/pull/{n}", "octo-author",
                    ["reviewer"], head, T0, False, 14, [{"action": a, "threads": []} for a in actions], [], False,
                    False, "ACME-1234", T0, T0)

@pytest.fixture
def store(tmp_path):
    return ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))

def test_migration_2_applied(tmp_path):
    conn = connect(tmp_path / "r.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 2

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

def test_board_row_ignores_unknown_stored_keys():
    data = json.loads(row().to_json()) | {"future_field": 1}
    assert BoardRow.from_json(json.dumps(data)) == row()

def test_repo_state_ignores_unknown_stored_keys(tmp_path):
    conn = connect(tmp_path / "r.db")
    data = dataclasses.asdict(RepoState("acme/app", True, "ok")) | {"future_field": 1}
    conn.execute("INSERT INTO repo_state (scope, data) VALUES (?, ?)", ("acme/app", json.dumps(data)))
    assert ReviewerStore(conn).repo_state("acme/app") == RepoState("acme/app", True, "ok")

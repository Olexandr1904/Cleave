from __future__ import annotations
import threading
from pathlib import Path
import pytest
from config.schemas import LoadedProject, RepoConfig, ReviewerConfig, ReviewerGlobalConfig
import orchestrator.reviewer.db as rdb
from orchestrator.reviewer.db import MIGRATIONS, connect, reviewer_db_path
from orchestrator.switches import Switches, switch_scopes

@pytest.fixture
def db(tmp_path):
    return tmp_path / "rv" / "reviewer.db"

def test_first_boot_defaults(db):
    s = Switches(connect(db))
    assert (s.get("tickets"), s.get("reviews")) == ("paused", "running")

def test_first_boot_defaults_written_once(db):
    Switches(connect(db)).set("tickets", "running", by="telegram")
    assert Switches(connect(db)).get("tickets") == "running"

def test_effective_state_paused_if_either(db):
    s = Switches(connect(db))
    s.set("reviews", "paused", scope="p/r")
    assert not s.is_running("reviews", "p", "r")
    s.set("reviews", "running", scope="p/r")
    s.set("reviews", "paused")
    assert not s.is_running("reviews", "p", "r")
    s.set("reviews", "running")
    assert s.is_running("reviews", "p", "r")

def test_repo_without_row_inherits_global(db):
    s = Switches(connect(db))
    assert s.is_running("reviews", "p", "new") and not s.is_running("tickets", "p", "new")

def test_set_rejects_unknown_mode_and_state(db):
    s = Switches(connect(db))
    with pytest.raises(ValueError):
        s.set("deploys", "running")
    with pytest.raises(ValueError):
        s.set("tickets", "stopped")

def test_listener_called_and_errors_swallowed(db):
    s = Switches(connect(db))
    seen = []
    def boom(*a):
        raise RuntimeError("boom")
    s.add_listener(boom)
    s.add_listener(lambda *a: seen.append(a))
    s.set("tickets", "running", scope="p/r", by="dashboard")
    assert seen == [("tickets", "p/r", "running", "dashboard")]
    assert s.get("tickets", "p/r") == "running"

def test_snapshot_shape(db):
    s = Switches(connect(db))
    s.set("tickets", "paused", scope="p/b")
    snap = s.snapshot({"tickets": ["p/a", "p/b"], "reviews": ["p/b"]})
    assert snap["tickets"] == {"global": "paused", "repos": [
        {"scope": "p/a", "state": "running", "effective": "paused"},
        {"scope": "p/b", "state": "paused", "effective": "paused"}]}
    assert snap["reviews"] == {"global": "running", "repos": [
        {"scope": "p/b", "state": "running", "effective": "running"}]}

def test_switch_scopes():
    on = RepoConfig(reviewer=ReviewerConfig(enabled=True))
    projects = {"p": LoadedProject(repos={"b": on, "a": RepoConfig()})}
    assert switch_scopes(projects) == {"tickets": ["p/a", "p/b"], "reviews": ["p/b"]}

def test_default_db_path_is_under_cleavedata(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reviewer_db_path(ReviewerGlobalConfig()) == tmp_path / "CleaveData" / "reviewer" / "reviewer.db"

def test_connect_creates_dir_and_sets_user_version(db):
    conn = connect(db)
    assert db.parent.is_dir() and (db.parent.stat().st_mode & 0o777) == 0o700
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    connect(db)  # idempotent
    assert connect(db).execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)

def test_set_from_another_thread_persists(db):
    s = Switches(connect(db))
    errors = []
    def run():
        try:
            s.set("tickets", "running", by="dashboard")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert errors == []
    assert Switches(connect(db)).get("tickets") == "running"

def test_failed_migration_rolls_back(db, monkeypatch):
    connect(db)
    version = len(MIGRATIONS)
    bad = "CREATE TABLE half (x TEXT);\nINSERT INTO no_such_table VALUES (1)"
    monkeypatch.setattr(rdb, "MIGRATIONS", [*MIGRATIONS, bad])
    with pytest.raises(Exception):
        connect(db)
    conn = connect_plain(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='half'").fetchone() is None

def connect_plain(path):
    import sqlite3
    return sqlite3.connect(path)

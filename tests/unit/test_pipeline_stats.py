"""Tests for scripts/pipeline_stats.py — read-only reporting over the event store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.pipeline_stats import collect_ticket_stats


def _seed(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            project_id TEXT,
            ticket_id TEXT,
            agent_id TEXT,
            data_json TEXT DEFAULT '{}'
        )
    """)
    conn.executemany(
        "INSERT INTO events (timestamp, event_type, message, project_id, ticket_id,"
        " agent_id, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_counts_stage_entries_and_bounces(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    def transition(ts, frm, to):
        return (ts, "stage_transition", "", "proj", "T-1", None,
                json.dumps({"from_state": frm, "to_state": to}))
    _seed(db, [
        transition(1.0, "NEW", "ANALYSIS"),
        transition(2.0, "ANALYSIS", "PLAN_REVIEW"),
        transition(3.0, "PLAN_REVIEW", "ANALYSIS"),
        transition(4.0, "ANALYSIS", "PLAN_REVIEW"),
        transition(5.0, "PLAN_REVIEW", "DEV"),
        transition(6.0, "DEV", "SCOPE_CHECK"),
        transition(7.0, "SCOPE_CHECK", "DEV"),
        transition(8.0, "DEV", "SCOPE_CHECK"),
        transition(9.0, "SCOPE_CHECK", "CODE_REVIEW"),
    ])

    stats = collect_ticket_stats(str(db))
    assert len(stats) == 1
    s = stats[0]
    assert s.ticket_id == "T-1"
    assert s.project_id == "proj"
    assert s.stage_entries["DEV"] == 2
    assert s.stage_entries["PLAN_REVIEW"] == 2
    assert s.bounces["PLAN_REVIEW→ANALYSIS"] == 1
    assert s.bounces["SCOPE_CHECK→DEV"] == 1
    assert s.terminal_state == "CODE_REVIEW"


def test_sums_tokens_and_duration(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    def run(ts, inp, out, dur):
        return (ts, "agent_execution_detail", "", "proj", "T-1", "dev-agent",
                json.dumps({"input_tokens": inp, "output_tokens": out, "duration": dur}))
    _seed(db, [run(1.0, 100, 20, 5.0), run(2.0, 300, 40, 7.5)])

    s = collect_ticket_stats(str(db))[0]
    assert s.input_tokens == 400
    assert s.output_tokens == 60
    assert s.duration_seconds == 12.5


def test_ignores_events_with_no_ticket(tmp_path: Path) -> None:
    """poll_cycle and daemon_started carry no ticket_id and must not create rows."""
    db = tmp_path / "events.db"
    _seed(db, [(1.0, "poll_cycle", "", None, None, None, "{}")])
    assert collect_ticket_stats(str(db)) == []


def test_terminal_state_breaks_timestamp_ties_by_insertion_order(tmp_path: Path) -> None:
    """Two stage_transition rows with an identical timestamp must resolve by
    insertion order (id), not by whatever order SQLite happens to return for
    a tied sort key."""
    db = tmp_path / "events.db"
    def transition(ts, frm, to):
        return (ts, "stage_transition", "", "proj", "T-1", None,
                json.dumps({"from_state": frm, "to_state": to}))
    _seed(db, [
        transition(5.0, "DEV", "SCOPE_CHECK"),
        transition(5.0, "SCOPE_CHECK", "QA"),
    ])

    s = collect_ticket_stats(str(db))[0]
    assert s.terminal_state == "QA"


def test_a_missing_database_is_not_created(tmp_path: Path) -> None:
    """A module documented as read-only must not leave an empty database
    behind when the path is mistyped — the caller then re-runs against the
    file this run created and gets "no such table: events" forever."""
    missing = tmp_path / "typo.db"
    with pytest.raises(sqlite3.OperationalError):
        collect_ticket_stats(str(missing))
    assert not missing.exists()


def test_reporting_does_not_write_to_the_database(tmp_path: Path) -> None:
    """The connection is opened read-only, so a stray write would raise."""
    db = tmp_path / "events.db"
    _seed(db, [(1.0, "stage_transition", "", "proj", "T-1", None,
                json.dumps({"from_state": "NEW", "to_state": "ANALYSIS"}))])
    before = db.stat().st_mtime_ns
    assert collect_ticket_stats(str(db))[0].terminal_state == "ANALYSIS"
    assert db.stat().st_mtime_ns == before

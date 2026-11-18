# tests/unit/test_skillflow_board.py
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestrator.reviewer.db import connect
from orchestrator.skillflow.board import ComparisonBoard
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.sides import SideView, Workspaces
from orchestrator.skillflow.store import SkillStore

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
NOW = T0.isoformat()
SCOPE = "acme/app"
STORED = SideView("pr_open", True, 13, "u13", "", 42, 1000, 200, "pr_open|3||13")


@pytest.fixture
def board_rig(tmp_path):
    conn = connect(tmp_path / "reviewer.db")
    comparisons = ComparisonStore(conn)

    def comparison(ticket_id, state="open", **changes):
        c = comparisons.add(SCOPE, ticket_id, f"run-{ticket_id}", NOW, summary=f"{ticket_id} summary")
        if state != "open" or changes:
            c = comparisons.claim(c.id, ("open",), NOW, state, **changes)
        return c

    runs = SkillStore(conn)
    board = ComparisonBoard(comparisons, runs, Workspaces(lambda: []), now=lambda: T0)
    return SimpleNamespace(board=board, comparison=comparison, comparisons=comparisons, runs=runs)


def test_overview_tally_and_rows(board_rig):
    board_rig.comparison("ACME-1", state="closed", verdict="skill")
    board_rig.comparison("ACME-2", state="stuck", verdict="staged", reason="502")
    board_rig.comparison("ACME-3", state="open")
    data = board_rig.board.overview()
    assert data["tally"] == {"skill": 1, "staged": 1, "neither": 0, "undecided": 1}
    assert [r["ticket_id"] for r in data["rows"]] == ["ACME-3", "ACME-2", "ACME-1"]
    assert data["rows"][1]["reason"] == "502"


def test_sides_are_stored_once_decided_and_live_while_open(board_rig):
    sides = {"skill": STORED.as_dict(), "staged": STORED.as_dict()}
    board_rig.comparison("ACME-1", state="closed", verdict="neither", sides=sides)
    board_rig.comparison("ACME-2", state="closed", verdict="skill", sides={"skill": STORED.as_dict()})
    board_rig.comparison("ACME-3", state="open", sides=sides)
    open_row, half_stored, closed = board_rig.board.overview()["rows"]
    assert closed["skill_side"] == closed["staged_side"] == STORED.as_dict()
    assert half_stored["skill_side"] == STORED.as_dict() and half_stored["staged_side"]["state"] == "gone"
    assert open_row["skill_side"]["state"] == open_row["staged_side"]["state"] == "gone"


def test_reason_only_while_stuck(board_rig):
    board_rig.comparison("ACME-1", state="closed", verdict="staged", reason="502")
    assert board_rig.board.overview()["rows"][0]["reason"] == ""


# --- the final fix wave ---

def test_the_tally_counts_every_comparison_the_rows_the_newest_200(board_rig):
    for n in range(201):
        board_rig.comparison(f"ACME-{n}", state="closed", verdict="skill")
    data = board_rig.board.overview()
    assert data["tally"] == {"skill": 201, "staged": 0, "neither": 0, "undecided": 0} and len(data["rows"]) == 200


def test_a_skill_side_that_cannot_be_read_is_gone(board_rig, caplog):
    run = board_rig.runs.add_run(SCOPE, "ACME-1", NOW)
    c = board_rig.comparisons.add(SCOPE, "ACME-1", run.id, NOW)
    board_rig.comparisons.claim(c.id, ("open",), NOW, "open", rest_since={"skill": "not a time"})
    [row] = board_rig.board.overview()["rows"]
    assert row["skill_side"]["state"] == "gone" and "ValueError" in caplog.text


# --- the code-review fix wave ---

@pytest.mark.parametrize("state, field, value", [("open", "rest_since", [1, 2]), ("open", "rest_since", "a text"),
                                                 ("closed", "sides", [1, 2]), ("closed", "sides", "a text")])
def test_a_stored_field_of_a_wrong_shape_reads_as_gone(board_rig, caplog, state, field, value):
    board_rig.comparison("ACME-1", state=state, verdict="skill", **{field: value})
    [row] = board_rig.board.overview()["rows"]
    assert row["skill_side"]["state"] == row["staged_side"]["state"] == "gone"
    assert "AttributeError" in caplog.text

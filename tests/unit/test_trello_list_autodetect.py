"""Tests for fuzzy column-name matching."""

from __future__ import annotations

from integrations.trello.list_autodetect import autodetect_status_mapping


def _lst(name, list_id="x", pos=1):
    return {"id": list_id, "name": name, "pos": pos}


def test_standard_board_full_mapping():
    lists = [
        _lst("To Do", "L1", 1),
        _lst("In Progress", "L2", 2),
        _lst("Review", "L3", 3),
        _lst("Done", "L4", 4),
    ]
    result = autodetect_status_mapping(lists)
    assert result == {
        "todo": "L1",
        "in_progress": "L2",
        "in_review": "L3",
        "done": "L4",
    }


def test_alternative_names():
    lists = [
        _lst("Backlog", "B1", 1),
        _lst("Doing", "B2", 2),
        _lst("Code Review", "B3", 3),
        _lst("Shipped", "B4", 4),
    ]
    result = autodetect_status_mapping(lists)
    assert result == {
        "todo": "B1",
        "in_progress": "B2",
        "in_review": "B3",
        "done": "B4",
    }


def test_partial_board_missing_keys_absent():
    """A board missing a Review column returns three keys, not four."""
    lists = [_lst("Todo", "T1", 1), _lst("Doing", "T2", 2), _lst("Done", "T3", 3)]
    result = autodetect_status_mapping(lists)
    assert result == {"todo": "T1", "in_progress": "T2", "done": "T3"}
    assert "in_review" not in result


def test_tie_leftmost_wins():
    """Two lists match in_progress; the leftmost (lower pos) wins."""
    lists = [
        _lst("Doing", "D1", 2),
        _lst("WIP", "D2", 1),
    ]
    result = autodetect_status_mapping(lists)
    assert result["in_progress"] == "D2"


def test_unusual_names_empty():
    lists = [_lst("Sprint 12 — working", "X1"), _lst("Notes", "X2")]
    result = autodetect_status_mapping(lists)
    assert result == {}


def test_case_insensitive_and_normalized():
    lists = [_lst("TO-DO", "X1"), _lst("in_progress", "X2"), _lst("done", "X3")]
    result = autodetect_status_mapping(lists)
    assert result == {"todo": "X1", "in_progress": "X2", "done": "X3"}


def test_empty_input():
    assert autodetect_status_mapping([]) == {}


def test_ignores_lists_without_name():
    lists = [{"id": "X1"}, _lst("Done", "X2")]
    result = autodetect_status_mapping(lists)
    assert result == {"done": "X2"}

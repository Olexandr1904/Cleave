# tests/unit/test_reviewer_board.py
from __future__ import annotations
from dataclasses import replace
from orchestrator.reviewer.board import TurnChange, build_board
from orchestrator.reviewer.models import BoardSnapshot, PRDetails, SnapshotPR, Turn

T0, T1 = "2026-11-20T10:00:00+00:00", "2026-11-20T10:05:00+00:00"

def snap(*prs, me="octo"):
    return BoardSnapshot(me, tuple(prs))

def spr(n, turns, author="octo-author"):
    return SnapshotPR(n, f"Fix {n} ACME-{n}", f"u{n}", author, False, False, tuple(turns), {})

def det(n, head="a" * 40, author="octo-author", assignees=(), requested=("octo",), files=14):
    return PRDetails(n, f"Fix {n} ACME-{n}", f"u{n}", author, "open", head, False, tuple(assignees),
                     tuple(requested), files, "t")

def test_roles_turns_and_fields():
    u = build_board(snap(spr(1, [Turn("octo", "review"), Turn("octo-author", "fix_checks")])),
                    {1: det(1)}, {1: False}, {}, set(), T0)
    r = u.rows[1]
    assert r.roles == ["reviewer"] and r.my_turns == [{"action": "review", "threads": []}]
    assert r.other_turns == [{"login": "octo-author", "action": "fix_checks"}]
    assert (r.ticket_key, r.head_first_seen_at, r.last_change, r.pipeline) == ("ACME-1", T0, T0, False)
    assert [(t.number, t.action) for t in u.new_turns] == [(1, "review")] and u.gone_turns == []

def test_membership_rules():
    s = snap(spr(1, []), spr(2, []), spr(3, []), spr(4, []), spr(5, [], author="octo"))
    d = {1: det(1, requested=()), 2: det(2, requested=(), assignees=("octo",)), 3: det(3, requested=()),
         4: det(4), 5: det(5, author="octo", requested=())}
    u = build_board(s, d, {1: False, 2: False, 3: True, 4: False, 5: False}, {}, set(), T0)
    assert sorted(u.rows) == [2, 3, 4, 5]
    assert (u.rows[2].roles, u.rows[3].roles, u.rows[5].roles) == (["assignee"], ["reviewer"], ["author"])

def test_closed_pr_dropped():
    u = build_board(snap(spr(1, [Turn("octo", "review")])), {}, {}, {}, set(), T0)
    assert u.rows == {} and u.new_turns == []

def test_pr_closed_between_list_and_details_dropped():
    closed = replace(det(1), state="closed")
    u = build_board(snap(spr(1, [Turn("octo", "review")])), {1: closed}, {1: False}, {}, set(), T0)
    assert u.rows == {} and u.new_turns == []

def test_head_first_seen_and_last_change():
    first = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1)}, {1: False}, {}, set(), T0).rows
    same = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1)}, {1: False}, first, set(), T1)
    assert (same.rows[1].head_first_seen_at, same.rows[1].last_change, same.new_turns) == (T0, T0, [])
    pushed = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1, head="b" * 40)}, {1: False}, first, set(), T1)
    assert (pushed.rows[1].head_first_seen_at, pushed.rows[1].last_change) == (T1, T1)

def test_gone_turns_include_dropped_prs():
    prev = build_board(snap(spr(1, [Turn("octo", "review")]), spr(2, [Turn("octo", "merge")], author="octo")),
                       {1: det(1), 2: det(2, author="octo", requested=())}, {}, {}, set(), T0).rows
    u = build_board(snap(spr(1, [])), {1: det(1)}, {}, prev, set(), T1)
    assert sorted((t.number, t.action) for t in u.gone_turns) == [(1, "review"), (2, "merge")]

def test_pipeline_flag():
    u = build_board(snap(spr(7, [Turn("octo", "answer_threads", ("T1", "T2"))], author="octo")),
                    {7: det(7, author="octo", requested=())}, {}, {}, {7}, T0)
    assert u.rows[7].pipeline and u.new_turns[0].threads == ("T1", "T2")

def test_repeated_action_is_one_turn_change():
    # One address_review turn per reviewer who requested changes: one change, first turn's threads.
    turns = [Turn("octo", "address_review", ("T1",), "rev-a"),
             Turn("octo", "address_review", ("T2",), "rev-b")]
    d = {1: det(1, author="octo", requested=())}
    first = build_board(snap(spr(1, turns, author="octo")), d, {}, {}, set(), T0)
    assert first.new_turns == [TurnChange(1, "address_review", ("T1",))]
    gone = build_board(snap(spr(1, [], author="octo")), d, {}, first.rows, set(), T1)
    assert gone.gone_turns == [TurnChange(1, "address_review", ("T1",))]

def test_existing_row_gains_a_turn():
    first = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1)}, {1: False}, {}, set(), T0).rows
    u = build_board(snap(spr(1, [Turn("octo", "review"), Turn("octo", "merge")])), {1: det(1)}, {1: False},
                    first, set(), T1)
    assert u.new_turns == [TurnChange(1, "merge", ())] and u.gone_turns == []

def test_thread_turns_change_moves_with_my_thread_turns_only():
    """When the operator's own thread turns last changed (/code-review C6): another reviewer's turn, a role, the
    head or a turn of mine without threads leave it; a thread turn of mine that goes, or comes back the same,
    moves it."""
    T2, T3 = "2026-11-20T10:10:00+00:00", "2026-11-20T10:15:00+00:00"
    mine = [Turn("octo", "recheck", ("T1",))]
    first = build_board(snap(spr(1, mine)), {1: det(1)}, {1: False}, {}, set(), T0).rows
    assert first[1].thread_turns_change == T0
    others = build_board(snap(spr(1, [*mine, Turn("octo-2", "review"), Turn("octo", "merge")])),
                         {1: det(1, head="b" * 40, assignees=("octo",))}, {1: False}, first, set(), T1).rows
    assert (others[1].last_change, others[1].thread_turns_change) == (T1, T0)
    gone = build_board(snap(spr(1, [])), {1: det(1)}, {1: False}, others, set(), T2).rows
    back = build_board(snap(spr(1, mine)), {1: det(1)}, {1: False}, gone, set(), T3).rows
    assert (gone[1].thread_turns_change, back[1].thread_turns_change) == (T2, T3)
    # A row stored before the field existed starts from its last change.
    old = {1: replace(first[1], thread_turns_change="", last_change=T1)}
    assert build_board(snap(spr(1, mine)), {1: det(1)}, {1: False}, old, set(), T2).rows[1].thread_turns_change == T1


def test_my_turns_change_on_same_head_moves_last_change_only():
    first = build_board(snap(spr(1, [Turn("octo", "answer_threads", ("T1",))])), {1: det(1)}, {1: False},
                        {}, set(), T0).rows
    u = build_board(snap(spr(1, [Turn("octo", "answer_threads", ("T1", "T2"))])), {1: det(1)}, {1: False},
                    first, set(), T1)
    assert (u.rows[1].last_change, u.rows[1].head_first_seen_at) == (T1, T0)

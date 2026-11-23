"""Tests for orchestrator/ticket_actions.py: the ticket actions both Telegram and the dashboard call."""

from __future__ import annotations

import pytest

from orchestrator import ticket_actions
from orchestrator.constants import REPORT_BA, REPORT_DEV, REPORT_QA, REPORT_SCOPE_GUARD
from orchestrator.ticket_actions import TicketResult
from workspace.workspace import Workspace, WorkspaceState

ITERATIONS = {"analysis": 1, "dev": 2, "scope_check": 1, "qa": 3}


def _workspace(root, state, previous_state=None, **fields) -> Workspace:
    """A real workspace on disk at `root`, in `state`, so the transitions are the real ones."""
    root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(str(root), WorkspaceState(
        ticket_id="ACME-1", company_id="acme", repo_id="app", workspace_root=str(root),
        current_state=state, previous_state=previous_state, **fields,
    ))
    ws.save_state()
    return ws


def fresh(ws: Workspace) -> Workspace:
    """The same workspace on disk, put back where the fixture had it: FAILED after QA."""
    return _workspace(ws.root, "FAILED", "QA", stage_iterations=dict(ITERATIONS))


def human_input(ws: Workspace) -> str:
    return (ws.meta_dir / "human-input.md").read_text(encoding="utf-8")


@pytest.fixture
def tmp_ws(tmp_path):
    return _workspace(tmp_path / "ws", "FAILED", "DEV")


@pytest.fixture
def ws_at_gate(tmp_path):
    return _workspace(tmp_path / "ws", "AWAITING_APPROVAL", "PLAN_REVIEW")


@pytest.fixture
def ws_at_qa_gate(tmp_path):
    return _workspace(tmp_path / "ws", "AWAITING_APPROVAL", "QA")


@pytest.fixture
def tmp_ws_with_qa_report(tmp_path):
    ws = _workspace(tmp_path / "ws", "FAILED", "QA", stage_iterations=dict(ITERATIONS))
    (ws.reports_dir / REPORT_QA).write_text("## Status: PASS\n", encoding="utf-8")
    return ws


@pytest.fixture
def blocked_ws(tmp_path):
    return _workspace(tmp_path / "ws", "BLOCKED", "ANALYSIS", human_input_question="May it edit the CI file?")


@pytest.fixture
def pr_review_ws(tmp_path):
    return _workspace(tmp_path / "ws", "PR_REVIEW", "PUSHED")


def test_typed_reject_at_plan_review_goes_back_to_analysis(ws_at_gate):
    assert ticket_actions.reject(ws_at_gate, via="Telegram") == TicketResult("ANALYSIS")
    assert ws_at_gate.state.error is None


def test_reject_at_another_gate_fails_the_ticket(ws_at_qa_gate):
    assert ticket_actions.reject(ws_at_qa_gate, via="dashboard").state == "FAILED"
    assert ws_at_qa_gate.state.error == "Rejected by operator via dashboard"


def test_reject_of_a_ticket_not_at_a_gate_is_refused(tmp_ws):
    result = ticket_actions.reject(tmp_ws, via="dashboard")
    assert result == TicketResult("FAILED", "No workspace awaiting approval for ACME-1.")
    assert tmp_ws.state.error is None


def test_approve_moves_past_the_gate_and_wakes(ws_at_gate):
    woke = []
    assert ticket_actions.approve(ws_at_gate, wake=lambda: woke.append(1)) == TicketResult("DEV")
    assert ws_at_gate.state.current_state == "DEV" and woke == [1]


def test_retry_modes(tmp_ws_with_qa_report):
    assert ticket_actions.retry(tmp_ws_with_qa_report).state == "QA"  # where it stopped
    assert ticket_actions.retry(fresh(tmp_ws_with_qa_report), furthest=True).state == "PUSHED"
    ws = fresh(tmp_ws_with_qa_report)
    r = ticket_actions.retry(ws, stage="dev")
    assert r == TicketResult("DEV") and ws.state.current_state == "DEV"
    assert ws.state.stage_iterations == {"analysis": 1, "dev": 0, "scope_check": 0, "qa": 0}  # fresh from DEV


def test_retry_unknown_stage_is_refused(tmp_ws):
    result = ticket_actions.retry(tmp_ws, stage="nope")
    assert result.refused.startswith("Unknown stage 'nope'.")
    assert tmp_ws.state.current_state == "FAILED"


def test_retry_of_a_running_ticket_is_refused(tmp_path):
    woke = []
    ws = _workspace(tmp_path / "ws", "DEV", "ANALYSIS")
    result = ticket_actions.retry(ws, stage="dev", wake=lambda: woke.append(1))
    assert result == TicketResult("DEV", "ACME-1 is DEV now: nothing was done.") and woke == []


def test_skip_advances_past_the_stage_it_stopped_at(tmp_ws_with_qa_report):
    woke = []
    assert ticket_actions.skip(tmp_ws_with_qa_report, wake=lambda: woke.append(1)) == TicketResult("PUSHED")
    assert tmp_ws_with_qa_report.state.error is None and woke == [1]


def test_skip_of_a_running_ticket_is_refused(pr_review_ws):
    assert ticket_actions.skip(pr_review_ws).refused == "ACME-1 is PR_REVIEW now: nothing was done."


def test_answer_twice_writes_once(blocked_ws):  # Review Focus 4
    woke = []
    assert ticket_actions.answer(blocked_ws, "yes, allowed", wake=lambda: woke.append(1)).state == "ANALYSIS"
    second = ticket_actions.answer(blocked_ws, "yes, allowed")
    assert second.refused == "ACME-1 is ANALYSIS now: nothing was done."
    assert human_input(blocked_ws).count("yes, allowed") == 1 and woke == [1]
    assert "> May it edit the CI file?" in human_input(blocked_ws)  # recorded with the question it answers


def test_an_empty_answer_is_refused(blocked_ws):
    assert ticket_actions.answer(blocked_ws, "  ").refused == "Empty input — nothing to resume with."
    assert blocked_ws.state.current_state == "BLOCKED"


def test_reviewed_sets_the_signal_and_wakes(pr_review_ws):
    woke = []
    assert ticket_actions.reviewed(pr_review_ws, wake=lambda: woke.append(1)).refused == ""
    assert pr_review_ws.state.human_input_reply == "reviewed" and woke == [1]


def test_reviewed_outside_pr_review_is_refused(tmp_ws):
    assert ticket_actions.reviewed(tmp_ws).refused == "No workspace in PR_REVIEW for ACME-1."


class FakeEvents:
    """Records emit(); `last` is the last event with `data` reduced to the keys the tests assert."""

    KEYS = ("comment_id", "decision", "via")

    def __init__(self):
        self.calls = []

    def emit(self, kind, text, ticket_id=None, data=None):
        self.calls.append((kind, data or {}))

    @property
    def last(self):
        kind, data = self.calls[-1]
        return kind, {k: v for k, v in data.items() if k in self.KEYS}


def _comment(comment_id, **fields):
    return {"comment_id": comment_id, "file": "src/app.py", "line": 7, "author": "octo", "decision": None, **fields}


@pytest.fixture
def pr_review_ws_two_comments(tmp_path):
    return _workspace(tmp_path / "ws", "PR_REVIEW", "PUSHED",
                      pending_review_comments=[_comment("11"), _comment("12")])


@pytest.fixture
def pr_review_ws_hint_rounds_3(tmp_path):
    return _workspace(tmp_path / "ws", "PR_REVIEW", "PUSHED",
                      pending_review_comments=[_comment("11", hint_rounds=3)])


def test_a_decision_from_each_channel_is_stored_the_same(pr_review_ws_two_comments):
    events, woke = FakeEvents(), []
    d = ticket_actions.decide_comment(pr_review_ws_two_comments, "11", "wont_fix", "", via="dashboard", events=events)
    assert d.stored == "won't fix: operator decision" and d.remaining == 1 and woke == []
    assert events.last == ("pr_comment_decision_recorded", {"comment_id": "11", "decision": d.stored, "via": "dashboard"})
    d = ticket_actions.decide_comment(pr_review_ws_two_comments, "12", "fix", via="button",
                                      events=events, wake=lambda: woke.append(1))
    assert d.stored == "fix" and d.remaining == 0 and woke == [1]


def test_a_fourth_hint_is_refused(pr_review_ws_hint_rounds_3):
    d = ticket_actions.decide_comment(pr_review_ws_hint_rounds_3, "11", "reinvestigate", "look at the null case",
                                      via="dashboard")
    assert d.refused.startswith("Hint limit reached (3/3)")


def test_an_unknown_comment_is_refused(pr_review_ws_two_comments):
    assert ticket_actions.decide_comment(pr_review_ws_two_comments, "99", "fix", via="dashboard").refused == "Comment not found."


def test_a_hint_is_staged_and_wakes(pr_review_ws_two_comments):
    woke = []
    d = ticket_actions.decide_comment(pr_review_ws_two_comments, "11", "reinvestigate", "check null",
                                      via="reply", wake=lambda: woke.append(1))
    c = pr_review_ws_two_comments.state.pending_review_comments[0]
    assert d.stored == "reinvestigate" and d.remaining == 2 and woke == [1]
    assert c["pending_reinvestigation"] is True and c["last_hint"] == "check null"


def test_a_decision_outside_pr_review_is_refused(tmp_ws):
    assert ticket_actions.decide_comment(tmp_ws, "11", "fix", via="dashboard").refused == \
        "No workspace in PR_REVIEW for ACME-1."


def test_a_reply_decision_emits_the_matched_token_and_raw_text(pr_review_ws_two_comments):
    events = FakeEvents()
    ticket_actions.decide_comment(pr_review_ws_two_comments, "11", "fix", via="reply", matched_token="fix it",
                                  raw_text="fix it", events=events)
    kind, data = events.calls[-1]
    assert kind == "pr_comment_decision_recorded"
    assert data["matched_token"] == "fix it" and data["raw_text"] == "fix it" and data["via"] == "reply"


def test_a_button_decision_emits_the_matched_token_but_no_raw_text(pr_review_ws_two_comments):
    events = FakeEvents()
    ticket_actions.decide_comment(pr_review_ws_two_comments, "11", "fix", via="button",
                                  matched_token="button:fix", events=events)
    data = events.calls[-1][1]
    assert data["matched_token"] == "button:fix" and "raw_text" not in data


def test_the_fourth_hint_emits_exhausted_and_changes_nothing(pr_review_ws_hint_rounds_3):
    events, woke = FakeEvents(), []
    d = ticket_actions.decide_comment(pr_review_ws_hint_rounds_3, "11", "reinvestigate", "look at the null case",
                                      via="reply", events=events, wake=lambda: woke.append(1))
    kind, data = events.calls[-1]
    assert kind == "pr_comment_hint_exhausted" and data["attempted_hint_excerpt"] == "look at the null case"
    c = pr_review_ws_hint_rounds_3.state.pending_review_comments[0]
    assert "last_hint" not in c and "pending_reinvestigation" not in c and c["hint_rounds"] == 3
    assert d.stored == "" and woke == []


@pytest.mark.parametrize("state", ["DONE", "AWAITING_APPROVAL"])
def test_default_retry_of_a_ticket_that_is_not_stopped_is_refused(tmp_path, state):
    ws = _workspace(tmp_path / "ws", state, "QA")
    result = ticket_actions.retry(ws)
    assert result == TicketResult(state, f"ACME-1 is {state} now: nothing was done.")
    assert ws.state.current_state == state


def test_named_stage_retry_of_a_failed_ticket_still_works(tmp_ws):
    assert ticket_actions.retry(tmp_ws, stage="qa").state == "QA"


@pytest.mark.parametrize("judge", ["SCOPE_CHECK", "CODE_REVIEW", "QA"])
def test_furthest_retry_of_a_ticket_blocked_at_a_judge_goes_back_to_that_judge(tmp_path, judge):
    ws = _workspace(tmp_path / "ws", "BLOCKED", judge)
    for name in (REPORT_BA, REPORT_DEV, REPORT_SCOPE_GUARD, REPORT_QA):
        (ws.reports_dir / name).write_text("## Status: FAIL\n", encoding="utf-8")
    assert ticket_actions.retry(ws, furthest=True).state == judge


@pytest.mark.parametrize("report, expected", [
    (REPORT_BA, "PLAN_REVIEW"), (REPORT_DEV, "SCOPE_CHECK"), (REPORT_SCOPE_GUARD, "QA"),
])
def test_furthest_retry_follows_the_furthest_report(tmp_path, report, expected):
    ws = _workspace(tmp_path / "ws", "FAILED", "DEV")
    (ws.reports_dir / report).write_text("x", encoding="utf-8")
    assert ticket_actions.retry(ws, furthest=True).state == expected

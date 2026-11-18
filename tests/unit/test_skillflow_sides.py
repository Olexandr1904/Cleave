# tests/unit/test_skillflow_sides.py
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from orchestrator.skillflow.compare_models import Comparison
from orchestrator.skillflow.models import SkillRun
from orchestrator.skillflow.sides import (
    SideView, Workspaces, end_workspace, skill_view, staged_tokens, staged_view,
)
from workspace.workspace import InvalidTransitionError, Workspace, WorkspaceState

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
T0 = "2026-11-17T10:00:00+00:00"


def logged(minutes: int = 0) -> str:
    """The stamp _write_log puts in front of a line written `minutes` after T0: local time, no zone."""
    return (datetime.fromisoformat(T0) + timedelta(minutes=minutes)).astimezone().strftime("[%Y-%m-%d %H:%M:%S]")


@pytest.fixture
def run_factory():
    def make(**fields):
        base = dict(id="run1", scope="acme/app", ticket_id="ACME-1", state="queued", retry_at="",
                    created_at=T0, updated_at=T0)
        return SkillRun(**{**base, **fields})
    return make


@pytest.fixture
def ws_factory():
    names = count(1)

    def make(tmp_path, state="NEW", **fields):
        root = tmp_path / f"ws{next(names)}"
        root.mkdir()
        ws = Workspace(str(root), WorkspaceState(ticket_id="ACME-1", company_id="acme", repo_id="app",
                                                 workspace_root=str(root), current_state=state,
                                                 started_at=T0, **fields))
        ws.save_state()
        return ws
    return make


@pytest.fixture
def comparison_for():
    def make(ws, started_at=None):
        return Comparison(id="cmp:000000000001", scope="acme/app", ticket_id=ws.state.ticket_id, run_id="run1",
                          state="open", created_at=T0, updated_at=T0, workspace_root=str(ws.root),
                          workspace_started_at=ws.state.started_at if started_at is None else started_at)
    return make


def record_transitions(ws):
    seen = []
    real = ws.transition

    def transition(new_state, **extra):
        seen.append(new_state)
        real(new_state, **extra)
    ws.transition = transition
    return seen


def test_skill_view_of_an_open_pr(run_factory):
    run = run_factory(state="pr_open", pr_number=13, pr_url="u13", created_at="2026-11-17T10:00:00+00:00",
                      tokens_in=800, tokens_out=30, turn_count=4)
    view = skill_view(run, "2026-11-17T11:05:00+00:00", NOW)
    assert (view.at_rest, view.pr_number, view.minutes, view.tokens_in) == (True, 13, 65, 800)


def test_a_waiting_or_interrupted_run_is_not_at_rest(run_factory):
    for state in ("waiting", "interrupted", "queued", "running", "gates", "deferred"):
        assert skill_view(run_factory(state=state), "", NOW).at_rest is False


def test_a_turn_changes_the_fingerprint(run_factory):
    assert skill_view(run_factory(state="stopped", turn_count=3), "", NOW).fingerprint != \
           skill_view(run_factory(state="stopped", turn_count=4), "", NOW).fingerprint


def test_staged_view_reads_the_logs(tmp_path, ws_factory):
    ws = ws_factory(tmp_path, state="PR_REVIEW", pr_number=12, pr_url="u12")
    (ws.logs_dir / "dev.log").write_text(
        f"{logged()} model=m input_tokens=1000 output_tokens=40 tool_calls=3 tool_rounds=2 duration=5.0s\n"
        f"{logged(10)} model=m input_tokens=500 output_tokens=10 tool_calls=0 tool_rounds=0 duration=1.0s\n")
    (ws.logs_dir / "qa.log").write_text("garbage\n")
    view = staged_view(ws, "", NOW)
    assert (view.at_rest, view.pr_number, view.tokens_in, view.tokens_out) == (True, 12, 1500, 50)


def test_pr_review_iterations_do_not_move_the_staged_fingerprint(ws_factory, tmp_path):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    before = staged_view(ws, "", NOW).fingerprint
    ws.increment_iteration("pr_review")
    assert staged_view(ws, "", NOW).fingerprint == before


def test_gone_is_at_rest_with_no_pr():
    assert staged_view(None, "", NOW) == SideView("gone", True, 0, "", "", 0, 0, 0, "gone")


def test_find_matches_started_at(tmp_path, ws_factory, comparison_for):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    found = Workspaces(lambda: [ws]).find(comparison_for(ws))
    assert found is ws
    assert Workspaces(lambda: [ws]).find(comparison_for(ws, started_at="other")) is None


def test_find_reads_a_done_workspace_from_disk(tmp_path, ws_factory, comparison_for):
    ws = ws_factory(tmp_path, state="DONE")
    assert Workspaces(lambda: []).find(comparison_for(ws)).state.current_state == "DONE"


@pytest.mark.parametrize("start, end, hops", [
    ("PR_REVIEW", "DONE", ["DONE"]), ("BLOCKED", "ARCHIVED", ["FAILED", "ARCHIVED"]),
    ("FAILED", "ARCHIVED", ["ARCHIVED"]), ("DONE", "DONE", []), ("ARCHIVED", "ARCHIVED", [])])
def test_end_workspace(tmp_path, ws_factory, start, end, hops):
    ws = ws_factory(tmp_path, state=start)
    seen = record_transitions(ws)
    assert end_workspace(ws) == end and seen == hops


# --- the final fix wave ---

@pytest.mark.parametrize("start", ["DEFERRED", "DEV"])
def test_end_workspace_leaves_a_workspace_that_moved_on(tmp_path, ws_factory, start):
    ws = ws_factory(tmp_path, state=start)
    seen = record_transitions(ws)
    with pytest.raises(InvalidTransitionError):
        end_workspace(ws)
    assert seen == [] and ws.state.current_state == start


@pytest.mark.parametrize("content", ["[1, 2]", '"a text"'])
def test_find_reads_a_state_that_is_no_object_as_gone(tmp_path, ws_factory, comparison_for, caplog, content):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    c = comparison_for(ws)
    ws.state_path.write_text(content)
    with caplog.at_level(logging.WARNING):
        assert Workspaces(lambda: []).find(c) is None
    assert "AttributeError" in caplog.text


def test_a_log_that_cannot_be_read_counts_zero(tmp_path, ws_factory):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    (ws.logs_dir / "ba.log").mkdir()           # reading it raises IsADirectoryError
    (ws.logs_dir / "dev.log").write_text(f"{logged()} model=m input_tokens=1000 output_tokens=40 tool_calls=0\n")
    view = staged_view(ws, "", NOW)
    assert (view.tokens_in, view.tokens_out) == (1000, 40)


def test_reading_the_tokens_creates_no_directory(tmp_path, ws_factory):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    staged_view(ws, "", NOW)
    assert not (ws.root / "logs").exists()
    shutil.rmtree(ws.root)
    staged_view(ws, "", NOW)
    assert not ws.root.exists()


def test_only_the_agents_own_logs_are_counted(tmp_path, ws_factory):
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    (ws.logs_dir / "dev.log").write_text(f"{logged()} model=m input_tokens=1000 output_tokens=40 tool_calls=0\n")
    (ws.logs_dir / "dev-tools.log").write_text(f"{logged()} $ grep 'input_tokens=999 output_tokens=9' a.log\n")
    (ws.logs_dir / "dev.events.log").write_text(f"{logged()} input_tokens=999 output_tokens=9\n")
    view = staged_view(ws, "", NOW)
    assert (view.tokens_in, view.tokens_out) == (1000, 40)


# --- the code-review fix wave ---

@pytest.fixture
def kyiv_time(monkeypatch):
    """The host's local time is UTC+2, as _write_log's time.strftime then writes it."""
    monkeypatch.setenv("TZ", "EET-2")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_only_the_lines_from_the_workspaces_start_on_are_counted(tmp_path, ws_factory):
    # Every workspace of a ticket has the same root, and logs/ outlives a clean: an earlier run's lines are there.
    ws = ws_factory(tmp_path, state="PR_REVIEW")
    (ws.logs_dir / "dev.log").write_text(
        f"{logged(-1)} model=m input_tokens=999 output_tokens=9 tool_calls=0\n"
        f"{logged()} model=m input_tokens=1000 output_tokens=40 tool_calls=0\n"
        f"{logged(5)} model=m input_tokens=500 output_tokens=10 tool_calls=0\n"
        "[not a time] model=m input_tokens=7 output_tokens=7 tool_calls=0\n"
        "model=m input_tokens=3 output_tokens=3 tool_calls=0\n")
    assert staged_tokens(ws.root / "logs", datetime.fromisoformat(T0)) == (1500, 50)
    view = staged_view(ws, "", NOW)
    assert (view.tokens_in, view.tokens_out) == (1500, 50)


def test_a_log_stamp_is_read_as_the_hosts_local_time(tmp_path, ws_factory, kyiv_time):
    ws = ws_factory(tmp_path, state="PR_REVIEW")           # started 10:00 UTC, 12:00 in Kyiv
    (ws.logs_dir / "dev.log").write_text(
        "[2026-11-17 11:30:00] model=m input_tokens=999 output_tokens=9 tool_calls=0\n"     # 09:30 UTC: before
        "[2026-11-17 12:00:00] model=m input_tokens=1000 output_tokens=40 tool_calls=0\n")  # 10:00 UTC: at the start
    assert staged_tokens(ws.root / "logs", datetime.fromisoformat(T0)) == (1000, 40)

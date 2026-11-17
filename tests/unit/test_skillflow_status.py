# tests/unit/test_skillflow_status.py
from __future__ import annotations
import pytest
from orchestrator.skillflow.status import TurnEnd, parse_status_line, read_turn_end

FIXED = "ACME-1 | fixed | tested: partial | review: xhigh/xhigh/high, 2 findings | needs user: no"

@pytest.mark.parametrize("text", [
    FIXED,
    f"\n\n{FIXED}\nMore text.",
    f"`{FIXED}`",
    f"**{FIXED}**",
    f"  {FIXED}  ",
    f"```\n{FIXED}\n```",
    f"```text\n{FIXED}\n```",
    FIXED.replace("needs user: no", "Needs user: NO"),
    FIXED.replace("ACME-1", "acme-1"),
    FIXED.replace("ACME-1", "**ACME-1**"),
    FIXED.replace("needs user: no", "**needs user: no**"),
])
def test_status_line_is_read(text):
    status = parse_status_line(text, "ACME-1")
    assert (status.outcome, status.needs_user) == ("fixed", False)

def test_line_is_cleaned_and_key_is_the_given_one():
    assert parse_status_line(f"**{FIXED}**", "ACME-1").line == FIXED
    assert parse_status_line(FIXED.replace("ACME-1", "acme-1"), "ACME-1").key == "ACME-1"

@pytest.mark.parametrize("text", [
    "**ACME-1** | fixed | needs user: no",
    "ACME-1|fixed   |  needs user: no",
])
def test_line_is_rebuilt_from_the_clean_fields(text):
    assert parse_status_line(text, "ACME-1").line == "ACME-1 | fixed | needs user: no"

@pytest.mark.parametrize("outcome", ["**stopped: low-confidence**", "`stopped: low-confidence`"])
def test_markup_around_the_outcome_is_stripped(outcome):
    end = read_turn_end(f"ACME-1 | {outcome} | needs user: yes", "ACME-1")
    assert (end.kind, end.status.outcome) == ("stop", "stopped: low-confidence")

@pytest.mark.parametrize("text", [
    "",
    "Here is my plan. Reply ok to go on.",
    FIXED.replace("ACME-1", "ACME-2"),                 # another ticket's line
    "ACME-1 | fixed",                                  # no needs-user field
    "ACME-1 | fixed | needs user: maybe",
    "ACME-1 || needs user: yes",                       # no outcome
    "ACME-1 | ** | needs user: no",                    # an outcome of markup only
    "ACME-1 | `` | needs user: no",
    f"Summary first.\n{FIXED}",                        # not the first line
])
def test_not_a_status_line(text):
    assert parse_status_line(text, "ACME-1") is None

@pytest.mark.parametrize("outcome, kind", [
    ("fixed", "result"), ("done", "result"), ("no app fix", "result"),
    ("stopped: low-confidence", "stop"), ("Stopped: no-plan", "stop"),
])
def test_turn_end(outcome, kind):
    end = read_turn_end(f"ACME-1 | {outcome} | needs user: yes", "ACME-1")
    assert end.kind == kind and end.status.needs_user is True

# --- final fix wave: a stop in any markup is a stop ---

@pytest.mark.parametrize("outcome", ["_stopped: x_", "__stopped: x__", "~~stopped: x~~", "**stopped:** x"])
def test_a_stop_in_any_markup_is_a_stop(outcome):
    assert read_turn_end(f"ACME-1 | {outcome} | needs user: yes", "ACME-1").kind == "stop"

def test_the_reviewers_italic_stop_is_a_stop():
    end = read_turn_end("ACME-1 | _stopped: low-confidence_ | needs user: yes", "ACME-1")
    assert (end.kind, end.status.outcome) == ("stop", "stopped: low-confidence")

@pytest.mark.parametrize("field, needs_user", [("needs user: **no**", False), ("needs user: _yes_", True)])
def test_a_wrapped_needs_user_value_is_read(field, needs_user):
    status = parse_status_line(f"ACME-1 | fixed | {field}", "ACME-1")
    assert status is not None and status.needs_user is needs_user

def test_an_outcome_that_names_a_stop_later_is_a_result():
    assert read_turn_end("ACME-1 | fixed, not stopped | needs user: no", "ACME-1").kind == "result"

def test_anything_else_is_waiting():
    assert read_turn_end("Which screen do you mean?", "ACME-1") == TurnEnd("waiting")

# --- code-review fix wave: `_` is markup on both sides of the word ---

@pytest.mark.parametrize("outcome", ["_stopped_: low-confidence", "__stopped__: x", "**_stopped_**: x",
                                     "~~_stopped_~~: x", "[_stopped_] x"])
def test_markup_around_the_word_alone_is_a_stop(outcome):
    assert read_turn_end(f"ACME-1 | {outcome} | needs user: yes", "ACME-1").kind == "stop"

@pytest.mark.parametrize("outcome", ["stoppedX", "stopped2", "unstopped", "not stopped", "fixed, not stopped"])
def test_a_word_that_only_holds_stopped_is_a_result(outcome):
    assert read_turn_end(f"ACME-1 | {outcome} | needs user: yes", "ACME-1").kind == "result"

# tests/unit/test_skillflow_messages.py
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import pytest
from orchestrator.quality_gates import GateResult
from orchestrator.skillflow.messages import (
    cleave_stop_message, deferred_line, failed_line, header, interrupted_message, no_change_message,
    parked_buttons, pr_message, relay, result_note, stop_message, waiting_message, worktree_left_line,
)
from orchestrator.skillflow.models import SkillRun
from orchestrator.skillflow.status import read_turn_end
from orchestrator.tg_i18n import fill, marked_texts, telegram_language
from orchestrator.tg_uk import TEXTS

NOW = "2026-11-16T10:00:00+00:00"
RUN = SkillRun("r1", "acme/app", "ACME-1", "waiting", "", NOW, NOW, skill="fix-this", chat_id="-100",
               outcome="stopped: low-confidence", reason="push rejected: non-fast-forward", phase="push",
               pr_url="https://github.com/acme/app/pull/5", status_line="ACME-1 | fixed | needs user: no")
NOTES = Path("/data/skill/repos/acme/app/ai_instructions/ACME-1")
CUT = "… (cut: see the notes directory for the session's files)"

def test_header_and_buttons():
    assert header(RUN) == "ACME-1 · skill run (fix-this)"
    assert [(b.label, b.action) for b in parked_buttons("r1")] == [("Approve", "sk_ok:r1"), ("Drop", "sk_drop:r1")]

def test_waiting_message_relays_the_text_unchanged():
    text = waiting_message(RUN, "План такий: …\nДай ок.", NOTES)
    assert text.startswith("ACME-1 · skill run (fix-this)\n\nПлан такий: …\nДай ок.\n")
    assert f"Notes: {NOTES}" in text and text.endswith("Reply to answer, or tap Approve to say ok.")

def test_stop_messages():
    assert stop_message(RUN, "t", NOTES).splitlines()[0] == "ACME-1 · skill run (fix-this) — stopped: low-confidence"
    assert cleave_stop_message(RUN).splitlines()[0].endswith("— stopped: push rejected: non-fast-forward")
    assert "Approve tries this step again" in cleave_stop_message(RUN)

def test_pr_message():
    text = pr_message(RUN, [GateResult("test", "t", 0, True, ""), GateResult("lint", "l", 0, True, "")])
    assert "draft PR opened: https://github.com/acme/app/pull/5" in text
    assert "ACME-1 | fixed | needs user: no" in text and "Gates: test ok, lint ok" in text
    assert "Gates: none configured" in pr_message(RUN, [])

def test_relay_cuts_a_long_text():
    text = relay("x" * 20000)
    assert len(text) < 9200 and text.endswith("… (cut: see the notes directory for the session's files)")
    assert relay("short") == "short"

def test_interrupted_and_deferred():
    assert "interrupted by a restart at the push step" in interrupted_message(RUN)
    run = replace(RUN, retry_at="2026-11-16T15:30:00+00:00", reason="usage limit")
    assert deferred_line(run).endswith("— deferred until 15:30 UTC (usage limit)")

def test_a_deferral_to_another_day_names_the_day():
    run = replace(RUN, updated_at="2026-11-16T23:50:00+00:00", retry_at="2026-11-17T01:30:00+00:00",
                  reason="usage limit")
    assert deferred_line(run).endswith("— deferred until 17 Nov 01:30 UTC (usage limit)")

@pytest.mark.parametrize("outcome, first", [
    ("Stopped:  low-confidence ", "— stopped: low-confidence"),
    ("STOPPED: needs a device", "— stopped: needs a device"),
    ("stopped", "— stopped"),
    ("blocked by a flaky test", "— stopped: blocked by a flaky test"),
])
def test_the_stop_condition_is_the_outcome_without_its_stopped(outcome, first):
    assert stop_message(replace(RUN, outcome=outcome), "t", NOTES).splitlines()[0] == f"{header(RUN)} {first}"

# Each closing line offers only what the bot accepts in the run's state: waiting, stopped and interrupted take
# Approve, a reply and Drop; no_change a reply only; a result going on to its PR nothing.
@pytest.mark.parametrize("text, first, closing", [
    (waiting_message(RUN, "t", NOTES), "ACME-1 · skill run (fix-this)",
     "Reply to answer, or tap Approve to say ok."),
    (stop_message(RUN, "t", NOTES), "ACME-1 · skill run (fix-this) — stopped: low-confidence",
     "Reply with new information to continue, or tap Drop."),
    (cleave_stop_message(RUN), "ACME-1 · skill run (fix-this) — stopped: push rejected: non-fast-forward",
     "Approve tries this step again; a reply goes to the session."),
    (result_note(replace(RUN, outcome="fixed"), "t", NOTES), "ACME-1 · skill run (fix-this) — fixed, needs you",
     "Cleave goes on to the commit, its gates and the draft PR."),
    (no_change_message(replace(RUN, outcome="no app fix"), "t", NOTES),
     "ACME-1 · skill run (fix-this) — no app fix (no change)", "A reply resumes the session."),
    (interrupted_message(RUN), "ACME-1 · skill run (fix-this) — interrupted by a restart at the push step",
     "Approve continues it; Drop ends it."),
])
def test_first_and_closing_lines(text, first, closing):
    lines = text.splitlines()
    assert (lines[0], lines[-1]) == (first, closing)

def test_relayed_messages_carry_the_notes_before_the_closing_line():
    for build in (waiting_message, stop_message, result_note, no_change_message):
        assert build(RUN, "t", NOTES).splitlines()[-2:-1] == [f"Notes: {NOTES}"]

def test_cleave_stop_detail_follows_a_blank_line():
    lines = cleave_stop_message(RUN, "test `./gradlew test` exit 1:\nLoginTest failed").splitlines()
    assert lines[1:4] == ["", "test `./gradlew test` exit 1:", "LoginTest failed"]

def test_one_line_messages():
    assert failed_line(replace(RUN, reason="internal error: KeyError")) == (
        "ACME-1 · skill run (fix-this) — failed: internal error: KeyError")
    assert worktree_left_line(RUN, Path("/data/wt/ACME-1"), "it has uncommitted changes") == (
        "ACME-1 · skill run (fix-this) — worktree left at /data/wt/ACME-1: it has uncommitted changes")

def test_a_no_change_without_a_worktree_says_so():
    run = replace(RUN, outcome="fixed")
    text = no_change_message(run, "t", NOTES, missing_worktree=Path("/data/wt/ACME-1"))
    assert text.splitlines()[:2] == ["ACME-1 · skill run (fix-this) — fixed (no change)", "No worktree at /data/wt/ACME-1."]
    assert text.splitlines()[-2:] == [f"Notes: {NOTES}", "A reply resumes the session."]
    assert "No worktree" not in no_change_message(run, "t", NOTES)

@pytest.mark.parametrize("outcome, first", [
    ("_stopped_: low-confidence", "— stopped: low-confidence"),
    ("__stopped__: x", "— stopped: x"),
    ("**_stopped_**: x", "— stopped: x"),
    ("~~_stopped_~~: x", "— stopped: x"),
    ("[_stopped_] x", "— stopped: x"),
    ("**stopped:** x", "— stopped: x"),
])
def test_the_stop_condition_of_a_marked_up_stop_has_no_second_stopped(outcome, first):
    """The outcome as the status reader keeps it, its wrapping markup stripped."""
    status = read_turn_end(f"ACME-1 | {outcome} | needs user: yes", "ACME-1").status
    assert stop_message(replace(RUN, outcome=status.outcome), "t", NOTES).splitlines()[0] == f"{header(RUN)} {first}"

@pytest.mark.parametrize("text", ["", "x" * 30000, "**bold** `code` [link](http://x) _under_ <b>", "\n\n\n"])
def test_the_skills_text_cannot_break_a_builder(text):
    for build in (waiting_message, stop_message, result_note, no_change_message):
        message = build(RUN, text, NOTES)
        assert message.splitlines()[-2:] == [f"Notes: {NOTES}", build(RUN, "t", NOTES).splitlines()[-1]]
        assert (text[:100] in message) and (CUT in message) == (len(text) > 9000)


# --- the language ---

@pytest.fixture
def run():
    return replace(RUN)


@pytest.fixture
def catalogue_uk():
    # The real catalogue: the tests check that the skill flow's own lines are in it.
    return TEXTS


def test_in_ukrainian_the_skills_text_is_marked_for_translation(run, tmp_path, catalogue_uk):
    # The skill writes English; the Telegram setting asks for Ukrainian: its text goes out translated when sent.
    skill_text = "Which screen? Яке поле?"
    with telegram_language("uk"):
        built = [waiting_message(run, skill_text, tmp_path), stop_message(run, skill_text, tmp_path),
                 result_note(run, skill_text, tmp_path), no_change_message(run, skill_text, tmp_path)]
    for text in built:
        assert marked_texts(text) == [skill_text]
    assert "Reply to answer" not in built[0]


def test_in_ukrainian_the_cut_line_is_not_part_of_the_marked_text(catalogue_uk):
    with telegram_language("uk"):
        text = relay("x " * 10000)
    assert marked_texts(text) == [("x " * 10000)[:9000]]
    assert fill(text, {}).endswith(TEXTS[CUT])

def test_cleave_stop_keeps_its_reason(run, catalogue_uk):
    run.reason = "push failed: [rejected] fetch first"
    with telegram_language("uk"):
        assert "push failed: [rejected] fetch first" in cleave_stop_message(run)

def test_buttons_are_translated(catalogue_uk):
    with telegram_language("uk"):
        assert [b.label for b in parked_buttons("r1")] != ["Approve", "Drop"]

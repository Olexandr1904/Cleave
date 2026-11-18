# tests/unit/test_skillflow_runner.py
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import timedelta

import pytest

from integrations.llm.claude_code_adapter import CLIError, QuotaExhaustedError
from orchestrator.skillflow import runner as runner_module
from orchestrator.skillflow.checkout import CheckoutError
from orchestrator.skillflow.messages import parked_buttons
from orchestrator.skillflow.prompts import CONTINUE, CUT_PREFIX
from orchestrator.skillflow.runner import LOGIN_BACKOFF
from tests.unit.skillflow_rig import NOW, T0, TOKEN, Rig

IN_TWO_HOURS = T0 + timedelta(hours=2)
RESULT = "ACME-1 | no app fix | needs user: no"
TIMEOUT = RuntimeError("Claude Code CLI timed out after 14400s")
LOGIN = CLIError("x", result_error="Not logged in · Please run /login")


def boom(*args, **kwargs):
    raise RuntimeError("boom")


# --- the brief's tests ---

async def test_unattended_result_without_a_change_ends_as_no_change(tmp_path):
    rig = Rig(tmp_path, texts=["ACME-1 | no app fix | tested: none | review: -, 0 findings | needs user: no"])
    run = await rig.go()
    assert (run.state, run.outcome, run.session_started) == ("no_change", "no app fix", True)
    assert rig.checkout.calls[:3] == ["prepare", "missing_files", "write_ticket"]
    call = rig.llm.calls[0]
    assert "Arguments: `ACME-1 unattended`" in call["prompt"] and call["resume"] is False
    assert call["session_id"] == run.session_id != ""
    assert "no app fix (no change)" in rig.notifier.sent[-1].text

async def test_a_result_with_no_worktree_says_where_it_looked(tmp_path):
    # A worktree_dir that is not where the skill makes its worktree would end every run as no_change.
    rig = Rig(tmp_path, texts=["ACME-1 | fixed | needs user: no"])
    rig.worktree.rmdir()
    run = await rig.go()
    assert run.state == "no_change" and f"No worktree at {rig.worktree}." in rig.notifier.sent[-1].text

async def test_a_result_with_its_worktree_there_names_none_missing(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT])
    run = await rig.go()
    assert run.state == "no_change" and "No worktree" not in rig.notifier.sent[-1].text

async def test_manual_mode_waits_for_the_operator(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Here is the plan. Reply ok."])
    run = await rig.go()
    assert run.state == "waiting" and "unattended`" not in rig.llm.calls[0]["prompt"]
    sent = rig.notifier.sent[-1]
    assert "Here is the plan. Reply ok." in sent.text and [b.action for b in sent.buttons] == [f"sk_ok:{run.id}", f"sk_drop:{run.id}"]
    assert rig.store.anchor("-100", sent.message_id) == run.id

async def test_approval_resumes_the_same_session(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok.", "ACME-1 | no app fix | needs user: no"])
    run = await rig.go()
    rig.store.claim(run.id, ("waiting",), NOW, "queued", approved=True)
    run = await rig.go()
    second = rig.llm.calls[1]
    assert second["resume"] is True and second["session_id"] == rig.llm.calls[0]["session_id"]
    assert second["prompt"].startswith("ok\n") and run.approved is False and rig.checkout.calls.count("prepare") == 1

async def test_a_reply_text_goes_to_the_session(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Which screen?", "Plan. Reply ok."])
    run = await rig.go()
    rig.store.claim(run.id, ("waiting",), NOW, "queued", pending="the login screen")
    run = await rig.go()
    assert rig.llm.calls[1]["prompt"].startswith("The operator replies:\n\nthe login screen")
    assert (run.state, run.pending) == ("waiting", "")

async def test_the_skills_stop_parks_the_run(tmp_path):
    rig = Rig(tmp_path, texts=["ACME-1 | stopped: low-confidence | tested: none | review: - | needs user: yes\nWhy…"])
    run = await rig.go()
    assert (run.state, run.outcome, run.needs_user) == ("stopped", "stopped: low-confidence", True)
    assert "— stopped: low-confidence" in rig.notifier.sent[-1].text and rig.notifier.sent[-1].buttons

@pytest.mark.parametrize("break_it, reason", [
    (lambda r: setattr(r.checkout, "prepare_error", CheckoutError("the skill checkout has local changes: x")), "local changes"),
    (lambda r: setattr(r.checkout, "missing", ["local.properties"]), "required file missing in the skill checkout: local.properties"),
    (lambda r: r.repo.cfg.skill_flow.build_env.append("ANDROID_HOME"), "build variable ANDROID_HOME is not set"),
    (lambda r: setattr(r.vcs, "error", RuntimeError("boom")), "host facts not read: RuntimeError"),
])
async def test_a_failed_prepare_stops_before_any_session(tmp_path, break_it, reason):
    rig = Rig(tmp_path); break_it(rig)
    run = await rig.go()
    assert run.state == "stopped" and reason in run.reason and rig.llm.calls == [] and run.session_id == ""

async def test_host_facts_leave_cleaves_own_branch_and_pr_out(tmp_path):
    rig = Rig(tmp_path, texts=["ACME-1 | no app fix | needs user: no"])
    rig.checkout.branches = ["feature/ACME-1-acme-1", "ACME-1-old-try"]
    rig.vcs.prs = [(4, "ACME-1: staged", "feature/ACME-1-acme-1"), (9, "ACME-1: by hand", "ACME-1-old-try")]
    await rig.go()
    prompt = rig.llm.calls[0]["prompt"]
    assert "#9 ACME-1: by hand (ACME-1-old-try)" in prompt and "#4" not in prompt

async def test_usage_limit_defers_and_then_continues_the_session(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("limit", retry_at=IN_TWO_HOURS), "ACME-1 | no app fix | needs user: no"])
    run = await rig.go()
    assert (run.state, run.retry_at, run.cut, run.deferrals) == ("deferred", IN_TWO_HOURS.isoformat(), True, 1)
    assert "deferred until" in rig.notifier.sent[-1].text and rig.runner.next_run() is None
    rig.clock.advance(hours=3)
    run = await rig.go()
    again = rig.llm.calls[1]
    assert again["resume"] is True and again["prompt"].startswith(CONTINUE) and run.state == "no_change" and run.deferrals == 0

async def test_a_cut_first_turn_without_a_session_starts_cold(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("limit", retry_at=None), CLIError("No conversation found"),
                               "ACME-1 | no app fix | needs user: no"])
    await rig.go(); rig.clock.advance(hours=2)
    run = await rig.go()
    cold = rig.llm.calls[2]
    assert cold["resume"] is False and cold["session_id"] != rig.llm.calls[0]["session_id"]
    assert "Run this repo's skill" in cold["prompt"] and run.state == "no_change"

async def test_three_deferrals_in_a_row_stop_the_run(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("l", retry_at=None)] * 3)
    for _ in range(3):
        run = await rig.go(); rig.clock.advance(hours=2)
    assert run.state == "stopped" and "deferred 3 times" in run.reason

async def test_a_failed_session_stops_and_approve_tries_again(tmp_path):
    rig = Rig(tmp_path, texts=["Plan. Reply ok.", RuntimeError("Claude Code CLI timed out after 14400s"),
                               "ACME-1 | no app fix | needs user: no"], mode="manual")
    run = await rig.go(); rig.store.claim(run.id, ("waiting",), NOW, "queued", approved=True)
    run = await rig.go()
    assert run.state == "stopped" and run.reason.startswith("session failed: ") and run.approved is True
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    assert (await rig.go()).state == "no_change" and rig.llm.calls[2]["prompt"].startswith(CUT_PREFIX + "ok\n")

async def test_the_slot_is_held_only_during_the_turn(tmp_path):
    rig = Rig(tmp_path, texts=["Plan. Reply ok."], mode="manual")
    rig.llm.on_call = lambda: rig.seen.append(rig.slot.locked())
    await rig.go()
    assert rig.seen == [True] and not rig.slot.locked()
    assert rig.checkout.slot_during_prepare is False

def test_next_run_gates(tmp_path):
    rig = Rig(tmp_path)
    assert rig.runner.next_run() is not None
    for gate in ("tickets_paused", "claude_login", "forge:acme/app"):
        rig.block(gate); assert rig.runner.next_run() is None; rig.unblock(gate)

async def test_restart_interrupts_and_approve_continues_the_same_session(tmp_path):
    rig = Rig(tmp_path, texts=["ACME-1 | no app fix | needs user: no"])
    run = rig.store.in_states(("queued",))[0]
    run.state, run.session_id, run.session_started, run.phase = "running", "sid-1", True, "work"; rig.store.save(run, NOW)
    await rig.runner.start(); await rig.runner.flush_outbox()
    assert rig.store.run(run.id).state == "interrupted"
    assert "interrupted by a restart at the work step" in rig.notifier.sent[-1].text and rig.notifier.sent[-1].buttons
    rig.store.claim(run.id, ("interrupted",), NOW, "queued")
    await rig.go()
    assert rig.llm.calls[0]["resume"] is True and rig.llm.calls[0]["session_id"] == "sid-1"
    assert rig.llm.calls[0]["prompt"].startswith(CONTINUE)

async def test_telegram_down_when_a_run_parks(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok."])
    rig.notifier.fail = True
    run = await rig.go()
    assert run.state == "waiting" and run.unsent == 1 and rig.notifier.sent == []
    rig.notifier.fail = False
    await rig.runner.flush_outbox()
    assert rig.store.run(run.id).unsent == 0 and "Plan. Reply ok." in rig.notifier.sent[-1].text

async def test_an_internal_error_fails_the_run(tmp_path, monkeypatch):
    rig = Rig(tmp_path, texts=["x"])
    monkeypatch.setattr(runner_module, "read_turn_end", boom)
    run = await rig.go()
    assert (run.state, run.reason) == ("failed", "internal error: RuntimeError")

async def test_state_changes_are_emitted(tmp_path):
    rig = Rig(tmp_path, texts=["ACME-1 | no app fix | needs user: no"])
    await rig.go()
    assert [e.data["state"] for e in rig.events if e.type == "skill_run_state"] == ["running", "no_change"]


# --- the addendum's tests ---

async def test_the_key_is_filled_in_without_format(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT])
    rig.repo.cfg.skill_flow.notes_dir = "notes/{key}/{not-a-field}"
    rig.repo.cfg.skill_flow.worktree_dir = "worktrees/{key}-{0}"
    run = await rig.go()
    assert run.state == "no_change" and rig.checkout.ticket == "notes/ACME-1/{not-a-field}"
    assert ("has_change", rig.checkout.path / "worktrees/ACME-1-{0}") in rig.checkout.calls
    assert f"Notes: {rig.checkout.path / 'notes/ACME-1/{not-a-field}'}" in rig.notifier.sent[-1].text

async def test_a_reply_while_the_parked_message_goes_out_is_kept(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok."])
    run_id = rig.store.in_states(("queued",))[0].id
    rig.notifier.during = lambda: rig.store.claim(run_id, ("waiting",), NOW, "queued", pending="the login screen")
    run = await rig.go()
    assert (run.state, run.pending, run.outbox, run.unsent) == ("queued", "the login screen", [], 0)
    assert "Plan. Reply ok." in rig.notifier.sent[-1].text and run.ask_message_ids == [rig.notifier.sent[-1].message_id]

async def test_the_ask_message_ids_are_every_part_of_the_newest_message_with_buttons(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan.\n" * 1500, RESULT])
    run = await rig.go()
    asked = [m.message_id for m in rig.notifier.sent]
    assert len(asked) >= 2 and rig.notifier.sent[-1].buttons and run.ask_message_ids == asked
    rig.store.claim(run.id, ("waiting",), NOW, "queued", approved=True)
    run = await rig.go()
    assert run.state == "no_change" and rig.notifier.sent[-1].message_id not in asked
    assert run.ask_message_ids == asked

async def test_a_message_with_buttons_that_sends_no_part_keeps_the_ask_message_ids(tmp_path):
    rig = Rig(tmp_path)
    [run] = rig.store.in_states(("queued",))
    run.state, run.ask_message_ids, run.outbox = "waiting", [7], [{"text": " \n ", "buttons": True}]
    rig.store.save(run, NOW)
    await rig.runner.flush_outbox()
    got = rig.store.run(run.id)
    assert (got.outbox, got.ask_message_ids) == ([], [7]) and rig.notifier.sent == []

def _internal_error(rig, monkeypatch):
    monkeypatch.setattr(runner_module, "read_turn_end", boom)

@pytest.mark.parametrize("texts, setup, buttons", [
    (["Which screen?"], None, True),                                             # waiting
    (["ACME-1 | stopped: low-confidence | needs user: no"], None, True),         # the skill's stop
    ([], lambda r, m: setattr(r.checkout, "missing", ["local.properties"]), True),   # Cleave's stop
    ([RESULT], None, False),                                                     # no change
    ([QuotaExhaustedError("limit", retry_at=None)], None, False),                # the deferred line
    (["x"], _internal_error, False),                                             # the failed line
])
async def test_only_a_parked_message_carries_the_buttons(tmp_path, monkeypatch, texts, setup, buttons):
    rig = Rig(tmp_path, texts=texts)
    if setup:
        setup(rig, monkeypatch)
    run = await rig.go()
    assert rig.notifier.sent[-1].buttons == (parked_buttons(run.id) if buttons else None)

async def test_a_result_note_goes_out_once_and_the_run_goes_on_to_publish(tmp_path, monkeypatch):
    rig = Rig(tmp_path, texts=["ACME-1 | fixed | needs user: yes\nCheck the copy on the login screen."])
    rig.checkout.changed, published = True, []

    async def publish(repo, run):            # the commit step (Task 12); the failure saves the run once more
        published.append(run.phase)
        raise RuntimeError("the commit step")

    monkeypatch.setattr(rig.runner, "_publish", publish)
    run = await rig.go()
    assert published == ["commit"] and (run.state, run.phase, run.unsent) == ("failed", "commit", 0)
    assert [m.buttons for m in rig.notifier.sent] == [None, None]
    note, failed = (m.text for m in rig.notifier.sent)             # the note went out once
    assert "— fixed, needs you" in note and "Check the copy on the login screen." in note
    assert failed.endswith("— failed: internal error: RuntimeError")

async def test_tokens_cost_and_turns_add_up_over_the_turns(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok.", RESULT])
    run = await rig.go()
    rig.store.claim(run.id, ("waiting",), NOW, "queued", approved=True)
    run = await rig.go()
    assert (run.tokens_in, run.tokens_out, run.cost_usd, run.turns, run.turn_count) == (2000, 400, 1.0, 6, 2)

async def test_a_build_variable_gone_before_a_later_turn_stops_the_run(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok."])
    run = await rig.go()
    env = rig.llm.calls[0]["env"]
    assert env["JAVA_HOME"] == "/jdk" and env["GIT_AUTHOR_NAME"] == "Cleave Bot" and "GITHUB_TOKEN" not in env
    del rig.environ["JAVA_HOME"]
    rig.store.claim(run.id, ("waiting",), NOW, "queued", approved=True)
    run = await rig.go()
    assert (run.state, run.reason) == ("stopped", "build variable JAVA_HOME is not set in the daemon's environment")
    assert len(rig.llm.calls) == 1 and run.approved is True and rig.notifier.sent[-1].buttons

# --- the controller's follow-up: a turn that started and did not end ok is cut ---

async def test_a_failed_first_turn_is_continued_and_then_started_cold(tmp_path):
    rig = Rig(tmp_path, texts=[RuntimeError("Claude Code CLI timed out after 14400s"),
                               CLIError("No conversation found"), RESULT])
    run = await rig.go()
    assert (run.state, run.cut, run.session_started) == ("stopped", True, False)
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    first, resumed, cold = rig.llm.calls
    assert (resumed["prompt"], resumed["resume"], resumed["session_id"]) == (CONTINUE, True, first["session_id"])
    assert cold["resume"] is False and cold["session_id"] != first["session_id"]
    assert "Run this repo's skill" in cold["prompt"] and (run.state, run.cut) == ("no_change", False)

async def test_a_restart_in_the_first_turn_is_continued_and_then_started_cold(tmp_path):
    rig = Rig(tmp_path, texts=[asyncio.CancelledError(), CLIError("No conversation found"), RESULT])
    run = rig.runner.next_run()
    with pytest.raises(asyncio.CancelledError):          # the daemon stops in the middle of the turn
        await rig.runner.run_job(run)
    left = rig.store.run(run.id)
    assert (left.state, left.cut, left.session_started) == ("running", True, False)
    await rig.runner.start()
    rig.store.claim(run.id, ("interrupted",), NOW, "queued")
    run = await rig.go()
    first, resumed, cold = rig.llm.calls
    assert (resumed["prompt"], resumed["resume"], resumed["session_id"]) == (CONTINUE, True, first["session_id"])
    assert cold["resume"] is False and cold["session_id"] != first["session_id"]
    assert "Run this repo's skill" in cold["prompt"] and run.state == "no_change"

async def test_a_worktree_that_cannot_be_read_stops_the_run_and_approve_tries_again(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT, RESULT])
    rig.checkout.change_error = CheckoutError("git status failed: fatal: not a git repository")
    run = await rig.go()
    assert (run.state, run.reason) == ("stopped", "worktree state not read: git status failed: fatal: not a git "
                                                  "repository")
    assert rig.notifier.sent[-1].buttons == parked_buttons(run.id)
    rig.checkout.change_error = None
    rig.store.claim(run.id, ("stopped",), NOW, "queued", approved=True)
    run = await rig.go()
    assert run.state == "no_change" and rig.llm.calls[1]["resume"] is True


async def test_a_damaged_row_does_not_stall_the_loop(tmp_path, caplog):
    rig = Rig(tmp_path, texts=[RESULT])
    healthy = rig.store.in_states(("queued",))[0]
    rig.store._conn.execute(
        "INSERT INTO skill_runs (id, scope, ticket_id, state, retry_at, unsent, created_at, updated_at, data) "
        "VALUES ('damaged1', 'acme/app', 'ACME-2', 'queued', '', 1, ?, ?, '{not json')",
        ((T0 - timedelta(hours=1)).isoformat(), NOW))
    with caplog.at_level(logging.WARNING):
        assert rig.runner.next_run().id == healthy.id
        stop = asyncio.Event()
        task = asyncio.create_task(rig.runner.loop(stop))
        for _ in range(500):
            if rig.store.run(healthy.id).state == "no_change":
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert rig.store.run(healthy.id).state == "no_change"
    assert "damaged1" in caplog.text and "cycle failed" not in caplog.text


# --- review round 1 ---

@pytest.mark.parametrize("park", ["stopped", "interrupted"])
async def test_a_reply_to_a_cut_first_turn_reaches_the_session(tmp_path, park):
    rig = Rig(tmp_path, mode="manual", texts=[TIMEOUT if park == "stopped" else asyncio.CancelledError(),
                                              "Plan. Reply ok."])
    run = rig.runner.next_run()
    if park == "stopped":
        await rig.runner.run_job(run)
    else:
        with pytest.raises(asyncio.CancelledError):      # the daemon stops in the middle of the turn
            await rig.runner.run_job(run)
        await rig.runner.start()
    rig.store.claim(run.id, (park,), NOW, "queued", pending="skip the UI tests, they hang")
    run = await rig.go()
    resumed = rig.llm.calls[1]
    assert resumed["resume"] is True and resumed["session_id"] == rig.llm.calls[0]["session_id"]
    assert resumed["prompt"].startswith(CUT_PREFIX + "The operator replies:\n\nskip the UI tests, they hang")
    assert (run.state, run.pending) == ("waiting", "")

async def test_a_cut_first_turn_without_a_reply_is_continued_with_the_bare_continue(tmp_path):
    rig = Rig(tmp_path, texts=[TIMEOUT, RESULT])
    run = await rig.go()
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    await rig.go()
    assert rig.llm.calls[1]["prompt"] == CONTINUE

async def test_a_resumed_first_turn_that_times_out_stops_and_starts_no_second_session(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("limit", retry_at=None), TIMEOUT, RESULT])
    await rig.go(); rig.clock.advance(hours=2)
    run = await rig.go()
    assert (run.state, run.reason) == ("stopped", "session failed: Claude Code CLI timed out after 14400s")
    first, resumed = rig.llm.calls
    assert resumed["resume"] is True and resumed["session_id"] == first["session_id"] == run.session_id

async def test_the_cold_start_reads_the_clis_words_in_any_case(tmp_path):
    rig = Rig(tmp_path, texts=[TIMEOUT, CLIError("Claude Code returned error: NO CONVERSATION FOUND with session "
                                                 "ID: x"), RESULT])
    run = await rig.go()
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert run.state == "no_change" and rig.llm.calls[2]["resume"] is False

async def test_a_run_dropped_between_next_run_and_run_job_is_skipped(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT])
    run = rig.runner.next_run()
    rig.store.claim(run.id, ("queued",), NOW, "dropped")
    await rig.runner.run_job(run)
    assert rig.store.run(run.id).state == "dropped" and rig.llm.calls == [] and rig.checkout.calls == []
    assert rig.events == []

async def test_the_limit_wait_holds_every_run_and_ends(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("limit", retry_at=IN_TWO_HOURS)])
    other = rig.add_run("ACME-2", (T0 + timedelta(minutes=1)).isoformat())
    run = await rig.go()
    assert run.state == "deferred" and rig.runner.next_run() is None
    rig.clock.advance(hours=1, minutes=59)
    assert rig.runner.next_run() is None
    rig.clock.advance(minutes=1)
    assert rig.runner.next_run().id == run.id != other.id          # the oldest first, at the reset time exactly

async def test_a_past_reset_time_is_an_hour_from_now(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("limit", retry_at=T0 - timedelta(minutes=5))])
    run = await rig.go()
    assert (run.state, run.retry_at, run.reason) == ("deferred", (T0 + timedelta(hours=1)).isoformat(),
                                                     "usage limit reached")

async def test_a_login_failure_defers_fifteen_minutes_and_holds_every_run(tmp_path):
    rig = Rig(tmp_path, texts=[LOGIN])
    rig.add_run("ACME-2", (T0 + timedelta(minutes=1)).isoformat())
    run = await rig.go()
    assert (run.state, run.reason, run.retry_at, run.cut, run.deferrals) == (
        "deferred", "claude login failing", (T0 + LOGIN_BACKOFF).isoformat(), True, 1)
    sent = rig.notifier.sent[-1]
    assert sent.buttons is None and sent.text.endswith("deferred until 10:15 UTC (claude login failing)")
    assert rig.runner.next_run() is None
    rig.clock.advance(minutes=15)
    assert rig.runner.next_run().id == run.id

async def test_one_line_per_wait_then_the_stop_with_buttons(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("l", retry_at=None)] * 3)
    rig.add_run("ACME-2", (T0 + timedelta(minutes=1)).isoformat())
    for _ in range(3):
        await rig.go(); rig.clock.advance(hours=2)
    texts = [m.text for m in rig.notifier.sent]
    assert len(texts) == 2 and "deferred until" in texts[0] and "stopped: deferred 3 times" in texts[1]
    assert rig.notifier.sent[0].buttons is None and rig.notifier.sent[1].buttons
    rig.clock.advance(hours=-2)
    assert rig.runner.next_run() is None          # the third limit holds the other run too
    assert [e.data["state"] for e in rig.events] == ["running", "deferred", "running", "deferred", "running",
                                                     "stopped"]

async def test_a_deferral_after_an_approve_says_so_again(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok.", QuotaExhaustedError("l", retry_at=None),
                                              TIMEOUT, QuotaExhaustedError("l", retry_at=None)])
    run = await rig.go()
    rig.store.claim(run.id, ("waiting",), NOW, "queued", approved=True)
    run = await rig.go(); rig.clock.advance(hours=2)
    assert run.state == "deferred" and len(rig.notifier.sent) == 2
    run = await rig.go()
    assert run.state == "stopped" and len(rig.notifier.sent) == 3
    rig.store.claim(run.id, ("stopped",), rig.clock.now().isoformat(), "queued")
    run = await rig.go()
    assert (run.state, run.deferrals) == ("deferred", 2)
    assert len(rig.notifier.sent) == 4 and "deferred until" in rig.notifier.sent[-1].text

async def test_an_error_while_the_parked_message_goes_out_keeps_the_reply(tmp_path, caplog):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok."])
    run_id = rig.store.in_states(("queued",))[0].id

    async def flush():
        rig.store.claim(run_id, ("waiting",), NOW, "queued", pending="the login screen")
        raise RuntimeError("boom")

    rig.runner.flush_outbox = flush
    with caplog.at_level(logging.WARNING):
        run = await rig.go()
    assert (run.state, run.pending, run.unsent) == ("queued", "the login screen", 1)
    assert [e.data["state"] for e in rig.events] == ["running", "waiting"]

async def test_a_parking_save_that_fails_ends_the_run_as_failed(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok."])
    real, failed = rig.store.save, []

    def save(run, now):
        if run.state == "waiting" and not failed:
            failed.append(run.state)
            raise sqlite3.OperationalError("database is locked")
        real(run, now)

    rig.store.save = save
    run = await rig.go()
    assert failed and (run.state, run.reason) == ("failed", "internal error: OperationalError")
    [sent] = rig.notifier.sent                     # the waiting message never stood: only the failed line
    assert sent.text.endswith("— failed: internal error: OperationalError") and sent.buttons is None

async def test_recover_keeps_what_was_unsent_and_adds_one_message(tmp_path):
    rig = Rig(tmp_path)
    run = rig.store.in_states(("queued",))[0]
    run.state, run.phase, run.outbox = "gates", "gates", [{"text": "note", "buttons": False}]
    rig.store.save(run, NOW)
    await rig.runner.start(); await rig.runner.start()              # a second start adds nothing
    got = rig.store.run(run.id)
    assert got.state == "interrupted" and [e["buttons"] for e in got.outbox] == [False, True]
    assert "at the gates step" in got.outbox[1]["text"] and got.reason == "daemon restarted"
    assert [e.data["state"] for e in rig.events] == ["interrupted"]

async def _until(condition, tries=300):
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0.01)

async def test_the_loop_sends_what_an_outbox_still_holds(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan. Reply ok."])
    rig.notifier.fail = True
    run = await rig.go()
    assert run.unsent == 1
    rig.notifier.fail = False
    stop = asyncio.Event()
    task = asyncio.create_task(rig.runner.loop(stop))
    await _until(lambda: rig.notifier.sent)
    stop.set(); await asyncio.wait_for(task, 5)
    assert rig.store.run(run.id).unsent == 0 and "Plan. Reply ok." in rig.notifier.sent[-1].text

async def test_wake_cuts_the_idle_wait_short(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT], idle_seconds=3600)
    run_id = rig.store.in_states(("queued",))[0].id
    rig.block("tickets_paused")
    stop = asyncio.Event()
    task = asyncio.create_task(rig.runner.loop(stop))
    await asyncio.sleep(0.05)                       # a cycle found nothing to run: it waits an hour
    rig.unblock("tickets_paused"); rig.runner.wake()
    await _until(lambda: rig.store.run(run_id).state == "no_change")
    stop.set(); await asyncio.wait_for(task, 5)
    assert rig.store.run(run_id).state == "no_change"

@pytest.mark.parametrize("state, retry_at, unsent, data, error", [
    ("deferred", "", 0, "{}", "ValueError"),                    # next_run reads the retry time
    ("deferred", "soon", 0, "{}", "ValueError"),
    ("stopped", "", 1, '{"outbox": [1]}', "TypeError"),         # flush_outbox reads the entries
    ("stopped", "", 1, '{"outbox": null}', "TypeError"),
])
async def test_a_field_of_a_wrong_shape_holds_back_only_its_own_run(tmp_path, caplog, state, retry_at, unsent,
                                                                     data, error):
    rig = Rig(tmp_path, texts=[RESULT])
    healthy = rig.store.in_states(("queued",))[0]
    rig.store._conn.execute(
        "INSERT INTO skill_runs (id, scope, ticket_id, state, retry_at, unsent, created_at, updated_at, data) "
        "VALUES ('odd1', 'acme/app', 'ACME-2', ?, ?, ?, ?, ?, ?)",
        (state, retry_at, unsent, (T0 - timedelta(hours=1)).isoformat(), NOW, data))
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(rig.runner.loop(stop))
        await _until(lambda: rig.store.run(healthy.id).state == "no_change")
        stop.set()
        await asyncio.wait_for(task, 5)
    assert rig.store.run(healthy.id).state == "no_change" and "cycle failed" not in caplog.text
    assert any("odd1" in r.getMessage() and error in r.getMessage() for r in caplog.records)
    if state == "stopped":                          # the damaged outbox holds back none of the healthy run's
        assert rig.store.run(healthy.id).unsent == 0 and "no app fix (no change)" in rig.notifier.sent[-1].text

async def test_a_session_error_reaches_the_reason_redacted(tmp_path, caplog):
    rig = Rig(tmp_path, texts=[CLIError(f"Claude Code returned error: the remote is https://x:{TOKEN}@github.com")])
    with caplog.at_level(logging.DEBUG):
        run = await rig.go()
    assert run.reason == "session failed: Claude Code returned error: the remote is https://x:***@github.com"
    assert TOKEN not in caplog.text and all(TOKEN not in m.text for m in rig.notifier.sent)

async def test_approve_after_a_failed_prepare_prepares_again_and_starts_the_session(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT])
    rig.vcs.error = RuntimeError("boom")
    run = await rig.go()
    assert (run.state, run.session_id, rig.checkout.calls.count("write_ticket")) == ("stopped", "", 1)
    rig.vcs.error = None
    rig.store.claim(run.id, ("stopped",), NOW, "queued", approved=True)
    run = await rig.go()
    assert run.state == "no_change" and run.session_id != "" and rig.checkout.calls.count("prepare") == 2
    [call] = rig.llm.calls
    assert call["resume"] is False and "Run this repo's skill" in call["prompt"]


# --- Task 12's addendum: Task 11's leftovers ---

async def test_a_stop_whose_save_fails_ends_the_run_as_failed(tmp_path):
    rig = Rig(tmp_path)
    rig.checkout.missing = ["local.properties"]
    real, failed = rig.store.save, []

    def save(run, now):
        if run.state == "stopped" and not failed:
            failed.append(run.state)
            raise sqlite3.OperationalError("database is locked")
        real(run, now)

    rig.store.save = save
    run = await rig.go()
    assert failed and (run.state, run.reason) == ("failed", "internal error: OperationalError")

async def test_recover_skips_a_damaged_row_and_moves_the_others(tmp_path, caplog):
    rig = Rig(tmp_path)
    healthy = rig.store.in_states(("queued",))[0]
    healthy.state = "running"; rig.store.save(healthy, NOW)
    rig.store._conn.execute(
        "INSERT INTO skill_runs (id, scope, ticket_id, state, retry_at, unsent, created_at, updated_at, data) "
        "VALUES ('odd1', 'acme/app', 'ACME-2', 'running', '', 0, ?, ?, '{\"outbox\": null}')",
        ((T0 - timedelta(hours=1)).isoformat(), NOW))
    with caplog.at_level(logging.WARNING):
        rig.runner.recover()
    assert rig.store.run(healthy.id).state == "interrupted" and rig.store.run("odd1").state == "running"
    assert any("odd1" in r.getMessage() and "TypeError" in r.getMessage() for r in caplog.records)

async def test_the_cold_start_reads_the_whole_error_text(tmp_path):
    missing = CLIError("Claude Code returned error: " + "x" * 400 + " No conversation found with session ID: s")
    rig = Rig(tmp_path, texts=[TIMEOUT, missing, RESULT])
    run = await rig.go()
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert run.state == "no_change" and rig.llm.calls[2]["resume"] is False


# --- code-review fix wave: the runner ---

async def test_a_restart_while_a_run_waits_for_the_slot_cuts_no_turn(tmp_path):
    rig = Rig(tmp_path, texts=[RESULT])
    run = rig.runner.next_run()
    await rig.slot.acquire()                            # a staged build holds the slot
    job = asyncio.create_task(rig.runner.run_job(run))
    await _until(lambda: rig.store.run(run.id).session_id)
    await asyncio.sleep(0.05)                           # prepared, now waiting for the slot
    job.cancel()                                        # the daemon stops
    with pytest.raises(asyncio.CancelledError):
        await job
    rig.slot.release()
    left = rig.store.run(run.id)
    assert (left.state, left.cut, left.turn_count, rig.llm.calls) == ("running", False, 0, [])
    await rig.runner.start()
    rig.store.claim(run.id, ("interrupted",), NOW, "queued")
    run = await rig.go()
    [first] = rig.llm.calls
    assert first["resume"] is False and "Run this repo's skill" in first["prompt"] and run.state == "no_change"

async def test_a_new_limit_after_an_ok_turn_in_a_job_taken_from_deferred_is_told(tmp_path):
    rig = Rig(tmp_path, texts=[QuotaExhaustedError("l", retry_at=None), "ACME-1 | fixed | needs user: no",
                               QuotaExhaustedError("l", retry_at=None)])
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    run = await rig.go()
    assert run.state == "deferred" and len(rig.notifier.sent) == 1
    rig.clock.advance(hours=2)
    run = await rig.go()                               # the work turn ends ok; the commit turn meets a new limit
    assert (run.state, run.phase) == ("deferred", "commit")
    assert [m.text for m in rig.notifier.sent if "deferred until" in m.text] and len(rig.notifier.sent) == 2

async def test_a_message_that_fails_on_a_later_part_goes_on_where_it_stopped(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Plan.\n" * 1400])     # a waiting message of three parts
    failures = []

    def part_two_fails_twice():
        rig.notifier.fail = len(rig.notifier.sent) == 1 and len(failures) < 2
        if rig.notifier.fail:
            failures.append(1)

    rig.notifier.during = part_two_fails_twice
    run = await rig.go()
    assert (run.state, len(rig.notifier.sent), run.unsent) == ("waiting", 1, 1)
    await rig.runner.flush_outbox()                    # part 2 fails again
    await rig.runner.flush_outbox()
    texts = [m.text for m in rig.notifier.sent]
    assert len(texts) == 3 and texts.count(texts[0]) == 1 and rig.notifier.sent[-1].buttons
    got = rig.store.run(run.id)
    assert (got.unsent, got.ask_message_ids) == (0, [500, 501, 502])


# --- the language ---

async def test_runner_messages_follow_the_language(tmp_path):
    rig = Rig(tmp_path, mode="manual", texts=["Which screen?"], language_of=lambda scope: "uk")
    await rig.go()
    sent = rig.notifier.sent[-1]
    assert "Which screen?" in sent.text and "Reply to answer, or tap Approve to say ok." not in sent.text
    assert [b.label for b in sent.buttons] != ["Approve", "Drop"]

async def test_a_message_recover_queued_goes_out_in_the_runs_language(tmp_path):
    asked = []
    rig = Rig(tmp_path, language_of=lambda scope: asked.append(scope) or "uk")
    [run] = rig.store.runs_for_ticket("acme/app", "ACME-1")
    rig.store.claim(run.id, ("queued",), NOW, "running")
    rig.runner.recover()
    await rig.runner.flush_outbox()     # the loop's flush, outside any job
    sent = rig.notifier.sent[-1]
    assert "interrupted by a restart" not in sent.text and [b.label for b in sent.buttons] != ["Approve", "Drop"]
    assert set(asked) == {"acme/app"}

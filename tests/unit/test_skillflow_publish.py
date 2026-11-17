# tests/unit/test_skillflow_publish.py
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
import pytest

from integrations.llm.claude_code_adapter import QuotaExhaustedError
from orchestrator.skillflow import runner as runner_module
from orchestrator.skillflow.checkout import CheckoutError, SkillCheckout
from orchestrator.skillflow.prompts import CUT_PREFIX
from orchestrator.skillflow.runner import SkillRepo
from tests.unit.skillflow_rig import NOW, PR_URL, SCOPE, T0, TOKEN, Rig
from tests.unit.test_skillflow_checkout import commit, git, make_origin

FIXED = "ACME-1 | fixed | tested: partial | review: xhigh, 1 findings | needs user: no"
TIMEOUT = RuntimeError("Claude Code CLI timed out after 14400s")
URL_7 = "https://github.com/acme/app/pull/7"


def ready(tmp_path, texts=(FIXED, "Committed."), **kwargs):
    """A run whose session reports a change and commits it on ACME-1-fix with its description written."""
    rig = Rig(tmp_path, texts=list(texts), **kwargs)
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    return rig


def raising(error):
    async def call(*args, **kwargs):
        raise error
    return call


def boom(*args, **kwargs):
    raise RuntimeError("boom")


async def _until(condition, tries=300):
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0.01)


# --- the brief's tests ---

async def test_result_with_a_change_ends_in_a_draft_pr(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "Committed."], gates={"test": "true", "lint": "true"})
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("## Summary\nFix.")
    run = await rig.go()
    assert (run.state, run.branch, run.pr_number, run.pr_url) == ("pr_open", "ACME-1-fix", 5, "https://github.com/acme/app/pull/5")
    assert "Commit it in the ticket's worktree" in rig.llm.calls[1]["prompt"] and rig.llm.calls[1]["resume"] is True
    assert rig.checkout.calls[-1] == ("push", "ACME-1-fix")
    assert rig.vcs.opened == [("ACME-1: Login crashes [skill]", "## Summary\nFix.", "ACME-1-fix", "develop", True)]
    assert "draft PR opened: https://github.com/acme/app/pull/5" in rig.notifier.sent[-1].text
    assert "Gates: test ok, lint ok" in rig.notifier.sent[-1].text

async def test_needs_user_is_told_and_the_run_goes_on(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED.replace("needs user: no", "needs user: yes"), "Committed."])
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    run = await rig.go()
    assert run.state == "pr_open" and run.needs_user is True
    assert any("fixed, needs you" in m.text for m in rig.notifier.sent)

@pytest.mark.parametrize("states, description, reason", [
    ([("ACME-1-fix", "not committed: ?? new.txt")], "d", "commit check failed: not committed: ?? new.txt"),
    ([("", "not on a branch")], "d", "commit check failed: not on a branch"),
    ([("ACME-1-fix", "")], None, "commit check failed: the PR description file is missing"),
    ([("ACME-1-fix", "")], "  \n", "commit check failed: the PR description file is missing"),
])
async def test_a_bad_commit_is_never_pushed(tmp_path, states, description, reason):
    rig = Rig(tmp_path, texts=[FIXED, "Committed."])
    rig.checkout.changed, rig.checkout.states = True, states
    if description is not None: rig.write_description(description)
    run = await rig.go()
    assert (run.state, run.reason, run.phase) == ("stopped", reason, "commit")
    assert ("push", "ACME-1-fix") not in rig.checkout.calls and rig.vcs.opened == []

async def test_a_failing_gate_goes_back_to_the_session(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "Committed.", "Fixed and committed."], gates={"test": "test -f fixed.flag"})
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", ""), ("ACME-1-fix", "")]; rig.write_description("d")
    rig.llm.after_call[2] = lambda: (rig.worktree / "fixed.flag").touch()
    run = await rig.go()
    fix = rig.llm.calls[2]["prompt"]
    assert "test: `test -f fixed.flag` exited 1" in fix and (run.state, run.gate_round) == ("pr_open", 1)

async def test_gates_that_keep_failing_stop_the_run(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "Committed.", "Tried.", "Tried again."], gates={"test": "echo FAILED LoginTest; exit 1"})
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")] * 3; rig.write_description("d")
    run = await rig.go()
    assert (run.state, run.phase, run.gate_round) == ("stopped", "gates", 2)
    assert run.reason == "gates still failing after 2 rounds: test" and len(rig.llm.calls) == 4
    assert "FAILED LoginTest" in rig.notifier.sent[-1].text and rig.vcs.opened == []

async def test_approve_after_failing_gates_runs_them_again_without_a_session(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "Committed."], gates={"test": "test -f fixed.flag"}, gate_rounds=0)
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    run = await rig.go(); assert run.state == "stopped"
    (rig.worktree / "fixed.flag").touch(); rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert run.state == "pr_open" and len(rig.llm.calls) == 2

async def test_the_slot_is_held_during_the_gates(tmp_path, monkeypatch):
    rig = Rig(tmp_path, texts=[FIXED, "Committed."], gates={"test": "true"})
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    rig.spy_gates(monkeypatch)                         # records slot.locked() and the run's state while gates run
    await rig.go()
    assert rig.gate_seen == [(True, "gates")] and not rig.slot.locked()

async def test_a_rejected_push_stops_and_approve_pushes_again(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "Committed."])
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "push", "push failed: push rejected: non-fast-forward")
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert run.state == "pr_open" and len(rig.llm.calls) == 2 and rig.checkout.calls.count(("push", "ACME-1-fix")) == 2

async def test_a_pr_that_already_exists_is_reused(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "Committed."])
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    rig.vcs.existing = {"ACME-1-fix": (7, "https://github.com/acme/app/pull/7")}
    run = await rig.go()
    # Review round 1: the push comes first even then; the existing PR is used and none is opened.
    assert run.pr_number == 7 and rig.vcs.opened == [] and rig.checkout.calls.count(("push", "ACME-1-fix")) == 1

async def test_a_usage_limit_in_the_commit_turn_keeps_the_phase(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, QuotaExhaustedError("l", retry_at=None), "Committed."])
    rig.checkout.changed, rig.checkout.states = True, [("ACME-1-fix", "")]; rig.write_description("d")
    run = await rig.go()
    assert (run.state, run.phase) == ("deferred", "commit")
    rig.clock.advance(hours=2)
    run = await rig.go()
    assert run.state == "pr_open" and rig.llm.calls[2]["prompt"].startswith(CUT_PREFIX + "Cleave will now publish")

async def test_a_dropped_runs_worktree_is_removed_once(tmp_path):
    rig = Rig(tmp_path)
    run = rig.store.in_states(("queued",))[0]; rig.store.claim(run.id, ("queued",), NOW, "dropped")
    await rig.runner.sweep_dropped(); await rig.runner.sweep_dropped()
    assert rig.checkout.calls.count(("remove_worktree", rig.worktree)) == 1 and rig.store.run(run.id).cleaned is True

async def test_a_worktree_git_refuses_to_remove_is_reported(tmp_path):
    rig = Rig(tmp_path); rig.checkout.remove_answer = "contains modified or untracked files"
    run = rig.store.in_states(("queued",))[0]; rig.store.claim(run.id, ("queued",), NOW, "dropped")
    await rig.runner.sweep_dropped(); await rig.runner.flush_outbox()
    assert "worktree left at" in rig.notifier.sent[-1].text and "contains modified or untracked files" in rig.notifier.sent[-1].text


# --- the addendum's tests ---

def _bad_commit(rig):
    rig.checkout.states = [("ACME-1-fix", "not committed: M src/Login.kt")]

def _failing_gates(rig):
    rig.repo.cfg.skill_flow.gates, rig.repo.cfg.skill_flow.gate_rounds = {"test": "false"}, 0

def _rejected_push(rig):
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]

@pytest.mark.parametrize("setup, phase", [(_bad_commit, "commit"), (_failing_gates, "gates"),
                                          (_rejected_push, "push")])
async def test_a_reply_while_the_stop_goes_out_is_kept(tmp_path, setup, phase):
    rig = ready(tmp_path); setup(rig)
    run_id = rig.store.in_states(("queued",))[0].id
    rig.notifier.during = lambda: rig.store.claim(run_id, ("stopped",), NOW, "queued", pending="skip the UI tests")
    run = await rig.go()
    assert (run.state, run.phase, run.pending, run.outbox) == ("queued", phase, "skip the UI tests", [])

async def test_the_branch_is_kept_when_the_gates_break(tmp_path, monkeypatch):
    rig = ready(tmp_path)
    monkeypatch.setattr(runner_module, "run_gates", raising(RuntimeError("boom")))
    run = await rig.go()
    assert (run.state, run.reason, run.branch) == ("failed", "internal error: RuntimeError", "ACME-1-fix")
    assert not rig.slot.locked()

async def test_the_branch_of_a_fix_is_kept_before_the_gates_run_again(tmp_path, monkeypatch):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Fixed on a new branch."], gates={"test": "false"})
    rig.checkout.states = [("ACME-1-fix", ""), ("ACME-1-fix-2", "")]
    real, gate_runs = runner_module.run_gates, []

    async def gates(*args, **kwargs):            # the second run of the gates breaks
        gate_runs.append(1)
        if len(gate_runs) == 2:
            raise RuntimeError("boom")
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_gates", gates)
    run = await rig.go()
    assert (run.state, run.branch) == ("failed", "ACME-1-fix-2")

async def test_the_pr_is_kept_when_its_message_cannot_be_built(tmp_path, monkeypatch):
    rig = ready(tmp_path)
    monkeypatch.setattr(runner_module, "pr_message", boom)
    run = await rig.go()
    assert (run.state, run.pr_number, run.pr_url) == ("failed", 5, PR_URL)

@pytest.mark.parametrize("texts, states, gates", [
    ([FIXED, "Not committed: the pre-commit hook rejects the import order."],
     [("ACME-1-fix", "not committed: M src/Login.kt")], {}),
    ([FIXED, "Committed.", "Not committed: the pre-commit hook rejects the import order."],
     [("ACME-1-fix", ""), ("ACME-1-fix", "not committed: M src/Login.kt")], {"test": "false"}),     # after a fix
])
async def test_a_failed_commit_check_tells_what_the_session_said(tmp_path, texts, states, gates):
    rig = Rig(tmp_path, texts=texts, gates=gates)
    rig.checkout.changed, rig.checkout.states = True, states; rig.write_description("d")
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "commit",
                                                  "commit check failed: not committed: M src/Login.kt")
    sent = rig.notifier.sent[-1]
    assert "Not committed: the pre-commit hook rejects the import order." in sent.text and sent.buttons
    assert ("push", "ACME-1-fix") not in rig.checkout.calls

async def test_the_sessions_answer_in_a_stop_is_relayed_cut(tmp_path):
    rig = Rig(tmp_path, texts=[FIXED, "y" * 10000])
    rig.checkout.changed, rig.checkout.states = True, [("", "not on a branch")]
    run = await rig.go()
    assert run.state == "stopped" and "… (cut: see the notes directory" in rig.notifier.sent[-1].text

@pytest.mark.parametrize("damage", ["worktree", "row"])
async def test_a_dropped_run_that_cannot_be_cleaned_holds_back_only_itself(tmp_path, caplog, damage):
    rig = Rig(tmp_path)
    healthy = rig.store.in_states(("queued",))[0]
    rig.store.claim(healthy.id, ("queued",), NOW, "dropped")
    earlier = (T0 - timedelta(hours=1)).isoformat()
    if damage == "worktree":
        bad_id, error = rig.add_run("ACME-2", earlier).id, "CheckoutError"
        rig.store.claim(bad_id, ("queued",), NOW, "dropped")
        rig.checkout.worktree_errors["worktrees/ACME-2"] = CheckoutError("'worktrees/ACME-2' is not inside the "
                                                                         "skill checkout")
    else:                                           # an outbox of the wrong shape, read to report a refusal
        bad_id, error = "odd1", "TypeError"
        rig.checkout.remove_answer = "contains modified or untracked files"
        rig.store._conn.execute(
            "INSERT INTO skill_runs (id, scope, ticket_id, state, retry_at, unsent, created_at, updated_at, data) "
            "VALUES ('odd1', 'acme/app', 'ACME-2', 'dropped', '', 0, ?, ?, '{\"outbox\": null}')", (earlier, NOW))
    with caplog.at_level(logging.WARNING):
        await rig.runner.sweep_dropped()
    assert rig.store.run(healthy.id).cleaned is True and rig.store.run(bad_id).cleaned is False
    assert ("remove_worktree", rig.worktree) in rig.checkout.calls
    assert [r.getMessage() for r in caplog.records if bad_id in r.getMessage() and error in r.getMessage()]

async def test_what_went_out_during_the_removal_does_not_come_back(tmp_path):
    rig = Rig(tmp_path); rig.checkout.remove_answer = "contains modified or untracked files"
    run = rig.store.in_states(("queued",))[0]
    rig.store.claim(run.id, ("queued",), NOW, "dropped", outbox=[{"text": "an older line", "buttons": False}])
    rig.checkout.on_remove = lambda: rig.store.drop_sent(run.id, 1, NOW)     # the older line went out meanwhile
    await rig.runner.sweep_dropped()
    got = rig.store.run(run.id)
    assert got.cleaned is True and len(got.outbox) == 1 and "worktree left at" in got.outbox[0]["text"]

async def test_a_forge_error_stops_the_run_with_its_text_redacted_on_one_line(tmp_path, caplog):
    rig = ready(tmp_path)
    text = f"GitHub POST /repos/acme/app/pulls → 422: Validation Failed\nhttps://x:{TOKEN}@github.com " + "z" * 400
    rig.vcs.open_pr = raising(RuntimeError(text))
    with caplog.at_level(logging.DEBUG):
        run = await rig.go()
    assert (run.state, run.phase) == ("stopped", "push") and ("push", "ACME-1-fix") in rig.checkout.calls
    assert run.reason.startswith("pull request not opened: GitHub POST /repos/acme/app/pulls → 422: Validation "
                                 "Failed https://x:***@github.com zzz")
    assert len(run.reason) == len("pull request not opened: ") + 300
    assert TOKEN not in caplog.text and all(TOKEN not in m.text for m in rig.notifier.sent)

async def test_a_network_error_of_the_forge_stops_the_run(tmp_path):
    rig = ready(tmp_path)
    rig.vcs.open_pr = raising(httpx.ConnectError("connection refused"))
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "push", "pull request not opened: connection refused")

async def test_an_unexpected_exception_of_the_forge_is_an_internal_error(tmp_path):
    rig = ready(tmp_path)
    rig.vcs.open_pr = raising(KeyError(f"https://x:{TOKEN}@github.com"))
    run = await rig.go()
    assert (run.state, run.reason) == ("failed", "internal error: KeyError")

async def test_a_pr_a_failed_request_opened_after_all_is_used(tmp_path):
    rig = ready(tmp_path)

    async def open_pr(*args, **kwargs):
        rig.vcs.existing["ACME-1-fix"] = (7, URL_7)      # the forge made it; its answer was lost
        raise RuntimeError("GitHub POST /repos/acme/app/pulls → 422: A pull request already exists")

    rig.vcs.open_pr = open_pr
    run = await rig.go()
    assert (run.state, run.pr_number, run.pr_url) == ("pr_open", 7, URL_7) and rig.vcs.finds == ["ACME-1-fix"] * 2


# --- what the brief leaves to the implementer ---

async def test_gates_taken_again_check_the_tree_first(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", TIMEOUT], gates={"test": "false"})
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "gates",
                                                  "session failed: Claude Code CLI timed out after 14400s")
    rig.checkout.states = [("ACME-1-fix", "not committed: M src/Login.kt")]     # the cut fix turn left its edit
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "commit",
                                                  "commit check failed: not committed: M src/Login.kt")
    assert len(rig.llm.calls) == 3 and ("push", "ACME-1-fix") not in rig.checkout.calls and rig.vcs.opened == []

async def test_a_usage_limit_in_a_fix_turn_keeps_its_round(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", QuotaExhaustedError("l", retry_at=None), "Tried."],
                gates={"test": "false"})
    run = await rig.go()
    assert (run.state, run.phase, run.gate_round) == ("deferred", "gates", 1)
    rig.clock.advance(hours=2)
    run = await rig.go()
    assert (run.state, run.reason, len(rig.llm.calls)) == ("stopped", "gates still failing after 2 rounds: test", 4)
    assert rig.llm.calls[3]["prompt"].startswith(CUT_PREFIX + "Cleave's gates failed")

async def test_the_gates_get_the_sessions_environment(tmp_path):
    rig = ready(tmp_path, gates={"test": "env > gate-env.txt"})
    run = await rig.go()
    env = (rig.worktree / "gate-env.txt").read_text()
    assert run.state == "pr_open" and "JAVA_HOME=/jdk" in env and "GIT_AUTHOR_NAME=Cleave Bot" in env
    assert "GITHUB_TOKEN" not in env

async def test_only_the_turns_and_the_gates_hold_the_slot(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Fixed."], gates={"test": "test -f fixed.flag"})
    rig.llm.after_call[2] = lambda: (rig.worktree / "fixed.flag").touch()
    rig.llm.on_call = lambda: rig.seen.append(rig.slot.locked())
    run = await rig.go()
    assert run.state == "pr_open" and rig.seen == [True] * 3 and rig.checkout.under_slot == []
    assert {"commit_state", "changed_files", ("push", "ACME-1-fix")} <= set(rig.checkout.calls)
    assert [e.data["state"] for e in rig.events] == ["running", "gates", "pr_open"]

async def test_the_pr_after_a_push_retry_names_the_gates_that_passed(tmp_path):
    rig = ready(tmp_path, gates={"test": "true", "lint": "true"})
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert run.state == "pr_open" and "Gates: test ok, lint ok" in rig.notifier.sent[-1].text

async def test_the_pr_body_is_the_description_cut(tmp_path):
    rig = ready(tmp_path); rig.write_description("d" * 70000)
    await rig.go()
    assert rig.vcs.opened[0][1] == "d" * 60000

async def test_the_loop_cleans_dropped_runs_when_nothing_runs(tmp_path):
    rig = Rig(tmp_path)
    run = rig.store.in_states(("queued",))[0]; rig.store.claim(run.id, ("queued",), NOW, "dropped")
    stop = asyncio.Event()
    task = asyncio.create_task(rig.runner.loop(stop))
    await _until(lambda: rig.store.run(run.id).cleaned)
    stop.set(); await asyncio.wait_for(task, 5)
    assert rig.store.run(run.id).cleaned is True and ("remove_worktree", rig.worktree) in rig.checkout.calls

async def test_a_dropped_run_leaves_the_worktree_a_later_run_of_the_ticket_works_in(tmp_path):
    rig = Rig(tmp_path)
    dropped = rig.store.in_states(("queued",))[0]; rig.store.claim(dropped.id, ("queued",), NOW, "dropped")
    rig.add_run("ACME-1", (T0 + timedelta(minutes=5)).isoformat())     # queued: worktrees/ACME-1 is its now
    await rig.runner.sweep_dropped()
    assert rig.store.run(dropped.id).cleaned is True and ("remove_worktree", rig.worktree) not in rig.checkout.calls


# --- review round 1 ---

COUNTED = {"test": "echo ran >> gate-runs.txt"}      # each run of the gates adds a line


def gate_runs(rig):
    runs = rig.worktree / "gate-runs.txt"
    return len(runs.read_text().splitlines()) if runs.exists() else 0


def pushes(rig, branch="ACME-1-fix"):
    return rig.checkout.calls.count(("push", branch))


def real_rig(tmp_path, texts, gates=None):
    """The rig with the real SkillCheckout on a hand-made clone of a local bare forge."""
    bare, _ = make_origin(tmp_path)
    git("clone", str(bare), str(tmp_path / "checkout"))
    (tmp_path / "rig").mkdir()
    rig = Rig(tmp_path / "rig", texts=texts, gates=gates)
    checkout = SkillCheckout(tmp_path / "checkout", f"file://{bare}", "develop", "tok")
    (checkout.path / "local.properties").write_text("sdk.dir=/sdk\n")
    rig.runner._repos[SCOPE] = SkillRepo("acme", "app", rig.repo.cfg, checkout, rig.vcs)
    return rig, checkout, bare

async def test_a_change_made_after_its_pr_was_opened_is_pushed_to_it(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", FIXED, "Committed again."], gates={"test": "true"})
    made = {}

    async def open_pr(*args, **kwargs):
        made["ACME-1-fix"] = (7, URL_7)                # the forge made it; its answer and the second look were lost
        raise httpx.ReadTimeout("timed out")

    rig.vcs.open_pr = open_pr
    run = await rig.go()
    assert (run.state, run.phase, run.reason, pushes(rig)) == ("stopped", "push",
                                                               "pull request not opened: timed out", 1)
    rig.vcs.existing.update(made)
    # A reply (Task 13): the session changes the code again and commits.
    rig.store.claim(run.id, ("stopped",), NOW, "queued", pending="also cover the empty password", phase="work")
    run = await rig.go()
    assert (run.state, run.pr_number, pushes(rig), rig.vcs.opened) == ("pr_open", 7, 2, [])

@pytest.mark.parametrize("branch", ["develop", "feature/ACME-1-acme-1"])    # the base, the staged flow's own
async def test_a_change_on_the_base_or_the_staged_branch_is_never_pushed(tmp_path, branch):
    rig = ready(tmp_path); rig.checkout.states = [(branch, "")]
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == (
        "stopped", "commit", f"commit check failed: the change is on {branch}, which Cleave never pushes")
    assert pushes(rig, branch) == 0 and rig.vcs.opened == []

async def test_real_git_a_commit_on_the_base_branch_never_reaches_the_forge(tmp_path):
    rig, co, bare = real_rig(tmp_path, [FIXED, "Committed."], gates={"test": "true"})
    wt = co.worktree("worktrees/ACME-1")
    before = git("rev-parse", "refs/heads/develop", cwd=bare)

    def work():                                          # the hand-made clone's own develop, taken by a worktree
        git("worktree", "add", str(wt), "develop", cwd=co.path)
        (wt / "fix.txt").write_text("fix")

    def commit_it():
        git("add", ".", cwd=wt); commit(wt, "ACME-1: fix")
        (co.path / "ai_instructions" / "ACME-1" / "PR_DESCRIPTION.md").write_text("d")

    rig.llm.after_call[0], rig.llm.after_call[1] = work, commit_it
    run = await rig.go()
    assert (run.state, run.reason) == ("stopped",
                                       "commit check failed: the change is on develop, which Cleave never pushes")
    assert git("rev-parse", "refs/heads/develop", cwd=bare) == before and rig.vcs.opened == []

async def test_a_cut_fix_turns_uncommitted_edit_is_never_what_the_gates_judge(tmp_path):
    # The fix turn made the gate pass with an edit it did not commit, then met the usage limit. Taken again from
    # deferred, with no operator in between, the run must not gate that tree and push the commit that failed.
    rig = ready(tmp_path, texts=[FIXED, "Committed.", QuotaExhaustedError("l", retry_at=None)],
                gates={"test": "echo ran >> gate-runs.txt; test -f fixed.flag"})

    def cut_mid_fix():
        (rig.worktree / "fixed.flag").touch()                                 # the edit, not committed
        rig.checkout.states = [("ACME-1-fix", "not committed: ?? fixed.flag")]

    rig.llm.after_call[2] = cut_mid_fix
    run = await rig.go()
    assert (run.state, run.phase, gate_runs(rig)) == ("deferred", "gates", 1)
    rig.clock.advance(hours=2)
    run = await rig.go()
    assert (run.state, run.phase, pushes(rig)) == ("stopped", "commit", 0) and rig.vcs.opened == []
    assert gate_runs(rig) == 1                                                # the gates never saw that tree

async def test_approve_on_gates_checks_and_gates_again_with_all_the_rounds_and_no_commit_turn(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Tried.", "Tried again.", "Third.", "Fourth."],
                gates={"test": "false"})
    run = await rig.go()
    assert (run.state, run.phase, run.gate_round, len(rig.llm.calls)) == ("stopped", "gates", 2, 4)
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.phase, run.gate_round, len(rig.llm.calls)) == ("stopped", "gates", 2, 6)
    assert rig.llm.calls[4]["prompt"].startswith("Cleave's gates failed") and pushes(rig) == 0

async def test_the_fake_tests_gate_goes_through_the_fix_loop_and_stops(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Tried.", "Tried again."], gates={"test": "true"})
    test = rig.worktree / "app" / "src" / "test" / "LoginTest.kt"
    test.parent.mkdir(parents=True)
    test.write_text('val source = File("app/src/main/java/Login.kt").readText()\n')
    rig.checkout.files = ["app/src/test/LoginTest.kt"]
    run = await rig.go()
    assert (run.state, run.phase, run.gate_round, len(rig.llm.calls)) == ("stopped", "gates", 2, 4)
    assert run.reason == "gates still failing after 2 rounds: fake-tests"
    assert "fake-tests: `Cleave's source-reading test check` exited 1" in rig.llm.calls[2]["prompt"]
    assert "app/src/test/LoginTest.kt" in rig.notifier.sent[-1].text and pushes(rig) == 0

async def test_a_branch_moved_by_hand_before_approve_is_gated_again_before_it_is_pushed(tmp_path):
    rig = ready(tmp_path, gates=COUNTED)
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    assert (run.state, run.phase, run.gated_sha, gate_runs(rig)) == ("stopped", "push", "b" * 40, 1)
    rig.checkout.sha = "c" * 40                          # the branch moved since its gates
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.gated_sha, gate_runs(rig), len(rig.llm.calls), pushes(rig)) == (
        "pr_open", "c" * 40, 2, 2, 2)
    retry = rig.checkout.calls[rig.checkout.calls.index(("push", "ACME-1-fix")) + 1:]
    assert retry.index("changed_files") < retry.index(("push", "ACME-1-fix"))

async def test_the_gated_commit_is_kept_before_the_push_step_starts(tmp_path):
    rig = ready(tmp_path, gates={"test": "true"})
    real, checks = rig.checkout.commit_state, []

    async def commit_state(worktree):
        checks.append(worktree)
        if len(checks) == 2:                             # the push step's check breaks
            raise RuntimeError("boom")
        return await real(worktree)

    rig.checkout.commit_state = commit_state
    run = await rig.go()
    assert (run.state, run.phase, run.gated_sha) == ("failed", "push", "b" * 40) and pushes(rig) == 0

async def test_an_unmoved_branch_is_pushed_again_with_no_second_gate_run(tmp_path):
    rig = ready(tmp_path, gates=COUNTED)
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.gated_sha, gate_runs(rig), pushes(rig)) == ("pr_open", "b" * 40, 1, 2)

async def test_a_push_retry_checks_the_commit_first(tmp_path):
    rig = ready(tmp_path, gates={"test": "true"})
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    rig.checkout.states = [("ACME-1-fix", "not committed: M src/Login.kt")]     # a hand changed the worktree since
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "commit",
                                                  "commit check failed: not committed: M src/Login.kt")
    assert pushes(rig) == 1

async def test_a_phase_that_is_none_of_the_four_fails_the_run(tmp_path):
    rig = ready(tmp_path, gates={"test": "true"})
    run = rig.store.in_states(("queued",))[0]
    run.phase, run.branch, run.session_id, run.gated_sha = "Gates", "ACME-1-fix", "s", "b" * 40   # a damaged phase
    rig.store.save(run, NOW)
    run = await rig.go()
    assert (run.state, run.reason) == ("failed", "internal error: ValueError")
    assert rig.llm.calls == [] and rig.checkout.calls == [] and rig.vcs.opened == []

async def test_a_symlinked_description_stops_the_run_and_is_never_read(tmp_path):
    rig = ready(tmp_path)
    secret = tmp_path / ".env"                           # the rig's secret path, outside the notes directory
    secret.write_text(f"GITHUB_TOKEN={TOKEN}\n")
    link = rig.checkout.path / "ai_instructions" / "ACME-1" / "PR_DESCRIPTION.md"
    link.unlink(); link.symlink_to(secret)
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == ("stopped", "commit",
                                                  "commit check failed: the PR description file is a symlink")
    assert rig.vcs.opened == [] and pushes(rig) == 0 and all(TOKEN not in m.text for m in rig.notifier.sent)

async def test_a_token_in_the_description_is_masked_in_the_pr_body(tmp_path):
    rig = ready(tmp_path); rig.write_description(f"## Summary\nThe remote was https://x:{TOKEN}@github.com")
    await rig.go()
    assert rig.vcs.opened[0][1] == "## Summary\nThe remote was https://x:***@github.com"

async def test_a_token_in_a_gates_output_is_masked_in_the_stop_message(tmp_path):
    rig = ready(tmp_path, gates={"test": f"echo remote https://x:{TOKEN}@github.com; exit 1"}, gate_rounds=0)
    run = await rig.go()
    assert run.state == "stopped" and "remote https://x:***@github.com" in rig.notifier.sent[-1].text
    assert all(TOKEN not in m.text for m in rig.notifier.sent)

async def test_approve_on_commit_asks_for_the_commit_again_and_gives_the_rounds_again(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Could not commit.", "Committed now.", "Tried.", "Again."],
                gates={"test": "false"})
    rig.checkout.states = [("ACME-1-fix", ""), ("ACME-1-fix", "not committed: M a.kt"), ("ACME-1-fix", "")]
    run = await rig.go()                                   # commit ok, gates fail, fix turn, its check fails
    assert (run.state, run.phase, run.gate_round, len(rig.llm.calls)) == ("stopped", "commit", 1, 3)
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()                                   # commit turn, check, gates, two fix turns, stop
    assert rig.llm.calls[3]["prompt"].startswith("Cleave will now publish")
    assert (run.state, run.phase, run.gate_round, len(rig.llm.calls)) == ("stopped", "gates", 2, 6)

async def test_the_stop_of_failing_gates_shows_each_gate_and_the_end_of_its_output(tmp_path):
    long_gate = "head -c 900 /dev/zero | tr '\\0' a; echo TAIL-END; exit 3"
    rig = ready(tmp_path, gates={"test": long_gate, "lint": "echo lint broke; exit 2"}, gate_rounds=0)
    run = await rig.go()
    text = rig.notifier.sent[-1].text
    assert run.reason == "gates still failing after 0 rounds: test, lint"
    assert f"test: `{long_gate}` exited 3\n" + "a" * 592 + "TAIL-END" in text and "a" * 593 not in text
    assert "lint: `echo lint broke; exit 2` exited 2\nlint broke" in text


# --- Task 13 review round 1: Drop's answer rests on a landed push, not on the phase ---

@pytest.mark.parametrize("error, state", [(httpx.ConnectError("connection refused"), "stopped"),
                                          (KeyError("unexpected"), "failed")])
async def test_a_landed_push_is_stored_when_opening_the_pr_then_fails(tmp_path, error, state):
    """Stored right after the push: an internal error parks the stored row, not the object."""
    rig = ready(tmp_path)
    rig.vcs.open_pr = raising(error)
    run = await rig.go()
    assert (run.state, run.pushed) == (state, True)


async def test_a_push_that_never_landed_is_not_recorded(tmp_path):
    rig = ready(tmp_path)
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    assert (run.state, run.phase, run.pushed) == ("stopped", "push", False)


# --- final fix wave ---

async def test_real_git_a_branch_already_on_the_forge_is_never_pushed(tmp_path):
    # The reviewer's probe: the session based its worktree on someone else's branch that is on the forge.
    rig, co, bare = real_rig(tmp_path, [FIXED, "Committed."], gates={"test": "true"})
    git("push", "origin", "HEAD:refs/heads/release/2.0", cwd=tmp_path / "work")
    before = git("rev-parse", "refs/heads/release/2.0", cwd=bare)
    wt = co.worktree("worktrees/ACME-1")

    def session_works():
        git("worktree", "add", "-b", "release/2.0", str(wt), "origin/release/2.0", cwd=co.path)
        (wt / "fix.txt").write_text("fix")

    def session_commits():
        git("add", ".", cwd=wt); commit(wt, "ACME-1: fix")
        (co.path / "ai_instructions" / "ACME-1" / "PR_DESCRIPTION.md").write_text("d")

    rig.llm.after_call[0], rig.llm.after_call[1] = session_works, session_commits
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == (
        "stopped", "commit",
        "commit check failed: the change is on release/2.0, which is already on the forge and is not this run's")
    assert git("rev-parse", "refs/heads/release/2.0", cwd=bare) == before and rig.vcs.opened == []

async def test_a_branch_this_runs_own_push_put_on_the_forge_is_pushed_again(tmp_path):
    rig = ready(tmp_path)
    rig.vcs.open_pr = raising(httpx.ConnectError("connection refused"))
    run = await rig.go()
    assert (run.state, run.phase, run.pushed) == ("stopped", "push", True)
    rig.checkout.forge_branches.add("ACME-1-fix")       # the push landed: the branch is on the forge now
    del rig.vcs.open_pr
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, pushes(rig)) == ("pr_open", 2) and ("on_forge", "ACME-1-fix") in rig.checkout.calls

async def test_another_branch_on_the_forge_is_refused_after_the_runs_own_push(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", FIXED, "Committed on the release branch."])
    rig.vcs.open_pr = raising(httpx.ConnectError("connection refused"))
    run = await rig.go()
    assert (run.state, run.phase, run.pushed, pushes(rig)) == ("stopped", "push", True, 1)
    rig.checkout.forge_branches |= {"ACME-1-fix", "release/2.0"}
    rig.checkout.states = [("release/2.0", "")]           # the session commits on someone else's branch
    rig.store.claim(run.id, ("stopped",), NOW, "queued", pending="also cover the empty password", phase="work")
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == (
        "stopped", "commit",
        "commit check failed: the change is on release/2.0, which is already on the forge and is not this run's")
    assert pushes(rig, "release/2.0") == 0 and pushes(rig) == 1

async def test_a_commit_made_while_the_gates_run_is_gated_before_it_is_pushed(tmp_path, monkeypatch):
    rig = ready(tmp_path, gates=COUNTED)
    real = runner_module.run_gates

    async def moving(*args, **kwargs):
        results = await real(*args, **kwargs)
        if gate_runs(rig) == 1:
            rig.checkout.sha = "c" * 40                  # a commit made while the first gates ran
        return results

    monkeypatch.setattr(runner_module, "run_gates", moving)
    run = await rig.go()
    assert (run.state, run.gated_sha, gate_runs(rig), pushes(rig)) == ("pr_open", "c" * 40, 2, 1)
    before_push = rig.checkout.calls[:rig.checkout.calls.index(("push", "ACME-1-fix"))]
    assert before_push.count("changed_files") == 2

async def test_real_git_a_gate_that_keeps_committing_stops_the_run(tmp_path):
    assert runner_module.MAX_REGATES == 2
    rig, co, bare = real_rig(tmp_path, [FIXED, "Committed."], gates={"test": "git commit -q --allow-empty -m gate"})
    wt = co.worktree("worktrees/ACME-1")

    def session_works():
        git("worktree", "add", "--no-track", "-b", "ACME-1-fix", str(wt), "origin/develop", cwd=co.path)
        (wt / "fix.txt").write_text("fix")

    def session_commits():
        git("add", ".", cwd=wt); commit(wt, "ACME-1: fix")
        (co.path / "ai_instructions" / "ACME-1" / "PR_DESCRIPTION.md").write_text("d")

    rig.llm.after_call[0], rig.llm.after_call[1] = session_works, session_commits
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == (
        "stopped", "push", "the branch kept moving: it moved each of the 2 times its gates ran again")
    gate_commits = git("rev-list", "--count", "origin/develop..HEAD", cwd=wt)
    assert gate_commits == "4"                           # the session's commit, then one per run of the gates
    assert git("for-each-ref", "refs/heads/ACME-1-fix", cwd=bare) == "" and rig.vcs.opened == []

async def test_the_commit_and_fix_prompts_name_the_description_by_its_absolute_path(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Fixed."], gates={"test": "test -f fixed.flag"})
    rig.llm.after_call[2] = lambda: (rig.worktree / "fixed.flag").touch()
    run = await rig.go()
    path = rig.checkout.path / "ai_instructions" / "ACME-1" / "PR_DESCRIPTION.md"
    assert run.state == "pr_open" and path.is_absolute()
    assert all(f"`{path}`" in call["prompt"] for call in rig.llm.calls[1:])


@pytest.mark.parametrize("landed, answer", [
    (True, "ACME-1 · Dropped. The branch ACME-1-fix is on the forge; Cleave pushes nothing more."),
    (False, "ACME-1 · Dropped. A push of ACME-1-fix was started and may have reached the forge; Cleave pushes "
            "nothing more."),
])
async def test_drop_after_a_reply_sent_a_push_stop_back_to_work(tmp_path, landed, answer):
    """The reviewer's sequence: stopped in the push step (the push landed and the PR was not opened, or the push
    was rejected), a text reply sends it back to the work step, it parks again, then Drop."""
    from orchestrator.skillflow.replies import SkillReplies
    rig = ready(tmp_path, texts=(FIXED, "Committed.", "Which base should the PR target?"))
    if landed:
        rig.vcs.open_pr = raising(httpx.ConnectError("connection refused"))
    else:
        rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    replies = SkillReplies(rig.store, rig.chat, rig.runner.wake, now=rig.clock.now)
    run = await rig.go()
    assert (run.state, run.phase) == ("stopped", "push")
    await replies.handle_reply(rig.store.run(run.id).chat_id, rig.notifier.sent[-1].message_id, "target develop")
    run = await rig.go()
    assert (run.state, run.phase) == ("waiting", "work")
    await replies.handle_callback("sk_drop", run.id, run.chat_id, rig.notifier.sent[-1].message_id)
    assert rig.store.run(run.id).state == "dropped" and rig.notifier.sent[-1].text == answer


# --- code-review fix wave: the push step ---

async def test_the_push_is_handed_the_commit_the_gates_passed(tmp_path):
    rig = ready(tmp_path, gates={"test": "true"})
    run = await rig.go()
    assert (run.state, run.gated_sha, rig.checkout.pushed_shas) == ("pr_open", "b" * 40, ["b" * 40])

async def test_a_push_that_landed_but_was_reported_failed_is_pushed_again(tmp_path):
    rig = ready(tmp_path)
    rig.checkout.push_errors = [CheckoutError("push rejected: git push timed out after 300 s")]
    run = await rig.go()
    assert (run.state, run.phase, run.pushed, run.push_branch) == ("stopped", "push", False, "ACME-1-fix")
    rig.checkout.forge_branches.add("ACME-1-fix")       # the forge took the ref all the same; a fetch shows it
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.pushed, pushes(rig)) == ("pr_open", True, 2)

async def test_a_push_cut_by_a_restart_is_pushed_again(tmp_path):
    rig = ready(tmp_path)
    rig.checkout.push_errors = [asyncio.CancelledError()]   # the daemon stops while git pushes
    run = rig.runner.next_run()
    with pytest.raises(asyncio.CancelledError):
        await rig.runner.run_job(run)
    assert rig.store.run(run.id).push_branch == "ACME-1-fix"
    await rig.runner.start()
    rig.checkout.forge_branches.add("ACME-1-fix")
    rig.store.claim(run.id, ("interrupted",), NOW, "queued")
    run = await rig.go()
    assert (run.state, pushes(rig)) == ("pr_open", 2)

async def test_a_run_that_tried_a_push_is_refused_another_branch_on_the_forge(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", FIXED, "Committed on the release branch."])
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    assert (run.state, run.phase, run.push_branch) == ("stopped", "push", "ACME-1-fix")
    rig.checkout.forge_branches |= {"ACME-1-fix", "release/2.0"}
    rig.checkout.states = [("release/2.0", "")]           # the session moves to someone else's branch
    rig.store.claim(run.id, ("stopped",), NOW, "queued", pending="also cover the empty password", phase="work")
    run = await rig.go()
    assert (run.state, run.phase, run.reason) == (
        "stopped", "commit",
        "commit check failed: the change is on release/2.0, which is already on the forge and is not this run's")
    assert pushes(rig, "release/2.0") == 0

async def test_gates_that_fail_again_in_the_push_step_stop_in_gates_and_approve_gives_the_rounds(tmp_path):
    rig = ready(tmp_path, texts=[FIXED, "Committed.", "Fixed.", "Fixed again."], gates={"test": "test -f fixed.flag"},
                gate_rounds=1)
    flag = rig.worktree / "fixed.flag"
    rig.llm.after_call[2] = flag.touch
    rig.checkout.push_errors = [CheckoutError("push rejected: non-fast-forward")]
    run = await rig.go()
    assert (run.state, run.phase, run.gate_round) == ("stopped", "push", 1)      # its one round used up
    flag.unlink(); rig.checkout.sha = "c" * 40           # the branch moved by hand, and the gate fails on it
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert (run.state, run.phase, len(rig.llm.calls)) == ("stopped", "gates", 3)
    rig.llm.after_call[3] = flag.touch
    rig.store.claim(run.id, ("stopped",), NOW, "queued")
    run = await rig.go()
    assert rig.llm.calls[3]["prompt"].startswith("Cleave's gates failed")
    assert (run.state, run.gated_sha, pushes(rig)) == ("pr_open", "c" * 40, 2)

async def test_publish_refuses_the_work_phase(tmp_path):
    rig = ready(tmp_path)
    [run] = rig.store.in_states(("queued",))
    with pytest.raises(ValueError, match="'work'"):
        await rig.runner._publish(rig.repo, run)
    assert rig.checkout.calls == [] and rig.llm.calls == [] and rig.vcs.opened == []

def _with_summary(rig, summary):
    [run] = rig.store.in_states(("queued",))
    run.summary = summary
    rig.store.save(run, NOW)

async def test_a_long_summary_is_cut_so_the_pr_title_fits_githubs_limit(tmp_path):
    rig = ready(tmp_path); _with_summary(rig, "x" * 300)
    run = await rig.go()
    title = rig.vcs.opened[0][0]
    assert run.state == "pr_open" and len(title) == 256
    assert title == "ACME-1: " + "x" * (256 - len("ACME-1: … [skill]")) + "… [skill]"

async def test_a_summary_that_fits_the_title_is_not_cut(tmp_path):
    summary = "y" * (256 - len("ACME-1:  [skill]"))
    rig = ready(tmp_path); _with_summary(rig, summary)
    await rig.go()
    assert rig.vcs.opened[0][0] == f"ACME-1: {summary} [skill]"

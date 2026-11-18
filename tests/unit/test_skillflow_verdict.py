# tests/unit/test_skillflow_verdict.py
from __future__ import annotations

import logging

import pytest

from orchestrator.skillflow.compare_messages import SKILL_LEFT_LINE, TRACKER_NONE_LINE
from orchestrator.skillflow.compare_models import STEPS
from tests.unit.test_skillflow_keeper import NOW, TOKEN, Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


async def test_skill_verdict_writes(rig):
    await rig.decided("skill")
    assert rig.forge.calls == [("mark_ready", 13), ("pr_info", 13), ("set_title", 13, "ACME-1: Fix login"),
                               ("pr_info", 12), ("comment_on_pr", 12, "Closed by Cleave: the comparison of the two flows chose the skill flow's PR #13."),
                               ("pr_info", 12), ("close_pr", 12)]
    assert rig.tracker.calls == [("transition", "ACME-1", "In Review"), ("comment", "ACME-1", "PR opened: u13")]
    assert rig.runs.run(rig.run_id).released is True and rig.runs.run(rig.run_id).state == "pr_open"
    assert rig.staged.state.current_state == "DONE"
    c = rig.comparisons.get(rig.cid)
    assert c.state == "closed" and c.steps_done == list(STEPS)
    assert "Ready for review: PR #13 u13" in rig.chat.sent[-1][0]

async def test_neither_closes_nothing(rig):
    await rig.decided("neither")
    assert rig.forge.calls == [] and rig.tracker.calls == []
    assert rig.runs.run(rig.run_id).state == "dropped"
    assert "Both PRs stay drafts." in rig.chat.sent[-1][0]

async def test_staged_winner_drops_the_skill_run_and_ends_the_workspace(rig):
    await rig.decided("staged")
    assert ("mark_ready", 12) in rig.forge.calls and ("close_pr", 13) in rig.forge.calls
    assert rig.runs.run(rig.run_id).state == "dropped" and rig.staged.state.current_state == "DONE"

async def test_a_failed_step_is_stuck_and_retry_continues_from_it(rig):
    rig.forge.fail("comment_on_pr", RuntimeError("GitHub POST … → 502: bad gateway"))
    await rig.decided("skill")
    c = rig.comparisons.get(rig.cid)
    assert c.state == "stuck" and c.steps_done == ["ready", "title"]
    assert "commenting on PR #12 failed" in rig.chat.sent[-1][0] and rig.chat.sent[-1][1][0].action == "sk_retry:" + rig.cid
    rig.forge.heal(); rig.forge.calls.clear()
    rig.comparisons.claim(rig.cid, ("stuck",), NOW, "deciding")       # what Retry does
    await rig.keeper.cycle()
    assert rig.forge.calls[0] == ("pr_info", 12) and ("mark_ready", 13) not in rig.forge.calls
    assert rig.comparisons.get(rig.cid).state == "closed"

async def test_a_closed_loser_gets_no_second_comment(rig):
    rig.forge.prs[12] = "closed"
    await rig.decided("skill")
    assert not any(call[0] == "comment_on_pr" for call in rig.forge.calls)

async def test_a_running_loser_makes_the_skill_step_wait(rig):
    await rig.message_out()
    done = ["ready", "title", "comment", "close", "tracker"]
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="staged", steps_done=done)
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "running")    # taken up just before the verdict
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.steps_done) == ("deciding", done) and "not finished" not in rig.chat.sent[-1][0]
    rig.runs.claim(rig.run_id, ("running",), NOW, "stopped")
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).state == "closed" and rig.runs.run(rig.run_id).state == "dropped"

async def test_a_missing_workspace_is_a_report_line(rig):
    rig.delete_workspace()
    await rig.decided("skill")
    assert "Staged workspace: not found." in rig.chat.sent[-1][0]


# --- beyond the brief's list ---

async def test_a_stuck_reason_is_redacted_and_cut(rig):
    rig.forge.fail("mark_ready", RuntimeError(f"GitHub POST {TOKEN} → 502: " + "x" * 400))
    await rig.decided("skill")
    c = rig.comparisons.get(rig.cid)
    assert c.state == "stuck" and TOKEN not in c.reason and len(c.reason) == 300
    assert TOKEN not in rig.chat.sent[-1][0] and "marking PR #13 ready failed" in rig.chat.sent[-1][0]

async def test_a_bug_in_a_step_parks_the_verdict_with_its_class(rig):
    def find(c):
        raise AttributeError("a state of a wrong shape")

    await rig.message_out()
    rig.workspaces.find = find
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill",
                          steps_done=["ready", "title", "comment", "close", "tracker", "skill"])
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.reason) == ("stuck", "AttributeError")
    assert "ending the staged workspace failed: AttributeError" in rig.chat.sent[-1][0]

async def test_a_failed_tracker_write_is_a_report_line(rig):
    async def refuse(ticket_id, status):
        raise RuntimeError("tracker down")

    rig.tracker.transition_ticket = refuse
    await rig.decided("skill")
    c = rig.comparisons.get(rig.cid)
    assert c.state == "closed" and "Tracker: move to In Review failed: RuntimeError" in rig.chat.sent[-1][0]

async def test_the_verdict_is_an_event(rig):
    await rig.decided("staged")
    assert [(e.type, e.data) for e in rig.events] == [
        ("comparison_verdict", {"verdict": "staged", "scope": "acme/app"})]

async def test_no_tracker_is_cleaves_own_line(rig):
    rig.tracker = None
    await rig.decided("skill")
    c = rig.comparisons.get(rig.cid)
    assert [TRACKER_NONE_LINE, {}] in c.report and "Tracker: none configured." in rig.chat.sent[-1][0]


# --- the final fix wave ---

async def test_a_skill_winner_not_pr_open_is_left_as_it_is(rig):
    # publish.py stores the PR before it parks the run: a run can be failed with a PR.
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "failed", reason="internal error: KeyError")
    await rig.decided("skill")
    c, run = rig.comparisons.get(rig.cid), rig.runs.run(rig.run_id)
    assert c.state == "closed" and (run.state, run.released) == ("failed", False)
    assert [SKILL_LEFT_LINE, {"state": "failed"}] in c.report
    assert "Skill run: failed; left as it is." in rig.chat.sent[-1][0]

async def test_a_workspace_that_moved_on_mid_verdict_is_left_as_it_is(rig):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill",
                          steps_done=["ready", "title", "comment", "close", "tracker", "skill"])
    rig.staged.state = "DEV"
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).state == "closed" and rig.staged.state.current_state == "DEV"
    assert "Staged workspace: InvalidTransitionError" in rig.chat.sent[-1][0]

async def test_every_recorded_step_is_logged(rig, caplog):
    with caplog.at_level(logging.INFO, logger="orchestrator.skillflow.verdict"):
        await rig.decided("skill")
    lines = [r.getMessage() for r in caplog.records
             if r.name == "orchestrator.skillflow.verdict" and r.levelno == logging.INFO]
    assert len(lines) == len(STEPS) and all(rig.cid in line for line in lines)
    assert "step ready" in lines[0] and "pr=13" in lines[0] and "step close" in lines[3] and "pr=12" in lines[3]

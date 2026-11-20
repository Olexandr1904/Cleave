"""A judge's rejection on its last allowed attempt stops the ticket on that rejection.

The plan reviewer rejected a plan for the second time of two. The ticket went back to the BA, who fixed the plan,
and on its way to a third review it hit the cap: blocked, with an escalation quoting the second rejection as if
it still stood. Nobody had read the fixed plan, and the operator was asked about a defect that was gone.

Now the ticket stops at the judge, on the verdict the operator is shown, and their go-ahead sends the work to be
fixed: judging the same work once more would only repeat the verdict.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import ParsedIntent
from orchestrator.agent_runtime import AgentResult, _write_agent_output
from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
from orchestrator.escalation import build_rejected_hint
from orchestrator.pipeline.agent_stage import handle_agent_stage
from orchestrator.pipeline.driver import advance_workspace
from orchestrator.workflow_router import load_workflow
from workspace.workspace import Stage, Workspace, WorkspaceState

WORKFLOW = load_workflow(str(Path(__file__).parent.parent.parent / "workflows" / "default-workflow.yaml"))
ESCALATION_MSG = 100
REJECTION = "Status: FAIL\n\n### Files to Modify\nThe plan edits the architecture rules file."


def _workspace(tmp_path: Path, state: str, iterations: dict, **extra) -> Workspace:
    root = tmp_path / "ACME-1"
    for name in ("meta", "reports", "logs", "source"):
        (root / name).mkdir(parents=True)
    ws = Workspace(str(root), WorkspaceState(
        ticket_id="ACME-1", company_id="acme", repo_id="app", workspace_root=str(root),
        current_state=state, stage_iterations=iterations, **extra,
    ))
    ws.reports_dir.mkdir(parents=True, exist_ok=True)
    ws.save_state()
    return ws


def _notifier() -> AsyncMock:
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=ESCALATION_MSG)
    return notifier


async def _review_the_plan(ws: Workspace, notifier: AsyncMock) -> list[str]:
    """Run plan review with a reviewer that rejects; the stages the ticket was sent on to."""
    async def reject(*args, **kwargs):
        _write_agent_output(ws.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW, REJECTION)
        return AgentResult(agent_id="plan-review-agent", success=True, output="done")

    runtime = MagicMock()
    runtime.execute = reject
    advanced: list[str] = []
    await handle_agent_stage(
        ws, "plan_review", WORKFLOW.stages["plan_review"],
        workflow=WORKFLOW, agent_runtime=runtime, repo_config=None,
        notifier=notifier, mode_handler=None, get_chat_id=lambda w: "1",
        dry_run=False, event_bus=None,
        advance_to_stage_fn=lambda w, s: advanced.append(s),
        on_ticket_done_fn=AsyncMock(),
        build_gate_summary_fn=MagicMock(return_value=("", [])),
        notify_verification_blocked_fn=AsyncMock(),
        notify_deferred_fn=AsyncMock(),
        notify_failed_fn=AsyncMock(),
    )
    return advanced


async def test_a_rejection_with_an_attempt_left_goes_to_be_fixed(tmp_path):
    ws = _workspace(tmp_path, Stage.PLAN_REVIEW, {"analysis": 1})

    assert await _review_the_plan(ws, _notifier()) == ["analysis"]

    assert ws.state.current_state == Stage.PLAN_REVIEW  # the fake advance does not move it; nothing blocked it
    assert ws.state.rejected_at is None


async def test_a_rejection_on_the_last_attempt_stops_the_ticket_on_it(tmp_path):
    ws = _workspace(tmp_path, Stage.PLAN_REVIEW, {"analysis": 2, "plan_review": 1})
    notifier = _notifier()

    assert await _review_the_plan(ws, notifier) == []  # not back to the BA

    assert (ws.state.current_state, ws.state.previous_state) == (Stage.BLOCKED, Stage.PLAN_REVIEW)
    assert ws.state.rejected_at == Stage.PLAN_REVIEW
    assert ws.state.stage_iterations["analysis"] == 2  # the BA did not run again
    message = notifier.send_message.await_args.args[1]
    assert "PLAN_REVIEW — rejected 2 of 2 times" in message
    assert "The plan edits the architecture rules file." in message
    assert "it may say PASS" not in message  # the hint of a cap reached with nothing judged
    assert "The plan edits the architecture rules file." in ws.state.human_input_question


def _rejected_for_the_last_time(tmp_path: Path, judge: str, iterations: dict) -> Workspace:
    return _workspace(
        tmp_path, Stage.BLOCKED, iterations, previous_state=judge, rejected_at=judge,
        human_input_pending=True, escalation_msg_id=ESCALATION_MSG, escalation_chat_id="1",
    )


def _handler(ws: Workspace, parsed: ParsedIntent | None = None) -> CommandHandler:
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    parser = AsyncMock()
    parser.parse = AsyncMock(return_value=parsed or ParsedIntent(intent="status", params={}))
    return CommandHandler(
        intent_parser=parser, notifier=notifier, mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [ws],
    )


async def _next_stage_run(ws: Workspace) -> str | None:
    """The stage the driver runs next, or None if it escalates instead."""
    ran: list[str] = []

    async def run_stage(workspace, stage_id, stage_def):
        ran.append(stage_id)

    escalate = AsyncMock()
    await advance_workspace(
        ws, workflow=WORKFLOW, mode_handler=None,
        handle_agent_stage_fn=run_stage, handle_action_stage_fn=run_stage, handle_escalate_fn=escalate,
    )
    return ran[0] if len(ran) == 1 and escalate.await_count == 0 else None


@pytest.mark.parametrize("reply", ["retry", "Leave that file to me, I will edit it by hand."])
async def test_the_operators_go_ahead_sends_a_rejected_plan_to_the_ba(tmp_path, reply):
    ws = _rejected_for_the_last_time(tmp_path, Stage.PLAN_REVIEW, {"analysis": 2, "plan_review": 2})

    assert await _handler(ws).handle_reply(ESCALATION_MSG, reply, "1") is True

    assert await _next_stage_run(ws) == "analysis"  # not a third review of the same plan
    assert ws.state.current_state == Stage.ANALYSIS
    assert ws.state.rejected_at is None
    assert ws.state.stage_iterations == {"analysis": 0, "plan_review": 0}


async def test_rejected_code_goes_to_a_dev_with_attempts_to_fix_it_in(tmp_path):
    """Dev was at its own cap when the reviewer rejected the code. Sent back with that count, it would have
    escalated before running."""
    stuck = {"analysis": 1, "plan_review": 1, "dev": 3, "scope_check": 1, "code_review": 2}
    ws = _rejected_for_the_last_time(tmp_path, Stage.CODE_REVIEW, dict(stuck))

    assert await _handler(ws).handle_reply(ESCALATION_MSG, "retry", "1") is True

    assert await _next_stage_run(ws) == "dev"
    assert ws.state.stage_iterations == {"analysis": 1, "plan_review": 1, "dev": 0, "scope_check": 0, "code_review": 0}


async def test_skip_accepts_the_rejected_work(tmp_path):
    ws = _rejected_for_the_last_time(tmp_path, Stage.PLAN_REVIEW, {"analysis": 2, "plan_review": 2})

    assert await _handler(ws).handle_reply(ESCALATION_MSG, "skip", "1") is True

    assert ws.state.current_state == Stage.DEV
    assert ws.state.rejected_at is None
    assert await _next_stage_run(ws) == "dev"


async def test_a_retry_that_names_the_judge_runs_the_judge(tmp_path):
    """The operator who edits the plan by hand asks for the review itself: `retry ACME-1 from plan_review`."""
    ws = _rejected_for_the_last_time(tmp_path, Stage.PLAN_REVIEW, {"analysis": 2, "plan_review": 2})
    handler = _handler(ws, ParsedIntent(intent="retry", params={"ticket_id": "ACME-1", "from_stage": "plan_review"}))

    await handler.handle_message("retry ACME-1 from plan_review", "1")

    assert ws.state.rejected_at is None
    assert await _next_stage_run(ws) == "plan_review"


async def test_a_rejection_left_behind_does_not_send_a_later_visit_away(tmp_path):
    """The mark is for the resume it was made for. A ticket that left the block another way and reaches the
    judge again, with new work, is judged."""
    ws = _rejected_for_the_last_time(tmp_path, Stage.PLAN_REVIEW, {"analysis": 2, "plan_review": 2})
    handler = _handler(ws, ParsedIntent(intent="retry", params={"ticket_id": "ACME-1", "from_stage": "analysis"}))

    await handler.handle_message("retry ACME-1 from analysis", "1")
    ws.transition(Stage.PLAN_REVIEW)  # the BA is done

    assert await _next_stage_run(ws) == "plan_review"


def test_the_hint_says_what_each_reply_does():
    hint = build_rejected_hint("analysis", sep="---")

    assert "ANALYSIS" in hint
    assert '"retry"' in hint and '"skip"' in hint
    assert "it may say PASS" not in hint

"""A stage the operator resumes gets a fresh attempt budget.

A ticket blocked by a stage's attempt cap keeps that stage's counter at the cap. Resumed with the counter untouched,
the driver escalates again before the agent runs: the "retry" the escalation message offers did nothing, and the
context the operator typed never reached an agent.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import ParsedIntent
from orchestrator.pipeline.driver import advance_workspace
from orchestrator.workflow_router import load_workflow
from workspace.workspace import Stage, Workspace, WorkspaceState

WORKFLOW = Path(__file__).parent.parent.parent / "workflows" / "default-workflow.yaml"
ESCALATION_MSG = 100


def _blocked_at_the_cap(tmp_path: Path, stage: str = Stage.SCOPE_CHECK, iterations: dict | None = None) -> Workspace:
    root = tmp_path / "ACME-1"
    (root / "meta").mkdir(parents=True)
    state = WorkspaceState(
        ticket_id="ACME-1", company_id="acme", repo_id="app", workspace_root=str(root),
        current_state=Stage.BLOCKED, previous_state=stage,
        stage_iterations=iterations or {"dev": 3, "scope_check": 2},
        human_input_pending=True, escalation_msg_id=ESCALATION_MSG, escalation_chat_id="1",
    )
    ws = Workspace(str(root), state)
    ws.save_state()
    return ws


def _handler(ws: Workspace, parsed: ParsedIntent | None = None) -> CommandHandler:
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=1)
    parser = AsyncMock()
    parser.parse = AsyncMock(return_value=parsed or ParsedIntent(intent="status", params={}))
    return CommandHandler(
        intent_parser=parser, notifier=notifier, mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [ws],
    )


async def _runs_the_agent(ws: Workspace) -> bool:
    """True if the driver's next step runs the stage (its agent or action), False if it escalates instead."""
    run_stage, escalate = AsyncMock(), AsyncMock()
    await advance_workspace(
        ws, workflow=load_workflow(str(WORKFLOW)), mode_handler=None,
        handle_agent_stage_fn=run_stage, handle_action_stage_fn=run_stage, handle_escalate_fn=escalate,
    )
    return run_stage.await_count == 1 and escalate.await_count == 0


@pytest.mark.parametrize("reply", ["retry", "The detekt failure is expected, carry on."])
async def test_a_reply_to_the_escalation_runs_the_stage_again(tmp_path, reply):
    ws = _blocked_at_the_cap(tmp_path)

    assert await _handler(ws).handle_reply(ESCALATION_MSG, reply, "1") is True

    assert ws.state.current_state == Stage.SCOPE_CHECK
    assert ws.state.stage_iterations == {"dev": 3, "scope_check": 0}  # only the resumed stage
    assert await _runs_the_agent(ws)


async def test_an_answer_typed_without_replying_runs_the_stage_again(tmp_path):
    ws = _blocked_at_the_cap(tmp_path)
    handler = _handler(ws, ParsedIntent(intent="provide_input", params={"input_text": "carry on"}))

    await handler.handle_message("carry on", "1")

    assert ws.state.current_state == Stage.SCOPE_CHECK
    assert ws.state.stage_iterations == {"dev": 3, "scope_check": 0}
    assert await _runs_the_agent(ws)


async def test_pr_review_at_its_cap_runs_again_too(tmp_path):
    """Every stage with a cap, also the one "retry <ticket> from <stage>" has no name for."""
    ws = _blocked_at_the_cap(tmp_path, Stage.PR_REVIEW, {"pr_review": 3})

    assert await _handler(ws).handle_reply(ESCALATION_MSG, "retry", "1") is True

    assert ws.state.current_state == Stage.PR_REVIEW
    assert ws.state.stage_iterations == {"pr_review": 0}
    assert await _runs_the_agent(ws)


def test_the_question_ends_with_the_block_it_was_asked_in(tmp_path):
    """A reply is recorded with the question it answers. Kept after the ticket moved on, an old question would
    be attached to the answer of a later block that asked none: "may the pipeline edit release.yml?" beside a
    "yes, go ahead" meant for something else reads as permission."""
    ws = _blocked_at_the_cap(tmp_path)
    ws.update_state(human_input_question="May the pipeline edit .github/workflows/release.yml?")

    ws.transition(Stage.SCOPE_CHECK)

    assert ws.state.human_input_question is None

"""Pipeline state-machine driver: advance one workspace one step.

Reads the workspace's current state, maps to a workflow stage, and
dispatches to the agent-stage or action-stage executor. Handles
manual-mode approval gates and AWAITING_APPROVAL auto-resume.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from integrations.base.notifier import Button
from integrations.telegram.handlers.approval import APPROVAL_NEXT_STATE
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator import tg_format
from orchestrator.constants import REPORT_BA
from orchestrator.pipeline.agent_stage import _emit, _log_pipeline
from orchestrator.workflow_router import (
    WorkflowDefinition,
    get_next_stage,
    should_escalate as workflow_should_escalate,
)
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


_STAGE_TO_STATE = {
    "analysis": Stage.ANALYSIS,
    "dev": Stage.DEV,
    "scope_check": Stage.SCOPE_CHECK,
    "qa": Stage.QA,
    "push": Stage.PUSHED,
    "pr_review": Stage.PR_REVIEW,
    "done": Stage.DONE,
    "escalate": Stage.BLOCKED,
}

_STATE_TO_STAGE = {v: k for k, v in _STAGE_TO_STATE.items()}


def _stage_to_state(stage_id: str) -> str | None:
    return _STAGE_TO_STATE.get(stage_id)


def _state_to_stage(state: str) -> str | None:
    return _STATE_TO_STAGE.get(state)


def advance_to_stage(
    workspace: Workspace,
    stage_id: str,
    *,
    event_bus: Any | None = None,
) -> None:
    """Transition workspace to the state corresponding to a workflow stage."""
    state_name = _stage_to_state(stage_id)
    if state_name:
        _log_pipeline(workspace, f"→ {state_name}")
        _emit(
            event_bus,
            "stage_transition",
            f"{workspace.state.ticket_id}: {workspace.state.current_state} -> {state_name}",
            project_id=workspace.state.company_id,
            ticket_id=workspace.state.ticket_id,
            data={"from_state": workspace.state.current_state, "to_state": state_name},
        )
        workspace.transition(state_name)
    else:
        logger.warning("Cannot map stage '%s' to state", stage_id)


def build_gate_summary(
    workspace: Workspace, gate_state: str,
) -> tuple[str, list[Button]]:
    """Build a summary message and buttons for an approval gate notification."""
    state = workspace.state
    tid = state.ticket_id
    title = tg_format.read_ticket_title(workspace)
    buttons = [
        Button(label="Approve", action=f"approve:{tid}"),
        Button(label="Reject", action=f"reject:{tid}"),
    ]

    if gate_state == Stage.PR_REVIEW:
        resolution_file = workspace.reports_dir / "pr-review-resolution.md"
        summary = ""
        if resolution_file.exists():
            content = resolution_file.read_text(encoding="utf-8")
            # Extract the last Resolution Summary line
            for line in reversed(content.splitlines()):
                if line.startswith("Fixed:") or line.startswith("## Resolution Summary"):
                    summary = line
                    break
            if not summary:
                summary = "Review complete."
        else:
            comments_file = workspace.reports_dir / "pr-review-comments.md"
            if comments_file.exists():
                count = comments_file.read_text(encoding="utf-8").count("## Comment by")
                summary = f"{count} comment(s) processed."
            else:
                summary = "No PR comments found."
        hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
        text = (
            f"{hdr}\n"
            f"PR: {state.pr_url or 'N/A'}\n"
            f"{summary}"
        )
        return text, buttons

    if gate_state == Stage.ANALYSIS:
        # Include BA summary from ba.md
        ba_file = workspace.reports_dir / REPORT_BA
        summary = ""
        if ba_file.exists():
            content = ba_file.read_text(encoding="utf-8")
            # Extract first heading + summary paragraph
            lines = content.strip().splitlines()
            for line in lines:
                if line.startswith("## Summary") or line.startswith("## Fix") or line.startswith("## Root"):
                    continue
                if line.strip() and not line.startswith("#"):
                    summary = tg_format.strip_markdown(line.strip()[:200])
                    break
        gate_title = f"Analysis complete.\n{summary}" if summary else "Analysis complete."
        gate_title += "\n\nApprove = start coding. Reject = back to analysis."
    elif gate_state == Stage.QA:
        gate_title = "QA passed.\n\nApprove = push code & open PR. Reject = back to dev."
    else:
        gate_title = f"Awaiting approval at {gate_state}"

    hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
    text = (
        f"{hdr}\n"
        f"{gate_title}"
    )
    return text, buttons


async def advance_workspace(
    workspace: Workspace,
    *,
    workflow: WorkflowDefinition,
    mode_handler: ModeHandler | None,
    handle_agent_stage_fn: Callable,
    handle_action_stage_fn: Callable,
    handle_escalate_fn: Callable,
    _resume_depth: int = 0,
) -> None:
    """Advance a workspace through the pipeline.

    `_resume_depth` is internal: the auto-resume branch tail-calls back into
    this function so the resumed workspace doesn't wait a full poll cycle.
    Capped to prevent stack overflow if cascading gates ever chain.
    """
    state = workspace.state
    current = state.current_state

    if current == Stage.BLOCKED:
        return  # Waiting for human reply

    if current == Stage.MANUAL_CONTROL:
        return  # Under human control — skip entirely

    if current == Stage.AWAITING_APPROVAL:
        # Manual→Auto mid-flight: if the operator switched to auto while
        # this workspace was parked at a gate, auto-approve and resume.
        # Otherwise keep waiting for an explicit approve/reject.
        if not (mode_handler and mode_handler.get_mode() == "auto"):
            return  # Waiting for operator approval
        previous = state.previous_state
        next_state = APPROVAL_NEXT_STATE.get(previous)
        if not next_state:
            logger.warning(
                "Cannot auto-resume %s: unknown previous gate %s",
                state.ticket_id, previous,
            )
            return
        workspace.transition(next_state)
        logger.info(
            "Auto-resumed %s from AWAITING_APPROVAL (gate=%s) to %s "
            "after mode switch to auto",
            state.ticket_id, previous, next_state,
        )
        # Re-advance so the resumed workspace doesn't wait a full poll cycle.
        if _resume_depth >= 5:
            logger.warning(
                "advance_workspace auto-resume cap hit for %s (depth=%d); "
                "next poll cycle will pick it up",
                state.ticket_id, _resume_depth,
            )
            return
        await advance_workspace(
            workspace,
            workflow=workflow,
            mode_handler=mode_handler,
            handle_agent_stage_fn=handle_agent_stage_fn,
            handle_action_stage_fn=handle_action_stage_fn,
            handle_escalate_fn=handle_escalate_fn,
            _resume_depth=_resume_depth + 1,
        )
        return

    if current in (Stage.DONE, Stage.ARCHIVED):
        return  # Terminal

    # Map pipeline state to workflow stage
    stage_id = _state_to_stage(current)
    if not stage_id:
        return

    stage_def = workflow.stages.get(stage_id)
    if not stage_def:
        logger.warning("No stage definition for '%s'", stage_id)
        return

    # Check iteration cap -> escalate. A workflow that loops back into an
    # earlier stage with a stale at-max counter must clear
    # state.stage_iterations[<stage_id>] explicitly at transition time —
    # otherwise it will escalate immediately on re-entry.
    if stage_def.max_iterations > 0:
        iterations = state.stage_iterations.get(stage_id, 0)
        if workflow_should_escalate(stage_id, workflow, iterations):
            next_stage = get_next_stage(stage_id, workflow, "max_iterations")
            if next_stage == "escalate":
                await handle_escalate_fn(workspace, is_max_iterations=True)
            return

    # Dispatch: agent stage or action stage
    if stage_def.agent:
        await handle_agent_stage_fn(workspace, stage_id, stage_def)
    elif stage_def.action:
        await handle_action_stage_fn(workspace, stage_id, stage_def)

"""Pipeline state-machine driver: advance one workspace one step.

Reads the workspace's current state, maps to a workflow stage, and
dispatches to the agent-stage or action-stage executor. Handles
manual-mode approval gates and AWAITING_APPROVAL auto-resume.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from integrations.base.notifier import Button
from integrations.telegram.handlers.approval import APPROVAL_NEXT_STATE
from integrations.telegram.handlers.mode import ModeHandler
from orchestrator import tg_format
from orchestrator.agent_runtime import last_attempt_block
from orchestrator.constants import REPORT_BA, RUNTIME_OUTPUT_PLAN_REVIEW
from orchestrator.pipeline.agent_stage import _emit, _log_pipeline
from orchestrator.tg_i18n import detail, model_text, tr
from orchestrator.workflow_router import (
    WorkflowDefinition,
    get_next_stage,
    should_escalate as workflow_should_escalate,
)
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


_STAGE_TO_STATE = {
    "analysis": Stage.ANALYSIS,
    "plan_review": Stage.PLAN_REVIEW,
    "dev": Stage.DEV,
    "scope_check": Stage.SCOPE_CHECK,
    "code_review": Stage.CODE_REVIEW,
    "qa": Stage.QA,
    "push": Stage.PUSHED,
    "pr_review": Stage.PR_REVIEW,
    "done": Stage.DONE,
    "escalate": Stage.BLOCKED,
}

_STATE_TO_STAGE = {v: k for k, v in _STAGE_TO_STATE.items()}


def _stage_to_state(stage_id: str) -> str | None:
    return _STAGE_TO_STATE.get(stage_id)


def state_to_stage(state: str) -> str | None:
    return _STATE_TO_STAGE.get(state)


def reset_attempts_from(workspace: Workspace, state: str) -> None:
    """Give the stage an operator sends a ticket back to, and every stage after it, a fresh attempt budget.

    A stage at its cap escalates before it runs (see advance_workspace). Sent back to a stage with that
    counter at the cap, a ticket escalated again without an agent run; restarted from an earlier stage, it did
    so at each later stage whose counter was still at its cap. Stages before the restart point keep their counts.
    """
    stage_ids = list(_STAGE_TO_STATE)
    stage_id = state_to_stage(state)
    if stage_id is None:
        return
    iterations = workspace.state.stage_iterations
    iterations[stage_id] = 0
    for later in stage_ids[stage_ids.index(stage_id) + 1:]:
        if later in iterations:
            iterations[later] = 0


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


def _extract_ba_verdict(ba_file: Any) -> str:
    """One-line BA decision for the approval-gate message.

    Prefers the `## Verdict` section of ba.md — a single sentence the BA
    writes stating what must be fixed (bug) or implemented (story). Falls
    back to the first sentence of the file's first prose line for plans
    written before the Verdict section existed.
    """
    if not ba_file.exists():
        return ""
    lines = ba_file.read_text(encoding="utf-8").splitlines()

    # Preferred: text under the `## Verdict` heading.
    collecting = False
    for line in lines:
        stripped = line.strip()
        if not collecting:
            if stripped.lower() == "## verdict":
                collecting = True
            continue
        if stripped.startswith("#"):
            break
        if stripped:
            return tg_format.strip_markdown(stripped)[:240]

    # Fallback: first prose line, trimmed to its first sentence.
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            text = tg_format.strip_markdown(stripped)
            match = re.match(r".*?[.!?](?:\s|$)", text)
            return (match.group(0).strip() if match else text)[:240]
    return ""


def _extract_plan_review_verdict(report_file: Any) -> str:
    """One-line plan-review decision for the approval-gate message.

    The file's own first line is never the verdict: `_write_agent_output`
    opens every appended attempt with a `---` separator and an `**Attempt:**`
    header. Isolate the newest attempt first, then take its first non-blank
    line — the agent's contract puts `Status: PASS`/`Status: FAIL` there.
    """
    if not report_file.exists():
        return ""
    block = last_attempt_block(report_file.read_text(encoding="utf-8"))
    for line in block.splitlines():
        stripped = line.strip()
        if stripped:
            return tg_format.strip_markdown(stripped)[:240]
    return ""


def build_gate_summary(
    workspace: Workspace, gate_state: str,
) -> tuple[str, list[Button]]:
    """Build a summary message and buttons for an approval gate notification."""
    state = workspace.state
    tid = state.ticket_id
    title = tg_format.read_ticket_title(workspace)
    buttons = [
        Button(label=tr("Approve"), action=f"approve:{tid}"),
        Button(label=tr("Reject"), action=f"reject:{tid}"),
    ]

    if gate_state == Stage.PR_REVIEW:
        resolution_file = workspace.reports_dir / "pr-review-resolution.md"
        summary = ""
        if resolution_file.exists():
            content = resolution_file.read_text(encoding="utf-8")
            # Extract the last Resolution Summary line
            for line in reversed(content.splitlines()):
                if line.startswith("Fixed:") or line.startswith("## Resolution Summary"):
                    summary = detail(line)
                    break
            if not summary:
                summary = tr("Review complete.")
        else:
            comments_file = workspace.reports_dir / "pr-review-comments.md"
            if comments_file.exists():
                count = comments_file.read_text(encoding="utf-8").count("## Comment by")
                summary = tr("{count} comment(s) processed.", count=count)
            else:
                summary = tr("No PR comments found.")
        hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
        text = (
            f"{hdr}\n"
            f"PR: {state.pr_url or tr('N/A')}\n"
            f"{summary}"
        )
        return text, buttons

    if gate_state == Stage.PLAN_REVIEW:
        # Surface both verdicts — the BA's plan and the plan-reviewer's
        # sign-off on it — so the operator can sanity-check a plan the
        # machine has already vetted before sending it to dev.
        ba_verdict = _extract_ba_verdict(workspace.reports_dir / REPORT_BA)
        review_verdict = _extract_plan_review_verdict(
            workspace.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW,
        )
        gate_title = tr("Plan review complete.")
        if ba_verdict:
            gate_title += "\n\n" + tr("BA decision:") + f"\n{model_text(ba_verdict)}"
        if review_verdict:
            gate_title += "\n\n" + tr("Plan reviewer:") + f"\n{model_text(review_verdict)}"
        gate_title += "\n\n" + tr("Approve = start coding. Reject = back to analysis.")
    else:
        gate_title = tr("Awaiting approval at {gate_state}", gate_state=gate_state)

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
    stage_id = state_to_stage(current)
    if not stage_id:
        return

    stage_def = workflow.stages.get(stage_id)
    if not stage_def:
        logger.warning("No stage definition for '%s'", stage_id)
        return

    # The judge here rejected this work on its last allowed attempt and the
    # ticket stopped on that verdict (see handle_agent_stage). The operator's
    # go-ahead brought it back: the work goes to be fixed, with fresh attempts
    # for the fix and for everything after it. Judged again unchanged, it
    # would only be rejected again.
    if state.rejected_at == current:
        fix_stage = get_next_stage(stage_id, workflow, "fail")
        workspace.update_state(rejected_at=None)
        _log_pipeline(workspace, f"{stage_id} rejected this work for the last time — {fix_stage} reworks it")
        reset_attempts_from(workspace, _stage_to_state(fix_stage))
        advance_to_stage(workspace, fix_stage)
        await advance_workspace(
            workspace,
            workflow=workflow,
            mode_handler=mode_handler,
            handle_agent_stage_fn=handle_agent_stage_fn,
            handle_action_stage_fn=handle_action_stage_fn,
            handle_escalate_fn=handle_escalate_fn,
            _resume_depth=_resume_depth,
        )
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

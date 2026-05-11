"""Escalation / BLOCKED state assembly.

When the pipeline cannot progress automatically (verification failure, max
iterations, agent-reported escalate), this module formats the operator
message and updates workspace state.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from integrations.base.notifier import NotifierInterface
from orchestrator import tg_format
from orchestrator.constants import (
    REPORT_BA_QUESTIONS,
    STAGE_RUNTIME_OUTPUT,
)
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


BLOCKED_REASON_MAX_CHARS = 800

BOILERPLATE_LINE_PATTERNS = (
    re.compile(r"^-{3,}$"),
    re.compile(r"^={3,}$"),
    re.compile(r"^\*\*Attempt.*\*\*$"),
    re.compile(r"^## Decision:"),
)


def truncate_reason(text: str) -> str:
    if len(text) <= BLOCKED_REASON_MAX_CHARS:
        return text
    return text[:BLOCKED_REASON_MAX_CHARS] + "…"


def build_blocked_reason(workspace: Any, stage_id: str) -> str:
    """Extract a human-readable reason for why a workspace is blocked.

    For analysis: prefer the BA agent's numbered questions
    (`ai_pipeline/<ticket>/ba-questions.md`). For other stages: prefer the
    stage-specific runtime output file (e.g. `qa-agent-output.md` for the
    qa stage) so that a different stage's more-recent output is never
    mistakenly shown as the block reason. Falls back to a generic message.
    """
    reports = workspace.reports_dir
    ticket_id = workspace.state.ticket_id
    artifact_path = f"ai_pipeline/{ticket_id}/"
    if not reports.exists():
        return f"Pipeline stuck at {stage_id}. Check {artifact_path} for details."

    if stage_id == "analysis":
        questions = reports / REPORT_BA_QUESTIONS
        if questions.exists():
            text = questions.read_text(encoding="utf-8").strip()
            if text:
                return truncate_reason(text)

    # Prefer the runtime output file for this specific stage so we never
    # show a different stage's (e.g. scope-guard) output when QA is blocked.
    stage_output = None
    runtime_filename = STAGE_RUNTIME_OUTPUT.get(stage_id)
    if runtime_filename:
        candidate = reports / runtime_filename
        if candidate.exists():
            stage_output = candidate
        else:
            return f"{stage_id} agent produced no output (may have timed out or crashed). Check pipeline logs."

    if stage_output is None:
        # Stage has no known agent output — don't show an unrelated stage's file.
        return f"Pipeline stuck at {stage_id}. Check pipeline logs for details."

    raw = stage_output.read_text(encoding="utf-8")
    # Strip leading boilerplate and blank lines.
    lines = raw.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if any(p.match(stripped) for p in BOILERPLATE_LINE_PATTERNS):
            continue
        start = i
        break
    else:
        return f"Pipeline stuck at {stage_id}. Check {artifact_path} for details."

    body = "\n".join(lines[start:]).strip()
    if not body:
        return f"Pipeline stuck at {stage_id}. Check {artifact_path} for details."
    return truncate_reason(body)


async def handle_escalate(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    *,
    workflow: Any = None,
    event_bus: Any = None,
    is_max_iterations: bool = False,
) -> None:
    """Send escalation notification and block workspace."""
    state = workspace.state

    if not notifier:
        logger.warning("No notifier configured, cannot escalate %s", state.ticket_id)
        workspace.transition(Stage.FAILED)
        workspace.update_state(error="No notifier configured for escalation")
        return

    if not chat_id:
        logger.warning("No chat_id for escalation of %s", state.ticket_id)
        workspace.transition(Stage.FAILED)
        workspace.update_state(error="No Telegram chat_id configured")
        return

    stage = state.previous_state or state.current_state
    sep = "─" * 30
    title = tg_format.read_ticket_title(workspace)
    hdr = tg_format.tg_header("🔔", state.company_id, state.ticket_id, title)
    stage_id_str = stage.lower() if isinstance(stage, str) else str(stage).lower()
    is_agent_stage = stage_id_str in STAGE_RUNTIME_OUTPUT
    if is_max_iterations:
        stage_def = workflow.stages.get(stage_id_str) if workflow else None
        cap = stage_def.max_iterations if stage_def else "?"
        iterations = state.stage_iterations.get(stage_id_str, 0)
        header = f"{hdr}\nStage: {stage} — stuck after {iterations} attempts\n"
    else:
        header = f"{hdr}\nStage: {stage}\n"

    reason = tg_format.strip_markdown(build_blocked_reason(workspace, stage_id_str))
    if is_max_iterations:
        hint = (
            f"\n{sep}\n"
            f"The agent ran {iterations} times without completing this stage. "
            f"Last output is shown above — it may say PASS but something is still blocking progress.\n\n"
            f"Options:\n"
            f"  Reply with context — give the agent more information and resume\n"
            f"  Reply \"skip\" — advance past this stage to the next one\n"
            f"  Reply \"retry\" — re-run this stage\n"
            f"  Send \"retry {state.ticket_id} from {stage_id_str}\" — reset counter and run again\n"
            f"  Send \"retry {state.ticket_id} from dev\" — restart the dev agent from scratch"
        )
    elif is_agent_stage:
        hint = (
            f"\n{sep}\n"
            f"↩️ Reply with your answer or additional context, or:\n"
            f"  Reply \"skip\" — advance past this stage to the next one\n"
            f"  Reply \"retry\" — re-run this stage"
        )
    else:
        hint = (
            f"\n{sep}\n"
            f"Options:\n"
            f"  Reply \"skip\" — advance past this stage to the next one\n"
            f"  Reply \"retry\" — re-run this stage\n"
            f"  Send \"retry {state.ticket_id}\" to retry from this stage\n"
            f"  Send \"retry {state.ticket_id} from dev\" to restart from an earlier stage"
        )
    message = f"{header}\n{reason}{hint}"

    try:
        msg_id = await notifier.send_message(chat_id, message)
        workspace.transition(Stage.BLOCKED)
        workspace.update_state(human_input_question=reason)
        workspace.state.escalation_msg_id = msg_id
        workspace.state.escalation_chat_id = chat_id
        workspace.save_state()
        logger.info("Escalated %s via Telegram (msg_id=%d)", state.ticket_id, msg_id)
        if event_bus is not None:
            event_bus.emit(
                "escalation_sent",
                f"Escalated {workspace.state.ticket_id} to human",
                project_id=workspace.state.company_id,
                ticket_id=workspace.state.ticket_id,
                data={"reason": reason},
            )
    except Exception as e:
        logger.error("Telegram send failed for %s: %s", state.ticket_id, e)
        workspace.transition(Stage.FAILED)
        workspace.update_state(error=f"Telegram notification failed: {e}")

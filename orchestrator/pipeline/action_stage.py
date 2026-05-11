"""Action stage dispatcher: route to the named action, verify, advance."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from integrations.base.notifier import Button, NotifierInterface
from orchestrator import stage_verifier, tg_format
from orchestrator.approval_gate import should_approval_gate
from orchestrator.escalation import handle_escalate
from orchestrator.pipeline.agent_stage import _emit, _log_pipeline, rollback_iteration
from orchestrator.workflow_router import WorkflowDefinition, get_next_stage
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


async def handle_action_stage(
    workspace: Workspace,
    stage_id: str,
    stage_def: Any,
    *,
    workflow: WorkflowDefinition,
    notifier: NotifierInterface | None,
    mode_handler: Any,
    get_chat_id: Callable[[], str],
    dry_run: bool,
    event_bus: Any | None,
    action_push_and_open_pr: Callable[[Workspace], Awaitable[Any]],
    action_fetch_pr_comments: Callable[[Workspace, Any], Awaitable[Any]],
    action_finalize: Callable[[Workspace], Awaitable[Any]],
    advance_to_stage_fn: Callable[[Workspace, str], None],
    on_ticket_done_fn: Callable[[Workspace], Awaitable[None]],
    build_gate_summary_fn: Callable[[Workspace, str], tuple[str, list[Button]]],
) -> None:
    """Execute an action stage with capture → execute → verify → transition."""
    action = stage_def.action
    state = workspace.state

    if dry_run:
        logger.info(
            "[DRY RUN] Would execute action '%s' for %s",
            action, state.ticket_id,
        )
        next_stage = get_next_stage(stage_id, workflow)
        if next_stage:
            advance_to_stage_fn(workspace, next_stage)
        return

    if action == "notify_human":
        await handle_escalate(
            workspace, notifier, get_chat_id() if notifier else "",
            workflow=workflow,
            event_bus=event_bus,
        )
        return

    stage_start_commit = stage_verifier.capture_stage_start(workspace, stage_id)
    workspace.increment_iteration(stage_id)

    if action == "push_and_open_pr":
        result = await action_push_and_open_pr(workspace)
    elif action == "fetch_pr_comments":
        result = await action_fetch_pr_comments(workspace, stage_def)
    elif action == "finalize":
        result = await action_finalize(workspace)
    else:
        logger.warning("Unknown action: %s", action)
        return

    if result.skipped:
        rollback_iteration(workspace, stage_id)
        return

    if not result.success:
        _emit(
            event_bus,
            "action_failed",
            f"Action {action} failed for {state.ticket_id}: {result.error}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            data={"stage": stage_id, "error": result.error},
        )
        if action == "push_and_open_pr":
            # PR creation failures need operator attention — escalate so a
            # Telegram message is sent and the ticket can be recovered.
            workspace.update_state(error=result.error)
            await handle_escalate(
                workspace, notifier, get_chat_id() if notifier else "",
                workflow=workflow,
                event_bus=event_bus,
            )
        else:
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=result.error)
        return

    verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
    if not verify_result.ok:
        error_msg = f"{stage_id}: {verify_result.reason}"
        workspace.transition(Stage.BLOCKED)
        workspace.update_state(error=error_msg)
        _emit(
            event_bus,
            "stage_verification_failed",
            f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            data={"stage": stage_id, "reason": verify_result.reason},
        )
        return

    if result.metadata:
        workspace.update_state(**result.metadata)

    current_state = workspace.state.current_state
    # Only gate on forward transitions (DONE, PR_REVIEW, etc.), not fix loops back to DEV
    if result.next_state != Stage.DEV and should_approval_gate(mode_handler, current_state):
        workspace.transition(Stage.AWAITING_APPROVAL)
        _emit(
            event_bus,
            "approval_requested",
            f"Awaiting approval for {state.ticket_id} after {current_state}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            data={"gate": current_state},
        )
        if notifier:
            chat_id = get_chat_id()
            summary, buttons = build_gate_summary_fn(workspace, current_state)
            await notifier.send_message(chat_id, summary, buttons=buttons)
        return

    _emit(
        event_bus,
        "stage_transition",
        f"{state.ticket_id}: {current_state} -> {result.next_state}",
        project_id=state.company_id, ticket_id=state.ticket_id,
        data={"from_state": current_state, "to_state": result.next_state},
    )
    workspace.transition(result.next_state)

    if result.next_state == Stage.DONE:
        await on_ticket_done_fn(workspace)

    meta_summary = ""
    if result.metadata.get("pr_url"):
        meta_summary = f" PR: {result.metadata['pr_url']}"
    _log_pipeline(workspace, f"{action} completed.{meta_summary} → {result.next_state}")
    _emit(
        event_bus,
        "action_completed",
        f"Action {action} completed for {state.ticket_id}",
        project_id=state.company_id, ticket_id=state.ticket_id,
        data={"stage": stage_id, **result.metadata},
    )

    # Notify when PR is created — user needs to review and reply
    if action == "push_and_open_pr" and result.metadata.get("pr_url") and notifier:
        chat_id = get_chat_id()
        if chat_id:
            pr_url = result.metadata["pr_url"]
            title = tg_format.read_ticket_title(workspace)
            hdr = tg_format.tg_header("🔗", state.company_id, state.ticket_id, title)
            msg = (
                f"{hdr}\n"
                f"PR opened: {pr_url}\n\n"
                f"Review the diff and merge when ready. The pipeline will wait.\n\n"
                f"If there are review comments, Cleave will escalate them one by one "
                f"for your decision (Fix or Won't Fix). Reply to any escalation message "
                f"to provide context.\n\n"
                f"When done: tap Review Complete or reply to this message."
            )
            pr_buttons = [Button(label="Review Complete", action=f"reviewed:{state.ticket_id}")]
            msg_id = await notifier.send_message(chat_id, msg, buttons=pr_buttons)
            workspace.state.escalation_msg_id = msg_id
            workspace.state.escalation_chat_id = chat_id
            workspace.state.human_input_reply = None  # clear stale "reviewed" from any prior run
            workspace.save_state()

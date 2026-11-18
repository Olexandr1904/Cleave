"""finalize action and ticket-done handling."""
from __future__ import annotations

import logging

from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TrackerInterface
from orchestrator import tg_format
from orchestrator.stage_verifier import ActionResult
from orchestrator.tg_i18n import tr
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


async def action_finalize(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    tracker: TrackerInterface | None,
) -> ActionResult:
    """Finalize a completed ticket. Returns ActionResult — caller transitions."""
    state = workspace.state

    if notifier:
        if chat_id:
            pr_url = state.pr_url or tr("(no PR)")
            title = tg_format.read_ticket_title(workspace)
            hdr = tg_format.tg_header("✅", state.company_id, state.ticket_id, title)
            message = (
                f"{hdr}\n"
                + tr("PR ready for human merge: {pr_url}", pr_url=pr_url)
            )
            try:
                await notifier.send_message(chat_id, message)
            except Exception as e:
                logger.warning("Finalize notification failed: %s", e)

    if tracker:
        try:
            await tracker.add_comment(
                state.ticket_id,
                f"Pipeline complete. PR ready for merge: {state.pr_url or 'N/A'}",
            )
        except Exception as e:
            logger.warning("Finalize tracker comment failed: %s", e)

    return ActionResult(
        success=True, next_state=Stage.DONE, error="", metadata={},
    )


async def on_ticket_done(
    workspace: Workspace,
    notifier: NotifierInterface | None,
    chat_id: str,
    tracker: TrackerInterface | None,
    in_review_status: str,
    handed_over: bool = False,
) -> None:
    """Handle ticket completion: TG notification + Jira status transition."""
    state = workspace.state

    # TG notification
    if notifier:
        if chat_id:
            title = tg_format.read_ticket_title(workspace)
            hdr = tg_format.tg_header("✅", state.company_id, state.ticket_id, title)
            if handed_over:
                body = tr("PR opened: {pr_url}\n\n"
                          "Reviewer mode takes it from here: its turns and review threads "
                          "come to the reviewer chat.\n\n"
                          "Ticket moved to review status.", pr_url=state.pr_url or tr("N/A"))
            else:
                body = tr("Pipeline complete.\n\n"
                          "PR ready for merge: {pr_url}\n\n"
                          "Ticket moved to review status.", pr_url=state.pr_url or tr("N/A"))
            await notifier.send_message(chat_id, f"{hdr}\n" + body)

    # Transition tracker ticket to in-review status (Jira: "In Review",
    # Trello: a list-name match). Fuzzy keywords are pipeline policy and
    # stay on this side of the port.
    if tracker:
        if in_review_status:
            try:
                available = await tracker.list_transitions(
                    state.ticket_id,
                )
                matched = None
                target_lower = in_review_status.lower()
                for name in available:
                    if target_lower in name.lower():
                        matched = name
                        break
                if matched is None:
                    for name in available:
                        if any(kw in name.lower() for kw in (
                            "review", "qa", "verification", "ready for qa",
                        )):
                            matched = name
                            break
                if matched is not None:
                    await tracker.transition_ticket(
                        state.ticket_id, matched,
                    )
                    logger.info(
                        "Transitioned %s to '%s'", state.ticket_id, matched,
                    )
                else:
                    logger.warning(
                        "Cannot transition %s to '%s' — available: %s",
                        state.ticket_id, in_review_status, available,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to transition %s on tracker: %s",
                    state.ticket_id, e,
                )

"""finalize action and ticket-done handling."""
from __future__ import annotations

import logging

from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TrackerInterface
from orchestrator import tg_format
from orchestrator.stage_verifier import ActionResult
from orchestrator.tg_i18n import detail, tr
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
    """Handle ticket completion: the tracker move to `in_review_status` (Jira: a status name, Trello: the
    "in_review" key of its list mapping), then a Telegram message that says whether the move happened."""
    state = workspace.state

    tracker_line = ""
    if tracker and in_review_status:
        try:
            await tracker.transition_ticket(state.ticket_id, in_review_status)
            tracker_line = tr("Tracker: moved to {status}.", status=in_review_status)
        except Exception as e:
            logger.warning("Failed to transition %s to '%s': %s", state.ticket_id, in_review_status, e)
            reason = str(e).splitlines()[0] if str(e) else type(e).__name__
            tracker_line = tr("Tracker: not moved to {status}: {reason}.", status=in_review_status,
                              reason=detail(reason.rstrip(".")))

    if notifier and chat_id:
        title = tg_format.read_ticket_title(workspace)
        hdr = tg_format.tg_header("✅", state.company_id, state.ticket_id, title)
        if handed_over:
            body = tr("PR opened: {pr_url}\n\n"
                      "Reviewer mode takes it from here: its turns and review threads "
                      "come to the reviewer chat.", pr_url=state.pr_url or tr("N/A"))
        else:
            body = tr("Pipeline complete.\n\nPR ready for merge: {pr_url}", pr_url=state.pr_url or tr("N/A"))
        await notifier.send_message(chat_id, f"{hdr}\n" + body + (f"\n\n{tracker_line}" if tracker_line else ""))

"""Approval handler — manages approve/reject for workspaces in AWAITING_APPROVAL."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

APPROVAL_NEXT_STATE = {
    # ANALYSIS is no longer an approval gate — APPROVAL_GATE_STATES governs
    # that — but workspaces parked at it before plan_review shipped are still
    # on disk. Without this entry Approve falls through to the "FAILED"
    # default in manual mode and the driver spins on "unknown previous gate
    # ANALYSIS" in auto mode. Route them into the new gate, not past it.
    "ANALYSIS": "PLAN_REVIEW",
    "PLAN_REVIEW": "DEV",
    "QA": "PUSHED",
    "PR_REVIEW": "DONE",
}

# Where "Reject" sends a workspace parked at a given gate. A gate that is not
# listed keeps its caller's own default (the previous state for the dashboard,
# FAILED for Telegram).
#
# Rejecting a plan means the *plan* is wrong, so it goes back to the analyst.
# Returning to PLAN_REVIEW would re-run the reviewer that just passed it,
# re-park at the same gate, and spend the stage's iteration budget.
APPROVAL_REJECT_STATE = {
    "PLAN_REVIEW": "ANALYSIS",
}


class ApprovalHandler:
    """Handles approval/rejection of workspaces awaiting manual approval."""

    def find_awaiting(
        self, workspaces: list[Any], ticket_id: str | None = None,
    ) -> list[Any]:
        """Find workspaces in AWAITING_APPROVAL state."""
        results = [
            ws for ws in workspaces
            if ws.state.current_state == "AWAITING_APPROVAL"
        ]
        if ticket_id:
            results = [ws for ws in results if ws.state.ticket_id == ticket_id]
        return results

    def resolve_next_state(self, workspace: Any) -> str:
        """Determine the next state based on which gate triggered the approval wait."""
        previous = workspace.state.previous_state
        return APPROVAL_NEXT_STATE.get(previous, "FAILED")

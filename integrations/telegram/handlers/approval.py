"""Approval handler — manages approve/reject for workspaces in AWAITING_APPROVAL."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_APPROVAL_NEXT_STATE = {
    "ANALYSIS": "DEV",
    "QA": "PUSHED",
    "PR_REVIEW": "DONE",
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
        return _APPROVAL_NEXT_STATE.get(previous, "FAILED")

"""Manual-mode approval gate logic.

A 'gate' is a state at which the pipeline waits for explicit operator
approval when running in manual mode. Auto mode bypasses gates entirely.
The set of gate states is fixed; the happy-path-next-stage map says which
forward transition each gate sits in front of.
"""
from __future__ import annotations

import logging

from integrations.telegram.handlers.mode import ModeHandler
from workspace.workspace import Stage

logger = logging.getLogger(__name__)

APPROVAL_GATE_STATES = {Stage.ANALYSIS, Stage.QA}

GATE_HAPPY_PATH_NEXT_STAGE = {
    Stage.ANALYSIS: "dev",
    Stage.QA: "push",
}


def should_approval_gate(
    mode_handler: ModeHandler | None,
    completed_state: str,
    next_stage: str | None = None,
) -> bool:
    """Should the workspace pause for approval after `completed_state`?

    Returns False if there is no mode_handler or mode is 'auto'. When
    `next_stage` is provided, gates only fire on happy-path transitions
    (failure loops and escalations bypass the gate). When `next_stage`
    is None, the gate fires whenever `completed_state` is in the gate set
    — used by callers that have already established they are on the happy
    path.
    """
    if not mode_handler or mode_handler.get_mode() != "manual":
        return False
    if completed_state not in APPROVAL_GATE_STATES:
        return False
    if next_stage is None:
        return True
    return next_stage == GATE_HAPPY_PATH_NEXT_STAGE.get(completed_state)

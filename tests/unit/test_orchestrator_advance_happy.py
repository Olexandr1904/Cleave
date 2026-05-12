"""Characterization tests for the advance_workspace happy path.

Pins down: terminal-state skip, max-iteration → escalate, agent stage success
→ advance, action stage success → advance.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _orc():
    orc = Orchestrator.__new__(Orchestrator)
    orc._workflow = SimpleNamespace(stages={})
    orc._dry_run = False
    orc._mode_handler = None
    orc._events = None
    orc._agent_runtime = MagicMock()
    orc._notifier = None
    orc._tracker = None
    orc._projects = {}
    orc._global_config = SimpleNamespace(telegram=SimpleNamespace(default_chat_id=""))
    orc._workspace_manager = MagicMock()
    return orc


def _ws(state):
    ws = MagicMock()
    ws.state = SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state=state,
        stage_iterations={}, previous_state="",
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.save_state = MagicMock()
    return ws


@pytest.mark.asyncio
async def test_advance_skips_terminal_states() -> None:
    """DONE, ARCHIVED, BLOCKED, MANUAL_CONTROL short-circuit immediately."""
    orc = _orc()
    for state in (Stage.DONE, Stage.ARCHIVED, Stage.BLOCKED, Stage.MANUAL_CONTROL):
        ws = _ws(state)
        await orc.advance_workspace(ws)
        ws.transition.assert_not_called()


@pytest.mark.asyncio
async def test_advance_max_iterations_escalates(monkeypatch) -> None:
    """When iteration count >= max_iterations, escalate is triggered."""
    orc = _orc()
    ws = _ws(Stage.DEV)
    ws.state.stage_iterations = {"dev": 5}

    orc._workflow = SimpleNamespace(stages={
        "dev": SimpleNamespace(max_iterations=5, agent="dev-agent", action=None),
    })

    # Patch routing helpers to deterministic answers
    monkeypatch.setattr(
        "orchestrator.pipeline.driver.workflow_should_escalate", lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.pipeline.driver.get_next_stage", lambda *a, **k: "escalate",
    )
    escalate_mock = AsyncMock()
    monkeypatch.setattr("orchestrator.escalation.handle_escalate", escalate_mock)

    await orc.advance_workspace(ws)
    escalate_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_advance_dispatches_agent_stage() -> None:
    """A stage with `agent` set routes to _handle_agent_stage."""
    orc = _orc()
    orc._handle_agent_stage = AsyncMock()
    orc._workflow = SimpleNamespace(stages={
        "dev": SimpleNamespace(max_iterations=0, agent="dev-agent", action=None),
    })
    ws = _ws(Stage.DEV)
    await orc.advance_workspace(ws)
    orc._handle_agent_stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_advance_dispatches_action_stage() -> None:
    """A stage with `action` set routes to _handle_action_stage."""
    orc = _orc()
    orc._handle_action_stage = AsyncMock()
    orc._workflow = SimpleNamespace(stages={
        "push": SimpleNamespace(
            max_iterations=0, agent=None, action="push_and_open_pr",
        ),
    })
    # Stage enum has PUSHED (not PUSH); _STATE_TO_STAGE maps Stage.PUSHED → "push".
    ws = _ws(Stage.PUSHED)
    await orc.advance_workspace(ws)
    orc._handle_action_stage.assert_awaited_once()

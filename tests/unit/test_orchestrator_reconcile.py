"""Characterization test for _reconcile_disk_workspaces."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.orchestrator import Orchestrator


def test_reconcile_adopts_disk_workspaces() -> None:
    """A workspace on disk but absent from _active_workspaces gets re-adopted."""
    orc = Orchestrator.__new__(Orchestrator)
    on_disk_ws = SimpleNamespace(state=SimpleNamespace(
        ticket_id="T-99", company_id="acme", current_state="dev",
    ))
    wm = MagicMock()
    wm.discover_workspaces.return_value = [on_disk_ws]
    orc._workspace_manager = wm
    orc._active_workspaces = []
    orc._events = None

    orc._reconcile_disk_workspaces()

    assert any(
        ws.state.ticket_id == "T-99" for ws in orc._active_workspaces
    )


def test_reconcile_no_duplicate_on_second_call() -> None:
    """A workspace already in _active_workspaces is not duplicated."""
    orc = Orchestrator.__new__(Orchestrator)
    ws = SimpleNamespace(state=SimpleNamespace(
        ticket_id="T-1", company_id="acme", current_state="dev",
    ))
    wm = MagicMock()
    wm.discover_workspaces.return_value = [ws]
    orc._workspace_manager = wm
    orc._active_workspaces = [ws]
    orc._events = None

    orc._reconcile_disk_workspaces()

    assert len([w for w in orc._active_workspaces if w.state.ticket_id == "T-1"]) == 1

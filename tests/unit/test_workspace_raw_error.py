from __future__ import annotations

import json
from pathlib import Path

from workspace.workspace import Workspace, WorkspaceState


def test_workspace_state_raw_error_round_trip(tmp_path: Path) -> None:
    state = WorkspaceState(
        ticket_id="T-1",
        company_id="acme",
        repo_id="repo",
        workspace_root=str(tmp_path),
        error="operator-facing summary",
        raw_error="CLI exit 137: killed by signal SIGKILL",
    )
    ws = Workspace(str(tmp_path), state=state)
    ws.save_state()

    loaded = json.loads((tmp_path / "state.json").read_text())
    assert loaded["error"] == "operator-facing summary"
    assert loaded["raw_error"] == "CLI exit 137: killed by signal SIGKILL"

    reloaded = Workspace(str(tmp_path)).state
    assert reloaded.raw_error == "CLI exit 137: killed by signal SIGKILL"


def test_workspace_state_raw_error_defaults_to_none(tmp_path: Path) -> None:
    state = WorkspaceState(
        ticket_id="T-2",
        company_id="acme",
        repo_id="repo",
        workspace_root=str(tmp_path),
    )
    assert state.raw_error is None

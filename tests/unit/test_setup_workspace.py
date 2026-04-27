import json
from pathlib import Path

from dashboard.setup_workspace import (
    create_setup_workspace,
    setup_workspace_path,
    write_state,
)


def test_setup_workspace_path_matches_ticket_shape(tmp_path: Path):
    p = setup_workspace_path(tmp_path, "acme", "acme-app")
    assert p == tmp_path / "acme" / "acme-app" / "setup"


def test_create_setup_workspace_creates_tree(tmp_path: Path):
    workspace = create_setup_workspace(
        base_dir=tmp_path,
        project_id="acme",
        repo_id="acme-app",
        redacted_input_md="# Project Setup Input\n- project_id: acme\n",
    )
    assert workspace.setup_dir.is_dir()
    assert workspace.tickets_dir.is_dir()
    assert (workspace.setup_dir / "meta" / "input.md").read_text().startswith(
        "# Project Setup Input"
    )
    assert (workspace.setup_dir / "logs").is_dir()
    state = json.loads((workspace.setup_dir / "state.json").read_text())
    assert state["current_state"] == "SETUP_PENDING"
    assert state["ticket_id"] == "setup"
    assert state["company_id"] == "acme"
    assert state["repo_id"] == "acme-app"
    assert state["title"] == "Workspace setup"


def test_write_state_transitions(tmp_path: Path):
    workspace = create_setup_workspace(
        base_dir=tmp_path,
        project_id="acme",
        repo_id="acme-app",
        redacted_input_md="",
    )
    write_state(workspace, "VALIDATING")
    state = json.loads((workspace.setup_dir / "state.json").read_text())
    assert state["current_state"] == "VALIDATING"
    assert state["previous_state"] == "SETUP_PENDING"

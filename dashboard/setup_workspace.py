"""Create and update the setup workspace tree for project-create runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SetupWorkspace:
    project_id: str
    repo_id: str
    project_dir: Path
    repo_dir: Path
    tickets_dir: Path
    setup_dir: Path


def setup_workspace_path(base_dir: Path, project_id: str, repo_id: str) -> Path:
    return Path(base_dir) / project_id / repo_id / "setup"


def create_setup_workspace(
    base_dir: Path,
    project_id: str,
    repo_id: str,
    redacted_input_md: str,
) -> SetupWorkspace:
    base = Path(base_dir)
    project_dir = base / project_id
    repo_dir = project_dir / repo_id
    tickets_dir = repo_dir / "tickets"
    setup_dir = repo_dir / "setup"

    tickets_dir.mkdir(parents=True, exist_ok=True)
    (setup_dir / "meta").mkdir(parents=True, exist_ok=True)
    (setup_dir / "reports").mkdir(parents=True, exist_ok=True)
    (setup_dir / "logs").mkdir(parents=True, exist_ok=True)

    (setup_dir / "meta" / "input.md").write_text(redacted_input_md, encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    state = {
        "ticket_id": "setup",
        "company_id": project_id,
        "repo_id": repo_id,
        "current_state": "SETUP_PENDING",
        "previous_state": None,
        "started_at": now,
        "last_updated_at": now,
        "kind": "setup",
    }
    (setup_dir / "state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )

    return SetupWorkspace(
        project_id=project_id,
        repo_id=repo_id,
        project_dir=project_dir,
        repo_dir=repo_dir,
        tickets_dir=tickets_dir,
        setup_dir=setup_dir,
    )


def write_state(
    workspace: SetupWorkspace,
    new_state: str,
    error: str | None = None,
) -> None:
    state_path = workspace.setup_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["previous_state"] = state.get("current_state")
    state["current_state"] = new_state
    state["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    if error is not None:
        state["error"] = error
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

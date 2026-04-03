"""Workspace object — represents a single ticket workspace with state access."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Valid status values
VALID_STATUSES = {"pending", "running", "waiting_for_human", "completed", "failed", "archived"}

# Valid status transitions
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running"},
    "running": {"running", "waiting_for_human", "completed", "failed"},
    "waiting_for_human": {"running"},
    "completed": {"archived"},
    "failed": {"archived"},
    "archived": set(),
}


@dataclass
class WorkspaceState:
    """Tracks the lifecycle of a single ticket through the pipeline."""
    ticket_id: str
    project_id: str
    repo_id: str
    workspace_root: str
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    current_stage: str = "pending"
    stage_iterations: dict[str, int] = field(default_factory=dict)
    human_input_pending: bool = False
    human_input_question: str | None = None
    human_input_reply: str | None = None
    started_at: str = ""
    last_updated_at: str = ""
    status: str = "pending"
    error: str | None = None

    def __post_init__(self) -> None:
        now = _now_iso()
        if not self.started_at:
            self.started_at = now
        if not self.last_updated_at:
            self.last_updated_at = now


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""


class Workspace:
    """Represents a single ticket workspace on disk."""

    def __init__(self, root: str, state: WorkspaceState | None = None) -> None:
        self._root = Path(root)
        self._state = state

    @property
    def root(self) -> Path:
        return self._root

    @property
    def repo_dir(self) -> Path:
        return self._root / "repo"

    @property
    def context_dir(self) -> Path:
        return self._root / "context"

    @property
    def logs_dir(self) -> Path:
        return self._root / "logs"

    @property
    def state_path(self) -> Path:
        return self._root / "state.json"

    @property
    def state(self) -> WorkspaceState:
        if self._state is None:
            self._state = self._load_state()
        return self._state

    def _load_state(self) -> WorkspaceState:
        """Load state from state.json on disk."""
        with open(self.state_path) as f:
            data = json.load(f)
        return WorkspaceState(**data)

    def save_state(self) -> None:
        """Atomically write state.json (temp file + rename)."""
        data = asdict(self._state)
        self._state.last_updated_at = _now_iso()
        data["last_updated_at"] = self._state.last_updated_at

        # Atomic write: write to temp file in same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._root), suffix=".tmp", prefix="state_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(self.state_path))
        except Exception:
            # Clean up temp file on error
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def update_state(self, **kwargs: Any) -> None:
        """Update state fields and save atomically."""
        state = self.state
        for key, value in kwargs.items():
            if not hasattr(state, key):
                raise ValueError(f"Unknown state field: {key}")
            setattr(state, key, value)
        self.save_state()

    def transition_status(self, new_status: str) -> None:
        """Transition workspace status with validation."""
        current = self.state.status
        if new_status not in VALID_STATUSES:
            raise InvalidTransitionError(f"Unknown status: {new_status}")
        if new_status not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransitionError(
                f"Cannot transition from '{current}' to '{new_status}'"
            )
        self.update_state(status=new_status)

    def increment_iteration(self, agent_id: str) -> int:
        """Increment and return iteration count for an agent."""
        state = self.state
        count = state.stage_iterations.get(agent_id, 0) + 1
        state.stage_iterations[agent_id] = count
        self.save_state()
        return count

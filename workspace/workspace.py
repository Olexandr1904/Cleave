"""Workspace object — represents a single ticket workspace with state access."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Valid pipeline states (architecture-v2 §3.3)
VALID_STATES = {
    "NEW", "ANALYSIS", "DEV", "SCOPE_CHECK", "QA",
    "PUSHED", "PR_REVIEW", "DONE",
    "BLOCKED", "FAILED", "ARCHIVED",
}

# Valid state transitions (architecture-v2 §3.3)
VALID_TRANSITIONS: dict[str, set[str]] = {
    "NEW":          {"ANALYSIS", "FAILED"},
    "ANALYSIS":     {"DEV", "BLOCKED", "FAILED"},
    "DEV":          {"SCOPE_CHECK", "BLOCKED", "FAILED"},
    "SCOPE_CHECK":  {"QA", "DEV", "BLOCKED", "FAILED"},
    "QA":           {"PUSHED", "DEV", "BLOCKED", "FAILED"},
    "PUSHED":       {"PR_REVIEW", "BLOCKED", "FAILED"},
    "PR_REVIEW":    {"DEV", "DONE", "BLOCKED", "FAILED"},
    "DONE":         {"ARCHIVED"},
    "BLOCKED":      {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "FAILED"},
    "FAILED":       set(),
    "ARCHIVED":     set(),
}


@dataclass
class WorkspaceState:
    """Tracks the lifecycle of a single ticket through the pipeline."""
    ticket_id: str
    company_id: str
    repo_id: str
    workspace_root: str
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    current_state: str = "NEW"
    previous_state: str | None = None
    stage_iterations: dict[str, int] = field(default_factory=dict)
    human_input_pending: bool = False
    human_input_question: str | None = None
    human_input_reply: str | None = None
    started_at: str = ""
    last_updated_at: str = ""
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
    def source_dir(self) -> Path:
        return self._root / "source"

    @property
    def meta_dir(self) -> Path:
        return self._root / "meta"

    @property
    def reports_dir(self) -> Path:
        return self._root / "reports"

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

    def transition(self, new_state: str) -> None:
        """Transition workspace to a new pipeline state with validation.

        For BLOCKED: stores previous_state so we can resume later.
        For resuming from BLOCKED: previous_state is cleared.
        """
        current = self.state.current_state
        if new_state not in VALID_STATES:
            raise InvalidTransitionError(f"Unknown state: {new_state}")
        if new_state not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransitionError(
                f"Cannot transition from '{current}' to '{new_state}'"
            )

        updates: dict[str, Any] = {"current_state": new_state}

        if new_state == "BLOCKED":
            updates["previous_state"] = current
            updates["human_input_pending"] = True
        elif current == "BLOCKED":
            # Resuming from BLOCKED — clear pending flag
            updates["previous_state"] = None
            updates["human_input_pending"] = False

        self.update_state(**updates)

    def increment_iteration(self, stage_id: str) -> int:
        """Increment and return iteration count for a stage."""
        state = self.state
        count = state.stage_iterations.get(stage_id, 0) + 1
        state.stage_iterations[stage_id] = count
        self.save_state()
        return count

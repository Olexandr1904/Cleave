"""Workspace object — represents a single ticket workspace with state access."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class Stage(StrEnum):
    """Pipeline stages (architecture-v2 §3.3)."""
    NEW = "NEW"
    ANALYSIS = "ANALYSIS"
    DEV = "DEV"
    SCOPE_CHECK = "SCOPE_CHECK"
    QA = "QA"
    PUSHED = "PUSHED"
    PR_REVIEW = "PR_REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    MANUAL_CONTROL = "MANUAL_CONTROL"
    DEFERRED = "DEFERRED"


VALID_STATES = set(Stage)

# Valid state transitions (architecture-v2 §3.3)
VALID_TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.NEW:                {Stage.ANALYSIS, Stage.FAILED},
    Stage.ANALYSIS:           {Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL},
    Stage.DEV:                {Stage.SCOPE_CHECK, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL},
    Stage.SCOPE_CHECK:        {Stage.QA, Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL},
    Stage.QA:                 {Stage.PUSHED, Stage.DEV, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL},
    Stage.PUSHED:             {Stage.PR_REVIEW, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.MANUAL_CONTROL},
    Stage.PR_REVIEW:          {Stage.DEV, Stage.DONE, Stage.BLOCKED, Stage.FAILED, Stage.DEFERRED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL},
    Stage.DONE:               {Stage.ARCHIVED},
    Stage.BLOCKED:            {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.FAILED, Stage.MANUAL_CONTROL},
    Stage.FAILED:             {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.MANUAL_CONTROL, Stage.ARCHIVED},
    Stage.ARCHIVED:           set(),
    Stage.AWAITING_APPROVAL:  {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.DONE, Stage.FAILED, Stage.MANUAL_CONTROL},
    Stage.MANUAL_CONTROL:     {Stage.ANALYSIS},
    Stage.DEFERRED:           {Stage.ANALYSIS, Stage.DEV, Stage.SCOPE_CHECK, Stage.QA, Stage.PUSHED, Stage.PR_REVIEW, Stage.FAILED, Stage.MANUAL_CONTROL},
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
    escalation_msg_id: int | None = None
    escalation_chat_id: str | None = None
    started_at: str = ""
    last_updated_at: str = ""
    error: str | None = None
    manual_control_started_at: str | None = None
    manual_control_comment: str | None = None
    retry_at: str | None = None
    pending_review_comments: list[dict] | None = None
    review_cycle: int = 0
    comments_to_resolve: list[int] | None = None

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
        return self._root / "source" / "reports"

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

    def transition(self, new_state: Stage, **extra: Any) -> None:
        """Transition workspace to a new pipeline state with validation.

        For BLOCKED/AWAITING_APPROVAL/MANUAL_CONTROL/DEFERRED/FAILED: stores
        previous_state so we can resume later.
        For resuming from any paused state: previous_state is cleared.
        retry_at is only valid while in DEFERRED — it is cleared on any
        transition to a non-DEFERRED state.

        Note: transitioning between two paused states (e.g. DEFERRED → FAILED)
        stores the paused state as previous_state; the original active stage is
        not preserved. Callers resuming from FAILED should not assume
        previous_state is an active stage.

        Extra kwargs are applied in the same atomic save.
        """
        current = self.state.current_state
        if new_state not in VALID_STATES:
            raise InvalidTransitionError(f"Unknown state: {new_state}")
        if new_state not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransitionError(
                f"Cannot transition from '{current}' to '{new_state}'"
            )

        updates: dict[str, Any] = {"current_state": new_state}

        paused_states = {Stage.BLOCKED, Stage.AWAITING_APPROVAL, Stage.MANUAL_CONTROL, Stage.DEFERRED, Stage.FAILED}
        if new_state in paused_states:
            updates["previous_state"] = current
            updates["human_input_pending"] = True
        elif current in paused_states:
            # Resuming from a paused state — clear pending flag
            updates["previous_state"] = None
            updates["human_input_pending"] = False

        # retry_at is meaningful only while in DEFERRED; clear on any other target
        if new_state != Stage.DEFERRED:
            updates["retry_at"] = None

        updates.update(extra)
        self.update_state(**updates)

    def increment_iteration(self, stage_id: str) -> int:
        """Increment and return iteration count for a stage."""
        state = self.state
        count = state.stage_iterations.get(stage_id, 0) + 1
        state.stage_iterations[stage_id] = count
        self.save_state()
        return count


@dataclass
class AdminWorkspaceState:
    """State for admin (non-ticket) workspaces."""
    operation: str  # "add", "list", "remove"
    workspace_root: str
    status: str = "pending"  # "pending", "in_progress", "completed", "failed"
    started_at: str = ""
    last_updated_at: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        now = _now_iso()
        if not self.started_at:
            self.started_at = now
        if not self.last_updated_at:
            self.last_updated_at = now


class AdminWorkspace:
    """Lightweight workspace for admin operations (no ticket context, no source dir)."""

    def __init__(self, root: str, state: AdminWorkspaceState | None = None) -> None:
        self._root = Path(root)
        self._state = state

    @classmethod
    def create(cls, root: str, operation: str) -> AdminWorkspace:
        """Create a new admin workspace with directory structure."""
        root_path = Path(root)
        (root_path / "meta").mkdir(parents=True, exist_ok=True)
        (root_path / "reports").mkdir(parents=True, exist_ok=True)
        (root_path / "logs").mkdir(parents=True, exist_ok=True)

        state = AdminWorkspaceState(operation=operation, workspace_root=root)
        ws = cls(root, state)
        ws.save_state()
        return ws

    @property
    def root(self) -> Path:
        return self._root

    @property
    def source_dir(self) -> None:
        return None

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
    def state(self) -> AdminWorkspaceState:
        if self._state is None:
            self._state = self._load_state()
        return self._state

    def _load_state(self) -> AdminWorkspaceState:
        with open(self.state_path) as f:
            data = json.load(f)
        return AdminWorkspaceState(**data)

    def save_state(self) -> None:
        """Atomically write state.json (temp file + rename)."""
        data = asdict(self._state)
        self._state.last_updated_at = _now_iso()
        data["last_updated_at"] = self._state.last_updated_at

        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._root), suffix=".tmp", prefix="state_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(self.state_path))
        except Exception:
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

"""Mode handler — manages auto/manual pipeline mode with persistence."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_MODES = {"auto", "manual"}


class ModeHandler:
    """Manages the pipeline mode (auto/manual) with file persistence."""

    def __init__(self, state_file_path: str, default_mode: str = "auto") -> None:
        self._state_path = Path(state_file_path)
        self._mode = default_mode
        self._state: dict = {}
        self._load()

    def _load(self) -> None:
        """Load mode from daemon state file."""
        if self._state_path.exists():
            try:
                self._state = json.loads(self._state_path.read_text())
                self._mode = self._state.get("mode", self._mode)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load daemon state: %s", e)

    def get_mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Switch mode and persist to disk."""
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}. Must be one of {VALID_MODES}")

        self._mode = mode
        self._state["mode"] = mode
        self._state["mode_changed_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
        logger.info("Pipeline mode set to: %s", mode)

    def _save(self) -> None:
        """Persist state to disk."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state, indent=2))

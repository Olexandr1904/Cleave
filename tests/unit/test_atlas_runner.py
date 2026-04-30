"""Smoke tests for dashboard.atlas_runner.run_supervised.

The supervisor wraps a user-supplied `atlas_fn` and is the single place that
decides what state to write on success vs failure, when to invoke rollback,
and that on_complete always fires. These tests pin those contracts so a
future refactor can't silently lose them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dashboard.atlas_runner import run_supervised
from dashboard.setup_workspace import SetupWorkspace, create_setup_workspace


def _make_setup_workspace(tmp_path: Path) -> SetupWorkspace:
    return create_setup_workspace(
        base_dir=tmp_path,
        project_id="acme",
        repo_id="acme-app",
        redacted_input_md="# input\n",
    )


def _read_state(ws: SetupWorkspace) -> dict:
    return json.loads((ws.setup_dir / "state.json").read_text())


@pytest.mark.asyncio
async def test_happy_path_transitions_to_setup_done(tmp_path):
    ws = _make_setup_workspace(tmp_path)
    on_failure = MagicMock()
    on_complete = MagicMock()

    async def atlas_ok(workspace, config_dir):
        # Atlas would normally write configs / call APIs here.
        return None

    await run_supervised(ws, tmp_path / "config", atlas_ok, on_failure, on_complete)

    state = _read_state(ws)
    assert state["current_state"] == "SETUP_DONE"
    on_failure.assert_not_called()
    on_complete.assert_called_once()


@pytest.mark.asyncio
async def test_failure_writes_setup_failed_and_calls_rollback(tmp_path):
    ws = _make_setup_workspace(tmp_path)
    on_failure = MagicMock()
    on_complete = MagicMock()

    async def atlas_bad(workspace, config_dir):
        raise RuntimeError("jira validation rejected the token")

    await run_supervised(ws, tmp_path / "config", atlas_bad, on_failure, on_complete)

    state = _read_state(ws)
    assert state["current_state"] == "SETUP_FAILED"
    assert "jira validation rejected the token" in state.get("error", "")
    on_failure.assert_called_once()
    on_complete.assert_called_once()

    # Failure report contains the traceback for postmortem
    report = (ws.setup_dir / "reports" / "project-setup-output.md").read_text()
    assert "## Failure" in report
    assert "RuntimeError" in report


@pytest.mark.asyncio
async def test_on_complete_fires_even_when_rollback_raises(tmp_path):
    """A buggy on_failure must not prevent on_complete from running.

    Otherwise the dashboard slot stays "in progress" forever after any
    rollback bug.
    """
    ws = _make_setup_workspace(tmp_path)
    on_failure = MagicMock(side_effect=RuntimeError("rollback exploded"))
    on_complete = MagicMock()

    async def atlas_bad(workspace, config_dir):
        raise RuntimeError("primary failure")

    await run_supervised(ws, tmp_path / "config", atlas_bad, on_failure, on_complete)

    on_complete.assert_called_once()
    assert _read_state(ws)["current_state"] == "SETUP_FAILED"


@pytest.mark.asyncio
async def test_on_complete_fires_even_when_atlas_succeeds_but_complete_callback_is_called_first(
    tmp_path,
):
    """on_complete must always be invoked once, regardless of on_failure noise."""
    ws = _make_setup_workspace(tmp_path)
    on_failure = MagicMock()
    on_complete = MagicMock()

    async def atlas_ok(workspace, config_dir):
        return None

    await run_supervised(ws, tmp_path / "config", atlas_ok, on_failure, on_complete)

    on_complete.assert_called_once()
    on_failure.assert_not_called()

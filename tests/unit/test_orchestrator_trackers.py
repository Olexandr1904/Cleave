"""Tests for the Orchestrator tracker registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_orchestrator() -> Orchestrator:
    """Construct an Orchestrator via __new__ for unit-test isolation."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._trackers = {}
    orch._projects = {}
    return orch


def test_register_tracker_stores_by_project_id():
    orch = _make_orchestrator()
    tracker = MagicMock()
    orch.register_tracker("acme", tracker)
    assert orch._trackers == {"acme": tracker}


def test_register_tracker_overwrites_existing():
    orch = _make_orchestrator()
    old, new = MagicMock(), MagicMock()
    orch.register_tracker("acme", old)
    orch.register_tracker("acme", new)
    assert orch._trackers["acme"] is new


def test_get_tracker_for_project_returns_registered():
    orch = _make_orchestrator()
    tracker = MagicMock()
    orch.register_tracker("acme", tracker)
    assert orch.get_tracker_for_project("acme") is tracker


def test_get_tracker_for_project_returns_none_when_missing():
    orch = _make_orchestrator()
    assert orch.get_tracker_for_project("missing") is None


def test_get_tracker_for_workspace_uses_company_id():
    orch = _make_orchestrator()
    tracker = MagicMock()
    orch.register_tracker("acme", tracker)
    ws = MagicMock()
    ws.state.company_id = "acme"
    assert orch.get_tracker_for_workspace(ws) is tracker


def test_get_tracker_for_workspace_returns_none_when_unregistered():
    orch = _make_orchestrator()
    ws = MagicMock()
    ws.state.company_id = "other"
    assert orch.get_tracker_for_workspace(ws) is None

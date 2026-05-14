"""Integration tests for the wizard's Trello validate-step branch."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _make_client():
    """Build a Starlette app with just the validate-step route attached."""
    from dashboard.web import validate_step
    app = Starlette(routes=[
        Route("/api/projects/validate-step", validate_step, methods=["POST"]),
    ])
    return TestClient(app)


def test_validate_step_trello_returns_lists_on_success():
    client = _make_client()
    with patch(
        "integrations.config.config_tools.validate_trello",
        AsyncMock(return_value={
            "success": True,
            "lists": [
                {"id": "L1", "name": "To Do", "pos": 1},
                {"id": "L2", "name": "Doing", "pos": 2},
            ],
        }),
    ):
        r = client.post("/api/projects/validate-step", json={
            "step": "trello",
            "data": {"api_key": "kkk", "token": "ttt", "board_id": "board-x"},
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["lists"]) == 2
    assert body["lists"][0]["name"] == "To Do"


def test_validate_step_trello_returns_error_on_bad_creds():
    client = _make_client()
    with patch(
        "integrations.config.config_tools.validate_trello",
        AsyncMock(return_value={"success": False, "error": "Invalid token"}),
    ):
        r = client.post("/api/projects/validate-step", json={
            "step": "trello",
            "data": {"api_key": "kkk", "token": "bad", "board_id": "board-x"},
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["checks"][0]["reason"] == "Invalid token"
    assert body["lists"] == []

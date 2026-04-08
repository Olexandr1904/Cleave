"""Tests for integrations/telegram/handlers/mode.py."""

from __future__ import annotations

import json

import pytest

from integrations.telegram.handlers.mode import ModeHandler


class TestModeHandler:
    @pytest.fixture
    def state_file(self, tmp_path):
        path = tmp_path / "daemon_state.json"
        path.write_text(json.dumps({
            "mode": "auto",
            "started_at": "2026-04-08T00:00:00Z",
        }))
        return path

    @pytest.fixture
    def handler(self, state_file):
        return ModeHandler(state_file_path=str(state_file))

    def test_get_mode_returns_current(self, handler):
        assert handler.get_mode() == "auto"

    def test_set_mode_to_manual(self, handler):
        handler.set_mode("manual")
        assert handler.get_mode() == "manual"

    def test_set_mode_persists_to_disk(self, handler, state_file):
        handler.set_mode("manual")
        data = json.loads(state_file.read_text())
        assert data["mode"] == "manual"
        assert "mode_changed_at" in data

    def test_set_mode_back_to_auto(self, handler):
        handler.set_mode("manual")
        handler.set_mode("auto")
        assert handler.get_mode() == "auto"

    def test_set_invalid_mode_raises(self, handler):
        with pytest.raises(ValueError, match="Invalid mode"):
            handler.set_mode("turbo")

    def test_load_from_disk_on_init(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"mode": "manual", "started_at": "2026-04-08T00:00:00Z"}))
        handler = ModeHandler(state_file_path=str(path))
        assert handler.get_mode() == "manual"

    def test_fallback_to_default_when_no_file(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        handler = ModeHandler(state_file_path=str(path), default_mode="auto")
        assert handler.get_mode() == "auto"

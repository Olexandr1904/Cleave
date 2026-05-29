"""Tests for config/host_os.py and the os= field in config_loader / schemas."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config.host_os import SUPPORTED_OS, terminal_launch_command


class TestTerminalLaunchCommand:
    def test_unix_returns_gnome_terminal(self):
        cmd = terminal_launch_command("unix")
        assert cmd == "gnome-terminal -- bash -c"

    def test_mac_returns_open_terminal(self):
        cmd = terminal_launch_command("mac")
        assert cmd == "open -a Terminal"

    def test_unknown_falls_back_to_unix(self):
        # Any unrecognised value falls through to the else branch (unix).
        cmd = terminal_launch_command("windows")
        assert cmd == "gnome-terminal -- bash -c"

    def test_supported_os_tuple_contains_expected_values(self):
        assert "unix" in SUPPORTED_OS
        assert "mac" in SUPPORTED_OS


class TestGlobalConfigOsField:
    """os= parsing and validation via load_global_config."""

    @pytest.fixture()
    def _env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        monkeypatch.setenv("CLAUDE_API_KEY", "key")

    def _write_global_yaml(self, tmp_path: Path, extra: str = "") -> Path:
        base = textwrap.dedent("""\
            telegram:
              bot_token: "${TELEGRAM_BOT_TOKEN}"
              default_chat_id: "${TELEGRAM_CHAT_ID}"
            claude:
              api_key: "${CLAUDE_API_KEY}"
        """)
        p = tmp_path / "global.yaml"
        p.write_text(base + extra)
        return tmp_path

    def test_default_os_is_unix(self, tmp_path, _env):
        from config.config_loader import load_global_config
        cfg_dir = self._write_global_yaml(tmp_path)
        cfg = load_global_config(str(cfg_dir))
        assert cfg.os == "unix"

    def test_os_mac_accepted(self, tmp_path, _env):
        from config.config_loader import load_global_config
        cfg_dir = self._write_global_yaml(tmp_path, "os: mac\n")
        cfg = load_global_config(str(cfg_dir))
        assert cfg.os == "mac"

    def test_os_unix_explicit(self, tmp_path, _env):
        from config.config_loader import load_global_config
        cfg_dir = self._write_global_yaml(tmp_path, "os: unix\n")
        cfg = load_global_config(str(cfg_dir))
        assert cfg.os == "unix"

    def test_invalid_os_raises_config_error(self, tmp_path, _env):
        from config.config_loader import ConfigError, load_global_config
        cfg_dir = self._write_global_yaml(tmp_path, "os: windows\n")
        with pytest.raises(ConfigError, match="'os' must be one of"):
            load_global_config(str(cfg_dir))

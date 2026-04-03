"""Tests for main.py CLI entry point."""

import subprocess
import sys
from pathlib import Path

import pytest

from main import main, parse_args


class TestParseArgs:
    def test_config_required(self):
        """AC3: Running without --config prints error and exits non-zero."""
        with pytest.raises(SystemExit) as exc_info:
            parse_args([])
        assert exc_info.value.code != 0

    def test_config_flag(self):
        """AC1: --config flag is accepted."""
        args = parse_args(["--config", "/tmp/config"])
        assert args.config == "/tmp/config"

    def test_project_flag(self):
        """AC1: --project flag is accepted."""
        args = parse_args(["--config", "/tmp/config", "--project", "acme"])
        assert args.project == "acme"

    def test_repo_flag(self):
        """AC1: --repo flag is accepted alongside --project."""
        args = parse_args(["--config", "/tmp/config", "--project", "acme", "--repo", "android"])
        assert args.repo == "android"

    def test_repo_without_project_errors(self):
        """--repo requires --project."""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--config", "/tmp/config", "--repo", "android"])
        assert exc_info.value.code != 0

    def test_dry_run_flag(self):
        """AC1: --dry-run flag is accepted."""
        args = parse_args(["--config", "/tmp/config", "--dry-run"])
        assert args.dry_run is True

    def test_dry_run_default_false(self):
        """--dry-run defaults to False."""
        args = parse_args(["--config", "/tmp/config"])
        assert args.dry_run is False

    def test_defaults(self):
        """Optional flags default to None/False."""
        args = parse_args(["--config", "/tmp/config"])
        assert args.project is None
        assert args.repo is None
        assert args.dry_run is False


FIXTURES_DIR = str(Path(__file__).parent.parent / "fixtures" / "config")


class TestMain:
    @staticmethod
    def _set_all_env(monkeypatch):
        for var in [
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CLAUDE_API_KEY",
            "TEST_JIRA_TOKEN", "TEST_JIRA_EMAIL", "TEST_TELEGRAM_CHAT_ID",
            "TEST_GITHUB_TOKEN",
        ]:
            monkeypatch.setenv(var, "test-val")

    def test_main_returns_zero(self, monkeypatch):
        """main() returns 0 on success (no matching projects = no daemon start)."""
        self._set_all_env(monkeypatch)
        result = main(["--config", FIXTURES_DIR, "--project", "nonexistent"])
        assert result == 0

    def test_main_with_all_flags(self, monkeypatch):
        """main() accepts all flag combinations."""
        self._set_all_env(monkeypatch)
        result = main(["--config", FIXTURES_DIR, "--project", "p", "--repo", "r", "--dry-run"])
        assert result == 0


class TestHelpOutput:
    def test_help_flag(self):
        """AC2: python main.py --help shows usage information."""
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            capture_output=True,
            text=True,
            cwd="/Users/Oleksandr_Brazhenko/Documents/sickle",
        )
        assert result.returncode == 0
        assert "Sickle" in result.stdout
        assert "--config" in result.stdout
        assert "--project" in result.stdout
        assert "--repo" in result.stdout
        assert "--dry-run" in result.stdout


class TestNoConfigError:
    def test_no_config_exits_nonzero(self):
        """AC3: Running without --config exits with non-zero code."""
        result = subprocess.run(
            [sys.executable, "main.py"],
            capture_output=True,
            text=True,
            cwd="/Users/Oleksandr_Brazhenko/Documents/sickle",
        )
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "required" in result.stderr.lower()

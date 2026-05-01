"""Tests for config/config_loader.py — Global config loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.config_loader import ConfigError, load_global_config, resolve_env_vars

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _set_env_vars(monkeypatch):
    """Set required environment variables for tests."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("CLAUDE_API_KEY", "test-api-key")


class TestResolveEnvVars:
    def test_resolves_single_var(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        assert resolve_env_vars("${MY_VAR}") == "hello"

    def test_resolves_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "foo")
        monkeypatch.setenv("B", "bar")
        assert resolve_env_vars("${A}-${B}") == "foo-bar"

    def test_no_vars_passthrough(self):
        assert resolve_env_vars("plain string") == "plain string"

    def test_missing_var_raises(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        with pytest.raises(ConfigError, match="NONEXISTENT_VAR"):
            resolve_env_vars("${NONEXISTENT_VAR}")


class TestLoadGlobalConfig:
    def test_valid_config(self):
        """AC1+AC2+AC3+AC4: Full valid global.yaml is parsed correctly."""
        config = load_global_config(str(FIXTURES_DIR))

        # AC2: env vars resolved
        assert config.telegram.bot_token == "test-bot-token"
        assert config.telegram.default_chat_id == "12345"
        assert config.claude.api_key == "test-api-key"

        # AC4: all fields parsed
        assert not hasattr(config.claude, "model")
        assert config.workspaces.base_dir == "/data"
        assert config.workspaces.max_age_days == 7
        assert config.defaults.poll_interval_seconds == 300
        assert config.defaults.max_iterations.scope_guard == 3
        assert config.defaults.max_iterations.fix == 3
        assert config.defaults.max_iterations.qa == 2
        assert config.defaults.max_iterations.dev == 2
        assert config.defaults.max_parallel_tickets == 5
        assert config.defaults.pr_comment_fetch_delay_minutes == 30
        assert config.logging.level == "INFO"
        assert config.logging.dir == "/var/log/cleave"
        assert config.heartbeat.enabled is True
        assert config.heartbeat.interval_hours == 24

        # AC3: operator profile
        assert config.operator.role == "Tech Lead"
        assert config.operator.stack == ["Python", "Kotlin"]
        assert config.operator.preferences == {"code_style": "clean"}
        assert config.operator.rules == ["No unused imports"]

    def test_missing_env_var(self, monkeypatch):
        """AC2: Missing env var raises clear error."""
        monkeypatch.delenv("CLAUDE_API_KEY")
        with pytest.raises(ConfigError, match="CLAUDE_API_KEY"):
            load_global_config(str(FIXTURES_DIR))

    def test_missing_config_file(self, tmp_path):
        """AC5: Missing file produces clear error with file path."""
        with pytest.raises(ConfigError, match="not found"):
            load_global_config(str(tmp_path))

    def test_empty_config_file(self, tmp_path):
        """AC6: Empty file produces clear error."""
        (tmp_path / "global.yaml").write_text("")
        with pytest.raises(ConfigError, match="empty"):
            load_global_config(str(tmp_path))

    def test_invalid_field(self, tmp_path):
        """AC5: Invalid fields produce clear validation errors."""
        (tmp_path / "global.yaml").write_text(
            "telegram:\n  bot_token: 'tok'\n  bad_field: 'oops'\n"
        )
        with pytest.raises(ConfigError, match="Invalid fields.*telegram"):
            load_global_config(str(tmp_path))

    def test_missing_optional_sections_use_defaults(self, tmp_path):
        """Sections not present in yaml get default values."""
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        config = load_global_config(str(tmp_path))
        assert config.logging.level == "DEBUG"
        # Other sections get defaults
        assert config.defaults.poll_interval_seconds == 60
        assert config.telegram.bot_token == ""
        assert config.operator.role == ""

    def test_partial_section(self, tmp_path):
        """A section with some fields uses defaults for the rest."""
        (tmp_path / "global.yaml").write_text(
            "workspaces:\n  base_dir: /custom\n"
        )
        config = load_global_config(str(tmp_path))
        assert config.workspaces.base_dir == "/custom"
        assert config.workspaces.max_age_days == 7  # default

    def test_non_mapping_raises(self, tmp_path):
        """Config file that isn't a mapping raises error."""
        (tmp_path / "global.yaml").write_text("- item1\n- item2\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_global_config(str(tmp_path))


class TestNewConfigSchemas:
    def test_pipeline_config_defaults(self):
        from config.schemas import PipelineConfig
        cfg = PipelineConfig()
        assert cfg.mode == "manual"

    def test_intent_parser_config_defaults(self):
        from config.schemas import IntentParserConfig
        cfg = IntentParserConfig()
        assert cfg.timeout_seconds == 30

    def test_global_config_has_pipeline(self):
        from config.schemas import PipelineConfig, GlobalConfig
        cfg = GlobalConfig()
        assert cfg.pipeline.mode == "manual"

    def test_global_config_has_intent_parser(self):
        from config.schemas import IntentParserConfig, GlobalConfig
        cfg = GlobalConfig()
        assert cfg.intent_parser.timeout_seconds == 30

    def test_project_config_has_pipeline(self):
        from config.schemas import ProjectConfig
        cfg = ProjectConfig()
        assert cfg.pipeline.mode == "manual"


class TestParseVcsSection:
    """The VCSConfig dataclass has top-level fields (skip_pre_push_hook, etc.)
    beyond the three nested sub-configs (provider, github, gitlab). Earlier
    the loader only handled the three nested ones and silently dropped any
    other top-level field — meaning operators could set
    `vcs.skip_pre_push_hook: true` in yaml and the value would never reach
    the runtime config object.
    """

    def test_parses_skip_pre_push_hook_true(self):
        from config.config_loader import _parse_vcs_section
        data = {
            "vcs": {
                "provider": "github",
                "github": {"token": "x", "owner": "o", "repo": "r"},
                "skip_pre_push_hook": True,
            },
        }
        cfg = _parse_vcs_section(data, "test.yaml")
        assert cfg.skip_pre_push_hook is True
        assert cfg.provider == "github"
        assert cfg.github.token == "x"

    def test_skip_pre_push_hook_default_false(self):
        from config.config_loader import _parse_vcs_section
        data = {"vcs": {"provider": "github", "github": {"token": "x", "owner": "o", "repo": "r"}}}
        cfg = _parse_vcs_section(data, "test.yaml")
        assert cfg.skip_pre_push_hook is False

    def test_unknown_top_level_vcs_field_raises(self):
        """Stay strict on typos — pass-through must validate against
        VCSConfig fields, not blindly accept anything."""
        from config.config_loader import ConfigError, _parse_vcs_section
        data = {"vcs": {"provider": "github", "skip_pre_pus_hook": True}}  # typo
        with pytest.raises(ConfigError):
            _parse_vcs_section(data, "test.yaml")

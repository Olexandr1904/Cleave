"""Tests for config cascade: project & repo config loading with overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.config_loader import ConfigError, load_config, merge_dicts

FIXTURES_DIR = str(Path(__file__).parent.parent / "fixtures" / "config")


@pytest.fixture(autouse=True)
def _set_env_vars(monkeypatch):
    """Set all required env vars for test fixtures."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("CLAUDE_API_KEY", "test-api-key")
    monkeypatch.setenv("TEST_JIRA_TOKEN", "jira-tok")
    monkeypatch.setenv("TEST_JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("TEST_TELEGRAM_CHAT_ID", "67890")
    monkeypatch.setenv("TEST_GITHUB_TOKEN", "gh-tok")


class TestMergeDicts:
    def test_simple_override(self):
        assert merge_dicts({"a": 1}, {"a": 2}) == {"a": 2}

    def test_adds_new_keys(self):
        assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_deep_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3, "c": 4}}
        assert merge_dicts(base, override) == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_override_replaces_non_dict_with_non_dict(self):
        assert merge_dicts({"a": [1]}, {"a": [2, 3]}) == {"a": [2, 3]}


class TestLoadConfig:
    def test_discovers_projects(self):
        """AC1: Config loader scans projects/ subdirectories."""
        _, projects = load_config(FIXTURES_DIR)
        assert "test-project" in projects

    def test_project_config_loaded(self):
        """AC2: project.yaml is loaded and merged."""
        _, projects = load_config(FIXTURES_DIR)
        proj = projects["test-project"]
        assert proj.config.project.id == "test-project"
        assert proj.config.project.name == "Test Project"
        assert proj.config.jira.url == "https://test.atlassian.net"
        assert proj.config.jira.project_key == "TEST"
        assert proj.config.jira.trigger_label == "ai-ready"

    def test_repo_config_loaded(self):
        """AC3: repo yaml is loaded and merged on top of project config."""
        _, projects = load_config(FIXTURES_DIR)
        repo = projects["test-project"].repos["test-repo"]
        assert repo.repo.id == "test-repo"
        assert repo.github.owner == "test-org"
        assert repo.github.default_branch == "develop"
        assert repo.git.clone_url == "git@github.com:test-org/test-repo.git"
        assert repo.git.depth == 1
        assert repo.jira_repo_label == "repo:test-repo"

    def test_cascade_override(self):
        """AC4: Lower-level values override; unset fields inherit."""
        _, projects = load_config(FIXTURES_DIR)
        repo = projects["test-project"].repos["test-repo"]

        # Repo overrides project defaults
        assert repo.defaults.max_fix_iterations == 5  # repo override

        # Project overrides global defaults
        assert repo.defaults.poll_interval_seconds == 600  # project override

        # Unset fields inherit from global
        assert repo.defaults.max_scope_iterations == 3  # global default
        assert repo.defaults.max_qa_iterations == 2  # global default

    def test_jira_inherited_by_repo(self):
        """Repo inherits jira config from project."""
        _, projects = load_config(FIXTURES_DIR)
        repo = projects["test-project"].repos["test-repo"]
        assert repo.jira.url == "https://test.atlassian.net"
        assert repo.jira.project_key == "TEST"

    def test_disabled_project(self, tmp_path):
        """AC5: enabled: false excludes project from discovery."""
        # Set up a minimal config dir
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        proj_dir = tmp_path / "projects" / "disabled-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text(
            "project:\n  id: disabled-proj\n  enabled: false\n"
        )

        _, projects = load_config(str(tmp_path))
        assert "disabled-proj" not in projects

    def test_disabled_repo(self, tmp_path):
        """AC5: enabled: false on repo excludes it."""
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        proj_dir = tmp_path / "projects" / "proj"
        repos_dir = proj_dir / "repos"
        repos_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text(
            "project:\n  id: proj\n  enabled: true\n"
        )
        (repos_dir / "disabled-repo.yaml").write_text(
            "repo:\n  id: disabled-repo\n  enabled: false\n"
        )

        _, projects = load_config(str(tmp_path))
        assert "proj" in projects
        assert "disabled-repo" not in projects["proj"].repos

    def test_project_filter(self):
        """AC6: --project filter limits to specified project."""
        _, projects = load_config(FIXTURES_DIR, project_filter="test-project")
        assert "test-project" in projects

    def test_project_filter_excludes_others(self, tmp_path):
        """AC6: --project filter excludes non-matching projects."""
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        for name in ["alpha", "beta"]:
            d = tmp_path / "projects" / name
            d.mkdir(parents=True)
            (d / "project.yaml").write_text(f"project:\n  id: {name}\n  enabled: true\n")

        _, projects = load_config(str(tmp_path), project_filter="alpha")
        assert "alpha" in projects
        assert "beta" not in projects

    def test_repo_filter(self, tmp_path):
        """AC6: --repo filter limits to specified repo."""
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        proj_dir = tmp_path / "projects" / "proj"
        repos_dir = proj_dir / "repos"
        repos_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text("project:\n  id: proj\n  enabled: true\n")
        (repos_dir / "repo-a.yaml").write_text("repo:\n  id: repo-a\n  enabled: true\n")
        (repos_dir / "repo-b.yaml").write_text("repo:\n  id: repo-b\n  enabled: true\n")

        _, projects = load_config(str(tmp_path), project_filter="proj", repo_filter="repo-a")
        assert "repo-a" in projects["proj"].repos
        assert "repo-b" not in projects["proj"].repos

    def test_no_projects_dir(self, tmp_path):
        """No projects/ directory returns empty projects dict."""
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        _, projects = load_config(str(tmp_path))
        assert projects == {}

    def test_project_id_from_dir_name(self, tmp_path):
        """Project id defaults to directory name if not in yaml."""
        (tmp_path / "global.yaml").write_text("logging:\n  level: DEBUG\n")
        proj_dir = tmp_path / "projects" / "my-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text("project:\n  enabled: true\n")

        _, projects = load_config(str(tmp_path))
        assert projects["my-proj"].config.project.id == "my-proj"

"""Tests for integrations/config/config_tools.py."""

from __future__ import annotations

import os

import pytest
import yaml

from integrations.config.config_tools import (
    resolve_env_var,
    list_projects,
    read_project_config,
)


class TestResolveEnvVar:
    def test_resolves_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert resolve_env_var("${MY_TOKEN}") == "secret123"

    def test_missing_env_var_raises(self):
        with pytest.raises(ValueError, match="not set"):
            resolve_env_var("${NONEXISTENT_VAR_XYZ}")

    def test_plain_string_returned_as_is(self):
        assert resolve_env_var("plain-value") == "plain-value"

    def test_empty_string(self):
        assert resolve_env_var("") == ""

    def test_partial_env_var_not_resolved(self):
        assert resolve_env_var("prefix-${") == "prefix-${"


class TestListProjects:
    def test_lists_projects(self, tmp_path):
        # Create project structure
        proj_dir = tmp_path / "projects" / "acme"
        proj_dir.mkdir(parents=True)
        repos_dir = proj_dir / "repos"
        repos_dir.mkdir()

        (proj_dir / "project.yaml").write_text(yaml.dump({
            "project": {"id": "acme", "name": "Acme Corp", "enabled": True},
        }))
        (repos_dir / "api.yaml").write_text("repo:\n  id: api\n")
        (repos_dir / "web.yaml").write_text("repo:\n  id: web\n")

        result = list_projects(str(tmp_path))
        assert len(result) == 1
        assert result[0]["id"] == "acme"
        assert result[0]["name"] == "Acme Corp"
        assert result[0]["repo_count"] == 2
        assert result[0]["enabled"] is True

    def test_empty_config_dir(self, tmp_path):
        (tmp_path / "projects").mkdir()
        result = list_projects(str(tmp_path))
        assert result == []

    def test_missing_projects_dir(self, tmp_path):
        result = list_projects(str(tmp_path))
        assert result == []

    def test_multiple_projects(self, tmp_path):
        for pid in ["alpha", "beta"]:
            proj_dir = tmp_path / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "repos").mkdir()
            (proj_dir / "project.yaml").write_text(yaml.dump({
                "project": {"id": pid, "name": pid.title(), "enabled": True},
            }))

        result = list_projects(str(tmp_path))
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"alpha", "beta"}


class TestReadProjectConfig:
    def test_reads_project_and_repos(self, tmp_path):
        proj_dir = tmp_path / "projects" / "acme"
        proj_dir.mkdir(parents=True)
        repos_dir = proj_dir / "repos"
        repos_dir.mkdir()

        project_yaml = {"project": {"id": "acme", "name": "Acme Corp"}, "jira": {"project_key": "ACM"}}
        (proj_dir / "project.yaml").write_text(yaml.dump(project_yaml))

        repo_yaml = {"repo": {"id": "api", "name": "API Service"}, "vcs": {"provider": "github"}}
        (repos_dir / "api.yaml").write_text(yaml.dump(repo_yaml))

        result = read_project_config(str(tmp_path), "acme")
        assert result["project"]["project"]["id"] == "acme"
        assert len(result["repos"]) == 1
        assert result["repos"]["api"]["repo"]["id"] == "api"

    def test_project_not_found(self, tmp_path):
        (tmp_path / "projects").mkdir()
        with pytest.raises(FileNotFoundError, match="not found"):
            read_project_config(str(tmp_path), "nonexistent")

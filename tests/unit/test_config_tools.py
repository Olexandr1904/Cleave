"""Tests for integrations/config/config_tools.py."""

from __future__ import annotations

import pytest
import yaml

from integrations.config.config_tools import (
    resolve_env_var,
    list_projects,
    read_project_config,
    write_project_config,
    write_repo_config,
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

    def test_embedded_env_var_resolved(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert resolve_env_var("Bearer ${MY_TOKEN}") == "Bearer secret123"


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

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../etc",
            "../etc",
            "foo/bar",
            "foo bar",
            "foo.bar",
            "",
            ".",
            "..",
            "foo\\bar",
        ],
    )
    def test_invalid_project_id_rejected(self, tmp_path, bad_id):
        (tmp_path / "projects").mkdir()
        with pytest.raises(ValueError, match="Invalid project_id"):
            read_project_config(str(tmp_path), bad_id)


class TestWriteProjectConfig:
    def test_writes_project_yaml(self, tmp_path):
        yaml_content = yaml.dump({
            "project": {"id": "acme", "name": "Acme Corp", "enabled": True},
            "jira": {"url": "https://acme.atlassian.net", "project_key": "ACM"},
        })
        result = write_project_config(str(tmp_path), "acme", yaml_content)
        assert result["success"] is True

        written = yaml.safe_load(
            (tmp_path / "projects" / "acme" / "project.yaml").read_text()
        )
        assert written["project"]["id"] == "acme"

    def test_creates_directories(self, tmp_path):
        yaml_content = yaml.dump({"project": {"id": "new"}})
        write_project_config(str(tmp_path), "new", yaml_content)
        assert (tmp_path / "projects" / "new" / "project.yaml").exists()

    def test_invalid_yaml_returns_error(self, tmp_path):
        result = write_project_config(str(tmp_path), "bad", "{{invalid yaml: [")
        assert result["success"] is False
        assert "error" in result
        assert not (tmp_path / "projects" / "bad" / "project.yaml").exists()
        assert not (tmp_path / "projects" / "bad").exists()

    @pytest.mark.parametrize("bad_id", ["../etc", "foo/bar", "with space", "", "..", ".hidden"])
    def test_invalid_project_id_rejected(self, tmp_path, bad_id):
        with pytest.raises(ValueError, match="Invalid project_id"):
            write_project_config(str(tmp_path), bad_id, "project: {}\n")


class TestWriteRepoConfig:
    def test_writes_repo_yaml(self, tmp_path):
        yaml_content = yaml.dump({
            "repo": {"id": "api", "name": "API Service"},
            "vcs": {"provider": "github"},
        })
        result = write_repo_config(str(tmp_path), "acme", "api", yaml_content)
        assert result["success"] is True

        written = yaml.safe_load(
            (tmp_path / "projects" / "acme" / "repos" / "api.yaml").read_text()
        )
        assert written["repo"]["id"] == "api"

    def test_creates_directories(self, tmp_path):
        yaml_content = yaml.dump({"repo": {"id": "web"}})
        write_repo_config(str(tmp_path), "acme", "web", yaml_content)
        assert (tmp_path / "projects" / "acme" / "repos" / "web.yaml").exists()

    def test_invalid_yaml_returns_error(self, tmp_path):
        result = write_repo_config(str(tmp_path), "acme", "bad", "{{not yaml")
        assert result["success"] is False
        assert not (tmp_path / "projects" / "acme" / "repos" / "bad.yaml").exists()
        assert not (tmp_path / "projects" / "acme").exists()

    @pytest.mark.parametrize("bad_id", ["../etc", "foo/bar", "with space", "", ".."])
    def test_invalid_project_id_rejected(self, tmp_path, bad_id):
        with pytest.raises(ValueError, match="Invalid project_id"):
            write_repo_config(str(tmp_path), bad_id, "api", "repo: {}\n")

    @pytest.mark.parametrize("bad_id", ["../etc", "foo/bar", "with space", "", ".."])
    def test_invalid_repo_id_rejected(self, tmp_path, bad_id):
        with pytest.raises(ValueError, match="Invalid repo_id"):
            write_repo_config(str(tmp_path), "acme", bad_id, "repo: {}\n")

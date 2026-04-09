"""Tests for integrations/config/config_tools.py."""

from __future__ import annotations

import pytest
import yaml

from pathlib import Path

import httpx
import respx

from integrations.config.config_tools import (
    resolve_env_var,
    list_projects,
    read_project_config,
    write_project_config,
    write_repo_config,
    remove_project,
    validate_jira,
    validate_github,
    validate_gitlab,
    validate_jenkins,
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


class TestRemoveProject:
    def test_removes_project_with_backup(self, tmp_path):
        proj_dir = tmp_path / "projects" / "old"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text("project:\n  id: old\n")
        repos_dir = proj_dir / "repos"
        repos_dir.mkdir()
        (repos_dir / "api.yaml").write_text("repo:\n  id: api\n")

        result = remove_project(str(tmp_path), "old")
        assert result["success"] is True
        assert "backup_path" in result
        assert not proj_dir.exists()

        backup_path = Path(result["backup_path"])
        assert backup_path.exists()
        assert (backup_path / "project.yaml").exists()
        assert (backup_path / "repos" / "api.yaml").exists()

    def test_project_not_found(self, tmp_path):
        (tmp_path / "projects").mkdir()
        result = remove_project(str(tmp_path), "nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_backup_directory_created(self, tmp_path):
        proj_dir = tmp_path / "projects" / "test"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text("project:\n  id: test\n")

        remove_project(str(tmp_path), "test")
        backups = tmp_path / ".backups"
        assert backups.exists()
        backup_dirs = list(backups.iterdir())
        assert len(backup_dirs) == 1
        assert backup_dirs[0].name.startswith("test-")

    @pytest.mark.parametrize("bad_id", ["../etc", "foo/bar", "with space", "", "..", ".hidden"])
    def test_invalid_project_id_rejected(self, tmp_path, bad_id):
        with pytest.raises(ValueError, match="Invalid project_id"):
            remove_project(str(tmp_path), bad_id)

    def test_backup_failure_leaves_project_intact(self, tmp_path, monkeypatch):
        import shutil as _shutil
        proj_dir = tmp_path / "projects" / "alpha"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text("project:\n  id: alpha\n")

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(_shutil, "copytree", boom)
        result = remove_project(str(tmp_path), "alpha")
        assert result["success"] is False
        assert "Backup failed" in result["error"]
        assert proj_dir.exists()
        assert (proj_dir / "project.yaml").exists()

    def test_rmtree_failure_returns_error_dict(self, tmp_path, monkeypatch):
        import shutil as _shutil
        proj_dir = tmp_path / "projects" / "beta"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.yaml").write_text("project:\n  id: beta\n")

        def boom(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(_shutil, "rmtree", boom)
        result = remove_project(str(tmp_path), "beta")
        assert result["success"] is False
        assert "Removal failed" in result["error"]
        assert "backup preserved" in result["error"]
        assert proj_dir.exists()


class TestValidateJira:
    @respx.mock
    async def test_success(self):
        respx.get("https://acme.atlassian.net/rest/api/3/project/ACM").mock(
            return_value=httpx.Response(200, json={"key": "ACM", "name": "Acme Project"})
        )
        result = await validate_jira("https://acme.atlassian.net", "token123", "bot@acme.com", "ACM")
        assert result["success"] is True
        assert result["project_name"] == "Acme Project"

    @respx.mock
    async def test_auth_failure(self):
        respx.get("https://acme.atlassian.net/rest/api/3/project/ACM").mock(
            return_value=httpx.Response(401)
        )
        result = await validate_jira("https://acme.atlassian.net", "bad-token", "bot@acme.com", "ACM")
        assert result["success"] is False
        assert "401" in result["error"] or "auth" in result["error"].lower()

    @respx.mock
    async def test_project_not_found(self):
        respx.get("https://acme.atlassian.net/rest/api/3/project/BAD").mock(
            return_value=httpx.Response(404)
        )
        result = await validate_jira("https://acme.atlassian.net", "token123", "bot@acme.com", "BAD")
        assert result["success"] is False
        assert "404" in result["error"] or "not found" in result["error"].lower()


class TestValidateGitHub:
    @respx.mock
    async def test_success(self):
        respx.get("https://api.github.com/repos/acme/api").mock(
            return_value=httpx.Response(200, json={
                "full_name": "acme/api",
                "default_branch": "main",
            })
        )
        result = await validate_github("token123", "acme", "api")
        assert result["success"] is True
        assert result["full_name"] == "acme/api"
        assert result["default_branch"] == "main"

    @respx.mock
    async def test_auth_failure(self):
        respx.get("https://api.github.com/repos/acme/api").mock(
            return_value=httpx.Response(401)
        )
        result = await validate_github("bad-token", "acme", "api")
        assert result["success"] is False

    @respx.mock
    async def test_repo_not_found(self):
        respx.get("https://api.github.com/repos/acme/nonexistent").mock(
            return_value=httpx.Response(404)
        )
        result = await validate_github("token123", "acme", "nonexistent")
        assert result["success"] is False


class TestValidateGitLab:
    @respx.mock
    async def test_success(self):
        respx.get("https://gitlab.com/api/v4/projects/12345").mock(
            return_value=httpx.Response(200, json={"name": "My Project", "id": 12345})
        )
        result = await validate_gitlab("token123", "12345", "https://gitlab.com")
        assert result["success"] is True
        assert result["project_name"] == "My Project"

    @respx.mock
    async def test_auth_failure(self):
        respx.get("https://gitlab.com/api/v4/projects/12345").mock(
            return_value=httpx.Response(401)
        )
        result = await validate_gitlab("bad-token", "12345", "https://gitlab.com")
        assert result["success"] is False


class TestValidateJenkins:
    @respx.mock
    async def test_success(self):
        respx.get("https://jenkins.acme.com/job/my-project/api/json").mock(
            return_value=httpx.Response(200, json={"displayName": "My Project Build"})
        )
        result = await validate_jenkins(
            "https://jenkins.acme.com", "admin", "token123", "my-project"
        )
        assert result["success"] is True
        assert result["job_name"] == "My Project Build"

    @respx.mock
    async def test_auth_failure(self):
        respx.get("https://jenkins.acme.com/job/my-project/api/json").mock(
            return_value=httpx.Response(401)
        )
        result = await validate_jenkins(
            "https://jenkins.acme.com", "admin", "bad-token", "my-project"
        )
        assert result["success"] is False

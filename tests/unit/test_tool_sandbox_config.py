"""Tests for config tool registration in tool sandbox."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
import yaml

from orchestrator.tool_sandbox import ALL_TOOLS, ToolError, ToolSandbox, get_tool_definitions


@pytest.fixture
def config_dir(tmp_path):
    """Create a config-live-like directory with a sample project."""
    projects_dir = tmp_path / "config-live" / "projects" / "acme"
    projects_dir.mkdir(parents=True)
    repos_dir = projects_dir / "repos"
    repos_dir.mkdir()

    (projects_dir / "project.yaml").write_text(yaml.dump({
        "project": {"id": "acme", "name": "Acme Corp", "enabled": True},
    }))
    (repos_dir / "api.yaml").write_text(yaml.dump({
        "repo": {"id": "api", "name": "API"},
    }))
    return tmp_path / "config-live"


@pytest.fixture
def workspace(tmp_path):
    """Create a minimal workspace."""
    ws = tmp_path / "ws"
    (ws / "source").mkdir(parents=True)
    (ws / "reports").mkdir()
    return ws


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


CONFIG_TOOL_NAMES = {
    "validate_jira", "validate_github", "validate_gitlab", "validate_jenkins",
    "list_projects", "read_project_config",
    "write_project_config", "write_repo_config", "remove_project",
}


class TestConfigToolsRegistered:
    def test_config_tools_in_all_tools(self):
        assert CONFIG_TOOL_NAMES.issubset(ALL_TOOLS)

    def test_sandbox_accepts_config_tools(self, workspace):
        sandbox = ToolSandbox(str(workspace), list(CONFIG_TOOL_NAMES))
        assert sandbox is not None

    def test_get_tool_definitions_includes_all_config_tools(self):
        defs = get_tool_definitions(sorted(CONFIG_TOOL_NAMES))
        names = {d["name"] for d in defs}
        assert names == CONFIG_TOOL_NAMES

    def test_each_config_tool_has_required_params(self):
        defs = {d["name"]: d for d in get_tool_definitions(sorted(CONFIG_TOOL_NAMES))}
        assert set(defs["validate_jira"]["input_schema"]["required"]) == {"url", "token", "email", "project_key"}
        assert set(defs["validate_github"]["input_schema"]["required"]) == {"token", "owner", "repo"}
        assert set(defs["validate_gitlab"]["input_schema"]["required"]) == {"token", "project_id"}
        assert set(defs["validate_jenkins"]["input_schema"]["required"]) == {"url", "username", "token", "job_key"}
        assert set(defs["list_projects"]["input_schema"]["required"]) == {"config_dir"}
        assert set(defs["read_project_config"]["input_schema"]["required"]) == {"config_dir", "project_id"}
        assert set(defs["write_project_config"]["input_schema"]["required"]) == {"config_dir", "project_id", "yaml_content"}
        assert set(defs["write_repo_config"]["input_schema"]["required"]) == {"config_dir", "project_id", "repo_id", "yaml_content"}
        assert set(defs["remove_project"]["input_schema"]["required"]) == {"config_dir", "project_id"}


class TestListProjectsTool:
    def test_list_projects_via_sandbox(self, workspace, config_dir):
        sandbox = ToolSandbox(str(workspace), ["list_projects"])
        result = run(sandbox.execute_tool("list_projects", {"config_dir": str(config_dir)}))
        assert "acme" in result
        assert "Acme Corp" in result

    def test_list_projects_empty(self, workspace, tmp_path):
        empty_config = tmp_path / "empty-config"
        empty_config.mkdir()
        sandbox = ToolSandbox(str(workspace), ["list_projects"])
        result = run(sandbox.execute_tool("list_projects", {"config_dir": str(empty_config)}))
        assert "No projects" in result


class TestReadProjectConfigTool:
    def test_read_project_via_sandbox(self, workspace, config_dir):
        sandbox = ToolSandbox(str(workspace), ["read_project_config"])
        result = run(sandbox.execute_tool(
            "read_project_config",
            {"config_dir": str(config_dir), "project_id": "acme"},
        ))
        assert "acme" in result
        assert "Acme Corp" in result

    def test_read_missing_project_raises_tool_error(self, workspace, config_dir):
        sandbox = ToolSandbox(str(workspace), ["read_project_config"])
        with pytest.raises(ToolError):
            run(sandbox.execute_tool(
                "read_project_config",
                {"config_dir": str(config_dir), "project_id": "nonexistent"},
            ))


class TestWriteProjectConfigTool:
    def test_write_project_via_sandbox(self, workspace, tmp_path):
        cfg = tmp_path / "new-config"
        cfg.mkdir()
        sandbox = ToolSandbox(str(workspace), ["write_project_config"])
        yaml_content = yaml.dump({"project": {"id": "test", "name": "Test"}})
        result = run(sandbox.execute_tool("write_project_config", {
            "config_dir": str(cfg),
            "project_id": "test",
            "yaml_content": yaml_content,
        }))
        assert "Successfully written to" in result
        assert (cfg / "projects" / "test" / "project.yaml").exists()


class TestWriteRepoConfigTool:
    def test_write_repo_via_sandbox(self, workspace, tmp_path):
        cfg = tmp_path / "new-config"
        cfg.mkdir()
        sandbox = ToolSandbox(str(workspace), ["write_repo_config"])
        yaml_content = yaml.dump({"repo": {"id": "api"}})
        result = run(sandbox.execute_tool("write_repo_config", {
            "config_dir": str(cfg),
            "project_id": "acme",
            "repo_id": "api",
            "yaml_content": yaml_content,
        }))
        assert "Successfully written to" in result
        assert (cfg / "projects" / "acme" / "repos" / "api.yaml").exists()


class TestRemoveProjectTool:
    def test_remove_project_via_sandbox(self, workspace, config_dir):
        sandbox = ToolSandbox(str(workspace), ["remove_project"])
        result = run(sandbox.execute_tool("remove_project", {
            "config_dir": str(config_dir),
            "project_id": "acme",
        }))
        assert "Removed project 'acme'" in result
        assert "Backup at:" in result
        assert not (config_dir / "projects" / "acme").exists()


class TestValidateJiraTool:
    @respx.mock
    def test_validate_jira_success_via_sandbox(self, workspace):
        respx.route(host="acme.atlassian.net").mock(
            return_value=httpx.Response(200, json={"name": "Acme"})
        )
        sandbox = ToolSandbox(str(workspace), ["validate_jira"])
        result = run(sandbox.execute_tool("validate_jira", {
            "url": "https://acme.atlassian.net",
            "token": "tok",
            "email": "bot@acme.com",
            "project_key": "ACM",
        }))
        assert result.startswith("OK: Jira project 'Acme'")

    @respx.mock
    def test_validate_jira_auth_failure_via_sandbox(self, workspace):
        respx.route(host="acme.atlassian.net").mock(
            return_value=httpx.Response(401)
        )
        sandbox = ToolSandbox(str(workspace), ["validate_jira"])
        result = run(sandbox.execute_tool("validate_jira", {
            "url": "https://acme.atlassian.net",
            "token": "bad",
            "email": "bot@acme.com",
            "project_key": "ACM",
        }))
        assert result.startswith("FAILED:")
        assert "401" in result


class TestMissingRequiredParam:
    def test_list_projects_without_config_dir(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["list_projects"])
        with pytest.raises(ToolError, match="config_dir"):
            run(sandbox.execute_tool("list_projects", {}))

    def test_validate_jira_missing_url_raises(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["validate_jira"])
        with pytest.raises(ToolError, match="validate_jira requires"):
            run(sandbox.execute_tool("validate_jira", {
                "token": "t", "email": "e@x.com", "project_key": "K",
            }))

    def test_validate_github_missing_owner_raises(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["validate_github"])
        with pytest.raises(ToolError, match="validate_github requires"):
            run(sandbox.execute_tool("validate_github", {
                "token": "t", "repo": "r",
            }))

    def test_validate_gitlab_missing_project_id_raises(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["validate_gitlab"])
        with pytest.raises(ToolError, match="validate_gitlab requires"):
            run(sandbox.execute_tool("validate_gitlab", {"token": "t"}))

    def test_validate_jenkins_missing_job_key_raises(self, workspace):
        sandbox = ToolSandbox(str(workspace), ["validate_jenkins"])
        with pytest.raises(ToolError, match="validate_jenkins requires"):
            run(sandbox.execute_tool("validate_jenkins", {
                "url": "https://jenkins.x", "username": "u", "token": "t",
            }))


class TestInvalidIdRaisesToolError:
    def test_write_project_config_invalid_id_raises(self, workspace, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        sandbox = ToolSandbox(str(workspace), ["write_project_config"])
        with pytest.raises(ToolError, match="Invalid project_id"):
            run(sandbox.execute_tool("write_project_config", {
                "config_dir": str(cfg),
                "project_id": "../etc/passwd",
                "yaml_content": "project: {id: x}",
            }))

    def test_write_repo_config_invalid_repo_id_raises(self, workspace, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        sandbox = ToolSandbox(str(workspace), ["write_repo_config"])
        with pytest.raises(ToolError, match="Invalid repo_id"):
            run(sandbox.execute_tool("write_repo_config", {
                "config_dir": str(cfg),
                "project_id": "acme",
                "repo_id": "../evil",
                "yaml_content": "repo: {id: x}",
            }))

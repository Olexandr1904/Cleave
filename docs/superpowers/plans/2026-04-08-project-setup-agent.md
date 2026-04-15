# Project Setup Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Atlas (`project-setup-agent`), a BMAD-style agent that onboards new projects into Sickle via guided conversational setup with live API validation.

**Architecture:** New `integrations/config/config_tools.py` module provides 9 config management and validation functions. These are registered as tools in `orchestrator/tool_sandbox.py`. A new admin workspace type supports non-ticket agent execution. The agent prompt lives in `agents/project-setup-agent.md`. Three Claude Code commands (`.claude/commands/`) provide the entry points.

**Tech Stack:** Python 3.10+, httpx (HTTP client, already a dependency), PyYAML (already a dependency), pytest + respx (testing)

**Spec:** `docs/superpowers/specs/2026-04-08-project-setup-agent-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `integrations/config/__init__.py` | Package init |
| `integrations/config/config_tools.py` | 9 functions: env var resolution, 4 validators, 5 config management ops |
| `orchestrator/tool_sandbox.py` | Register new tools in `ALL_TOOLS`, add tool definitions and handler dispatch |
| `workspace/workspace.py` | Add `AdminWorkspaceState` and `AdminWorkspace` for non-ticket workspaces |
| `agents/project-setup-agent.md` | BMAD-style agent definition with full conversational flow |
| `.claude/commands/add-project.md` | Claude Code command — triggers add operation |
| `.claude/commands/list-projects.md` | Claude Code command — triggers list operation |
| `.claude/commands/remove-project.md` | Claude Code command — triggers remove operation |
| `tests/unit/test_config_tools.py` | Unit tests for all 9 config tool functions |
| `tests/unit/test_tool_sandbox_config.py` | Tests for new tool registration in sandbox |
| `tests/unit/test_admin_workspace.py` | Tests for admin workspace type |

---

### Task 1: Config tools module — env var resolution and list/read

**Files:**
- Create: `integrations/config/__init__.py`
- Create: `integrations/config/config_tools.py`
- Create: `tests/unit/test_config_tools.py`

- [ ] **Step 1: Write failing tests for `resolve_env_var` and `list_projects`**

Create `tests/unit/test_config_tools.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'integrations.config'`

- [ ] **Step 3: Create the config tools module with resolve_env_var, list_projects, read_project_config**

Create `integrations/config/__init__.py` (empty file).

Create `integrations/config/config_tools.py`:

```python
"""Config management tools for the project-setup-agent.

Provides validation and CRUD operations on Sickle project config files
in the config-live/ directory.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_env_var(value: str) -> str:
    """Resolve a ${VAR_NAME} reference from the environment.

    If the value matches the pattern ${VAR_NAME}, look it up in os.environ.
    Plain strings are returned as-is.

    Raises:
        ValueError: If the env var is not set.
    """
    if not value:
        return value
    match = ENV_VAR_PATTERN.match(value)
    if not match:
        return value
    var_name = match.group(1)
    val = os.environ.get(var_name)
    if val is None:
        raise ValueError(f"Environment variable `{var_name}` is not set.")
    return val


def list_projects(config_dir: str) -> list[dict[str, Any]]:
    """List all projects in the config directory.

    Scans {config_dir}/projects/ for subdirectories containing project.yaml.

    Returns:
        List of dicts with keys: id, name, repo_count, enabled.
    """
    projects_dir = Path(config_dir) / "projects"
    if not projects_dir.exists():
        return []

    results = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue
        project_file = entry / "project.yaml"
        if not project_file.exists():
            continue

        data = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
        project_info = data.get("project", {})

        repos_dir = entry / "repos"
        repo_count = 0
        if repos_dir.exists():
            repo_count = sum(1 for f in repos_dir.iterdir() if f.suffix in (".yaml", ".yml"))

        results.append({
            "id": project_info.get("id", entry.name),
            "name": project_info.get("name", entry.name),
            "repo_count": repo_count,
            "enabled": project_info.get("enabled", True),
        })

    return results


def read_project_config(config_dir: str, project_id: str) -> dict[str, Any]:
    """Read a project's full configuration (project.yaml + all repo configs).

    Returns:
        Dict with keys: project (parsed project.yaml), repos (dict of repo_id -> parsed yaml).

    Raises:
        FileNotFoundError: If the project directory or project.yaml doesn't exist.
    """
    proj_dir = Path(config_dir) / "projects" / project_id
    project_file = proj_dir / "project.yaml"
    if not project_file.exists():
        raise FileNotFoundError(f"Project '{project_id}' not found at {proj_dir}")

    project_data = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}

    repos = {}
    repos_dir = proj_dir / "repos"
    if repos_dir.exists():
        for repo_file in sorted(repos_dir.iterdir()):
            if repo_file.suffix in (".yaml", ".yml"):
                repo_data = yaml.safe_load(repo_file.read_text(encoding="utf-8")) or {}
                repo_id = repo_data.get("repo", {}).get("id", repo_file.stem)
                repos[repo_id] = repo_data

    return {"project": project_data, "repos": repos}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/config/__init__.py integrations/config/config_tools.py tests/unit/test_config_tools.py
git commit -m "feat: add config tools module with env var resolution, list and read"
```

---

### Task 2: Config tools — write_project_config and write_repo_config

**Files:**
- Modify: `integrations/config/config_tools.py`
- Modify: `tests/unit/test_config_tools.py`

- [ ] **Step 1: Write failing tests for write functions**

Append to `tests/unit/test_config_tools.py`:

```python
from integrations.config.config_tools import (
    write_project_config,
    write_repo_config,
)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py::TestWriteProjectConfig tests/unit/test_config_tools.py::TestWriteRepoConfig -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement write_project_config and write_repo_config**

Add to `integrations/config/config_tools.py`:

```python
def write_project_config(
    config_dir: str, project_id: str, yaml_content: str
) -> dict[str, Any]:
    """Write project.yaml for a project.

    Creates the project directory if needed. Validates the YAML after writing.

    Returns:
        Dict with keys: success (bool), path (str), error (str, if failed).
    """
    proj_dir = Path(config_dir) / "projects" / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    project_file = proj_dir / "project.yaml"

    project_file.write_text(yaml_content, encoding="utf-8")

    # Validate by reading back
    try:
        yaml.safe_load(project_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return {"success": False, "path": str(project_file), "error": str(e)}

    logger.info("Wrote project config: %s", project_file)
    return {"success": True, "path": str(project_file)}


def write_repo_config(
    config_dir: str, project_id: str, repo_id: str, yaml_content: str
) -> dict[str, Any]:
    """Write a repo config file for a project.

    Creates the repos directory if needed. Validates the YAML after writing.

    Returns:
        Dict with keys: success (bool), path (str), error (str, if failed).
    """
    repos_dir = Path(config_dir) / "projects" / project_id / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_file = repos_dir / f"{repo_id}.yaml"

    repo_file.write_text(yaml_content, encoding="utf-8")

    # Validate by reading back
    try:
        yaml.safe_load(repo_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return {"success": False, "path": str(repo_file), "error": str(e)}

    logger.info("Wrote repo config: %s", repo_file)
    return {"success": True, "path": str(repo_file)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/config/config_tools.py tests/unit/test_config_tools.py
git commit -m "feat: add write_project_config and write_repo_config"
```

---

### Task 3: Config tools — remove_project

**Files:**
- Modify: `integrations/config/config_tools.py`
- Modify: `tests/unit/test_config_tools.py`

- [ ] **Step 1: Write failing tests for remove_project**

Append to `tests/unit/test_config_tools.py`:

```python
from integrations.config.config_tools import remove_project


class TestRemoveProject:
    def test_removes_project_with_backup(self, tmp_path):
        # Create a project
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

        # Verify backup exists
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
        # Should have exactly one backup dir starting with "test-"
        backup_dirs = list(backups.iterdir())
        assert len(backup_dirs) == 1
        assert backup_dirs[0].name.startswith("test-")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py::TestRemoveProject -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement remove_project**

Add to `integrations/config/config_tools.py`:

```python
import shutil
from datetime import datetime, timezone


def remove_project(config_dir: str, project_id: str) -> dict[str, Any]:
    """Remove a project, backing it up first.

    Backs up to {config_dir}/.backups/{project_id}-{YYYYMMDD-HHMMSS}/.
    Fails if the backup cannot be created.

    Returns:
        Dict with keys: success (bool), backup_path (str), error (str, if failed).
    """
    proj_dir = Path(config_dir) / "projects" / project_id
    if not proj_dir.exists():
        return {"success": False, "error": f"Project '{project_id}' not found at {proj_dir}"}

    # Create backup
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(config_dir) / ".backups" / f"{project_id}-{timestamp}"
    try:
        shutil.copytree(str(proj_dir), str(backup_dir))
    except OSError as e:
        return {"success": False, "error": f"Backup failed: {e}"}

    # Remove the project directory
    shutil.rmtree(str(proj_dir))

    logger.info("Removed project '%s', backup at %s", project_id, backup_dir)
    return {"success": True, "backup_path": str(backup_dir)}
```

Update the imports at the top of the file to include `shutil` and `datetime`:

```python
import shutil
from datetime import datetime, timezone
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/config/config_tools.py tests/unit/test_config_tools.py
git commit -m "feat: add remove_project with backup"
```

---

### Task 4: Config tools — validation functions

**Files:**
- Modify: `integrations/config/config_tools.py`
- Modify: `tests/unit/test_config_tools.py`

- [ ] **Step 1: Write failing tests for validation functions**

Append to `tests/unit/test_config_tools.py`:

```python
import httpx
import respx

from integrations.config.config_tools import (
    validate_jira,
    validate_github,
    validate_gitlab,
    validate_jenkins,
)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py::TestValidateJira tests/unit/test_config_tools.py::TestValidateGitHub tests/unit/test_config_tools.py::TestValidateGitLab tests/unit/test_config_tools.py::TestValidateJenkins -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the four validation functions**

Add to `integrations/config/config_tools.py`:

```python
import httpx

VALIDATE_TIMEOUT = 15


async def validate_jira(
    url: str, token: str, email: str, project_key: str
) -> dict[str, Any]:
    """Validate Jira credentials and project key.

    Hits {url}/rest/api/3/project/{project_key} with Basic auth.

    Returns:
        Dict with keys: success (bool), project_name (str), error (str, if failed).
    """
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(
            auth=(email, token),
            timeout=VALIDATE_TIMEOUT,
        ) as client:
            response = await client.get(f"{url}/rest/api/3/project/{project_key}")
            if response.status_code == 200:
                data = response.json()
                return {"success": True, "project_name": data.get("name", project_key)}
            return {
                "success": False,
                "error": f"Jira returned HTTP {response.status_code} for project '{project_key}'",
            }
    except httpx.ConnectError as e:
        return {"success": False, "error": f"Connection failed: {e}"}
    except httpx.TimeoutException:
        return {"success": False, "error": f"Connection to {url} timed out"}


async def validate_github(token: str, owner: str, repo: str) -> dict[str, Any]:
    """Validate GitHub token and repo access.

    Hits https://api.github.com/repos/{owner}/{repo} with Bearer token.

    Returns:
        Dict with keys: success (bool), full_name (str), default_branch (str),
        error (str, if failed).
    """
    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=VALIDATE_TIMEOUT,
        ) as client:
            response = await client.get(f"https://api.github.com/repos/{owner}/{repo}")
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "full_name": data.get("full_name", f"{owner}/{repo}"),
                    "default_branch": data.get("default_branch", "main"),
                }
            return {
                "success": False,
                "error": f"GitHub returned HTTP {response.status_code} for {owner}/{repo}",
            }
    except httpx.ConnectError as e:
        return {"success": False, "error": f"Connection failed: {e}"}
    except httpx.TimeoutException:
        return {"success": False, "error": "Connection to GitHub timed out"}


async def validate_gitlab(
    token: str, project_id: str, url: str = "https://gitlab.com"
) -> dict[str, Any]:
    """Validate GitLab token and project access.

    Hits {url}/api/v4/projects/{project_id} with Private-Token header.

    Returns:
        Dict with keys: success (bool), project_name (str), error (str, if failed).
    """
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(
            headers={"Private-Token": token},
            timeout=VALIDATE_TIMEOUT,
        ) as client:
            response = await client.get(f"{url}/api/v4/projects/{project_id}")
            if response.status_code == 200:
                data = response.json()
                return {"success": True, "project_name": data.get("name", project_id)}
            return {
                "success": False,
                "error": f"GitLab returned HTTP {response.status_code} for project {project_id}",
            }
    except httpx.ConnectError as e:
        return {"success": False, "error": f"Connection failed: {e}"}
    except httpx.TimeoutException:
        return {"success": False, "error": f"Connection to {url} timed out"}


async def validate_jenkins(
    url: str, username: str, token: str, job_key: str
) -> dict[str, Any]:
    """Validate Jenkins credentials and job access.

    Hits {url}/job/{job_key}/api/json with Basic auth.

    Returns:
        Dict with keys: success (bool), job_name (str), error (str, if failed).
    """
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(
            auth=(username, token),
            timeout=VALIDATE_TIMEOUT,
        ) as client:
            response = await client.get(f"{url}/job/{job_key}/api/json")
            if response.status_code == 200:
                data = response.json()
                return {"success": True, "job_name": data.get("displayName", job_key)}
            return {
                "success": False,
                "error": f"Jenkins returned HTTP {response.status_code} for job '{job_key}'",
            }
    except httpx.ConnectError as e:
        return {"success": False, "error": f"Connection failed: {e}"}
    except httpx.TimeoutException:
        return {"success": False, "error": f"Connection to {url} timed out"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_tools.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/config/config_tools.py tests/unit/test_config_tools.py
git commit -m "feat: add Jira, GitHub, GitLab, Jenkins validation functions"
```

---

### Task 5: Register config tools in tool sandbox

**Files:**
- Modify: `orchestrator/tool_sandbox.py`
- Create: `tests/unit/test_tool_sandbox_config.py`

- [ ] **Step 1: Write failing tests for new tool registration**

Create `tests/unit/test_tool_sandbox_config.py`:

```python
"""Tests for config tool registration in tool sandbox."""

from __future__ import annotations

import asyncio
import os

import pytest
import yaml

from orchestrator.tool_sandbox import ToolError, ToolSandbox, get_tool_definitions


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
    source = tmp_path / "ws" / "source"
    source.mkdir(parents=True)
    reports = tmp_path / "ws" / "reports"
    reports.mkdir()
    return tmp_path / "ws"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestConfigToolsRegistered:
    def test_config_tools_in_all_tools(self):
        from orchestrator.tool_sandbox import ALL_TOOLS
        config_tools = {
            "validate_jira", "validate_github", "validate_gitlab", "validate_jenkins",
            "list_projects", "read_project_config",
            "write_project_config", "write_repo_config", "remove_project",
        }
        assert config_tools.issubset(ALL_TOOLS)

    def test_sandbox_accepts_config_tools(self, workspace):
        sandbox = ToolSandbox(
            str(workspace),
            ["list_projects", "write_project_config"],
        )
        assert sandbox is not None

    def test_get_tool_definitions_includes_config_tools(self):
        defs = get_tool_definitions(["list_projects", "read_project_config"])
        names = {d["name"] for d in defs}
        assert names == {"list_projects", "read_project_config"}


class TestListProjectsTool:
    def test_list_projects_via_sandbox(self, workspace, config_dir):
        sandbox = ToolSandbox(str(workspace), ["list_projects"])
        result = run(sandbox.execute_tool("list_projects", {
            "config_dir": str(config_dir),
        }))
        assert "acme" in result
        assert "Acme Corp" in result


class TestWriteProjectConfigTool:
    def test_write_via_sandbox(self, workspace, tmp_path):
        config_dir = tmp_path / "new-config"
        config_dir.mkdir()

        sandbox = ToolSandbox(str(workspace), ["write_project_config"])
        yaml_content = yaml.dump({"project": {"id": "test", "name": "Test"}})
        result = run(sandbox.execute_tool("write_project_config", {
            "config_dir": str(config_dir),
            "project_id": "test",
            "yaml_content": yaml_content,
        }))
        assert "success" in result.lower() or "written" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_tool_sandbox_config.py -v`
Expected: FAIL — config tools not in `ALL_TOOLS`

- [ ] **Step 3: Register config tools in tool_sandbox.py**

Modify `orchestrator/tool_sandbox.py`:

At the top, update `ALL_TOOLS`:

```python
ALL_TOOLS = {
    "read_file",
    "write_file",
    "list_directory",
    "search_code",
    "run_command",
    "git_operation",
    # Config management tools (project-setup-agent)
    "validate_jira",
    "validate_github",
    "validate_gitlab",
    "validate_jenkins",
    "list_projects",
    "read_project_config",
    "write_project_config",
    "write_repo_config",
    "remove_project",
}
```

Add the imports near the top:

```python
import yaml
from integrations.config import config_tools
```

Add tool handler methods to the `ToolSandbox` class. These tools are NOT workspace-confined — they operate on `config_dir` passed as a parameter:

```python
    async def _tool_validate_jira(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_jira(
            url=params.get("url", ""),
            token=params.get("token", ""),
            email=params.get("email", ""),
            project_key=params.get("project_key", ""),
        )
        if result["success"]:
            return f"OK: Jira project '{result['project_name']}' is accessible."
        return f"FAILED: {result['error']}"

    async def _tool_validate_github(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_github(
            token=params.get("token", ""),
            owner=params.get("owner", ""),
            repo=params.get("repo", ""),
        )
        if result["success"]:
            return f"OK: GitHub repo '{result['full_name']}' is accessible (default branch: {result['default_branch']})."
        return f"FAILED: {result['error']}"

    async def _tool_validate_gitlab(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_gitlab(
            token=params.get("token", ""),
            project_id=params.get("project_id", ""),
            url=params.get("url", "https://gitlab.com"),
        )
        if result["success"]:
            return f"OK: GitLab project '{result['project_name']}' is accessible."
        return f"FAILED: {result['error']}"

    async def _tool_validate_jenkins(self, params: dict[str, Any]) -> str:
        result = await config_tools.validate_jenkins(
            url=params.get("url", ""),
            username=params.get("username", ""),
            token=params.get("token", ""),
            job_key=params.get("job_key", ""),
        )
        if result["success"]:
            return f"OK: Jenkins job '{result['job_name']}' is accessible."
        return f"FAILED: {result['error']}"

    async def _tool_list_projects(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        if not config_dir:
            raise ToolError("list_projects requires 'config_dir' parameter")
        projects = config_tools.list_projects(config_dir)
        if not projects:
            return "No projects found."
        lines = []
        for p in projects:
            status = "enabled" if p["enabled"] else "disabled"
            lines.append(f"- {p['id']}: {p['name']} ({p['repo_count']} repos, {status})")
        return "\n".join(lines)

    async def _tool_read_project_config(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        if not config_dir or not project_id:
            raise ToolError("read_project_config requires 'config_dir' and 'project_id'")
        try:
            data = config_tools.read_project_config(config_dir, project_id)
            return yaml.dump(data, default_flow_style=False)
        except FileNotFoundError as e:
            raise ToolError(str(e)) from e

    async def _tool_write_project_config(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        yaml_content = params.get("yaml_content", "")
        if not config_dir or not project_id or not yaml_content:
            raise ToolError("write_project_config requires 'config_dir', 'project_id', 'yaml_content'")
        result = config_tools.write_project_config(config_dir, project_id, yaml_content)
        if result["success"]:
            return f"Successfully written to {result['path']}"
        return f"Failed: {result.get('error', 'unknown error')}"

    async def _tool_write_repo_config(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        repo_id = params.get("repo_id", "")
        yaml_content = params.get("yaml_content", "")
        if not config_dir or not project_id or not repo_id or not yaml_content:
            raise ToolError("write_repo_config requires 'config_dir', 'project_id', 'repo_id', 'yaml_content'")
        result = config_tools.write_repo_config(config_dir, project_id, repo_id, yaml_content)
        if result["success"]:
            return f"Successfully written to {result['path']}"
        return f"Failed: {result.get('error', 'unknown error')}"

    async def _tool_remove_project(self, params: dict[str, Any]) -> str:
        config_dir = params.get("config_dir", "")
        project_id = params.get("project_id", "")
        if not config_dir or not project_id:
            raise ToolError("remove_project requires 'config_dir' and 'project_id'")
        result = config_tools.remove_project(config_dir, project_id)
        if result["success"]:
            return f"Removed project '{project_id}'. Backup at: {result['backup_path']}"
        return f"Failed: {result['error']}"
```

Add tool definitions to the `all_definitions` dict inside `get_tool_definitions`:

```python
        "validate_jira": {
            "name": "validate_jira",
            "description": "Validate Jira credentials and project key by hitting the Jira REST API.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Jira instance URL (e.g. https://company.atlassian.net)"},
                    "token": {"type": "string", "description": "Jira API token (resolved from env var)"},
                    "email": {"type": "string", "description": "Jira account email"},
                    "project_key": {"type": "string", "description": "Jira project key (e.g. PROJ)"},
                },
                "required": ["url", "token", "email", "project_key"],
            },
        },
        "validate_github": {
            "name": "validate_github",
            "description": "Validate GitHub token and repo access.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"type": "string", "description": "GitHub personal access token"},
                    "owner": {"type": "string", "description": "GitHub org or user"},
                    "repo": {"type": "string", "description": "Repository name"},
                },
                "required": ["token", "owner", "repo"],
            },
        },
        "validate_gitlab": {
            "name": "validate_gitlab",
            "description": "Validate GitLab token and project access.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"type": "string", "description": "GitLab personal access token"},
                    "project_id": {"type": "string", "description": "GitLab numeric project ID"},
                    "url": {"type": "string", "description": "GitLab instance URL (default: https://gitlab.com)"},
                },
                "required": ["token", "project_id"],
            },
        },
        "validate_jenkins": {
            "name": "validate_jenkins",
            "description": "Validate Jenkins credentials and job access.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Jenkins URL (e.g. https://jenkins.company.com)"},
                    "username": {"type": "string", "description": "Jenkins username"},
                    "token": {"type": "string", "description": "Jenkins API token"},
                    "job_key": {"type": "string", "description": "Jenkins job path (e.g. my-project/main)"},
                },
                "required": ["url", "username", "token", "job_key"],
            },
        },
        "list_projects": {
            "name": "list_projects",
            "description": "List all projects in the Sickle config directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config-live/ directory"},
                },
                "required": ["config_dir"],
            },
        },
        "read_project_config": {
            "name": "read_project_config",
            "description": "Read a project's full configuration (project.yaml + all repo configs).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config-live/ directory"},
                    "project_id": {"type": "string", "description": "Project ID to read"},
                },
                "required": ["config_dir", "project_id"],
            },
        },
        "write_project_config": {
            "name": "write_project_config",
            "description": "Write project.yaml for a project. Creates directories if needed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config-live/ directory"},
                    "project_id": {"type": "string", "description": "Project ID (directory name)"},
                    "yaml_content": {"type": "string", "description": "Full YAML content for project.yaml"},
                },
                "required": ["config_dir", "project_id", "yaml_content"],
            },
        },
        "write_repo_config": {
            "name": "write_repo_config",
            "description": "Write a repo config file for a project. Creates directories if needed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config-live/ directory"},
                    "project_id": {"type": "string", "description": "Project ID"},
                    "repo_id": {"type": "string", "description": "Repo ID (file name without .yaml)"},
                    "yaml_content": {"type": "string", "description": "Full YAML content for the repo config"},
                },
                "required": ["config_dir", "project_id", "repo_id", "yaml_content"],
            },
        },
        "remove_project": {
            "name": "remove_project",
            "description": "Remove a project from config (backs up first to .backups/).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_dir": {"type": "string", "description": "Path to config-live/ directory"},
                    "project_id": {"type": "string", "description": "Project ID to remove"},
                },
                "required": ["config_dir", "project_id"],
            },
        },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_tool_sandbox_config.py tests/unit/test_tool_sandbox.py -v`
Expected: All tests PASS (both new and existing)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/tool_sandbox.py tests/unit/test_tool_sandbox_config.py
git commit -m "feat: register config tools in tool sandbox"
```

---

### Task 6: Admin workspace type

**Files:**
- Modify: `workspace/workspace.py`
- Create: `tests/unit/test_admin_workspace.py`

- [ ] **Step 1: Write failing tests for admin workspace**

Create `tests/unit/test_admin_workspace.py`:

```python
"""Tests for admin workspace type."""

from __future__ import annotations

import json

import pytest

from workspace.workspace import AdminWorkspace, AdminWorkspaceState


class TestAdminWorkspaceState:
    def test_defaults(self):
        state = AdminWorkspaceState(
            operation="add",
            workspace_root="/tmp/admin-ws",
        )
        assert state.operation == "add"
        assert state.status == "pending"
        assert state.started_at != ""
        assert state.error is None

    def test_valid_operations(self):
        for op in ("add", "list", "remove"):
            state = AdminWorkspaceState(operation=op, workspace_root="/tmp/ws")
            assert state.operation == op


class TestAdminWorkspace:
    @pytest.fixture
    def admin_dir(self, tmp_path):
        ws_root = tmp_path / "admin-ws"
        ws_root.mkdir()
        return ws_root

    def test_creates_directories(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        assert (admin_dir / "meta").exists()
        assert (admin_dir / "reports").exists()
        assert (admin_dir / "logs").exists()
        assert not (admin_dir / "source").exists()  # No source dir for admin

    def test_state_saved_and_loaded(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        ws.save_state()

        loaded = AdminWorkspace(str(admin_dir))
        assert loaded.state.operation == "add"
        assert loaded.state.status == "pending"

    def test_no_source_dir_property(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="list")
        assert ws.source_dir is None

    def test_meta_and_reports_dirs(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        assert ws.meta_dir == admin_dir / "meta"
        assert ws.reports_dir == admin_dir / "reports"

    def test_update_status(self, admin_dir):
        ws = AdminWorkspace.create(str(admin_dir), operation="add")
        ws.update_state(status="completed")
        ws.save_state()

        loaded = AdminWorkspace(str(admin_dir))
        assert loaded.state.status == "completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_admin_workspace.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement AdminWorkspace**

Add to `workspace/workspace.py`:

```python
@dataclass
class AdminWorkspaceState:
    """State for admin (non-ticket) workspaces."""
    operation: str  # "add", "list", "remove"
    workspace_root: str
    status: str = "pending"  # "pending", "in_progress", "completed", "failed"
    started_at: str = ""
    last_updated_at: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        now = _now_iso()
        if not self.started_at:
            self.started_at = now
        if not self.last_updated_at:
            self.last_updated_at = now


class AdminWorkspace:
    """Lightweight workspace for admin operations (no ticket context, no source dir)."""

    def __init__(self, root: str, state: AdminWorkspaceState | None = None) -> None:
        self._root = Path(root)
        self._state = state

    @classmethod
    def create(cls, root: str, operation: str) -> "AdminWorkspace":
        """Create a new admin workspace with directory structure."""
        root_path = Path(root)
        (root_path / "meta").mkdir(parents=True, exist_ok=True)
        (root_path / "reports").mkdir(parents=True, exist_ok=True)
        (root_path / "logs").mkdir(parents=True, exist_ok=True)

        state = AdminWorkspaceState(operation=operation, workspace_root=root)
        ws = cls(root, state)
        ws.save_state()
        return ws

    @property
    def root(self) -> Path:
        return self._root

    @property
    def source_dir(self) -> None:
        return None

    @property
    def meta_dir(self) -> Path:
        return self._root / "meta"

    @property
    def reports_dir(self) -> Path:
        return self._root / "reports"

    @property
    def logs_dir(self) -> Path:
        return self._root / "logs"

    @property
    def state_path(self) -> Path:
        return self._root / "state.json"

    @property
    def state(self) -> AdminWorkspaceState:
        if self._state is None:
            self._state = self._load_state()
        return self._state

    def _load_state(self) -> AdminWorkspaceState:
        with open(self.state_path) as f:
            data = json.load(f)
        return AdminWorkspaceState(**data)

    def save_state(self) -> None:
        data = asdict(self._state)
        self._state.last_updated_at = _now_iso()
        data["last_updated_at"] = self._state.last_updated_at

        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(self._root), suffix=".tmp", prefix="state_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path_str, str(self.state_path))
        except Exception:
            if os.path.exists(tmp_path_str):
                os.unlink(tmp_path_str)
            raise

    def update_state(self, **kwargs: Any) -> None:
        state = self.state
        for key, value in kwargs.items():
            if not hasattr(state, key):
                raise ValueError(f"Unknown state field: {key}")
            setattr(state, key, value)
        self.save_state()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_admin_workspace.py tests/unit/test_workspace.py -v`
Expected: All tests PASS (both new and existing)

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_admin_workspace.py
git commit -m "feat: add AdminWorkspace type for non-ticket agent operations"
```

---

### Task 7: Agent definition

**Files:**
- Create: `agents/project-setup-agent.md`

- [ ] **Step 1: Write the agent prompt**

Create `agents/project-setup-agent.md`:

````markdown
---
agent:
  id: "project-setup-agent"
  name: "Atlas"
  title: "Project Setup Specialist"
  model: ""

persona:
  role: "DevOps Onboarding Specialist"
  style: "Methodical, thorough, validates before proceeding"
  identity: "Configuration specialist who onboards new projects into the Sickle pipeline"

core_principles:
  - "Always validate credentials before writing config"
  - "Use environment variable references for all secrets"
  - "Provide sensible defaults — minimize user input for common setups"
  - "Never overwrite existing config without explicit confirmation"

tools:
  - validate_jira
  - validate_github
  - validate_gitlab
  - validate_jenkins
  - list_projects
  - read_project_config
  - write_project_config
  - write_repo_config
  - remove_project

inputs:
  - "operation (add | list | remove)"
  - "meta/input.md (orchestrator mode — pre-provided answers)"
  - "meta/answers.md (orchestrator mode — human replies to questions)"

outputs:
  - "config-live/projects/{project_id}/project.yaml"
  - "config-live/projects/{project_id}/repos/{repo_id}.yaml"
  - "reports/project-setup-output.md (orchestrator mode — summary)"
  - "reports/questions.md (orchestrator mode — pending questions)"

decision_policy:
  when_to_run: "Triggered by Claude Code command or Telegram /add-project"
  when_to_skip: "N/A — admin operation, not part of ticket pipeline"
  success_outcome: "Config files written and validated"
  failure_outcome: "Validation failed — user informed of specific errors"
  max_iterations: 1

dependencies:
  tasks: []
  checklists: []
---

# Project Setup Agent — Atlas

## Activation

You are Atlas, a Project Setup Specialist. Your role is to onboard new projects
into the Sickle autonomous development pipeline. You guide users through
collecting project configuration details, validate credentials against live APIs,
and write the YAML config files.

You support three operations: **add**, **list**, and **remove**.

## Operation: Add

Guide the user through a conversational flow to set up a new project with one
repository. Ask one question at a time. Provide sensible defaults in parentheses.
The user can accept defaults by confirming.

**IMPORTANT:** All secrets (API tokens, passwords) MUST use environment variable
references (`${VAR_NAME}`). Never write raw secrets into config files.

### Phase 1 — Project Identity

1. Ask for **project ID** (slug, e.g. `acme`) and **display name** (e.g. `Acme Corp`)
2. Call `list_projects` to check for duplicates — if the ID already exists:
   - Show the existing project details
   - Ask: overwrite, pick a different ID, or abort?

### Phase 2 — Jira Integration

3. Ask for **Jira URL** (e.g. `https://company.atlassian.net`)
4. Ask for **Jira project key** (e.g. `ACME`)
5. Ask for **Jira email** for API auth (e.g. `bot@company.com`)
6. Ask for **env var name** for the Jira token (default: `JIRA_TOKEN`)
7. Ask for **trigger label** (default: `ai-pipeline`) and any **ignore labels** (comma-separated, optional)
8. Ask for **Jira status mappings** — provide defaults:
   - todo: "To Do"
   - in_progress: "In Progress"
   - in_review: "In Review"
   - done: "Done"
9. Call `validate_jira` with the provided URL, resolved token, email, and project key
   - On success: report project name and proceed
   - On failure: report the specific error, ask user to fix, offer to skip validation

### Phase 3 — VCS Setup

10. Ask: **GitHub or GitLab?**
11. **If GitHub:**
    - Ask **owner** (org or user) and **repo name**
    - Ask **env var for token** (default: `GITHUB_TOKEN`)
    - Ask **default branch** (default: `develop`)
    - Ask **branch prefix** (default: `feature`)
    - Ask **merge method**: squash / merge / rebase (default: `squash`)
12. **If GitLab:**
    - Ask **GitLab URL** (default: `https://gitlab.com`)
    - Ask **project ID** (numeric)
    - Ask **env var for token** (default: `GITLAB_TOKEN`)
    - Ask **default branch** (default: `develop`)
    - Ask **branch prefix** (default: `feature`)
13. Derive `clone_url` automatically:
    - GitHub: `https://${TOKEN_VAR}@github.com/{owner}/{repo}.git`
    - GitLab: `https://oauth2:${TOKEN_VAR}@{gitlab_host}/{project_path}.git`
14. Call `validate_github` or `validate_gitlab` with resolved token
    - On success: report repo name / default branch and proceed
    - On failure: report error, ask user to fix, offer to skip

### Phase 4 — CI/CD Setup

15. Ask: **GitHub Actions or Jenkins?**
16. **If GitHub Actions:** no extra input needed (uses VCS token)
17. **If Jenkins:**
    - Ask **Jenkins URL** (e.g. `https://jenkins.company.com`)
    - Ask **job key** (e.g. `my-project/main`)
    - Ask **env var for username** (default: `JENKINS_USERNAME`)
    - Ask **env var for token** (default: `JENKINS_TOKEN`)
18. Call `validate_jenkins` if Jenkins — report success or error

### Phase 5 — Quality Gates

19. Ask for **lint command** and whether it's a hard gate (default: yes). Optional — user can skip.
20. Ask for **test command** and hard gate (default: yes). Optional.
21. Ask for **build/check command** and hard gate (default: yes). Optional.

### Phase 6 — Extras

22. Ask for **Telegram chat ID override** (optional — default: inherit from global)
23. Ask for **architecture rules file** path (optional, e.g. `docs/arch-rules.md`)
24. Ask for **protected files** list (optional, comma-separated, e.g. `.github/, build.gradle.kts`)
25. Ask for **max concurrent tickets** (optional — default: inherit from project/global)

### Phase 7 — Write & Confirm

26. Display a full summary table of all collected values
27. Ask for explicit confirmation before writing
28. Generate the `project.yaml` content following this structure:

```yaml
project:
  id: "{project_id}"
  name: "{display_name}"
  enabled: true

jira:
  url: "{jira_url}"
  token: "${JIRA_TOKEN_VAR}"
  email: "{jira_email}"
  project_key: "{project_key}"
  trigger_labels: [{trigger_labels}]
  ignore_labels: [{ignore_labels}]
  statuses:
    todo: "{status_todo}"
    in_progress: "{status_in_progress}"
    in_review: "{status_in_review}"
    done: "{status_done}"

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  default_chat_id: "{telegram_chat_id_or_inherit}"

parallelism:
  max_concurrent_tickets: {max_concurrent}

defaults:
  poll_interval_seconds: 300
  max_iterations:
    scope_guard: 3
    fix: 3
    qa: 2
    dev: 2
  pr_comment_fetch_delay_minutes: 30
```

29. Generate the `repos/{repo_id}.yaml` content following this structure (GitHub example):

```yaml
repo:
  id: "{repo_id}"
  name: "{repo_display_name}"
  enabled: true

vcs:
  provider: "github"
  github:
    token: "${GITHUB_TOKEN_VAR}"
    owner: "{owner}"
    repo: "{repo_name}"
    default_branch: "{default_branch}"
    branch_prefix: "{branch_prefix}"
    merge_method: "{merge_method}"

ci:
  provider: "github_actions"

git:
  clone_url: "{clone_url}"
  commit_author_name: "Sickle Bot"
  commit_author_email: "sickle@pipeline.local"
  depth: 1

architecture:
  rules_file: "{rules_file}"
  protected_files: [{protected_files}]

linting:
  run_command: "{lint_command}"
  hard_gate: {lint_hard_gate}

testing:
  run_command: "{test_command}"
  hard_gate: {test_hard_gate}

build:
  check_command: "{build_command}"
  hard_gate: {build_hard_gate}

parallelism:
  max_concurrent_tickets: {max_concurrent}
```

30. Call `write_project_config` and `write_repo_config`
31. Report what was written
32. List which environment variables need to be set:
    - `{JIRA_TOKEN_VAR}` — Jira API token
    - `{VCS_TOKEN_VAR}` — GitHub/GitLab token
    - `{JENKINS_*}` — if Jenkins was selected

## Operation: List

1. Call `list_projects` with the config directory
2. Display results as a formatted table:

```
Project          Repos      Enabled
───────────────────────────────────
{id}             {count}    {yes/no}
```

3. If no projects exist, say so.

## Operation: Remove

1. If no project ID was provided, call `list_projects` and ask which one to remove
2. Call `read_project_config` to show what will be deleted
3. Ask for explicit confirmation: "Remove project '{id}' and all repo configs? A backup will be created first."
4. Call `remove_project`
5. Report: what was removed and the backup location

## Constraints

- NEVER write raw secrets — always use `${ENV_VAR}` references
- NEVER overwrite existing config without explicit user confirmation
- NEVER skip validation without informing the user of the risk
- Config files are only written at the end after user confirms the summary
- All YAML must be valid — verify after writing
````

- [ ] **Step 2: Verify the agent is discovered by the resource registry**

Run: `cd /home/admin0/tot && python -c "from config.resource_registry import discover_resources; r = discover_resources('.'); a = r.get_agent('project-setup-agent'); print(f'Found: {a.id}, name: {a.name}, title: {a.title}, tools: {a.metadata.get(\"tools\", [])}')""`
Expected: `Found: project-setup-agent, name: Atlas, title: Project Setup Specialist, tools: [validate_jira, validate_github, ...]`

- [ ] **Step 3: Commit**

```bash
git add agents/project-setup-agent.md
git commit -m "feat: add Atlas (project-setup-agent) agent definition"
```

---

### Task 8: Claude Code commands

**Files:**
- Create: `.claude/commands/add-project.md`
- Create: `.claude/commands/list-projects.md`
- Create: `.claude/commands/remove-project.md`

- [ ] **Step 1: Create the add-project command**

Create `.claude/commands/add-project.md`:

```markdown
# Add Project to Sickle Pipeline

You are acting as **Atlas**, the Project Setup Specialist agent for the Sickle pipeline.

Read the full agent prompt at `agents/project-setup-agent.md` and follow its **Operation: Add** flow exactly.

## Key rules:
- Ask **one question at a time**
- Provide **sensible defaults** — the user should be able to accept defaults for common setups
- Use **environment variable references** (`${VAR_NAME}`) for all secrets — never write raw tokens
- **Validate** credentials against live APIs before writing configs (offer to skip if env var not set)
- The config directory is `config-live/` relative to the project root
- Only write files **after** showing the full summary and getting user confirmation
- After writing, remind the user which env vars need to be set

Start by greeting the user and asking for the project ID and display name.
```

- [ ] **Step 2: Create the list-projects command**

Create `.claude/commands/list-projects.md`:

```markdown
# List Sickle Projects

You are acting as **Atlas**, the Project Setup Specialist agent for the Sickle pipeline.

Scan the `config-live/projects/` directory in the project root. For each project:
1. Read `project.yaml` to get the project name and enabled status
2. Count the `.yaml` files in `repos/` to get the repo count
3. Read each repo's `repo.yaml` to get the repo ID

Display the results as a formatted table:

```
Project          Repos                  Enabled
─────────────────────────────────────────────────
{id}             {repo_ids}             {yes/no}
```

If no projects exist, say "No projects configured yet. Use /add-project to set one up."
```

- [ ] **Step 3: Create the remove-project command**

Create `.claude/commands/remove-project.md`:

```markdown
# Remove Sickle Project

You are acting as **Atlas**, the Project Setup Specialist agent for the Sickle pipeline.

Read the full agent prompt at `agents/project-setup-agent.md` and follow its **Operation: Remove** flow.

The config directory is `config-live/` relative to the project root.

$ARGUMENTS

## Key rules:
- If a project ID was provided above, use it. Otherwise list all projects and ask which to remove.
- **Always show** the full project config (project.yaml + all repo configs) before asking for confirmation
- **Always back up** before deleting — copy to `config-live/.backups/{project_id}-{timestamp}/`
- Report the backup location after removal
```

- [ ] **Step 4: Verify commands are discoverable**

Run: `ls -la /home/admin0/tot/.claude/commands/`
Expected: `add-project.md`, `list-projects.md`, `remove-project.md` all present

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/add-project.md .claude/commands/list-projects.md .claude/commands/remove-project.md
git commit -m "feat: add Claude Code commands for project management"
```

---

### Task 9: Run full test suite and verify

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `cd /home/admin0/tot && python -m pytest tests/ -v --tb=short`
Expected: All tests PASS, including existing tests (no regressions)

- [ ] **Step 2: Verify agent registration end-to-end**

Run: `cd /home/admin0/tot && python -c "
from config.resource_registry import discover_resources, validate_dependencies
r = discover_resources('.')
print('Agents:', [a.id for a in r.list_type('agents')])
warnings = validate_dependencies(r)
print('Dependency warnings:', warnings if warnings else 'none')
"`
Expected: `project-setup-agent` appears in agents list, no dependency warnings

- [ ] **Step 3: Verify tool sandbox accepts all new tools**

Run: `cd /home/admin0/tot && python -c "
from orchestrator.tool_sandbox import get_tool_definitions
config_tools = ['validate_jira', 'validate_github', 'validate_gitlab', 'validate_jenkins', 'list_projects', 'read_project_config', 'write_project_config', 'write_repo_config', 'remove_project']
defs = get_tool_definitions(config_tools)
print(f'Tool definitions: {len(defs)}/9')
for d in defs:
    print(f'  - {d[\"name\"]}: {len(d[\"input_schema\"][\"required\"])} required params')
"`
Expected: 9/9 tool definitions, each with correct required params

- [ ] **Step 4: Commit any fixes if needed**

If any test failed, fix and commit. Otherwise skip this step.

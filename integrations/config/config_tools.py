"""Config management tools for the project-setup-agent.

Provides validation and CRUD operations on Sickle project config files
in the config-live/ directory.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from config.config_loader import ConfigError, resolve_env_vars

PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def resolve_env_var(value: str) -> str:
    """Resolve ${VAR_NAME} references in a string value.

    Delegates to config.config_loader.resolve_env_vars so that behavior
    stays consistent with the rest of the codebase (including support for
    embedded references like "Bearer ${TOKEN}"). The underlying ConfigError
    is re-raised as ValueError because tool sandbox callers expect a stdlib
    exception type.

    Raises:
        ValueError: If a referenced environment variable is not set.
    """
    if not value:
        return value
    try:
        return resolve_env_vars(value)
    except ConfigError as e:
        raise ValueError(str(e)) from e


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
        ValueError: If project_id contains characters other than alphanumerics,
            hyphens, or underscores (prevents path traversal from LLM-supplied input).
        FileNotFoundError: If the project directory or project.yaml doesn't exist.
    """
    if not project_id or not PROJECT_ID_PATTERN.match(project_id):
        raise ValueError(
            f"Invalid project_id '{project_id}': must contain only alphanumerics, "
            f"hyphens, and underscores."
        )

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


def write_project_config(
    config_dir: str, project_id: str, yaml_content: str
) -> dict[str, Any]:
    """Write project.yaml for a project.

    Creates the project directory if needed. Validates the YAML by reading it
    back after writing.

    Raises:
        ValueError: If project_id contains characters other than alphanumerics,
            hyphens, or underscores.

    Returns:
        Dict with keys: success (bool), path (str), error (str, if failed).
    """
    if not project_id or not PROJECT_ID_PATTERN.match(project_id):
        raise ValueError(
            f"Invalid project_id '{project_id}': must contain only alphanumerics, "
            f"hyphens, and underscores."
        )

    proj_dir = Path(config_dir) / "projects" / project_id
    project_file = proj_dir / "project.yaml"

    try:
        yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return {"success": False, "path": str(project_file), "error": str(e)}

    proj_dir.mkdir(parents=True, exist_ok=True)
    project_file.write_text(yaml_content, encoding="utf-8")

    return {"success": True, "path": str(project_file)}


def write_repo_config(
    config_dir: str, project_id: str, repo_id: str, yaml_content: str
) -> dict[str, Any]:
    """Write a repo config file for a project.

    Creates the repos directory if needed. Validates the YAML by reading it
    back after writing.

    Raises:
        ValueError: If project_id or repo_id contains characters other than
            alphanumerics, hyphens, or underscores.

    Returns:
        Dict with keys: success (bool), path (str), error (str, if failed).
    """
    if not project_id or not PROJECT_ID_PATTERN.match(project_id):
        raise ValueError(
            f"Invalid project_id '{project_id}': must contain only alphanumerics, "
            f"hyphens, and underscores."
        )
    if not repo_id or not PROJECT_ID_PATTERN.match(repo_id):
        raise ValueError(
            f"Invalid repo_id '{repo_id}': must contain only alphanumerics, "
            f"hyphens, and underscores."
        )

    repos_dir = Path(config_dir) / "projects" / project_id / "repos"
    repo_file = repos_dir / f"{repo_id}.yaml"

    try:
        yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return {"success": False, "path": str(repo_file), "error": str(e)}

    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_file.write_text(yaml_content, encoding="utf-8")

    return {"success": True, "path": str(repo_file)}


def remove_project(config_dir: str, project_id: str) -> dict[str, Any]:
    """Remove a project, backing it up first.

    Backs up to {config_dir}/.backups/{project_id}-{YYYYMMDD-HHMMSS}/.
    Fails if the backup cannot be created — the project is left intact.

    Raises:
        ValueError: If project_id contains characters other than alphanumerics,
            hyphens, or underscores.

    Returns:
        Dict with keys: success (bool), backup_path (str), error (str, if failed).
    """
    if not project_id or not PROJECT_ID_PATTERN.match(project_id):
        raise ValueError(
            f"Invalid project_id '{project_id}': must contain only alphanumerics, "
            f"hyphens, and underscores."
        )

    proj_dir = Path(config_dir) / "projects" / project_id
    if not proj_dir.exists():
        return {"success": False, "error": f"Project '{project_id}' not found at {proj_dir}"}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(config_dir) / ".backups" / f"{project_id}-{timestamp}"
    try:
        shutil.copytree(str(proj_dir), str(backup_dir))
    except OSError as e:
        return {"success": False, "error": f"Backup failed: {e}"}

    shutil.rmtree(str(proj_dir))

    return {"success": True, "backup_path": str(backup_dir)}

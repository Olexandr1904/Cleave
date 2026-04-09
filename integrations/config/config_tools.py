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

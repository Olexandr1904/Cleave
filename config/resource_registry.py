"""BMAD-style resource discovery and registry.

Scans agents/, tasks/, templates/, checklists/, and data/ directories
for .md files, parses YAML frontmatter metadata, and builds a registry
mapping resource type + id to file path and metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

RESOURCE_DIRS = {
    "agents": "agents",
    "tasks": "tasks",
    "templates": "templates",
    "checklists": "checklists",
    "data": "data",
}


@dataclass
class ResourceEntry:
    """A single discovered resource file."""
    id: str
    name: str
    resource_type: str
    file_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentEntry(ResourceEntry):
    """An agent resource with parsed agent-specific metadata."""
    title: str = ""
    dependencies: dict[str, list[str]] = field(default_factory=dict)


class ResourceRegistry:
    """Registry of all discovered BMAD-style resources."""

    def __init__(self) -> None:
        self._resources: dict[str, dict[str, ResourceEntry]] = {
            "agents": {},
            "tasks": {},
            "templates": {},
            "checklists": {},
            "data": {},
        }

    def add(self, resource_type: str, entry: ResourceEntry) -> None:
        self._resources[resource_type][entry.id] = entry

    def get_agent(self, agent_id: str) -> AgentEntry | None:
        entry = self._resources["agents"].get(agent_id)
        return entry if isinstance(entry, AgentEntry) else None

    def get_resource(self, resource_type: str, resource_id: str) -> ResourceEntry | None:
        return self._resources.get(resource_type, {}).get(resource_id)

    def list_type(self, resource_type: str) -> list[ResourceEntry]:
        return list(self._resources.get(resource_type, {}).values())

    def count(self, resource_type: str) -> int:
        return len(self._resources.get(resource_type, {}))

    @property
    def agents(self) -> dict[str, AgentEntry]:
        return self._resources["agents"]

    def summary(self) -> dict[str, int]:
        return {rt: len(entries) for rt, entries in self._resources.items()}


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown file.

    Returns (metadata_dict, body_text). If no frontmatter, returns ({}, full_content).
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_str = parts[1].strip()
    body = parts[2].strip()

    if not frontmatter_str:
        return {}, body

    try:
        metadata = yaml.safe_load(frontmatter_str)
        if not isinstance(metadata, dict):
            return {}, content
        return metadata, body
    except yaml.YAMLError:
        return {}, content


def _extract_id(metadata: dict, file_path: Path, resource_type: str) -> str:
    """Extract resource id from metadata or derive from filename."""
    # For agents, id is nested under 'agent' key
    if resource_type == "agents" and "agent" in metadata:
        return metadata["agent"].get("id", file_path.stem)
    return metadata.get("id", file_path.stem)


def _extract_name(metadata: dict, file_path: Path, resource_type: str) -> str:
    """Extract resource name from metadata or derive from filename."""
    if resource_type == "agents" and "agent" in metadata:
        return metadata["agent"].get("name", file_path.stem)
    return metadata.get("name", file_path.stem)


def _parse_agent_entry(metadata: dict, body: str, file_path: Path) -> AgentEntry:
    """Parse an agent .md file into an AgentEntry."""
    agent_meta = metadata.get("agent", {})
    persona_meta = metadata.get("persona", {})
    deps = metadata.get("dependencies", {}) or {}

    return AgentEntry(
        id=agent_meta.get("id", file_path.stem),
        name=agent_meta.get("name", file_path.stem),
        title=agent_meta.get("title", ""),
        resource_type="agents",
        file_path=str(file_path),
        metadata={
            "agent": agent_meta,
            "persona": persona_meta,
            "core_principles": metadata.get("core_principles", []),
            "tools": metadata.get("tools", []),
            "inputs": metadata.get("inputs", []),
            "outputs": metadata.get("outputs", []),
            "decision_policy": metadata.get("decision_policy", {}),
        },
        dependencies={
            "tasks": deps.get("tasks", []) or [],
            "templates": deps.get("templates", []) or [],
            "checklists": deps.get("checklists", []) or [],
            "data": deps.get("data", []) or [],
        },
    )


def _parse_resource_entry(
    metadata: dict, file_path: Path, resource_type: str
) -> ResourceEntry:
    """Parse a generic resource .md file into a ResourceEntry."""
    return ResourceEntry(
        id=_extract_id(metadata, file_path, resource_type),
        name=_extract_name(metadata, file_path, resource_type),
        resource_type=resource_type,
        file_path=str(file_path),
        metadata=metadata,
    )


def discover_resources(base_dir: str) -> ResourceRegistry:
    """Scan resource directories and build a ResourceRegistry.

    Args:
        base_dir: Root directory of the sickle project (where agents/, tasks/, etc. live).

    Returns:
        A populated ResourceRegistry.
    """
    registry = ResourceRegistry()
    base = Path(base_dir)

    for resource_type, dir_name in RESOURCE_DIRS.items():
        scan_dir = base / dir_name
        if not scan_dir.exists():
            continue

        for md_file in sorted(scan_dir.glob("*.md")):
            if md_file.name.startswith("README"):
                continue

            content = md_file.read_text(encoding="utf-8")
            metadata, body = _parse_frontmatter(content)

            if resource_type == "agents":
                entry = _parse_agent_entry(metadata, body, md_file)
            else:
                entry = _parse_resource_entry(metadata, md_file, resource_type)

            registry.add(resource_type, entry)

    return registry


def validate_dependencies(registry: ResourceRegistry) -> list[str]:
    """Validate that all agent dependency references resolve.

    Returns a list of warning messages for missing dependencies.
    """
    warnings = []

    for agent_id, agent in registry.agents.items():
        if not isinstance(agent, AgentEntry):
            continue
        for dep_type, dep_ids in agent.dependencies.items():
            for dep_id in dep_ids:
                if registry.get_resource(dep_type, dep_id) is None:
                    msg = (
                        f"Agent '{agent_id}' references {dep_type[:-1]} '{dep_id}' "
                        f"which was not found"
                    )
                    warnings.append(msg)
                    logger.warning(msg)

    return warnings

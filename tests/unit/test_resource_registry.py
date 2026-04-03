"""Tests for config/resource_registry.py — BMAD resource discovery."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from config.resource_registry import (
    AgentEntry,
    ResourceRegistry,
    discover_resources,
    validate_dependencies,
    _parse_frontmatter,
)

PROJECT_ROOT = str(Path(__file__).parent.parent.parent)


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nid: test\nname: Test\n---\n# Body"
        meta, body = _parse_frontmatter(content)
        assert meta["id"] == "test"
        assert body == "# Body"

    def test_no_frontmatter(self):
        content = "# Just a markdown file"
        meta, body = _parse_frontmatter(content)
        assert meta == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n---\n# Body"
        meta, body = _parse_frontmatter(content)
        assert meta == {}
        assert body == "# Body"

    def test_invalid_yaml(self):
        content = "---\n: invalid: yaml: {{{\n---\n# Body"
        meta, body = _parse_frontmatter(content)
        assert meta == {}


class TestDiscoverResources:
    def test_discovers_agents(self):
        """AC1: Scans agents/ for .md files and parses metadata."""
        registry = discover_resources(PROJECT_ROOT)
        assert registry.count("agents") >= 3  # dev, ba, pm agents

    def test_agent_metadata_parsed(self):
        """AC1: Agent id, name, title, dependencies are parsed."""
        registry = discover_resources(PROJECT_ROOT)
        dev = registry.get_agent("dev-agent")
        assert dev is not None
        assert dev.name == "James"
        assert dev.title == "Developer"
        assert "implement-code" in dev.dependencies["tasks"]
        assert "dev-scope-checklist" in dev.dependencies["checklists"]

    def test_agent_is_agent_entry(self):
        """Agents are AgentEntry instances."""
        registry = discover_resources(PROJECT_ROOT)
        dev = registry.get_agent("dev-agent")
        assert isinstance(dev, AgentEntry)

    def test_scans_all_resource_dirs(self):
        """AC2: Scans tasks/, templates/, checklists/, data/ directories."""
        registry = discover_resources(PROJECT_ROOT)
        # These dirs exist but may be empty — that's fine
        summary = registry.summary()
        assert "agents" in summary
        assert "tasks" in summary
        assert "templates" in summary
        assert "checklists" in summary
        assert "data" in summary

    def test_registry_maps_type_and_id(self):
        """AC3: Registry maps resource type + id to file path."""
        registry = discover_resources(PROJECT_ROOT)
        dev = registry.get_resource("agents", "dev-agent")
        assert dev is not None
        assert dev.file_path.endswith("dev-agent.md")

    def test_empty_directories(self, tmp_path):
        """AC6: Empty directories produce zero entries without errors."""
        for d in ["agents", "tasks", "templates", "checklists", "data"]:
            (tmp_path / d).mkdir()
        registry = discover_resources(str(tmp_path))
        assert registry.count("agents") == 0
        assert registry.count("tasks") == 0

    def test_missing_directories(self, tmp_path):
        """Non-existent directories are silently skipped."""
        registry = discover_resources(str(tmp_path))
        assert registry.count("agents") == 0

    def test_readme_skipped(self, tmp_path):
        """README.md files in resource dirs are not treated as resources."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "README.md").write_text("# Agents\nDocumentation.")
        registry = discover_resources(str(tmp_path))
        assert registry.count("agents") == 0

    def test_file_without_frontmatter(self, tmp_path):
        """Files without frontmatter use filename as id."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "my-task.md").write_text("# My Task\nDo the thing.")
        registry = discover_resources(str(tmp_path))
        task = registry.get_resource("tasks", "my-task")
        assert task is not None
        assert task.id == "my-task"

    def test_list_type(self):
        """list_type returns all entries for a resource type."""
        registry = discover_resources(PROJECT_ROOT)
        agents = registry.list_type("agents")
        assert len(agents) >= 3
        ids = {a.id for a in agents}
        assert "dev-agent" in ids
        assert "ba-agent" in ids
        assert "pm-agent" in ids


class TestValidateDependencies:
    def test_missing_dependency_warns(self, tmp_path, caplog):
        """AC4: Missing dependency produces a warning."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "broken-agent.md").write_text(
            "---\nagent:\n  id: broken-agent\n  name: Broken\ndependencies:\n"
            "  tasks:\n    - nonexistent-task\n---\n# Broken"
        )
        registry = discover_resources(str(tmp_path))
        with caplog.at_level(logging.WARNING):
            warnings = validate_dependencies(registry)
        assert len(warnings) > 0
        assert any("nonexistent-task" in w for w in warnings)

    def test_no_warnings_when_all_resolved(self, tmp_path):
        """No warnings when all dependencies exist."""
        agents_dir = tmp_path / "agents"
        tasks_dir = tmp_path / "tasks"
        agents_dir.mkdir()
        tasks_dir.mkdir()

        (agents_dir / "test-agent.md").write_text(
            "---\nagent:\n  id: test-agent\n  name: Test\ndependencies:\n  tasks:\n    - my-task\n---\n# Test"
        )
        (tasks_dir / "my-task.md").write_text("---\nid: my-task\n---\n# Task")

        registry = discover_resources(str(tmp_path))
        warnings = validate_dependencies(registry)
        assert warnings == []

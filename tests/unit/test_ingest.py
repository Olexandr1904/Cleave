"""Tests for orchestrator.ingest — multi-tracker behaviour."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_loaded_project(project_id: str):
    """Build a minimal LoadedProject for ingest tests.
    Mirrors the helper in tests/unit/test_health_runner.py:_make_project."""
    from config.schemas import (
        JiraConfig, LoadedProject, ProjectConfig, ProjectInfo,
        RepoConfig, RepoInfo, TrackerConfig, ParallelismConfig,
    )
    cfg = ProjectConfig(
        project=ProjectInfo(id=project_id, name=project_id, enabled=True),
        tracker=TrackerConfig(
            provider="jira",
            jira=JiraConfig(
                url=f"https://{project_id}.atlassian.net",
                token="t", email="bot@x.com", project_key=project_id.upper(),
                trigger_labels=["ai-pipeline"],
            ),
        ),
        parallelism=ParallelismConfig(max_concurrent_tickets=3),
    )
    repo = RepoConfig(repo=RepoInfo(id="main", name="main"), tracker_label="ai-pipeline")
    return LoadedProject(config=cfg, repos={"main": repo}, config_dir="")


@pytest.mark.asyncio
async def test_poll_isolates_per_tracker_failure(tmp_path):
    """One tracker raising must not block other trackers from being polled."""
    from config.schemas import GlobalConfig, WorkspacesConfig

    tracker_ok = AsyncMock()
    tracker_ok.poll_tickets.return_value = []  # empty — successful, no tickets
    tracker_bad = AsyncMock()
    tracker_bad.poll_tickets.side_effect = RuntimeError("boom")

    proj_a = _make_loaded_project("proj-a")
    proj_b = _make_loaded_project("proj-b")

    from orchestrator.ingest import poll_and_create_workspaces
    workspaces = await poll_and_create_workspaces(
        trackers={"proj-a": tracker_ok, "proj-b": tracker_bad},
        projects={"proj-a": proj_a, "proj-b": proj_b},
        active_workspaces=[],
        global_config=GlobalConfig(workspaces=WorkspacesConfig(base_dir=str(tmp_path))),
        workspace_manager=MagicMock(),
        default_model_provider=lambda: "model",
        repo_vcs={},
        notifier=None,
        dry_run=True,
        event_bus=None,
    )

    tracker_ok.poll_tickets.assert_awaited_once()
    tracker_bad.poll_tickets.assert_awaited_once()
    assert workspaces == []

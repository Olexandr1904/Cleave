"""Tests for Orchestrator hot-reload helpers: register_tracker, rescan_projects, poll_cycle wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_orch(
    projects=None,
    trackers=None,
    on_project_added=None,
    config_dir="/tmp/cfg",
):
    return Orchestrator(
        global_config=MagicMock(defaults=MagicMock(poll_interval_seconds=1)),
        projects=projects if projects is not None else {},
        registry=MagicMock(),
        workflow=MagicMock(),
        workspace_manager=MagicMock(discover_workspaces=lambda: []),
        agent_runtime=MagicMock(),
        default_model_provider=lambda: "claude-sonnet-4-6",
        trackers=trackers,
        vcs=None,
        notifier=None,
        dry_run=False,
        event_bus=None,
        config_dir=config_dir,
        on_project_added=on_project_added,
    )


def test_register_tracker_attaches_after_init():
    orch = _make_orch(trackers=None)
    assert not orch._trackers
    new_tracker = MagicMock()
    orch.register_tracker("acme", new_tracker)
    assert orch._trackers == {"acme": new_tracker}


def test_register_tracker_replaces_existing():
    old = MagicMock(name="old")
    orch = _make_orch(trackers={"acme": old})
    new = MagicMock(name="new")
    orch.register_tracker("acme", new)
    assert orch._trackers["acme"] is new


@pytest.mark.asyncio
async def test_rescan_adds_new_project_and_calls_hook(monkeypatch):
    hook = MagicMock()
    orch = _make_orch(projects={}, on_project_added=hook)

    new_proj = MagicMock(name="loaded_project")

    def fake_load_config(path, **kwargs):
        assert path == "/tmp/cfg"
        return (MagicMock(), {"demo": new_proj})

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == ["demo"]
    assert orch._projects["demo"] is new_proj
    hook.assert_called_once_with("demo", new_proj)


@pytest.mark.asyncio
async def test_rescan_does_not_recall_hook_for_existing_project(monkeypatch):
    hook = MagicMock()
    existing = MagicMock(name="existing_project")
    orch = _make_orch(projects={"demo": existing}, on_project_added=hook)

    def fake_load_config(path, **kwargs):
        return (MagicMock(), {"demo": existing})

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == []
    hook.assert_not_called()


@pytest.mark.asyncio
async def test_rescan_handles_config_error_gracefully(monkeypatch, caplog):
    import logging
    from config.config_loader import ConfigError
    hook = MagicMock()
    orch = _make_orch(projects={}, on_project_added=hook)

    def fake_load_config(path, **kwargs):
        raise ConfigError("bad yaml")

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    with caplog.at_level(logging.WARNING, logger="orchestrator.orchestrator"):
        added = await orch.rescan_projects()

    assert added == []
    assert orch._projects == {}
    hook.assert_not_called()
    assert "Rescan: load_config failed" in caplog.text


@pytest.mark.asyncio
async def test_rescan_handles_unexpected_error_gracefully(monkeypatch, caplog):
    """Non-ConfigError exceptions from load_config (e.g. PermissionError) are also swallowed."""
    import logging
    hook = MagicMock()
    orch = _make_orch(projects={}, on_project_added=hook)

    def fake_load_config(path, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    with caplog.at_level(logging.ERROR, logger="orchestrator.orchestrator"):
        added = await orch.rescan_projects()

    assert added == []
    assert orch._projects == {}
    hook.assert_not_called()
    assert "Rescan: unexpected error" in caplog.text


@pytest.mark.asyncio
async def test_rescan_with_no_hook_still_merges_projects(monkeypatch):
    """on_project_added=None is valid — orchestrator still tracks the new project."""
    orch = _make_orch(projects={}, on_project_added=None)

    new_proj = MagicMock(name="loaded_project")

    def fake_load_config(path, **kwargs):
        return (MagicMock(), {"demo": new_proj})

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == ["demo"]
    assert orch._projects["demo"] is new_proj


@pytest.mark.asyncio
async def test_poll_cycle_calls_rescan(monkeypatch):
    orch = _make_orch()
    orch._rescan_projects_from_disk = AsyncMock(return_value=[])

    await orch.poll_cycle()

    orch._rescan_projects_from_disk.assert_awaited_once()

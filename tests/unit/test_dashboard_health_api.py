from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from health.runner import ProjectHealth
from health.validators import ValidatorResult


@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "test.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def bus():
    return EventBus()


def _fake_projects():
    jira = SimpleNamespace(url="https://x", email="a@b", token="t", project_key="ACME")
    github = SimpleNamespace(token="gh", owner="acme", repo="mb")
    gitlab = SimpleNamespace(token="", url="", project_id="")
    vcs = SimpleNamespace(provider="github", github=github, gitlab=gitlab)
    repo_cfg = SimpleNamespace(vcs=vcs)
    return {"acme": SimpleNamespace(config=SimpleNamespace(jira=jira), repos={"acme-app": repo_cfg})}


class TestProjectsHealthEndpoint:
    def test_returns_all_green(self, bus, store):
        app = create_app(bus, store, projects=_fake_projects())

        fake_result = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(True, "jira", "ACME", "", ""),
                ValidatorResult(True, "github", "acme/mb", "", ""),
            ],
            checked_at=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
        with patch("dashboard.web.check_all", new=AsyncMock(return_value=[fake_result])):
            client = TestClient(app)
            resp = client.get("/api/projects/health")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["projects"]) == 1
        p = data["projects"][0]
        assert p["project_id"] == "acme"
        assert p["status"] == "green"
        assert len(p["checks"]) == 2

    def test_returns_red_when_jira_fails(self, bus, store):
        app = create_app(bus, store, projects=_fake_projects())

        fake_result = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(False, "jira", "ACME", "HTTP 401", "check token"),
                ValidatorResult(True, "github", "acme/mb", "", ""),
            ],
            checked_at=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
        with patch("dashboard.web.check_all", new=AsyncMock(return_value=[fake_result])):
            client = TestClient(app)
            resp = client.get("/api/projects/health")

        data = resp.json()
        assert data["projects"][0]["status"] == "red"
        assert data["projects"][0]["checks"][0]["reason"] == "HTTP 401"

    def test_refresh_param_forces_cache_bypass(self, bus, store):
        app = create_app(bus, store, projects=_fake_projects())
        mock = AsyncMock(return_value=[])
        with patch("dashboard.web.check_all", new=mock):
            client = TestClient(app)
            client.get("/api/projects/health?refresh=1")
        mock.assert_called_once()
        assert mock.call_args.kwargs.get("force") is True

    def test_empty_when_no_projects(self, bus, store):
        app = create_app(bus, store, projects={})
        client = TestClient(app)
        resp = client.get("/api/projects/health")
        assert resp.status_code == 200
        assert resp.json()["projects"] == []

    def test_missing_projects_returns_empty(self, bus, store):
        """No projects= wired in (dashboard without config)."""
        app = create_app(bus, store)
        client = TestClient(app)
        resp = client.get("/api/projects/health")
        assert resp.status_code == 200
        assert resp.json()["projects"] == []

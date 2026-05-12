from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from health.runner import ProjectHealth, HealthRunner, check_project
from health.validators import ValidatorResult


def _make_project(company_id="acme", vcs_provider="github"):
    jira = SimpleNamespace(url="https://acme.atlassian.net", email="a@b", token="t", project_key="ACME")
    github = SimpleNamespace(token="gh_tok", owner="acme", repo="acme-app")
    gitlab = SimpleNamespace(token="", url="", project_id="")
    vcs = SimpleNamespace(provider=vcs_provider, github=github, gitlab=gitlab)
    repo_cfg = SimpleNamespace(vcs=vcs)
    config = SimpleNamespace(tracker=SimpleNamespace(jira=jira))
    return SimpleNamespace(config=config, repos={"acme-app": repo_cfg})


class TestProjectHealthAggregation:
    def test_all_green(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(True, "jira", "ACME", "", ""),
                ValidatorResult(True, "github", "acme/acme-app", "", ""),
                ValidatorResult(True, "git_identity", "/ws", "", ""),
            ],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "green"

    def test_jira_failing_is_red(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[ValidatorResult(False, "jira", "ACME", "401", "check token")],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "red"

    def test_git_identity_failing_is_yellow(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(True, "jira", "ACME", "", ""),
                ValidatorResult(True, "github", "acme/mb", "", ""),
                ValidatorResult(False, "git_identity", "/ws", "missing", "git config ..."),
            ],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "yellow"

    def test_red_beats_yellow(self):
        ph = ProjectHealth(
            project_id="acme",
            checks=[
                ValidatorResult(False, "jira", "ACME", "401", ""),
                ValidatorResult(False, "git_identity", "/ws", "missing", ""),
            ],
            checked_at=datetime.now(timezone.utc),
        )
        assert ph.status == "red"


class TestCheckProject:
    @pytest.mark.asyncio
    async def test_runs_jira_and_github(self):
        proj = _make_project(vcs_provider="github")
        with patch("health.runner.check_jira", new=AsyncMock(return_value=ValidatorResult(True, "jira", "ACME", "", ""))), \
             patch("health.runner.check_github", new=AsyncMock(return_value=ValidatorResult(True, "github", "acme/acme-app", "", ""))):
            ph = await check_project("acme", proj)
        assert ph.project_id == "acme"
        names = {c.name for c in ph.checks}
        assert "jira" in names
        assert "github" in names

    @pytest.mark.asyncio
    async def test_runs_gitlab_when_provider_is_gitlab(self):
        proj = _make_project(vcs_provider="gitlab")
        proj.repos["acme-app"].vcs.gitlab = SimpleNamespace(token="gl", url="https://gl", project_id="42")
        with patch("health.runner.check_jira", new=AsyncMock(return_value=ValidatorResult(True, "jira", "ACME", "", ""))), \
             patch("health.runner.check_gitlab", new=AsyncMock(return_value=ValidatorResult(True, "gitlab", "42", "", ""))):
            ph = await check_project("acme", proj)
        names = {c.name for c in ph.checks}
        assert "gitlab" in names
        assert "github" not in names


class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_cache_hits_within_ttl(self):
        proj = _make_project()
        call_count = {"jira": 0}

        async def fake_jira(*a, **kw):
            call_count["jira"] += 1
            return ValidatorResult(True, "jira", "ACME", "", "")

        with patch("health.runner.check_jira", side_effect=fake_jira), \
             patch("health.runner.check_github", new=AsyncMock(return_value=ValidatorResult(True, "github", "acme/mb", "", ""))):
            runner = HealthRunner(ttl_seconds=60)
            await runner.check_all({"acme": proj})
            await runner.check_all({"acme": proj})
        assert call_count["jira"] == 1

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self):
        proj = _make_project()
        call_count = {"jira": 0}

        async def fake_jira(*a, **kw):
            call_count["jira"] += 1
            return ValidatorResult(True, "jira", "ACME", "", "")

        with patch("health.runner.check_jira", side_effect=fake_jira), \
             patch("health.runner.check_github", new=AsyncMock(return_value=ValidatorResult(True, "github", "acme/mb", "", ""))):
            runner = HealthRunner(ttl_seconds=60)
            await runner.check_all({"acme": proj})
            await runner.check_all({"acme": proj}, force=True)
        assert call_count["jira"] == 2

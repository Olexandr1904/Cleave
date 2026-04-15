from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.e2e.conftest import goto_and_wait_for_board, make_fake_projects


class TestDashboardHealthStrip:
    def test_all_green_shows_collapsed_pill(
        self, page, workspace_seeder, dashboard_server_custom, monkeypatch
    ):
        from health.runner import ProjectHealth
        from health.validators import ValidatorResult

        base, seed = workspace_seeder
        seed("ACME-1", "DEV")

        async def fake_check_all(projects, force=False):
            return [ProjectHealth(
                project_id="acme",
                checks=[
                    ValidatorResult(True, "jira", "ACME", "", ""),
                    ValidatorResult(True, "github", "acme/acme-mobile", "", ""),
                ],
                checked_at=datetime.now(timezone.utc),
            )]

        import dashboard.web
        monkeypatch.setattr(dashboard.web, "check_all", fake_check_all)

        ctx = dashboard_server_custom(projects=make_fake_projects())

        goto_and_wait_for_board(page, ctx["base_url"])
        pill = page.locator(".health-strip.green")
        assert pill.is_visible()
        assert "healthy" in pill.inner_text().lower()

    def test_red_shows_expanded_with_fix_hint(
        self, page, workspace_seeder, dashboard_server_custom, monkeypatch
    ):
        from health.runner import ProjectHealth
        from health.validators import ValidatorResult

        base, seed = workspace_seeder
        seed("ACME-1", "DEV")

        async def fake_check_all(projects, force=False):
            return [ProjectHealth(
                project_id="acme",
                checks=[
                    ValidatorResult(False, "jira", "ACME", "HTTP 401", "Check Jira token"),
                ],
                checked_at=datetime.now(timezone.utc),
            )]

        import dashboard.web
        monkeypatch.setattr(dashboard.web, "check_all", fake_check_all)

        ctx = dashboard_server_custom(projects=make_fake_projects())

        goto_and_wait_for_board(page, ctx["base_url"])
        strip = page.locator(".health-strip.red")
        assert strip.is_visible()
        assert "HTTP 401" in strip.inner_text()
        assert "Check Jira token" in strip.inner_text()

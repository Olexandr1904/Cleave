from __future__ import annotations

from datetime import datetime, timezone

from health.credentials import CredentialStatus
from tests.e2e.conftest import goto_and_wait_for_board, make_fake_projects


class FakeCredentialHealth:
    def __init__(self, statuses):
        self._statuses = statuses

    def snapshot(self):
        return self._statuses

    async def check_all(self):
        return self._statuses


def _status(state: str) -> CredentialStatus:
    now = datetime.now(timezone.utc)
    return CredentialStatus(
        "forge:acme-org/acme-app", "forge", "github", "GitHub acme-org/acme-app",
        "acme-org/acme-app", "GH_T", "vcs.github.token",
        state=state, fix="Set a new GH_T in .env", since=now, checked_at=now,
    )


class TestDashboardCredentialStrip:
    def test_all_ok_hides_strip(
        self, page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("ACME-1", "DEV")
        ctx = dashboard_server_custom(
            projects=make_fake_projects(),
            credential_health=FakeCredentialHealth([_status("ok")]),
        )

        goto_and_wait_for_board(page, ctx["base_url"])
        assert page.locator(".health-strip").count() == 0

    def test_invalid_shows_chip_with_env_var_and_fix(
        self, page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("ACME-1", "DEV")
        ctx = dashboard_server_custom(
            projects=make_fake_projects(),
            credential_health=FakeCredentialHealth([_status("invalid")]),
        )

        goto_and_wait_for_board(page, ctx["base_url"])
        strip = page.locator(".health-strip.red")
        assert strip.is_visible()
        text = strip.inner_text()
        assert "invalid or expired" in text
        assert "GH_T" in text
        assert "Set a new GH_T in .env" in text

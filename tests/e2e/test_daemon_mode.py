"""Daemon status sidebar + mode endpoint."""

from __future__ import annotations

import urllib.request
import json

from playwright.sync_api import Page, expect

from tests.e2e.conftest import goto_and_wait_for_board


class TestDaemonSidebarStatus:
    def test_sidebar_shows_current_mode(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        daemon = page.locator("#daemon-status")
        expect(daemon).to_contain_text("Mode:")
        expect(daemon).to_contain_text("manual")

    def test_sidebar_shows_active_and_blocked_counts(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        daemon = page.locator("#daemon-status")
        expect(daemon).to_contain_text("Active:")
        expect(daemon).to_contain_text("Blocked:")


class TestDaemonStatusEndpoint:
    def test_counts_match_workspace_states(
        self, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("C-1", "DEV")
        seed("C-2", "BLOCKED", previous_state="DEV")
        seed("C-3", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        seed("C-4", "MANUAL_CONTROL", previous_state="DEV")
        seed("C-5", "DONE")
        ctx = dashboard_server_custom()

        resp = urllib.request.urlopen(f"{ctx['base_url']}/api/daemon/status")
        data = json.loads(resp.read())
        assert data["active"] == 5  # get_active_workspaces returns all in the FakeOrchestrator
        assert data["blocked"] == 1
        assert data["awaiting"] == 1
        assert data["manual_control"] == 1
        assert data["mode"] == "manual"


class TestModeEndpoint:
    def test_set_mode_updates_handler(self, dashboard_server: dict):
        req = urllib.request.Request(
            f"{dashboard_server['base_url']}/api/daemon/mode",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"mode": "auto"}).encode(),
        )
        resp = urllib.request.urlopen(req)
        assert resp.status == 200
        assert dashboard_server["mode_handler"].get_mode() == "auto"

    def test_set_invalid_mode_rejected(self, dashboard_server: dict):
        req = urllib.request.Request(
            f"{dashboard_server['base_url']}/api/daemon/mode",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"mode": "bogus"}).encode(),
        )
        try:
            urllib.request.urlopen(req)
            raised = False
        except urllib.error.HTTPError as e:
            raised = True
            assert e.code == 400
        assert raised

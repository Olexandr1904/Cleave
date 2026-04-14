"""Take Control and Release Control flows — the big V2 feature."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e.conftest import goto_and_wait_for_board, wait_for_state_change


def state_path(base: Path, ticket_id: str) -> Path:
    return base / "acme" / "acme-mobile" / "tickets" / ticket_id / "state.json"


class TestTakeControlButton:
    def test_visible_for_active_states(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()  # DEV
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-take-control")).to_be_visible()

    def test_visible_for_blocked(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-3"]').click()  # BLOCKED
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-take-control")).to_be_visible()

    def test_hidden_for_done(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("D-1", "DONE")
        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="D-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-take-control")).to_have_count(0)

    def test_hidden_for_manual_control(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("MC-1", "MANUAL_CONTROL", previous_state="DEV")
        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="MC-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-take-control")).to_have_count(0)


class TestTakeControlNoAgent:
    def test_direct_transition_when_no_agent_running(
        self, page: Page, dashboard_server: dict
    ):
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-1")
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#act-take-control", timeout=3000)
        page.locator("#act-take-control").click()

        after = wait_for_state_change(sp, "DEV")
        assert after["current_state"] == "MANUAL_CONTROL"
        assert after["previous_state"] == "DEV"
        assert after["manual_control_started_at"] is not None


class TestTakeControlWithAgent:
    def test_confirmation_dialog_appears_when_agent_running(
        self, page: Page, dashboard_server: dict
    ):
        # Register a running agent for SPIKE-1
        dashboard_server["orchestrator"]._agent_runtime.register_running(
            "SPIKE-1", "dev-agent"
        )

        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#act-take-control", timeout=3000)
        page.locator("#act-take-control").click()

        # Dialog appears
        page.wait_for_selector(".dialog-overlay", timeout=3000)
        expect(page.locator(".dialog-title")).to_contain_text("Take Control of SPIKE-1")
        expect(page.locator(".dialog")).to_contain_text("Agent is currently running")
        expect(page.locator(".dialog")).to_contain_text("dev-agent")

    def test_cancel_dialog_preserves_state(
        self, page: Page, dashboard_server: dict
    ):
        dashboard_server["orchestrator"]._agent_runtime.register_running(
            "SPIKE-1", "dev-agent"
        )
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-1")
        before = json.loads(sp.read_text())

        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#act-take-control", timeout=3000)
        page.locator("#act-take-control").click()
        page.wait_for_selector(".dialog-overlay", timeout=3000)
        page.locator("#dlg-cancel").click()
        # Dialog gone
        expect(page.locator(".dialog-overlay")).to_have_count(0)

        # State unchanged
        after = json.loads(sp.read_text())
        assert after["current_state"] == before["current_state"] == "DEV"

    def test_confirm_dialog_kills_agent_and_transitions(
        self, page: Page, dashboard_server: dict
    ):
        runtime = dashboard_server["orchestrator"]._agent_runtime
        runtime.register_running("SPIKE-1", "dev-agent")
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-1")

        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#act-take-control", timeout=3000)
        page.locator("#act-take-control").click()
        page.wait_for_selector(".dialog-overlay", timeout=3000)
        page.locator("#dlg-confirm").click()

        after = wait_for_state_change(sp, "DEV")
        assert after["current_state"] == "MANUAL_CONTROL"
        assert "SPIKE-1" in runtime.cancelled


class TestManualControlBanner:
    def test_banner_shown_in_manual_control(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("MC-1", "MANUAL_CONTROL", previous_state="DEV")
        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="MC-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)

        banner = page.locator(".manual-banner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("You have control")
        expect(page.locator("#act-finished")).to_be_visible()
        expect(page.locator("#manual-comment")).to_be_visible()


class TestReleaseControl:
    def test_finished_button_transitions_to_analysis(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("MC-1", "MANUAL_CONTROL", previous_state="DEV")
        ctx = dashboard_server_custom()
        sp = Path(base) / "acme" / "acme-mobile" / "tickets" / "MC-1" / "state.json"

        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="MC-1"]').click()
        page.wait_for_selector("#act-finished", timeout=3000)
        page.locator("#manual-comment").fill("Fixed the thing manually")
        page.locator("#act-finished").click()

        after = wait_for_state_change(sp, "MANUAL_CONTROL")
        assert after["current_state"] == "ANALYSIS"
        assert after["manual_control_comment"] == "Fixed the thing manually"

    def test_finished_with_empty_comment_works(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("MC-2", "MANUAL_CONTROL", previous_state="DEV")
        ctx = dashboard_server_custom()
        sp = Path(base) / "acme" / "acme-mobile" / "tickets" / "MC-2" / "state.json"

        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="MC-2"]').click()
        page.wait_for_selector("#act-finished", timeout=3000)
        page.locator("#act-finished").click()

        after = wait_for_state_change(sp, "MANUAL_CONTROL")
        assert after["current_state"] == "ANALYSIS"

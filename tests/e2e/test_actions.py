"""Approve, Reject, Retry action buttons — full click → POST → state.json round-trip."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e.conftest import goto_and_wait_for_board, wait_for_state_change
from workspace.workspace import Stage


def state_path(base: Path, ticket_id: str, company: str = "acme", repo: str = "acme-app") -> Path:
    return base / company / repo / "tickets" / ticket_id / "state.json"


class TestApproveFromDetail:
    def test_approve_button_visible_only_for_awaiting(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])

        # DEV state — no approve button
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-approve")).to_have_count(0)

        # Back to board, then AWAITING state — approve button present
        page.locator("#back-btn").click()
        page.wait_for_selector(".card[data-ticket]", timeout=3000)
        page.locator('.card[data-ticket="SPIKE-2"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-approve")).to_be_visible()

    def test_approve_transitions_state_on_disk(
        self, page: Page, dashboard_server: dict
    ):
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-2")
        before = json.loads(sp.read_text())
        assert before["current_state"] == "AWAITING_APPROVAL"

        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-2"]').click()
        page.wait_for_selector("#act-approve", timeout=3000)
        page.locator("#act-approve").click()

        after = wait_for_state_change(sp, "AWAITING_APPROVAL")
        assert after["current_state"] in ("ANALYSIS", "DEV", "PUSHED", "DONE")


class TestApproveFromBoardCard:
    """Inline approve button on AWAITING cards in the board view."""

    def test_inline_approve_button_present(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        card = page.locator('.card[data-ticket="SPIKE-2"]')
        expect(card.locator('[data-action="approve"]')).to_be_visible()

    def test_inline_approve_does_not_trigger_navigation(
        self, page: Page, dashboard_server: dict
    ):
        """Clicking the inline approve button should transition state, not
        navigate to detail — event delegation guard must hold."""
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-2")
        goto_and_wait_for_board(page, dashboard_server["base_url"])

        page.locator('.card[data-ticket="SPIKE-2"] [data-action="approve"]').click()
        wait_for_state_change(sp, "AWAITING_APPROVAL")
        # Still on board — detail view did not open
        expect(page.locator("#detail-view")).to_have_count(0)


class TestReject:
    def test_reject_button_visible_for_awaiting(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-2"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-reject")).to_be_visible()

    def test_reject_returns_to_previous_state(
        self, page: Page, dashboard_server: dict
    ):
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-2")
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-2"]').click()
        page.wait_for_selector("#act-reject", timeout=3000)
        page.locator("#act-reject").click()

        after = wait_for_state_change(sp, "AWAITING_APPROVAL")
        # Reject always goes back to previous_state ("ANALYSIS" in seed)
        assert after["current_state"] == "ANALYSIS"


class TestRetry:
    def test_retry_visible_for_blocked(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-3"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-retry")).to_be_visible()

    def test_retry_not_visible_for_active(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()  # DEV
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-retry")).to_have_count(0)

    def test_retry_transitions_blocked_to_previous(
        self, page: Page, dashboard_server: dict
    ):
        sp = state_path(dashboard_server["workspace_dir"], "SPIKE-3")
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-3"]').click()
        page.wait_for_selector("#act-retry", timeout=3000)
        page.locator("#act-retry").click()

        after = wait_for_state_change(sp, "BLOCKED")
        assert after["current_state"] == "DEV"
        assert after["error"] is None

    def test_retry_visible_for_failed(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("F-1", "FAILED", previous_state="QA", error="Build broke")
        # Note: VALID_TRANSITIONS has FAILED terminal, so retry actually can't
        # transition it in the current code. This test just checks the button
        # appears (isBlocked includes FAILED).
        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])
        page.locator('.card[data-ticket="F-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator("#act-retry")).to_be_visible()

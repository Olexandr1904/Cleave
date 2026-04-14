"""Cross-cutting regression tests — the subtle bugs that bit us before."""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.e2e.conftest import goto_and_wait_for_board, wait_for_state_change


class TestCardClickSurvivesAutoRefresh:
    """Regression: clicks on cards must keep working across auto-refresh cycles.

    The original bug: per-card click listeners were bound in doRenderBoard,
    but recursive renderBoard() calls from approve button handlers replaced
    the cards' innerHTML, destroying listeners. The fix was to switch to
    event delegation on #content, which survives innerHTML replacement.
    """

    def test_click_works_after_auto_refresh(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])

        # Wait longer than one auto-refresh tick (5s) to force a re-render.
        # The delegated listener on #content must still fire.
        page.wait_for_timeout(5500)

        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator(".detail-ticket-id")).to_contain_text("SPIKE-1")

    def test_click_works_after_inline_approve_rerender(
        self, page: Page, dashboard_server: dict
    ):
        """The inline approve button on SPIKE-2 calls renderBoard() directly,
        which triggers a re-render. After that, clicking SPIKE-1 should still
        open its detail view.
        """
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        sp = (
            dashboard_server["workspace_dir"]
            / "acme" / "acme-mobile" / "tickets" / "SPIKE-2" / "state.json"
        )

        page.locator('.card[data-ticket="SPIKE-2"] [data-action="approve"]').click()
        wait_for_state_change(sp, "AWAITING_APPROVAL")

        # Now click SPIKE-1 card — should still open detail
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator(".detail-ticket-id")).to_contain_text("SPIKE-1")


class TestNavigationPreservesState:
    def test_board_to_detail_to_board_preserves_cards(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        page.locator("#back-btn").click()
        page.wait_for_selector(".card[data-ticket]", timeout=3000)
        expect(page.locator(".card[data-ticket]")).to_have_count(3)

    def test_board_to_eventlog_and_back(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator("#nav-eventlog").click()
        expect(page.locator("#view-title")).to_contain_text("Event Log")

        page.locator("#nav-board").click()
        page.wait_for_selector(".card[data-ticket]", timeout=3000)
        expect(page.locator(".card[data-ticket]")).to_have_count(3)

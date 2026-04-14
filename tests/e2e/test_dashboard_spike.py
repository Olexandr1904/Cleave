"""Smoke test — fastest path to verify the dashboard stack is healthy.

Runs in ~1s. If this fails, the full e2e suite is going to fail too;
start debugging here.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import goto_and_wait_for_board


class TestSmoke:
    def test_dashboard_boots_and_renders_cards(
        self, page: Page, dashboard_server: dict
    ):
        """Dashboard loads, JS modules execute, board renders cards from disk."""
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        expect(page.locator(".card[data-ticket]")).to_have_count(3)

    def test_clicking_card_opens_detail(self, page: Page, dashboard_server: dict):
        """The card-click → detail-view flow — the bug that prompted this suite."""
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator('.card[data-ticket="SPIKE-1"]').click()
        page.wait_for_selector("#detail-view", timeout=3000)
        expect(page.locator(".detail-ticket-id")).to_contain_text("SPIKE-1")

"""Board view: rendering, sort order, hide-done filter, toolbar stats, sidebar."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import goto_and_wait_for_board
from workspace.workspace import Stage


class TestBoardRendering:
    def test_board_renders_all_cards(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        cards = page.locator(".card[data-ticket]")
        expect(cards).to_have_count(3)

        ticket_ids = cards.evaluate_all("els => els.map(e => e.dataset.ticket)")
        assert set(ticket_ids) == {"SPIKE-1", "SPIKE-2", "SPIKE-3"}

    def test_each_card_shows_state_badge(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        badges = page.locator(".card[data-ticket] .state-badge")
        expect(badges).to_have_count(3)

        badge_texts = badges.evaluate_all("els => els.map(e => e.textContent.trim())")
        assert set(badge_texts) == {"DEV", "AWAITING_APPROVAL", "BLOCKED"}

    def test_blocked_card_has_error_display(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        card = page.locator('.card[data-ticket="SPIKE-3"]')
        expect(card.locator(".card-error")).to_contain_text("Test blocker")

    def test_card_shows_repo_id(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        card = page.locator('.card[data-ticket="SPIKE-1"]')
        expect(card.locator(".card-repo")).to_contain_text("acme-app")


class TestBoardSort:
    """Sort order: BLOCKED → AWAITING → MANUAL → active → NEW → DONE → FAILED → ARCHIVED."""

    def test_blocked_appears_before_awaiting_before_active_before_done(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("A-1", "BLOCKED", previous_state="DEV")
        seed("A-2", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        seed("A-3", "DEV")
        seed("A-4", "DONE")

        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])

        order = page.locator(".card[data-ticket]").evaluate_all(
            "els => els.map(e => e.dataset.ticket)"
        )
        assert order == ["A-1", "A-2", "A-3", "A-4"]

    def test_manual_control_ranks_after_awaiting(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("M-1", "MANUAL_CONTROL", previous_state="DEV")
        seed("M-2", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        seed("M-3", "DEV")

        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])

        order = page.locator(".card[data-ticket]").evaluate_all(
            "els => els.map(e => e.dataset.ticket)"
        )
        assert order == ["M-2", "M-1", "M-3"]


class TestHideDoneToggle:
    def test_toggle_hides_done_failed_archived(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("H-1", "DEV")
        seed("H-2", "DONE")
        seed("H-3", "FAILED")
        seed("H-4", "ARCHIVED")

        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])
        expect(page.locator(".card[data-ticket]")).to_have_count(4)

        page.locator("#toggle-done").check()
        page.wait_for_function(
            "document.querySelectorAll('.card[data-ticket]').length === 1",
            timeout=3000,
        )
        expect(page.locator('.card[data-ticket="H-1"]')).to_be_visible()


class TestToolbarStats:
    def test_stats_show_counts(self, page: Page, dashboard_server: dict):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        stats = page.locator("#toolbar-stats")
        text = stats.text_content() or ""
        assert "3 active" in text
        assert "1 blocked" in text


class TestSidebarProjectList:
    def test_project_list_populated_from_workspaces(
        self, page: Page, dashboard_server: dict
    ):
        """Regression: sidebar project list must come from workspace data,
        not /api/projects (the original v1 bug)."""
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        sidebar = page.locator("#project-list")
        expect(sidebar).to_contain_text("acme")

    def test_clicking_project_filters_board(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("A-1", "DEV", company="acme")
        seed("B-1", "DEV", company="beta")

        ctx = dashboard_server_custom()
        goto_and_wait_for_board(page, ctx["base_url"])
        expect(page.locator(".card[data-ticket]")).to_have_count(2)

        page.locator("#nav-proj-acme").click()
        page.wait_for_function(
            "document.querySelectorAll('.card[data-ticket]').length === 1",
            timeout=3000,
        )
        expect(page.locator('.card[data-ticket="A-1"]')).to_be_visible()

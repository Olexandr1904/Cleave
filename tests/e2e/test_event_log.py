"""Event log view — rendering, filter, timestamps."""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from dashboard.events import Event

from tests.e2e.conftest import goto_and_wait_for_board


class TestEventLogView:
    def test_clicking_nav_switches_to_event_log(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator("#nav-eventlog").click()
        expect(page.locator("#view-title")).to_contain_text("Event Log")

    def test_empty_event_log_shows_no_events_message(
        self, page: Page, dashboard_server: dict
    ):
        goto_and_wait_for_board(page, dashboard_server["base_url"])
        page.locator("#nav-eventlog").click()
        expect(page.locator("#content")).to_contain_text("No events found")


class TestEventLogWithEvents:
    def _seed_events(self, dashboard_server_custom):
        events = [
            Event(
                event_type="agent_dispatched",
                message="BA agent started on T-1",
                ticket_id="T-1",
                project_id="acme",
                agent_id="ba-agent",
                timestamp=time.time() - 60,
            ),
            Event(
                event_type="agent_completed",
                message="BA agent finished on T-1",
                ticket_id="T-1",
                project_id="acme",
                agent_id="ba-agent",
                timestamp=time.time() - 30,
            ),
            Event(
                event_type="stage_transition",
                message="T-1 moved ANALYSIS→DEV",
                ticket_id="T-1",
                project_id="acme",
                timestamp=time.time() - 10,
            ),
        ]
        return dashboard_server_custom(events=events)

    def test_events_render_with_badges(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        ctx = self._seed_events(dashboard_server_custom)
        page.goto(ctx["base_url"])
        page.wait_for_selector("#nav-eventlog", timeout=3000)
        page.locator("#nav-eventlog").click()

        page.wait_for_selector(".event-row", timeout=3000)
        rows = page.locator(".event-row")
        expect(rows).to_have_count(3)

        badges = page.locator(".event-badge")
        badge_texts = badges.evaluate_all("els => els.map(e => e.textContent.trim())")
        assert "agent_dispatched" in badge_texts
        assert "agent_completed" in badge_texts
        assert "stage_transition" in badge_texts

    def test_filter_dropdown_filters_events(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        ctx = self._seed_events(dashboard_server_custom)
        page.goto(ctx["base_url"])
        page.wait_for_selector("#nav-eventlog", timeout=3000)
        page.locator("#nav-eventlog").click()
        page.wait_for_selector(".event-row", timeout=3000)

        page.locator("#filter-type").select_option("stage_transition")
        page.wait_for_function(
            "document.querySelectorAll('.event-row').length === 1",
            timeout=3000,
        )
        expect(page.locator(".event-badge")).to_contain_text("stage_transition")

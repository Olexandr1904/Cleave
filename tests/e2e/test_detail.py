"""Detail view: header, pipeline bar, info, reports, events timeline."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from orchestrator.constants import RUNTIME_OUTPUT_BA
from tests.e2e.conftest import goto_and_wait_for_board, make_fake_projects
from workspace.workspace import Stage


def open_detail(page: Page, base_url: str, ticket_id: str) -> None:
    goto_and_wait_for_board(page, base_url)
    page.locator(f'.card[data-ticket="{ticket_id}"]').click()
    page.wait_for_selector("#detail-view", timeout=3000)


class TestDetailHeader:
    def test_header_shows_ticket_id_and_badge(self, page: Page, dashboard_server: dict):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")
        expect(page.locator(".detail-ticket-id")).to_contain_text("SPIKE-1")
        expect(page.locator(".detail-header .state-badge")).to_contain_text("DEV")

    def test_back_button_returns_to_board(self, page: Page, dashboard_server: dict):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")
        page.locator("#back-btn").click()
        page.wait_for_selector(".card[data-ticket]", timeout=3000)
        expect(page.locator(".card[data-ticket]")).to_have_count(3)


class TestDetailPipelineBar:
    def test_pipeline_stages_render(self, page: Page, dashboard_server: dict):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")
        stages = page.locator(".pipeline-stage")
        # 8 stages: NEW, ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW, DONE
        expect(stages).to_have_count(8)

    def test_current_stage_highlighted_for_active_state(
        self, page: Page, dashboard_server: dict
    ):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")  # DEV state
        current = page.locator(".stage-dot.current")
        expect(current).to_have_count(1)


class TestDetailInfoSection:
    def test_info_grid_shows_branch_and_repo(self, page: Page, dashboard_server: dict):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")
        info = page.locator(".info-grid")
        expect(info).to_contain_text("feature/spike-1")
        expect(info).to_contain_text("acme-app")
        expect(info).to_contain_text("acme")

    def test_error_panel_shown_for_blocked_state(
        self, page: Page, dashboard_server: dict
    ):
        open_detail(page, dashboard_server["base_url"], "SPIKE-3")
        # The error panel has text "Error / Escalation" and shows the error
        info_area = page.locator("#detail-view")
        expect(info_area).to_contain_text("Test blocker")


class TestDetailReports:
    def test_report_tabs_render_for_reports_and_meta(
        self, page: Page, dashboard_server: dict
    ):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")
        tabs = page.locator("#report-tabs .tab-btn")
        # SPIKE-1 has 1 report (RUNTIME_OUTPUT_BA) + 1 meta (ticket.md)
        expect(tabs).to_have_count(2)

    def test_clicking_report_tab_loads_content(
        self, page: Page, dashboard_server: dict
    ):
        open_detail(page, dashboard_server["base_url"], "SPIKE-1")
        page.locator(f'.tab-btn[data-file="{RUNTIME_OUTPUT_BA}"]').click()
        area = page.locator("#report-content-area")
        expect(area).to_contain_text("Test report content")


class TestDetailExternalLinks:
    """Backend should build Jira/repo/PR links from project config and the
    detail page should render them in the Info section."""

    def test_jira_and_repo_links_render(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("ACME-42", "DEV")
        ctx = dashboard_server_custom(projects=make_fake_projects())

        open_detail(page, ctx["base_url"], "ACME-42")
        info = page.locator("#detail-view .info-grid")
        expect(info).to_contain_text("Links")

        jira_link = page.locator(".info-link-jira")
        expect(jira_link).to_have_attribute(
            "href", "https://acme.atlassian.net/browse/ACME-42"
        )
        expect(jira_link).to_contain_text("ACME-42")

        repo_link = page.locator(".info-link-repo")
        expect(repo_link).to_have_attribute(
            "href", "https://github.com/acme/acme-app"
        )

    def test_pr_link_rendered_when_pr_url_set(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed(
            "ACME-99", "PR_REVIEW",
            pr_url="https://github.com/acme/acme-app/pull/123",
        )
        ctx = dashboard_server_custom(projects=make_fake_projects())

        open_detail(page, ctx["base_url"], "ACME-99")
        pr_link = page.locator(".info-link-pr")
        expect(pr_link).to_have_attribute(
            "href", "https://github.com/acme/acme-app/pull/123"
        )

    def test_no_links_section_without_project_config(
        self, page: Page, workspace_seeder, dashboard_server_custom
    ):
        base, seed = workspace_seeder
        seed("PLAIN-1", "DEV")
        ctx = dashboard_server_custom()  # no projects → no links

        open_detail(page, ctx["base_url"], "PLAIN-1")
        # Info section is present, but Links row should not be
        expect(page.locator(".info-grid")).not_to_contain_text("Links")

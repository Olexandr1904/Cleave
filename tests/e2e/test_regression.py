"""Cross-cutting regression tests — the subtle bugs that bit us before."""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.e2e.conftest import _seed_workspace, goto_and_wait_for_board, wait_for_state_change
from workspace.workspace import Stage


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
            / "acme" / "acme-app" / "tickets" / "SPIKE-2" / "state.json"
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


class TestPushVerificationRegression:
    """Regression: push stage must verify the branch is on the remote.

    Before the fix, action stages bypassed stage_verifier.verify(). A push
    that silently failed would leave the workspace in PR_REVIEW (or PUSHED)
    without detecting that no code was actually pushed.
    """

    def _init_repo(self, ws_path):
        import subprocess
        source = ws_path / "source"
        source.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=source, check=True)
        subprocess.run(["git", "checkout", "-b", "feature/test-push"], cwd=source, check=True)
        (source / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=source, check=True)

    def test_push_not_on_remote_lands_in_blocked(self, tmp_path):
        """create_pr reports success but ls-remote finds nothing → BLOCKED."""
        ws_path = _seed_workspace(tmp_path, "T-PUSH-1", "PUSHED", previous_state="QA")
        self._init_repo(ws_path)

        from workspace.workspace import Workspace
        ws = Workspace(str(ws_path))

        from orchestrator.stage_verifier import verify, capture_stage_start
        start = capture_stage_start(ws, "push")
        result = verify("push", ws, start)

        assert result.ok is False
        assert "branch not pushed" in result.reason or "ls-remote" in result.reason

    def test_push_succeeds_when_branch_on_remote(self, tmp_path):
        """Positive case: branch IS on remote → verify passes."""
        import subprocess

        ws_path = _seed_workspace(tmp_path, "T-PUSH-2", "PUSHED", previous_state="QA")
        source = ws_path / "source"
        source.mkdir(exist_ok=True)

        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=source, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
        subprocess.run(["git", "checkout", "-b", "feature/t-push-2"], cwd=source, check=True)
        (source / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=source, check=True)
        subprocess.run(
            ["git", "push", "-u", "origin", "feature/t-push-2"],
            cwd=source, check=True,
        )

        from workspace.workspace import Workspace
        ws = Workspace(str(ws_path))

        from orchestrator.stage_verifier import verify, capture_stage_start
        start = capture_stage_start(ws, "push")
        result = verify("push", ws, start)

        assert result.ok is True

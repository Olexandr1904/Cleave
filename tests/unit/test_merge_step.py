"""Tests for orchestrator/merge_step.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.schemas import GitHubConfig, JiraConfig, JiraStatusesConfig, RepoConfig, RepoInfo, VCSConfig
from integrations.base.vcs import PRStatus
from orchestrator.merge_step import merge_pr
from workspace.workspace import Workspace, WorkspaceState


@pytest.fixture
def workspace(tmp_path):
    ws_root = tmp_path / "test-ws"
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()

    state = WorkspaceState(
        ticket_id="TEST-42",
        company_id="test-project",
        repo_id="test-repo",
        workspace_root=str(ws_root),
        branch="feature/TEST-42-login",
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
        current_state="PR_REVIEW",
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


@pytest.fixture
def repo_config():
    return RepoConfig(
        repo=RepoInfo(id="test-repo"),
        vcs=VCSConfig(github=GitHubConfig(merge_method="squash")),
        jira=JiraConfig(statuses=JiraStatusesConfig(done="Done")),
    )


@pytest.fixture
def mock_vcs():
    vcs = AsyncMock()
    vcs.check_pr_status.return_value = PRStatus(all_passing=True, checks=[])
    return vcs


@pytest.fixture
def mock_tracker():
    return AsyncMock()


@pytest.fixture
def mock_notifier():
    return AsyncMock()


class TestMergePR:
    async def test_successful_merge(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")

        result = await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        assert result.success is True
        assert result.merged is True
        mock_vcs.merge_pr.assert_called_once_with(42, "squash")
        assert workspace.state.current_state == "DONE"

    async def test_jira_transitioned_to_done(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")

        await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        mock_tracker.transition_ticket.assert_called_once_with("TEST-42", "Done")
        mock_tracker.add_comment.assert_called_once()

    async def test_telegram_notification(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")

        await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        mock_notifier.send_message.assert_called_once()
        msg = mock_notifier.send_message.call_args[0][0]
        assert "TEST-42" in msg

    async def test_no_pr_number_fails(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        workspace.update_state(pr_number=None)

        result = await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        assert result.success is False
        assert "No PR number" in result.error

    async def test_no_scope_certificate_fails(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        result = await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        assert result.success is False
        assert result.failed_gate == "scope_certificate"

    async def test_ci_checks_failing(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_vcs.check_pr_status.return_value = PRStatus(
            all_passing=False,
            checks=[{"name": "tests", "passing": False}],
        )

        result = await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        assert result.success is False
        assert result.failed_gate == "ci_checks"

    async def test_merge_conflict(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_vcs.merge_pr.side_effect = Exception("Merge conflict detected")

        result = await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        assert result.success is False
        assert result.failed_gate == "merge_conflict"

    async def test_jira_failure_non_blocking(self, workspace, mock_vcs, mock_tracker, mock_notifier, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_tracker.transition_ticket.side_effect = Exception("Jira down")

        result = await merge_pr(workspace, mock_vcs, mock_tracker, mock_notifier, repo_config)

        assert result.success is True
        assert result.merged is True

    async def test_no_notifier(self, workspace, mock_vcs, mock_tracker, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")

        result = await merge_pr(workspace, mock_vcs, mock_tracker, None, repo_config)

        assert result.success is True

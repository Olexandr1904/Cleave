"""Tests for orchestrator/pr_creation.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.schemas import GitHubConfig, JiraConfig, JiraStatusesConfig, RepoConfig, RepoInfo, VCSConfig
from orchestrator.pr_creation import PRCreationResult, create_pr
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
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


@pytest.fixture
def repo_config():
    return RepoConfig(
        repo=RepoInfo(id="test-repo"),
        vcs=VCSConfig(github=GitHubConfig(default_branch="main")),
        jira=JiraConfig(statuses=JiraStatusesConfig(in_review="In Review")),
    )


@pytest.fixture
def mock_vcs():
    vcs = AsyncMock()
    vcs.open_pr.return_value = (42, "https://github.com/org/repo/pull/42")
    return vcs


@pytest.fixture
def mock_tracker():
    return AsyncMock()


class TestCreatePR:
    async def test_successful_pr_creation(self, workspace, mock_vcs, mock_tracker, repo_config):
        # Create scope certificate
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is True
        assert result.pr_number == 42
        assert "pull/42" in result.pr_url

        # Verify push was called
        mock_vcs.push.assert_called_once()

        # Verify PR was opened
        mock_vcs.open_pr.assert_called_once()
        call_kwargs = mock_vcs.open_pr.call_args
        assert "TEST-42" in call_kwargs.kwargs.get("title", call_kwargs.args[0] if call_kwargs.args else "")

        # Verify state updated
        assert workspace.state.pr_number == 42
        assert workspace.state.pr_url == "https://github.com/org/repo/pull/42"

    async def test_jira_transition(self, workspace, mock_vcs, mock_tracker, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")

        await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        mock_tracker.transition_ticket.assert_called_once_with("TEST-42", "In Review")
        mock_tracker.add_comment.assert_called_once()
        comment = mock_tracker.add_comment.call_args[0][1]
        assert "pull/42" in comment

    async def test_no_branch_fails(self, workspace, mock_vcs, mock_tracker, repo_config):
        workspace.update_state(branch=None)

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        assert "No branch" in result.error

    async def test_no_scope_certificate_fails(self, workspace, mock_vcs, mock_tracker, repo_config):
        # Don't create scope certificate
        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        assert "Scope certificate" in result.error

    async def test_push_failure(self, workspace, mock_vcs, mock_tracker, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_vcs.push.side_effect = Exception("Push rejected")

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        assert "Push rejected" in result.error

    async def test_jira_failure_non_blocking(self, workspace, mock_vcs, mock_tracker, repo_config):
        """Jira transition failure should not block PR creation."""
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_tracker.transition_ticket.side_effect = Exception("Jira down")

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        # PR creation succeeds even if Jira fails
        assert result.success is True
        assert result.pr_number == 42

    async def test_custom_pr_template(self, workspace, mock_vcs, mock_tracker, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        repo_config.pr_description_template = "Ticket: {ticket_id}\nURL: {ticket_url}"

        await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        call_args = mock_vcs.open_pr.call_args
        body = call_args.kwargs.get("body", call_args.args[1] if len(call_args.args) > 1 else "")
        assert "TEST-42" in body

    async def test_ticket_summary_from_context(self, workspace, mock_vcs, mock_tracker, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        (workspace.meta_dir / "ticket.json").write_text('{"summary": "Add login feature"}')

        await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        call_args = mock_vcs.open_pr.call_args
        title = call_args.kwargs.get("title", call_args.args[0] if call_args.args else "")
        assert "login feature" in title

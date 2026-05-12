"""Tests for orchestrator/pr_creation.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.schemas import GitHubConfig, JiraConfig, JiraStatusesConfig, RepoConfig, RepoInfo, TrackerConfig, VCSConfig
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
        tracker=TrackerConfig(jira=JiraConfig(statuses=JiraStatusesConfig(in_review="In Review"))),
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

    async def test_no_scope_check_fails(self, workspace, mock_vcs, mock_tracker, repo_config):
        # Don't create scope certificate or report
        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        assert "Scope check" in result.error

    async def test_push_failure(self, workspace, mock_vcs, mock_tracker, repo_config):
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_vcs.push.side_effect = Exception("Push rejected")

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        assert "Push rejected" in result.error

    async def test_skip_pre_push_hook_passes_through(self, workspace, mock_vcs, mock_tracker, repo_config):
        """When the project sets vcs.skip_pre_push_hook=true, push() must
        be called with skip_hooks=True so git push gets --no-verify."""
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        repo_config.vcs.skip_pre_push_hook = True

        await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        mock_vcs.push.assert_called_once()
        assert mock_vcs.push.call_args.kwargs.get("skip_hooks") is True

    async def test_default_does_not_skip_hooks(self, workspace, mock_vcs, mock_tracker, repo_config):
        """Default config: push goes through with skip_hooks=False so any
        project-installed pre-push hook still runs."""
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        # repo_config.vcs.skip_pre_push_hook is False by default

        await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        mock_vcs.push.assert_called_once()
        assert mock_vcs.push.call_args.kwargs.get("skip_hooks") is False

    async def test_environmental_hook_failure_retries_with_no_verify(
        self, workspace, mock_vcs, mock_tracker, repo_config,
    ):
        """When push fails with a Gradle/AAPT2 toolchain signature, the
        pipeline must retry with --no-verify rather than fail the workspace.
        A warning event is emitted so the operator sees what happened."""
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        # Simulate the real-world AAPT2 architecture-mismatch failure
        gradle_err = (
            "Git command failed: git -C /tmp/repo push -u origin feature/X\n"
            "AAPT2 aapt2-8.6.1-11315950-linux Daemon #0: Unexpected error output: "
            "/home/admin0/.gradle/caches/8.14.1/transforms/abc/transformed/"
            "aapt2-8.6.1-11315950-linux/aapt2: 2: Syntax error: \"(\" unexpected"
        )
        mock_vcs.push.side_effect = [
            RuntimeError(gradle_err),  # first attempt — hook trips on AAPT2
            None,                       # retry succeeds with --no-verify
        ]
        events_emitted: list[tuple[str, dict]] = []

        class _Bus:
            def emit(self, event_type: str, message: str, **kwargs):
                events_emitted.append((event_type, kwargs.get("data", {})))

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config, event_bus=_Bus())

        # Pipeline did NOT fail — PR creation succeeded after the bypass
        assert result.success is True
        # push was called twice: first without bypass, then with skip_hooks=True
        assert mock_vcs.push.call_count == 2
        assert mock_vcs.push.call_args_list[0].kwargs.get("skip_hooks") is False
        assert mock_vcs.push.call_args_list[1].kwargs.get("skip_hooks") is True
        # A push_hook_bypassed event was emitted carrying the original reason
        bypass_events = [e for e in events_emitted if e[0] == "push_hook_bypassed"]
        assert len(bypass_events) == 1
        assert "AAPT2" in bypass_events[0][1].get("reason", "")

    async def test_real_detekt_failure_does_not_silently_bypass(
        self, workspace, mock_vcs, mock_tracker, repo_config,
    ):
        """A genuine detekt code-quality finding looks nothing like the
        environmental signatures. The pipeline must surface it as a normal
        push failure (not silently skip)."""
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        mock_vcs.push.side_effect = RuntimeError(
            "detekt failed\n"
            "MagicNumber - 5:14 - This expression contains a magic number"
        )

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        # No bypass attempt — push only called once
        assert mock_vcs.push.call_count == 1
        assert "detekt failed" in result.error or "MagicNumber" in result.error

    async def test_explicit_skip_hooks_does_not_double_bypass(
        self, workspace, mock_vcs, mock_tracker, repo_config,
    ):
        """If the operator already configured skip_pre_push_hook=True and the
        push STILL fails, do not retry — that's a real failure on top of the
        bypass and the operator needs to see it."""
        (workspace.meta_dir / "scope-certificate.md").write_text("PASS")
        repo_config.vcs.skip_pre_push_hook = True
        mock_vcs.push.side_effect = RuntimeError(
            "AAPT2 aapt2-8.6.1-linux Daemon #0: Daemon startup failed"
        )

        result = await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        assert result.success is False
        # Single push attempt (no retry — already running with --no-verify)
        assert mock_vcs.push.call_count == 1

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
        (workspace.meta_dir / "ticket.md").write_text("# TEST-42: Add login feature\n\n**URL:** http://jira/TEST-42")

        await create_pr(workspace, mock_vcs, mock_tracker, repo_config)

        call_args = mock_vcs.open_pr.call_args
        title = call_args.kwargs.get("title", call_args.args[0] if call_args.args else "")
        assert "login feature" in title

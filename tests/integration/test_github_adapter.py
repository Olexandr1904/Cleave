"""Integration tests for GitHub adapter with mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from integrations.github.github_adapter import GitHubAdapter


@pytest.fixture
def adapter():
    return GitHubAdapter(token="fake-token", owner="test-org", repo="test-repo")


class TestGitHubOpenPR:
    @respx.mock
    async def test_open_pr(self, adapter):
        respx.post(
            "https://api.github.com/repos/test-org/test-repo/pulls",
        ).mock(return_value=httpx.Response(201, json={
            "number": 42,
            "html_url": "https://github.com/test-org/test-repo/pull/42",
        }))

        pr_number, pr_url = await adapter.open_pr(
            title="feat(TEST-123): Add login",
            body="## Summary\nLogin button",
            head_branch="feature/TEST-123-login",
            base_branch="develop",
        )
        assert pr_number == 42
        assert "pull/42" in pr_url


class TestGitHubPRComments:
    @respx.mock
    async def test_get_comments(self, adapter):
        respx.get(
            "https://api.github.com/repos/test-org/test-repo/pulls/42/comments",
        ).mock(return_value=httpx.Response(200, json=[
            {
                "id": 1,
                "body": "Consider using const here",
                "path": "src/login.ts",
                "line": 10,
                "user": {"login": "copilot"},
                "in_reply_to_id": None,
            }
        ]))

        comments = await adapter.get_pr_comments(42)
        assert len(comments) == 1
        assert comments[0].body == "Consider using const here"
        assert comments[0].author == "copilot"


class TestGitHubPRStatus:
    @respx.mock
    async def test_check_status_passing(self, adapter):
        respx.get(
            "https://api.github.com/repos/test-org/test-repo/pulls/42",
        ).mock(return_value=httpx.Response(200, json={
            "head": {"sha": "abc123"},
        }))
        respx.get(
            "https://api.github.com/repos/test-org/test-repo/commits/abc123/check-runs",
        ).mock(return_value=httpx.Response(200, json={
            "check_runs": [
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "test", "status": "completed", "conclusion": "success"},
            ]
        }))

        status = await adapter.check_pr_status(42)
        assert status.all_passing is True


class TestGitHubMerge:
    @respx.mock
    async def test_merge_pr(self, adapter):
        respx.put(
            "https://api.github.com/repos/test-org/test-repo/pulls/42/merge",
        ).mock(return_value=httpx.Response(200, json={"merged": True}))

        await adapter.merge_pr(42, merge_method="squash")

    @respx.mock
    async def test_close_pr(self, adapter):
        respx.patch(
            "https://api.github.com/repos/test-org/test-repo/pulls/42",
        ).mock(return_value=httpx.Response(200, json={"state": "closed"}))

        await adapter.close_pr(42)

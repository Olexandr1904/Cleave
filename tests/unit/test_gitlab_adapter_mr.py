"""Tests for GitLabAdapter MR / discussion / pipeline methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.base.vcs import PRComment
from integrations.gitlab.gitlab_adapter import GitLabAdapter


def _make_adapter() -> GitLabAdapter:
    adapter = GitLabAdapter.__new__(GitLabAdapter)
    adapter._token = "t"
    adapter._project_id = "group/proj"
    adapter._url = "https://gitlab.com"
    adapter._project_path = "group%2Fproj"
    adapter._client = MagicMock()
    adapter._discussion_cache = {}
    return adapter


@pytest.mark.asyncio
async def test_open_pr_posts_correct_payload_and_returns_iid_and_url():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value={
        "iid": 42,
        "web_url": "https://gitlab.com/group/proj/-/merge_requests/42",
    })

    iid, url = await adapter.open_pr(
        title="Add feature X",
        body="Implements ACME-123",
        head_branch="feature/x",
        base_branch="develop",
    )

    assert iid == 42
    assert url == "https://gitlab.com/group/proj/-/merge_requests/42"
    adapter._request.assert_awaited_once()
    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "POST"
    assert path == "/projects/group%2Fproj/merge_requests"
    assert kwargs["json"] == {
        "source_branch": "feature/x",
        "target_branch": "develop",
        "title": "Add feature X",
        "description": "Implements ACME-123",
    }


@pytest.mark.asyncio
async def test_find_pr_by_branch_returns_first_open_match():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value=[
        {"iid": 7, "web_url": "https://gitlab.com/group/proj/-/merge_requests/7"},
    ])

    result = await adapter.find_pr_by_branch("feature/x")
    assert result == (7, "https://gitlab.com/group/proj/-/merge_requests/7")
    kwargs = adapter._request.await_args.kwargs
    assert kwargs["params"] == {"source_branch": "feature/x", "state": "opened"}


@pytest.mark.asyncio
async def test_find_pr_by_branch_returns_none_when_empty():
    adapter = _make_adapter()
    adapter._request = AsyncMock(return_value=[])
    assert await adapter.find_pr_by_branch("feature/x") is None


@pytest.mark.asyncio
async def test_find_pr_by_branch_swallows_errors_and_returns_none():
    adapter = _make_adapter()
    adapter._request = AsyncMock(side_effect=RuntimeError("boom"))
    assert await adapter.find_pr_by_branch("feature/x") is None

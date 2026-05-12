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


def _diff_note(note_id: int, body: str, path: str = "a.py", line: int = 5):
    return {
        "id": note_id,
        "body": body,
        "author": {"username": "alice"},
        "position": {"new_path": path, "new_line": line},
    }


def _plain_note(note_id: int, body: str):
    return {
        "id": note_id,
        "body": body,
        "author": {"username": "alice"},
        # No "position" => general MR note, must be filtered out
    }


@pytest.mark.asyncio
async def test_get_pr_comments_returns_only_diff_position_notes():
    adapter = _make_adapter()
    discussions_page = [
        {
            "id": "abc123",
            "notes": [_diff_note(101, "fix indentation"), _diff_note(102, "rename var")],
        },
        {
            "id": "def456",
            "notes": [_plain_note(200, "LGTM overall")],
        },
    ]
    # 2 pages: first returns 2 discussions, second returns empty -> stop
    adapter._request = AsyncMock(side_effect=[discussions_page, []])

    comments = await adapter.get_pr_comments(42)

    assert len(comments) == 2
    assert all(isinstance(c, PRComment) for c in comments)
    assert {c.id for c in comments} == {101, 102}
    assert comments[0].path == "a.py"
    assert comments[0].line == 5
    assert comments[0].author == "alice"


@pytest.mark.asyncio
async def test_get_pr_comments_populates_discussion_cache():
    adapter = _make_adapter()
    discussions_page = [
        {"id": "disc-a", "notes": [_diff_note(11, "x")]},
        {"id": "disc-b", "notes": [_diff_note(22, "y"), _diff_note(23, "z")]},
    ]
    adapter._request = AsyncMock(side_effect=[discussions_page, []])

    await adapter.get_pr_comments(42)
    cache = adapter._discussion_cache[42]
    assert cache == {11: "disc-a", 22: "disc-b", 23: "disc-b"}


@pytest.mark.asyncio
async def test_get_pr_comments_paginates_until_short_page():
    """Loop until a page returns fewer than 100 items."""
    adapter = _make_adapter()
    page1 = [{"id": f"d{i}", "notes": [_diff_note(i, "x")]} for i in range(100)]
    page2 = [{"id": "last", "notes": [_diff_note(999, "z")]}]
    adapter._request = AsyncMock(side_effect=[page1, page2])

    comments = await adapter.get_pr_comments(42)
    assert len(comments) == 101
    assert adapter._request.await_count == 2


@pytest.mark.asyncio
async def test_reply_to_comment_uses_cached_discussion_id():
    adapter = _make_adapter()
    adapter._discussion_cache = {42: {101: "disc-X"}}
    adapter._request = AsyncMock(return_value={})

    await adapter.reply_to_comment(42, 101, "looks good")

    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "POST"
    assert path == "/projects/group%2Fproj/merge_requests/42/discussions/disc-X/notes"
    assert kwargs["json"] == {"body": "looks good"}


@pytest.mark.asyncio
async def test_reply_to_comment_refetches_discussions_on_cache_miss():
    adapter = _make_adapter()
    # Cache is empty — adapter must refetch and then post.
    discussions_page = [
        {"id": "disc-Y", "notes": [
            {"id": 555, "body": "x", "author": {}, "position": {"new_path": "f", "new_line": 1}},
        ]},
    ]
    # Sequence: discussions GET (page 1, short → break), then POST reply
    adapter._request = AsyncMock(side_effect=[discussions_page, {}])

    await adapter.reply_to_comment(42, 555, "thanks")

    assert adapter._request.await_count == 2
    final_call = adapter._request.await_args_list[-1]
    method, path = final_call.args[:2]
    assert method == "POST"
    assert "/discussions/disc-Y/notes" in path


@pytest.mark.asyncio
async def test_reply_to_comment_raises_on_hard_miss():
    adapter = _make_adapter()
    # Refetch returns no matching note; adapter must raise.
    adapter._request = AsyncMock(side_effect=[[], []])  # both pages empty

    with pytest.raises(RuntimeError) as exc:
        await adapter.reply_to_comment(42, 9999, "hi")
    assert "9999" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_comment_uses_cached_discussion_id():
    adapter = _make_adapter()
    adapter._discussion_cache = {42: {101: "disc-X"}}
    adapter._request = AsyncMock(return_value={})

    await adapter.resolve_comment(42, 101)

    method, path = adapter._request.await_args.args[:2]
    kwargs = adapter._request.await_args.kwargs
    assert method == "PUT"
    assert path == "/projects/group%2Fproj/merge_requests/42/discussions/disc-X"
    assert kwargs["params"] == {"resolved": "true"}

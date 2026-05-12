"""Tests for GitLabAdapter git subprocess wrappers: clone, branch, push."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.gitlab.gitlab_adapter import GitLabAdapter


def _make_adapter() -> GitLabAdapter:
    adapter = GitLabAdapter.__new__(GitLabAdapter)
    adapter._token = "glpat_TOKEN"
    adapter._project_id = "group/proj"
    adapter._url = "https://gitlab.com"
    adapter._project_path = "group%2Fproj"
    adapter._client = MagicMock()
    adapter._discussion_cache = {}
    return adapter


def _ok_proc():
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


@pytest.mark.asyncio
async def test_clone_repo_runs_git_clone():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.clone_repo("https://gitlab.com/group/proj.git", "/tmp/r")
    cmd = list(spawn.call_args.args)
    assert cmd[:2] == ["git", "clone"]
    assert cmd[-2:] == ["https://gitlab.com/group/proj.git", "/tmp/r"]


@pytest.mark.asyncio
async def test_clone_repo_with_depth_adds_flag():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.clone_repo("https://gitlab.com/g/p.git", "/tmp/r", depth=1)
    cmd = list(spawn.call_args.args)
    assert "--depth" in cmd
    assert "1" in cmd


@pytest.mark.asyncio
async def test_create_branch_runs_checkout_b():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.create_branch("/tmp/r", "feature/X")
    cmd = list(spawn.call_args.args)
    assert cmd == ["git", "-C", "/tmp/r", "checkout", "-b", "feature/X"]

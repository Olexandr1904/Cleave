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


@pytest.mark.asyncio
async def test_push_default_no_force_no_no_verify():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X")
    calls = [list(c.args) for c in spawn.call_args_list]
    assert len(calls) == 2  # set-url then push
    _, push_cmd = calls
    assert push_cmd == ["git", "-C", "/tmp/r", "push", "-u", "origin", "feature/X"]


@pytest.mark.asyncio
async def test_push_with_skip_hooks_adds_no_verify():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X", skip_hooks=True)
    push_cmd = list(spawn.call_args_list[-1].args)
    assert "--no-verify" in push_cmd
    assert "--force" not in push_cmd


@pytest.mark.asyncio
async def test_push_with_force_and_skip_hooks_keeps_both():
    adapter = _make_adapter()
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X", force=True, skip_hooks=True)
    push_cmd = list(spawn.call_args_list[-1].args)
    assert "--force" in push_cmd
    assert "--no-verify" in push_cmd


@pytest.mark.asyncio
async def test_push_refreshes_origin_url_with_oauth2_token():
    """Workspaces cloned before a token rotation have a stale token in
    origin. Rewrite first so push uses the adapter's current token, in
    the GitLab oauth2 form."""
    adapter = _make_adapter()
    adapter._token = "glpat_NEW"
    adapter._url = "https://gitlab.example.com"
    adapter._project_id = "group/proj"
    with patch(
        "integrations.gitlab.gitlab_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/r", "feature/X")
    set_url_cmd = list(spawn.call_args_list[0].args)
    assert set_url_cmd == [
        "git", "-C", "/tmp/r", "remote", "set-url", "origin",
        "https://oauth2:glpat_NEW@gitlab.example.com/group/proj.git",
    ]

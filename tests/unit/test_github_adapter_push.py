"""Tests for GitHubAdapter.push — --force and --no-verify flag wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.github.github_adapter import GitHubAdapter


def _make_adapter() -> GitHubAdapter:
    adapter = GitHubAdapter.__new__(GitHubAdapter)
    adapter._owner = "acme-org"
    adapter._repo = "acme-mobile"
    adapter._token = "x"
    adapter._client = MagicMock()
    return adapter


def _ok_proc():
    """Build a Process-like mock whose communicate() resolves to empty (rc=0)."""
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


@pytest.mark.asyncio
async def test_push_default_no_force_no_no_verify():
    adapter = _make_adapter()
    with patch(
        "integrations.github.github_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/repo", "feature/X")
    cmd = list(spawn.call_args.args)
    # exactly the standard push, no --force, no --no-verify
    assert cmd == ["git", "-C", "/tmp/repo", "push", "-u", "origin", "feature/X"]


@pytest.mark.asyncio
async def test_push_with_skip_hooks_adds_no_verify():
    """skip_hooks=True must append --no-verify so the project's local
    pre-push hook is bypassed (used when the hook is auto-installed by
    a Gradle task and incompatible with the pipeline host)."""
    adapter = _make_adapter()
    with patch(
        "integrations.github.github_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/repo", "feature/X", skip_hooks=True)
    cmd = list(spawn.call_args.args)
    assert "--no-verify" in cmd
    assert "--force" not in cmd
    # Standard order/positional pieces preserved
    assert cmd[:4] == ["git", "-C", "/tmp/repo", "push"]


@pytest.mark.asyncio
async def test_push_with_force_and_skip_hooks_keeps_both():
    adapter = _make_adapter()
    with patch(
        "integrations.github.github_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/repo", "feature/X", force=True, skip_hooks=True)
    cmd = list(spawn.call_args.args)
    assert "--force" in cmd
    assert "--no-verify" in cmd


@pytest.mark.asyncio
async def test_push_refreshes_origin_url_with_current_token():
    """Push must rewrite origin URL with the adapter's current token first.

    Workspaces cloned before a token rotation have the old token baked into
    their remote URL. Without this rewrite, the stale token reaches GitHub
    at push time and fails (e.g. lacks workflow scope), even though .env has
    a valid replacement.
    """
    adapter = _make_adapter()
    adapter._token = "ghp_NEW_TOKEN"
    with patch(
        "integrations.github.github_adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_ok_proc()),
    ) as spawn:
        await adapter.push("/tmp/repo", "feature/X")
    calls = [list(c.args) for c in spawn.call_args_list]
    assert len(calls) == 2, f"expected 2 git calls (set-url + push), got {calls}"
    set_url_cmd, push_cmd = calls
    assert set_url_cmd == [
        "git", "-C", "/tmp/repo", "remote", "set-url", "origin",
        "https://ghp_NEW_TOKEN@github.com/acme-org/acme-mobile.git",
    ]
    assert push_cmd[:4] == ["git", "-C", "/tmp/repo", "push"]

"""Tests for GitHubAdapter.push — --force and --no-verify flag wiring."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from integrations.github.github_adapter import GitHubAdapter


def _make_adapter() -> GitHubAdapter:
    adapter = GitHubAdapter.__new__(GitHubAdapter)
    adapter._owner = "acme-org"
    adapter._repo = "acme-mobile"
    adapter._token = "x"
    adapter._client = MagicMock()
    return adapter


def _ok_run(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_push_default_no_force_no_no_verify():
    adapter = _make_adapter()
    with patch("integrations.github.github_adapter.subprocess.run", side_effect=_ok_run) as run:
        await adapter.push("/tmp/repo", "feature/X")
    cmd = run.call_args.args[0]
    # exactly the standard push, no --force, no --no-verify
    assert cmd == ["git", "-C", "/tmp/repo", "push", "-u", "origin", "feature/X"]


@pytest.mark.asyncio
async def test_push_with_skip_hooks_adds_no_verify():
    """skip_hooks=True must append --no-verify so the project's local
    pre-push hook is bypassed (used when the hook is auto-installed by
    a Gradle task and incompatible with the pipeline host)."""
    adapter = _make_adapter()
    with patch("integrations.github.github_adapter.subprocess.run", side_effect=_ok_run) as run:
        await adapter.push("/tmp/repo", "feature/X", skip_hooks=True)
    cmd = run.call_args.args[0]
    assert "--no-verify" in cmd
    assert "--force" not in cmd
    # Standard order/positional pieces preserved
    assert cmd[:4] == ["git", "-C", "/tmp/repo", "push"]


@pytest.mark.asyncio
async def test_push_with_force_and_skip_hooks_keeps_both():
    adapter = _make_adapter()
    with patch("integrations.github.github_adapter.subprocess.run", side_effect=_ok_run) as run:
        await adapter.push("/tmp/repo", "feature/X", force=True, skip_hooks=True)
    cmd = run.call_args.args[0]
    assert "--force" in cmd
    assert "--no-verify" in cmd

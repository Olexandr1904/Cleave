"""Tests for the verify-diff range used to confirm PR-comment fixes landed."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """A small git repo with three commits we can range-diff."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run(["git", "init", "-q"], cwd=repo_dir)
    _run(["git", "config", "user.email", "t@t"], cwd=repo_dir)
    _run(["git", "config", "user.name", "t"], cwd=repo_dir)

    # Commit A: feature.kt
    (repo_dir / "feature.kt").write_text("a")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-q", "-m", "A"], cwd=repo_dir)
    sha_a = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

    # Commit B: fix.kt (the fix commit)
    (repo_dir / "fix.kt").write_text("b")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-q", "-m", "B"], cwd=repo_dir)
    sha_b = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

    # Commit C: test.kt (qa agent commit)
    (repo_dir / "test.kt").write_text("c")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-q", "-m", "C"], cwd=repo_dir)

    return SimpleNamespace(dir=repo_dir, sha_a=sha_a, sha_b=sha_b)


class TestGitDiffFiles:
    def test_default_fallback_returns_only_last_commit(self, repo):
        """No since_sha → falls back to HEAD~1 (single-commit diff)."""
        from orchestrator.orchestrator import Orchestrator

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = Orchestrator._git_diff_files(ws)
        assert files == {"test.kt"}  # only the QA commit

    def test_since_sha_returns_cumulative_diff(self, repo):
        """With since_sha set to A → captures both fix.kt and test.kt."""
        from orchestrator.orchestrator import Orchestrator

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = Orchestrator._git_diff_files(ws, since_sha=repo.sha_a)
        assert files == {"fix.kt", "test.kt"}

    def test_since_sha_empty_falls_back(self, repo):
        """Empty since_sha → same behavior as default."""
        from orchestrator.orchestrator import Orchestrator

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = Orchestrator._git_diff_files(ws, since_sha="")
        assert files == {"test.kt"}

    def test_invalid_sha_returns_empty_set(self, repo):
        """Bad sha → exception path → returns empty set (no crash)."""
        from orchestrator.orchestrator import Orchestrator

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = Orchestrator._git_diff_files(ws, since_sha="not-a-sha")
        assert files == set()

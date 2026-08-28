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
        from orchestrator.git_ops import git_diff_files

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = git_diff_files(ws)
        assert files == {"test.kt"}  # only the QA commit

    def test_since_sha_returns_cumulative_diff(self, repo):
        """With since_sha set to A → captures both fix.kt and test.kt."""
        from orchestrator.git_ops import git_diff_files

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = git_diff_files(ws, since_sha=repo.sha_a)
        assert files == {"fix.kt", "test.kt"}

    def test_since_sha_empty_falls_back(self, repo):
        """Empty since_sha → same behavior as default."""
        from orchestrator.git_ops import git_diff_files

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = git_diff_files(ws, since_sha="")
        assert files == {"test.kt"}

    def test_invalid_sha_returns_empty_set(self, repo):
        """Bad sha → exception path → returns empty set (no crash)."""
        from orchestrator.git_ops import git_diff_files

        ws = MagicMock()
        ws.source_dir = repo.dir

        files = git_diff_files(ws, since_sha="not-a-sha")
        assert files == set()


class TestGitDiffFilesVsBase:
    """Ground truth for 'what changed on this branch' — the question agents
    have answered wrongly for themselves (PR #1530 missed a modified file)."""

    @pytest.fixture
    def branch_repo(self, tmp_path):
        repo_dir = tmp_path / "branchrepo"
        repo_dir.mkdir()
        _run(["git", "init", "-q", "-b", "develop"], cwd=repo_dir)
        _run(["git", "config", "user.email", "t@t"], cwd=repo_dir)
        _run(["git", "config", "user.name", "t"], cwd=repo_dir)
        (repo_dir / "Base.kt").write_text("base")
        _run(["git", "add", "."], cwd=repo_dir)
        _run(["git", "commit", "-q", "-m", "base"], cwd=repo_dir)
        base_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
        # Stand in for the remote-tracking ref without needing a real remote.
        _run(["git", "update-ref", "refs/remotes/origin/develop", base_sha], cwd=repo_dir)
        _run(["git", "checkout", "-q", "-b", "feature/x"], cwd=repo_dir)
        (repo_dir / "Foo.kt").write_text("foo")
        _run(["git", "add", "."], cwd=repo_dir)
        _run(["git", "commit", "-q", "-m", "feature"], cwd=repo_dir)
        return repo_dir

    def test_lists_branch_changes(self, branch_repo):
        from orchestrator.git_ops import git_diff_files_vs_base

        ws = MagicMock()
        ws.source_dir = branch_repo
        assert git_diff_files_vs_base(ws, "develop") == ["Foo.kt"]

    def test_returns_empty_when_base_unresolvable(self, repo):
        from orchestrator.git_ops import git_diff_files_vs_base

        ws = MagicMock()
        ws.source_dir = repo.dir  # no origin refs at all
        assert git_diff_files_vs_base(ws, "develop") == []

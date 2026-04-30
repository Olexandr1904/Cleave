"""Tests for Orchestrator._ensure_branch_has_commits — defensive recovery
before push.

The squash bug fixed in ea8819c could leave a feature branch reset to base
with all the dev work staged but no commits recorded. The next push opened
an empty PR and required manual rescue. This helper detects that state on
every push attempt and recovers automatically — protects against the
already-damaged workspaces from before the fix AND against any future git
step that fails mid-way and leaves the branch orphaned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config.schemas import (
    GitConfig,
    GitHubConfig,
    JiraConfig,
    JiraStatusesConfig,
    RepoConfig,
    RepoInfo,
    VCSConfig,
)
from orchestrator.orchestrator import Orchestrator


def _init_repo_with_orphaned_staged_work(tmp_path: Path) -> Path:
    """Recreate the bug aftermath: branch reset to base, work in the index
    but no commits ahead."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "base@base"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Base"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    # Make a feature commit, then reset --soft → simulate the squash that
    # successfully reset but failed to record the follow-up commit.
    (repo / "real_work.txt").write_text("dev did something useful")
    subprocess.run(["git", "add", "real_work.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "feat(T-1): real work"], cwd=repo, check=True)
    subprocess.run(["git", "reset", "--soft", "HEAD~1"], cwd=repo, check=True)
    return repo


def _repo_config() -> RepoConfig:
    return RepoConfig(
        repo=RepoInfo(id="r"),
        vcs=VCSConfig(github=GitHubConfig()),
        jira=JiraConfig(statuses=JiraStatusesConfig()),
        git=GitConfig(
            commit_author_name="Sickle Bot",
            commit_author_email="sickle@pipeline.local",
        ),
    )


def _workspace(repo: Path, ticket_id: str = "T-1") -> MagicMock:
    ws = MagicMock()
    ws.source_dir = repo
    ws.state = MagicMock()
    ws.state.ticket_id = ticket_id
    ws.state.company_id = "acme"
    return ws


def test_recovers_branch_with_zero_commits_and_staged_work(tmp_path):
    """The exact ACME-11756 / 12053 scenario: branch reset to base, work
    staged, no commits ahead. Helper must commit the staged work so the
    upcoming push has something real to send."""
    repo = _init_repo_with_orphaned_staged_work(tmp_path)
    orch = Orchestrator.__new__(Orchestrator)
    orch._events = None

    # Pre-condition: 0 commits ahead but real_work.txt is staged
    pre_count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "origin/main..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert pre_count == "0"

    orch._ensure_branch_has_commits(_workspace(repo), _repo_config())

    # Post-condition: 1 commit ahead, real_work.txt is in it
    post_count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "origin/main..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert post_count == "1"
    files_in_commit = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert "real_work.txt" in files_in_commit


def test_uses_repo_config_author_for_recovery_commit(tmp_path):
    """Without explicit author the commit would fail when the host's global
    gitconfig is missing user.email — the same trap the original squash bug
    fell into. Verify repo_config author is applied."""
    repo = _init_repo_with_orphaned_staged_work(tmp_path)
    # Wipe local user config so only the explicit `-c` overrides could work.
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.email"], check=False)
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.name"], check=False)

    orch = Orchestrator.__new__(Orchestrator)
    orch._events = None
    orch._ensure_branch_has_commits(_workspace(repo), _repo_config())

    author = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert author == "Sickle Bot <sickle@pipeline.local>"


def test_no_op_when_branch_already_has_commits(tmp_path):
    """Happy path: real commits already on the branch. The helper must not
    create a duplicate / spurious recovery commit."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "X"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("feat")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "feat: real"], cwd=repo, check=True)

    pre_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    orch = Orchestrator.__new__(Orchestrator)
    orch._events = None
    orch._ensure_branch_has_commits(_workspace(repo), _repo_config())

    post_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert post_head == pre_head, "no recovery commit should be made"


def test_no_op_when_zero_commits_and_no_staged_work(tmp_path):
    """Branch is at base AND nothing staged — there's nothing to recover.
    Helper must not make a phantom 'no-op' commit."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "X"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)

    pre_count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    orch = Orchestrator.__new__(Orchestrator)
    orch._events = None
    orch._ensure_branch_has_commits(_workspace(repo), _repo_config())

    post_count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert post_count == pre_count


def test_does_not_commit_untracked_files(tmp_path):
    """Recovery is conservative: only commit what is ALREADY staged. Files
    the agent left untracked (build artifacts, scratch) must not get swept
    into the branch."""
    repo = _init_repo_with_orphaned_staged_work(tmp_path)
    # Drop an untracked file alongside the staged work
    (repo / "scratch.txt").write_text("not for commit")

    orch = Orchestrator.__new__(Orchestrator)
    orch._events = None
    orch._ensure_branch_has_commits(_workspace(repo), _repo_config())

    files_in_commit = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert "real_work.txt" in files_in_commit
    assert "scratch.txt" not in files_in_commit


def test_emits_recovery_event(tmp_path):
    """The recovery is automatic but must be visible: an event lands in
    events.db so operators can audit how often this defensive layer fires."""
    repo = _init_repo_with_orphaned_staged_work(tmp_path)
    events: list[tuple[str, dict]] = []

    class _Bus:
        def emit(self, event_type: str, message: str, **kwargs):
            events.append((event_type, kwargs.get("data", {})))

    orch = Orchestrator.__new__(Orchestrator)
    orch._events = _Bus()
    orch._ensure_branch_has_commits(_workspace(repo), _repo_config())

    recovery = [e for e in events if e[0] == "branch_recovered_from_orphan_state"]
    assert len(recovery) == 1
    assert recovery[0][1]["file_count"] == 1
    assert "real_work.txt" in recovery[0][1]["files"]

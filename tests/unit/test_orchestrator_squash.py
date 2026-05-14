"""Tests for Orchestrator._squash_feature_commits — atomicity + author config."""

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
    TrackerConfig,
    VCSConfig,
)
from orchestrator.pipeline.actions.push_and_open_pr import squash_feature_commits


def _init_repo(tmp_path: Path) -> Path:
    """Spin up a minimal git repo with a base commit + 3 feature commits.
    Mirrors the real workspace layout: source/ is a clone with commits."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    # Base commit on main (acts as the "develop" tip / remote ref)
    subprocess.run(["git", "config", "user.email", "base@base"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Base"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    # Pretend that base commit is what "remotes" knows about — use a fake remote
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    # Three feature commits on top
    for i, msg in enumerate(
        ["feat(T-1): the real change", "fix(T-1): tweak", "test(T-1): tests"], start=1,
    ):
        (repo / f"f{i}.txt").write_text(f"f{i}")
        subprocess.run(["git", "add", f"f{i}.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)
    return repo


def _repo_config_with_author() -> RepoConfig:
    return RepoConfig(
        repo=RepoInfo(id="r"),
        vcs=VCSConfig(github=GitHubConfig()),
        tracker=TrackerConfig(jira=JiraConfig(statuses=JiraStatusesConfig())),
        git=GitConfig(
            commit_author_name="Cleave Bot",
            commit_author_email="cleave@pipeline.local",
        ),
    )


def _workspace(repo: Path) -> MagicMock:
    ws = MagicMock()
    ws.source_dir = repo
    ws.state = MagicMock()
    ws.state.ticket_id = "T-1"
    return ws


def test_squashes_three_commits_into_one(tmp_path):
    """Happy path: three commits on the feature branch get squashed into
    one commit using the first commit's message as the squash message."""
    repo = _init_repo(tmp_path)

    ws = _workspace(repo)
    squash_feature_commits(ws, _repo_config_with_author())

    # One commit ahead of origin/main, with the first feature's message
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "origin/main..HEAD", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ["feat(T-1): the real change"]


def test_passes_author_via_git_dash_c(tmp_path, monkeypatch):
    """Even when the global gitconfig has no user.email, the squash commit
    must succeed because we pass author via `git -c user.email=...`."""
    repo = _init_repo(tmp_path)
    # Shadow HOME so the real ~/.gitconfig is invisible
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    # Wipe any local user.* so this repo has NO author config in the
    # situation this fix targets
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.email"], check=False)
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.name"], check=False)

    ws = _workspace(repo)
    squash_feature_commits(ws, _repo_config_with_author())

    # Squash succeeded with our explicit author
    author = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert author == "Cleave Bot <cleave@pipeline.local>"

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "origin/main..HEAD", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ["feat(T-1): the real change"]


def test_atomic_rollback_when_commit_step_fails(tmp_path):
    """If the post-reset commit fails for any reason, the branch must be
    restored to its original HEAD. Without rollback, the failed squash
    leaves an empty feature branch and the next push opens a 0-commit PR
    (the bug that triggered this fix)."""
    repo = _init_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Force the commit step to fail with a commit-msg hook that always
    # exits 1. The hook fires AFTER soft-reset but BEFORE the commit is
    # recorded, leaving the branch detached from its old commits with no
    # squashed commit to take their place — exactly the failure mode we
    # need to roll back.
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    hook.chmod(0o755)

    ws = _workspace(repo)
    squash_feature_commits(ws, _repo_config_with_author())

    # Branch must be back at HEAD_before (not at HEAD~3)
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before, (
        "Squash commit failed but branch was not restored — feature branch "
        "would push as 0 commits and create an empty PR"
    )
    # Three commits still ahead of origin
    count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "origin/main..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert count == "3"


def test_skips_when_only_one_commit_ahead(tmp_path):
    """No squash needed when only one feature commit exists. Function
    returns silently — branch unchanged."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "X"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    (repo / "b.txt").write_text("b")
    subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "feat: only one"], cwd=repo, check=True)

    ws = _workspace(repo)
    squash_feature_commits(ws, _repo_config_with_author())

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "origin/main..HEAD", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ["feat: only one"]

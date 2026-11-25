"""drop_pipeline_reports: the branch's own changes under ai_pipeline/ never reach the PR.

The dev agent's prompt told it to commit its reports, and it force-added them past .git/info/exclude, so every PR
carried hundreds of lines of pipeline paperwork. Whatever an agent commits, the push must not carry them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
from orchestrator.pipeline.actions.push_and_open_pr import (
    action_push_and_open_pr,
    drop_pipeline_reports,
    squash_feature_commits,
)

MESSAGE = "chore(T-1): keep pipeline reports out of the PR"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _repo(tmp_path: Path) -> Path:
    """A clone as Cleave makes one: a base that already holds an older ticket's report, ai_pipeline/ excluded, and a
    feature branch whose commit carries code plus force-added reports."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "agent@agent")
    _git(repo, "config", "user.name", "Agent")
    _write(repo, "base.txt", "base")
    _write(repo, "ai_pipeline/OLD-1/ba.md", "old plan")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, ".git/info/exclude", "ai_pipeline/\n")
    _git(repo, "checkout", "-q", "-b", "feature/T-1")
    _write(repo, "app.txt", "the change")
    _write(repo, "ai_pipeline/T-1/ba.md", "plan")
    _write(repo, "ai_pipeline/T-1/developer.md", "summary")
    _write(repo, "ai_pipeline/OLD-1/ba.md", "old plan, edited")
    _git(repo, "add", "app.txt")
    _git(repo, "add", "-f", "ai_pipeline")
    _git(repo, "commit", "-qm", "feat(T-1): the change")
    return repo


def _config() -> RepoConfig:
    return RepoConfig(
        repo=RepoInfo(id="r"),
        vcs=VCSConfig(github=GitHubConfig(default_branch="main"), default_branch="main"),
        tracker=TrackerConfig(jira=JiraConfig(statuses=JiraStatusesConfig())),
        git=GitConfig(commit_author_name="Cleave Bot", commit_author_email="cleave@pipeline.local"),
    )


def _workspace(repo: Path) -> MagicMock:
    ws = MagicMock()
    ws.source_dir = repo
    ws.state = MagicMock()
    ws.state.ticket_id = "T-1"
    return ws


def _branch_files(repo: Path) -> list[str]:
    return _git(repo, "diff", "--name-only", "origin/main...HEAD").split()


def test_the_branch_keeps_its_code_and_loses_its_reports(tmp_path):
    repo = _repo(tmp_path)

    dropped = drop_pipeline_reports(_workspace(repo), _config())

    assert _branch_files(repo) == ["app.txt"]
    assert sorted(dropped) == ["ai_pipeline/OLD-1/ba.md", "ai_pipeline/T-1/ba.md", "ai_pipeline/T-1/developer.md"]


def test_the_reports_stay_on_disk_for_the_agents(tmp_path):
    repo = _repo(tmp_path)

    drop_pipeline_reports(_workspace(repo), _config())

    assert (repo / "ai_pipeline/T-1/ba.md").read_text() == "plan"
    assert (repo / "ai_pipeline/OLD-1/ba.md").read_text() == "old plan, edited"


def test_a_report_the_base_already_holds_keeps_the_base_content(tmp_path):
    repo = _repo(tmp_path)

    drop_pipeline_reports(_workspace(repo), _config())

    assert _git(repo, "show", "HEAD:ai_pipeline/OLD-1/ba.md") == "old plan"


def test_the_drop_is_one_commit_by_the_configured_author(tmp_path):
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").strip()

    drop_pipeline_reports(_workspace(repo), _config())

    assert _git(repo, "rev-parse", "HEAD~1").strip() == head_before
    assert _git(repo, "log", "-1", "--format=%s|%an|%ae").strip() == f"{MESSAGE}|Cleave Bot|cleave@pipeline.local"


def test_the_clone_index_no_longer_stages_the_reports(tmp_path):
    """A later `git commit -a` by an agent must not put them back."""
    repo = _repo(tmp_path)

    drop_pipeline_reports(_workspace(repo), _config())

    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert _git(repo, "ls-files", "ai_pipeline/T-1") == ""


def test_other_staged_work_stays_out_of_the_drop_commit(tmp_path):
    """Staged but uncommitted work never passed the gates; the drop must not push it."""
    repo = _repo(tmp_path)
    _write(repo, "half.txt", "staged, not committed")
    _git(repo, "add", "half.txt")

    drop_pipeline_reports(_workspace(repo), _config())

    assert "half.txt" not in _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["half.txt"]


def test_a_branch_without_reports_gets_no_commit(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "origin/main")
    _git(repo, "checkout", "-q", "-B", "feature/T-1")
    _write(repo, "app.txt", "the change")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-qm", "feat(T-1): the change")
    head_before = _git(repo, "rev-parse", "HEAD").strip()

    assert drop_pipeline_reports(_workspace(repo), _config()) == []
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before


def test_without_a_base_ref_nothing_changes(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    head_before = _git(repo, "rev-parse", "HEAD").strip()

    assert drop_pipeline_reports(_workspace(repo), _config()) == []
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before


def test_the_squash_folds_the_drop_into_one_clean_commit(tmp_path):
    repo = _repo(tmp_path)

    drop_pipeline_reports(_workspace(repo), _config())
    squash_feature_commits(_workspace(repo), _config())

    assert _git(repo, "log", "--format=%s", "origin/main..HEAD").split("\n")[0] == "feat(T-1): the change"
    assert _git(repo, "rev-list", "--count", "origin/main..HEAD").strip() == "1"
    assert _branch_files(repo) == ["app.txt"]


def _ticket(repo: Path, pr_number: int | None) -> MagicMock:
    ws = _workspace(repo)
    ws.state = SimpleNamespace(ticket_id="T-1", company_id="c", branch="feature/T-1", pr_number=pr_number,
                               pr_url=f"https://example.test/pull/{pr_number}" if pr_number else None)
    ws.reports_dir = repo / "ai_pipeline" / "T-1"
    return ws


@pytest.mark.asyncio
async def test_a_push_to_an_open_pr_carries_no_reports(tmp_path):
    repo = _repo(tmp_path)
    pushed: list[list[str]] = []

    class Vcs:
        async def push(self, *args, **kwargs):
            pushed.append(_branch_files(repo))

    await action_push_and_open_pr(_ticket(repo, 7), Vcs(), _config(), None, "", None, None)

    assert pushed == [["app.txt"]]


@pytest.mark.asyncio
async def test_the_first_push_opens_a_pr_of_one_commit_without_reports(tmp_path):
    repo = _repo(tmp_path)
    seen: dict[str, object] = {}

    async def create_pr(*args, **kwargs):
        seen["files"] = _branch_files(repo)
        seen["commits"] = _git(repo, "rev-list", "--count", "origin/main..HEAD").strip()
        return SimpleNamespace(success=True, pr_url="https://example.test/pull/1", pr_number=1, error="")

    with patch("orchestrator.pipeline.actions.push_and_open_pr.create_pr", new=create_pr):
        await action_push_and_open_pr(_ticket(repo, None), MagicMock(), _config(), None, "", None, None)

    assert seen == {"files": ["app.txt"], "commits": "1"}

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.constants import RUNTIME_OUTPUT_QA, RUNTIME_OUTPUT_SCOPE_GUARD
from orchestrator.stage_verifier import VerifyResult, verify, capture_stage_start


def _init_repo_with_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _fake_workspace(source_dir: Path, reports_dir: Path | None = None) -> MagicMock:
    ws = MagicMock()
    ws.source_dir = source_dir
    ws.reports_dir = reports_dir or (source_dir.parent / "reports")
    return ws


class TestCaptureStageStart:
    def test_captures_current_head(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        sha = capture_stage_start(ws, "dev")
        assert sha is not None
        assert len(sha) == 40

    def test_returns_none_for_non_verifiable_stage(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        assert capture_stage_start(ws, "analysis") is None


class TestDevVerifier:
    def test_new_commit_passes(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        start = capture_stage_start(ws, "dev")
        (repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)

        r = verify("dev", ws, start)
        assert r.ok is True
        assert r.stage_id == "dev"

    def test_no_new_commit_fails(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path)
        ws = _fake_workspace(repo)
        start = capture_stage_start(ws, "dev")

        r = verify("dev", ws, start)
        assert r.ok is False
        assert r.stage_id == "dev"
        assert "commit" in r.reason.lower()


class TestScopeCheckVerifier:
    def test_report_file_exists_passes(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / RUNTIME_OUTPUT_SCOPE_GUARD).write_text("status: pass\n")
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("scope_check", ws, None)
        assert r.ok is True

    def test_report_file_missing_fails(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("scope_check", ws, None)
        assert r.ok is False
        assert RUNTIME_OUTPUT_SCOPE_GUARD in r.reason


class TestQaVerifier:
    def test_report_file_exists_passes(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / RUNTIME_OUTPUT_QA).write_text("all gates passed")
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("qa", ws, None)
        assert r.ok is True

    def test_report_file_missing_fails(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        ws = _fake_workspace(tmp_path / "src", reports)
        r = verify("qa", ws, None)
        assert r.ok is False


class TestPushVerifier:
    def test_push_succeeded(self, tmp_path):
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        repo = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "m"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
        (repo / "g.txt").write_text("y")
        subprocess.run(["git", "add", "g.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "f"], cwd=repo, check=True)
        subprocess.run(["git", "push", "-q", "origin", "feature"], cwd=repo, check=True)

        ws = _fake_workspace(repo)
        ws.state = SimpleNamespace(branch="feature")
        r = verify("push", ws, None)
        assert r.ok is True

    def test_push_not_done(self, tmp_path):
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        repo = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "m"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
        (repo / "g.txt").write_text("y")
        subprocess.run(["git", "add", "g.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "f"], cwd=repo, check=True)

        ws = _fake_workspace(repo)
        ws.state = SimpleNamespace(branch="feature")
        r = verify("push", ws, None)
        assert r.ok is False
        assert "remote" in r.reason.lower() or "push" in r.reason.lower()


class TestPrReviewVerifier:
    def test_pr_exists_passes(self, tmp_path):
        ws = _fake_workspace(tmp_path / "src")
        ws.state = SimpleNamespace(pr_number=42)
        r = verify("pr_review", ws, None)
        assert r.ok is True

    def test_no_pr_number_fails(self, tmp_path):
        ws = _fake_workspace(tmp_path / "src")
        ws.state = SimpleNamespace(pr_number=None)
        r = verify("pr_review", ws, None)
        assert r.ok is False

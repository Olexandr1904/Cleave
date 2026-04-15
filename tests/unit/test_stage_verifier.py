from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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

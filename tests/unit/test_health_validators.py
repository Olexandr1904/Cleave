from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from unittest.mock import patch, AsyncMock

from health.validators import ValidatorResult, check_jira, check_github, check_gitlab, check_git_identity


def test_validator_result_ok_shape():
    r = ValidatorResult(ok=True, name="jira", target="ACME", reason="", fix_hint="")
    assert r.ok is True
    assert r.name == "jira"
    assert r.target == "ACME"
    assert r.reason == ""
    assert r.fix_hint == ""


def test_validator_result_failure_shape():
    r = ValidatorResult(
        ok=False,
        name="git_identity",
        target="/tmp/ws",
        reason="user.email not set",
        fix_hint="git config --global user.email <you@company>",
    )
    assert r.ok is False
    assert "user.email" in r.reason
    assert "git config" in r.fix_hint


class TestCheckJira:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("health.validators.config_tools.validate_jira",
                   new=AsyncMock(return_value={"success": True, "project_name": "Acme"})):
            r = await check_jira("https://acme.atlassian.net", "me@x", "tok", "ACME")
        assert r.ok is True
        assert r.name == "jira"
        assert r.target == "ACME"
        assert r.reason == ""

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        with patch("health.validators.config_tools.validate_jira",
                   new=AsyncMock(return_value={"success": False, "error": "HTTP 401"})):
            r = await check_jira("https://x", "me@x", "bad", "ACME")
        assert r.ok is False
        assert "401" in r.reason
        assert r.fix_hint  # non-empty

    @pytest.mark.asyncio
    async def test_wrapper_does_not_raise(self):
        with patch("health.validators.config_tools.validate_jira",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            r = await check_jira("https://x", "me@x", "tok", "ACME")
        assert r.ok is False
        assert "boom" in r.reason or "RuntimeError" in r.reason


class TestCheckGithub:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("health.validators.config_tools.validate_github",
                   new=AsyncMock(return_value={"success": True, "full_name": "acme/mb", "default_branch": "main"})):
            r = await check_github("tok", "acme", "mb")
        assert r.ok is True
        assert r.name == "github"
        assert r.target == "acme/mb"

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        with patch("health.validators.config_tools.validate_github",
                   new=AsyncMock(return_value={"success": False, "error": "HTTP 401"})):
            r = await check_github("bad", "acme", "mb")
        assert r.ok is False
        assert "401" in r.reason


class TestCheckGitlab:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("health.validators.config_tools.validate_gitlab",
                   new=AsyncMock(return_value={"success": True, "project_name": "mb"})):
            r = await check_gitlab("tok", "123", "https://gitlab.example.com")
        assert r.ok is True
        assert r.name == "gitlab"
        assert r.target == "123"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


class TestCheckGitIdentity:
    def test_identity_set_via_local_config(self, tmp_path):
        repo = _init_repo(tmp_path)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=repo, check=True)
        r = check_git_identity(repo)
        assert r.ok is True
        assert r.name == "git_identity"
        assert r.reason == ""

    def test_identity_missing(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
        (tmp_path / "empty_home").mkdir()
        r = check_git_identity(repo)
        assert r.ok is False
        assert "user.email" in r.reason or "user.name" in r.reason
        assert "git config" in r.fix_hint

    def test_not_a_git_dir(self, tmp_path):
        r = check_git_identity(tmp_path)
        assert r.ok is False
        assert "git" in r.reason.lower()

    def test_does_not_raise_on_missing_git_binary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        r = check_git_identity(tmp_path)
        assert r.ok is False

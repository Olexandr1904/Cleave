from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from unittest.mock import patch, AsyncMock

from health.validators import ValidatorResult, check_jira, check_github, check_gitlab, check_git_identity, check_git_remote


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


class TestCheckGitRemote:
    def test_reachable_remote(self, tmp_path):
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        r = check_git_remote(clone)
        assert r.ok is True
        assert r.name == "git_remote"

    def test_unreachable_remote(self, tmp_path):
        repo = _init_repo(tmp_path)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://10.255.255.1/nonexistent.git"],
            cwd=repo, check=True,
        )
        r = check_git_remote(repo)
        assert r.ok is False
        assert r.reason

    def test_no_remote_configured(self, tmp_path):
        repo = _init_repo(tmp_path)
        r = check_git_remote(repo)
        assert r.ok is False
        assert "remote" in r.reason.lower() or "origin" in r.reason.lower()

    def test_not_a_git_dir(self, tmp_path):
        r = check_git_remote(tmp_path)
        assert r.ok is False


import json, os, stat, time
import httpx, respx
from health.validators import check_claude_login, check_telegram


def _fake_claude(tmp_path, body: str) -> str:
    p = tmp_path / "claude"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


async def test_check_github_failure_passes_status():
    with patch("health.validators.config_tools.validate_github",
               new=AsyncMock(return_value={"success": False, "error": "HTTP 403", "status": 403})):
        assert (await check_github("t", "o", "r")).extra["status"] == 403


@respx.mock
async def test_check_telegram_ok_and_401():
    route = respx.get("https://api.telegram.org/bot123:abc/getMe")
    route.respond(200, json={"ok": True, "result": {}})
    assert (await check_telegram("123:abc")).ok
    route.respond(401, json={"ok": False})
    r = await check_telegram("123:abc")
    assert not r.ok and r.extra["status"] == 401


@respx.mock
async def test_check_telegram_network_error_hides_token():
    respx.get("https://api.telegram.org/bot123:SECRET/getMe").mock(side_effect=httpx.ConnectError("https://api.telegram.org/bot123:SECRET/getMe"))
    r = await check_telegram("123:SECRET")
    assert r.reason == "ConnectError" and "SECRET" not in repr(r)


async def test_check_claude_login_logged_in(tmp_path):
    bin_ = _fake_claude(tmp_path, '[ "$*" = "auth status --json" ] || exit 9\n'
                                  'echo \'{"loggedIn": true, "loginExpiresAt": "2026-11-25T10:00:00.000Z"}\'\n')
    r = await check_claude_login(bin_)
    assert r.ok and r.extra["expires_at"] == "2026-11-25T10:00:00.000Z"


async def test_check_claude_login_logged_out_exit_1(tmp_path):
    r = await check_claude_login(_fake_claude(tmp_path, 'echo \'{"loggedIn": false, "loginExpiresAt": null}\'\nexit 1\n'))
    assert not r.ok and r.extra["logged_in"] is False


async def test_check_claude_login_env_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "k1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    monkeypatch.setenv("SOME_OTHER_VAR", "x")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    dump = tmp_path / "env.txt"
    bin_ = _fake_claude(tmp_path, f'env > {dump}\necho \'{{"loggedIn": true, "loginExpiresAt": null}}\'\n')
    assert (await check_claude_login(bin_)).ok
    lines = dump.read_text().splitlines()
    keys = {line.split("=", 1)[0] for line in lines}
    assert {"HOME", "PATH"} <= keys          # the shell itself may add PWD, SHLVL, _
    assert f"CLAUDE_CONFIG_DIR={tmp_path / 'profile'}" in lines
    assert not keys & {"CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "SOME_OTHER_VAR"}
    assert keys <= {"HOME", "PATH", "LANG", "TMPDIR", "USER", "CLAUDE_CONFIG_DIR", "PWD", "SHLVL", "_", "OLDPWD"}


async def test_check_claude_login_timeout_kills_and_reports(tmp_path):
    t0 = time.monotonic()
    r = await check_claude_login(_fake_claude(tmp_path, "sleep 30\n"), timeout_seconds=0.5)
    assert not r.ok and "timed out" in r.reason and time.monotonic() - t0 < 5


async def test_check_claude_login_non_json(tmp_path):
    r = await check_claude_login(_fake_claude(tmp_path, "echo 'Not logged in · Please run /login'\n"))
    assert not r.ok and r.extra["logged_in"] is None


async def test_check_claude_login_missing_binary(tmp_path):
    r = await check_claude_login(str(tmp_path / "nope"))
    assert not r.ok and r.reason == "claude not found"

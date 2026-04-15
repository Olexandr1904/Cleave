from __future__ import annotations

from health.validators import ValidatorResult


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


import pytest
from unittest.mock import patch, AsyncMock

from health.validators import check_jira, check_github, check_gitlab


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

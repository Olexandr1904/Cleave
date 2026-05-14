"""Tests for main._build_vcs_adapter — per-provider VCS dispatch."""

from __future__ import annotations

import pytest

from config.schemas import GitHubConfig, GitLabConfig, RepoConfig, VCSConfig
from integrations.github.github_adapter import GitHubAdapter
from integrations.gitlab.gitlab_adapter import GitLabAdapter
from main import _build_vcs_adapter


def _github_repo_cfg(token: str = "gh_t", owner: str = "acme", repo: str = "app") -> RepoConfig:
    cfg = RepoConfig()
    cfg.vcs = VCSConfig(
        provider="github",
        github=GitHubConfig(token=token, owner=owner, repo=repo),
    )
    return cfg


def _gitlab_repo_cfg(
    token: str = "glpat_t", project_id: str = "group/app",
    url: str = "https://gitlab.example.com",
) -> RepoConfig:
    cfg = RepoConfig()
    cfg.vcs = VCSConfig(
        provider="gitlab",
        gitlab=GitLabConfig(token=token, project_id=project_id, url=url),
    )
    return cfg


def test_build_vcs_adapter_github_returns_github_adapter():
    cfg = _github_repo_cfg(token="ghp_xyz", owner="acme-org", repo="acme-mobile")
    adapter = _build_vcs_adapter(cfg)
    assert isinstance(adapter, GitHubAdapter)
    assert adapter._token == "ghp_xyz"
    assert adapter._owner == "acme-org"
    assert adapter._repo == "acme-mobile"


def test_build_vcs_adapter_gitlab_returns_gitlab_adapter():
    cfg = _gitlab_repo_cfg(
        token="glpat_abc", project_id="group/sub/proj",
        url="https://gitlab.example.com",
    )
    adapter = _build_vcs_adapter(cfg)
    assert isinstance(adapter, GitLabAdapter)
    assert adapter._token == "glpat_abc"
    assert adapter._project_id == "group/sub/proj"
    assert adapter._url == "https://gitlab.example.com"


def test_build_vcs_adapter_gitlab_defaults_blank_url_to_gitlab_com():
    """A repo YAML written without a url field should still get gitlab.com."""
    cfg = _gitlab_repo_cfg(url="")
    adapter = _build_vcs_adapter(cfg)
    assert isinstance(adapter, GitLabAdapter)
    assert adapter._url == "https://gitlab.com"


def test_build_vcs_adapter_github_no_token_returns_none():
    cfg = _github_repo_cfg(token="")
    assert _build_vcs_adapter(cfg) is None


def test_build_vcs_adapter_gitlab_no_token_returns_none():
    cfg = _gitlab_repo_cfg(token="")
    assert _build_vcs_adapter(cfg) is None


def test_build_vcs_adapter_unknown_provider_returns_none():
    cfg = RepoConfig()
    cfg.vcs = VCSConfig(provider="bitbucket")
    assert _build_vcs_adapter(cfg) is None

"""Tests for GitLabAdapter.__init__ — field storage and httpx client setup."""

from __future__ import annotations

import pytest

from integrations.gitlab.gitlab_adapter import GitLabAdapter


def test_init_stores_token_project_and_url():
    adapter = GitLabAdapter(
        token="glpat_xyz",
        project_id="group/proj",
        url="https://gitlab.example.com",
    )
    assert adapter._token == "glpat_xyz"
    assert adapter._project_id == "group/proj"
    assert adapter._url == "https://gitlab.example.com"


def test_init_defaults_url_to_gitlab_com():
    adapter = GitLabAdapter(token="t", project_id="123")
    assert adapter._url == "https://gitlab.com"


def test_init_strips_trailing_slash_from_url():
    adapter = GitLabAdapter(token="t", project_id="123", url="https://gitlab.com/")
    assert adapter._url == "https://gitlab.com"


def test_init_urlencodes_project_id_for_paths():
    """Adapter must URL-encode project_id so namespaced paths like
    'group/proj' work in {url}/api/v4/projects/{id} routes."""
    adapter = GitLabAdapter(token="t", project_id="group/sub/proj")
    assert adapter._project_path == "group%2Fsub%2Fproj"


def test_init_numeric_project_id_passes_through():
    adapter = GitLabAdapter(token="t", project_id="12345")
    # quote() leaves digits alone
    assert adapter._project_path == "12345"

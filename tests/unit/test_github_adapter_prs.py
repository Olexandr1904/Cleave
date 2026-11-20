# tests/unit/test_github_adapter_prs.py
from __future__ import annotations
import json
import pytest, respx
from integrations.github.github_adapter import GitHubAdapter

API = "https://api.github.com/repos/acme/app"

@pytest.fixture
def adapter():
    return GitHubAdapter("tok", "acme", "app")

@respx.mock
async def test_open_pr_as_a_draft(adapter):
    route = respx.post(f"{API}/pulls").respond(201, json={"number": 5, "html_url": "u"})
    assert await adapter.open_pr("T", "B", "ACME-1-fix", "develop", draft=True) == (5, "u")
    assert json.loads(route.calls[0].request.content)["draft"] is True

@respx.mock
async def test_open_pr_default_sends_no_draft_flag(adapter):
    route = respx.post(f"{API}/pulls").respond(201, json={"number": 5, "html_url": "u"})
    await adapter.open_pr("T", "B", "h", "develop")
    assert "draft" not in json.loads(route.calls[0].request.content)

@respx.mock
async def test_open_pr_assigns_the_token_owner(adapter):
    respx.post(f"{API}/pulls").respond(201, json={"number": 5, "html_url": "u"})
    respx.get("https://api.github.com/user").respond(200, json={"login": "octo"})
    route = respx.post(f"{API}/issues/5/assignees").respond(201, json={})
    await adapter.open_pr("T", "B", "h", "develop")
    assert json.loads(route.calls[0].request.content) == {"assignees": ["octo"]}

@respx.mock
async def test_open_pr_returns_the_pr_when_the_assignment_fails(adapter):
    respx.post(f"{API}/pulls").respond(201, json={"number": 5, "html_url": "u"})
    respx.get("https://api.github.com/user").respond(403, json={"message": "Resource not accessible"})
    assert await adapter.open_pr("T", "B", "h", "develop") == (5, "u")

@respx.mock
async def test_open_prs_for_key(adapter):
    respx.get(f"{API}/pulls", params={"state": "open", "per_page": "100"}).respond(200, json=[
        {"number": 1, "title": "ACME-1: old try", "head": {"ref": "feature/x"}},
        {"number": 2, "title": "Other", "head": {"ref": "acme-1-second"}},
        {"number": 3, "title": "ACME-10: not it", "head": {"ref": "ACME-10-y"}},
    ])
    assert await adapter.open_prs_for_key("ACME-1") == [(1, "ACME-1: old try", "feature/x"), (2, "Other", "acme-1-second")]

@respx.mock
async def test_open_prs_for_key_leaves_out_a_key_inside_a_longer_one(adapter):
    respx.get(f"{API}/pulls", params={"state": "open", "per_page": "100"}).respond(200, json=[
        {"number": 1, "title": "WEBAB-12: x", "head": {"ref": "CAB-12-login"}},
        {"number": 2, "title": "Fix (AB-12)", "head": {"ref": "y"}},
    ])
    assert await adapter.open_prs_for_key("AB-12") == [(2, "Fix (AB-12)", "y")]

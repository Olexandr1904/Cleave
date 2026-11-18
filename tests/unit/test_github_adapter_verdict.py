# tests/unit/test_github_adapter_verdict.py
from __future__ import annotations
import json
import pytest, respx
from integrations.github.github_adapter import GitHubAdapter, PRInfo

API = "https://api.github.com/repos/acme/app"
GQL = "https://api.github.com/graphql"
PR = {"number": 7, "title": "ACME-1: Fix login [skill]", "state": "open", "draft": True,
      "node_id": "PR_kw7", "html_url": "https://github.com/acme/app/pull/7"}

@pytest.fixture
def adapter():
    return GitHubAdapter("tok", "acme", "app")

@respx.mock
async def test_pr_info(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json=PR)
    assert await adapter.pr_info(7) == PRInfo(7, "ACME-1: Fix login [skill]", "open", True, "PR_kw7", PR["html_url"])

@respx.mock
async def test_mark_ready_sends_the_mutation_with_the_node_id(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json=PR)
    gql = respx.post(GQL).respond(200, json={"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}})
    await adapter.mark_ready(7)
    body = json.loads(gql.calls[0].request.content)
    assert "markPullRequestReadyForReview" in body["query"] and body["variables"] == {"id": "PR_kw7"}

@respx.mock
async def test_mark_ready_of_a_ready_pr_sends_nothing(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json={**PR, "draft": False})
    gql = respx.post(GQL).respond(200, json={})
    await adapter.mark_ready(7)
    assert not gql.called

@respx.mock
async def test_mark_ready_raises_on_graphql_errors(adapter):
    respx.get(f"{API}/pulls/7").respond(200, json=PR)
    respx.post(GQL).respond(200, json={"errors": [{"message": "Resource not accessible by integration"}]})
    with pytest.raises(RuntimeError, match="not accessible"):
        await adapter.mark_ready(7)

@respx.mock
async def test_set_title(adapter):
    route = respx.patch(f"{API}/pulls/7").respond(200, json=PR)
    await adapter.set_title(7, "ACME-1: Fix login")
    assert json.loads(route.calls[0].request.content) == {"title": "ACME-1: Fix login"}

@respx.mock
async def test_comment_on_pr(adapter):
    route = respx.post(f"{API}/issues/7/comments").respond(201, json={"id": 1})
    await adapter.comment_on_pr(7, "Closed by Cleave")
    assert json.loads(route.calls[0].request.content) == {"body": "Closed by Cleave"}

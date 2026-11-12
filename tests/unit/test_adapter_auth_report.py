from __future__ import annotations
import logging
import httpx, pytest, respx
from config.schemas import (GitHubConfig, GitLabConfig, JiraConfig, RepoConfig, TrackerConfig,
                            TrelloConfig, VCSConfig)
from main import _build_tracker_for_project, _build_vcs_adapter
from integrations.github.github_adapter import GitHubAdapter
from integrations.gitlab.gitlab_adapter import GitLabAdapter
from integrations.jira.jira_adapter import JiraAdapter
from integrations.trello.trello_adapter import TrelloAdapter

def _adapters(cb):
    return [
        (GitHubAdapter("t", "o", "r", on_auth_failure=cb), "https://api.github.com/repos/o/r", "/repos/o/r"),
        (GitLabAdapter("t", "42", on_auth_failure=cb), "https://gitlab.com/api/v4/projects/42", "/projects/42"),
        (JiraAdapter("https://x", "e", "t", "K", on_auth_failure=cb), "https://x/rest/api/3/myself", "/myself"),
        (TrelloAdapter(api_key="k", token="t", board_id="b", on_auth_failure=cb), "https://api.trello.com/1/members/me", "/members/me"),
    ]

@pytest.mark.parametrize("i", range(4))
@pytest.mark.parametrize("code", [401, 403])
@respx.mock
async def test_auth_failure_is_reported_and_still_raises(i, code):
    seen = []
    adapter, url, path = _adapters(seen.append)[i]
    respx.route(url__startswith=url).respond(code)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", path)
    assert seen == [code]

@pytest.mark.parametrize("i", range(4))
@respx.mock
async def test_success_is_not_reported(i):
    seen = []
    adapter, url, path = _adapters(seen.append)[i]
    respx.route(url__startswith=url).respond(200, json={})
    await adapter._request("GET", path)
    assert seen == []

@respx.mock
async def test_failing_callback_does_not_change_the_error():
    def boom(_):
        raise RuntimeError("x")
    adapter = GitHubAdapter("t", "o", "r", on_auth_failure=boom)
    respx.route(url__startswith="https://api.github.com/repos/o/r").respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", "/repos/o/r")


# --- Code-review fix round, batch B: one response hook on every client ---

@respx.mock
async def test_github_graphql_auth_failure_is_reported():
    seen = []
    adapter = GitHubAdapter("t", "o", "r", on_auth_failure=seen.append)
    respx.post("https://api.github.com/graphql").respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._graphql_request("query { viewer { login } }")
    assert seen == [401]

@respx.mock
async def test_jira_attachment_auth_failure_is_reported():
    seen = []
    adapter = JiraAdapter("https://x", "e", "t", "K", on_auth_failure=seen.append)
    respx.get("https://x/rest/api/3/attachment/content/1").respond(403)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.download_attachment("https://x/rest/api/3/attachment/content/1")
    assert seen == [403]

@pytest.mark.parametrize("i", range(4))
@respx.mock
async def test_failing_callback_is_logged_and_the_error_still_raises(i, caplog):
    def boom(_):
        raise RuntimeError("callback broke")
    adapter, url, path = _adapters(boom)[i]
    respx.route(url__startswith=url).respond(401)
    with caplog.at_level(logging.WARNING), pytest.raises(httpx.HTTPStatusError) as e:
        await adapter._request("GET", path)
    assert e.value.response.status_code == 401
    assert "on_auth_failure callback failed: RuntimeError" in caplog.text
    assert "callback broke" not in caplog.text

@pytest.mark.parametrize("vcs,url,path", [
    (VCSConfig(provider="github", github=GitHubConfig(token="t", owner="o", repo="r")),
     "https://api.github.com/repos/o/r", "/repos/o/r"),
    (VCSConfig(provider="gitlab", gitlab=GitLabConfig(token="t", project_id="42")),
     "https://gitlab.com/api/v4/projects/42", "/projects/42"),
])
@respx.mock
async def test_build_vcs_adapter_passes_the_callback(vcs, url, path):
    seen = []
    adapter = _build_vcs_adapter(RepoConfig(vcs=vcs), on_auth_failure=seen.append)
    respx.route(url__startswith=url).respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", path)
    assert seen == [401]

@pytest.mark.parametrize("tracker,url,path", [
    (TrackerConfig(provider="jira", jira=JiraConfig(url="https://x", token="t", email="e", project_key="K")),
     "https://x/rest/api/3/myself", "/myself"),
    (TrackerConfig(provider="trello", trello=TrelloConfig(api_key="k", token="t", board_id="b")),
     "https://api.trello.com/1/members/me", "/members/me"),
])
@respx.mock
async def test_build_tracker_passes_the_callback(tracker, url, path):
    seen = []
    adapter = _build_tracker_for_project(tracker, "acme", on_auth_failure=seen.append)
    respx.route(url__startswith=url).respond(403)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", path)
    assert seen == [403]

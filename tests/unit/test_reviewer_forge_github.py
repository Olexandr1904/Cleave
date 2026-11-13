# tests/unit/test_reviewer_forge_github.py
from __future__ import annotations
from unittest.mock import AsyncMock
import httpx, pytest, respx
from integrations.github.github_adapter import GitHubAdapter
from orchestrator.reviewer.forge_github import ForgeError, GitHubForgeReader

BASE = "https://api.github.com/repos/acme-org/acme-app"
PR = {"number": 1712, "title": "Fix cart sync", "html_url": "https://github.com/acme-org/acme-app/pull/1712",
      "user": {"login": "octo-author"}, "state": "open", "head": {"sha": "a" * 40}, "draft": False,
      "assignees": [{"login": "octo"}], "requested_reviewers": [{"login": "octo2"}], "changed_files": 14,
      "updated_at": "2026-11-20T10:00:00Z"}

def reader():
    return GitHubForgeReader(GitHubAdapter("tok", "acme-org", "acme-app"))

@respx.mock
async def test_open_prs_paginates():
    route = respx.get(f"{BASE}/pulls")
    route.side_effect = [httpx.Response(200, json=[{"number": i, "updated_at": "t"} for i in range(100)]),
                         httpx.Response(200, json=[{"number": 100, "updated_at": "t2"}])]
    prs = await reader().open_prs()
    assert len(prs) == 101 and prs[100] == "t2"
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]

@respx.mock
async def test_details_cached_by_updated_at():
    route = respx.get(f"{BASE}/pulls/1712").respond(200, json=PR)
    r = reader()
    d = await r.details(1712, "2026-11-20T10:00:00Z")
    await r.details(1712, "2026-11-20T10:00:00Z")
    assert route.call_count == 1
    assert (d.author, d.head_sha, d.assignees, d.requested_reviewers, d.changed_files) == (
        "octo-author", "a" * 40, ("octo",), ("octo2",), 14)
    await r.details(1712, "2026-11-20T11:00:00Z")
    assert route.call_count == 2

@respx.mock
async def test_reviewed_by():
    respx.get(f"{BASE}/pulls/1712/reviews").respond(200, json=[{"user": {"login": "octo"}, "state": "PENDING"},
                                                              {"user": {"login": "x"}, "state": "APPROVED"}])
    assert await reader().reviewed_by(1712, "octo", "t") is False
    respx.get(f"{BASE}/pulls/1713/reviews").respond(200, json=[{"user": {"login": "octo"}, "state": "COMMENTED"}])
    assert await reader().reviewed_by(1713, "octo", "t") is True

@respx.mock
async def test_errors_become_forge_error_without_body():
    respx.get(f"{BASE}/pulls/9").respond(401, json={"message": "Bad credentials tok"})
    with pytest.raises(ForgeError, match="^GitHub 401$"):
        await reader().details(9, "t")


@respx.mock
async def test_details_refetched_while_response_lags_the_list():
    route = respx.get(f"{BASE}/pulls/1712")
    route.side_effect = [httpx.Response(200, json=PR),
                         httpx.Response(200, json={**PR, "head": {"sha": "b" * 40},
                                                   "updated_at": "2026-11-20T11:00:00Z"})]
    r = reader()
    assert (await r.details(1712, "2026-11-20T11:00:00Z")).head_sha == "a" * 40
    assert (await r.details(1712, "2026-11-20T11:00:00Z")).head_sha == "b" * 40
    await r.details(1712, "2026-11-20T11:00:00Z")
    assert route.call_count == 2


@respx.mock
async def test_pagination_stops_after_100_pages():
    def full_page(request):
        assert int(request.url.params["page"]) <= 100, "requested a page past the cap"
        return httpx.Response(200, json=[{"number": 1, "updated_at": "t"}] * 100)

    route = respx.get(f"{BASE}/pulls").mock(side_effect=full_page)
    with pytest.raises(ForgeError, match="^GitHub pagination did not end$"):
        await reader().open_prs()
    assert route.call_count == 100


@respx.mock
async def test_server_error_after_retries_is_status_only(monkeypatch):
    monkeypatch.setattr("integrations.github.github_adapter.asyncio.sleep", AsyncMock())
    route = respx.get(f"{BASE}/pulls/9").respond(502, text="upstream body tok")
    with pytest.raises(ForgeError, match="^GitHub 502$") as excinfo:
        await reader().details(9, "t")
    assert route.call_count == 3
    assert excinfo.value.__suppress_context__


@respx.mock
async def test_forbidden_is_status_only():
    respx.get(f"{BASE}/pulls/9/reviews").respond(403, json={"message": "rate limit tok"})
    with pytest.raises(ForgeError, match="^GitHub 403$"):
        await reader().reviewed_by(9, "octo", "t")


@pytest.mark.parametrize("error, name", [(httpx.ReadTimeout("timed out tok"), "ReadTimeout"),
                                         (httpx.ConnectError("refused tok"), "ConnectError")])
@respx.mock
async def test_transport_errors_are_class_name(monkeypatch, error, name):
    monkeypatch.setattr("integrations.github.github_adapter.asyncio.sleep", AsyncMock())
    respx.get(f"{BASE}/pulls").mock(side_effect=error)
    with pytest.raises(ForgeError, match=f"^{name}$"):
        await reader().open_prs()


@respx.mock
async def test_reviewed_by_cached_per_login_and_updated_at():
    pulls = respx.get(f"{BASE}/pulls").respond(200, json=[])
    reviews = respx.get(f"{BASE}/pulls/5/reviews").respond(200, json=[{"user": {"login": "octo"}, "state": "APPROVED"}])
    r = reader()
    assert await r.open_prs() == {}
    assert dict(pulls.calls[0].request.url.params) == {"state": "open", "per_page": "100", "page": "1"}
    assert await r.reviewed_by(5, "octo", "t") is True
    assert dict(reviews.calls[0].request.url.params) == {"per_page": "100", "page": "1"}
    await r.reviewed_by(5, "octo", "t")
    assert reviews.call_count == 1
    await r.reviewed_by(5, "octo2", "t")
    await r.reviewed_by(5, "octo", "t2")
    assert reviews.call_count == 3


@respx.mock
async def test_reviewed_by_skips_review_without_user():
    respx.get(f"{BASE}/pulls/5/reviews").respond(200, json=[{"user": None, "state": "APPROVED"}])
    assert await reader().reviewed_by(5, "octo", "t") is False

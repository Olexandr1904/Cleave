# tests/unit/test_reviewer_forge_github.py
from __future__ import annotations
import dataclasses
import json
from unittest.mock import AsyncMock
import httpx, pytest, respx
from integrations.github.github_adapter import GitHubAdapter
from orchestrator.reviewer.forge_github import ForgeError, GitHubForgeReader
from orchestrator.reviewer.models import ForgeReview

BASE = "https://api.github.com/repos/acme-org/acme-app"
PR = {"number": 1712, "title": "Fix cart sync", "html_url": "https://github.com/acme-org/acme-app/pull/1712",
      "user": {"login": "octo-author"}, "state": "open", "head": {"sha": "a" * 40}, "draft": False,
      "assignees": [{"login": "octo"}], "requested_reviewers": [{"login": "octo2"}], "changed_files": 14,
      "updated_at": "2026-11-20T10:00:00Z", "closed_at": None}

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


@respx.mock
async def test_pr_full():
    route = respx.get(f"{BASE}/pulls/1712").respond(200, json={
        **PR, "body": None, "base": {"ref": "develop", "sha": "b" * 40}, "additions": 120, "deletions": 30,
        "user": {"login": "octo"},
        "head": {"sha": "a" * 40, "ref": "feature/x", "repo": {"full_name": "acme-org/acme-app"}}})
    r = reader()
    full = await r.pr_full(1712)
    assert dataclasses.asdict(full) == {
        "number": 1712, "title": "Fix cart sync", "body": "", "url": "https://github.com/acme-org/acme-app/pull/1712",
        "state": "open", "head_sha": "a" * 40, "base": "develop", "additions": 120, "deletions": 30,
        "changed_files": 14, "closed_at": "", "author": "octo", "head_ref": "feature/x",
        "head_repo": "acme-org/acme-app"}
    assert (full.author, full.head_ref, full.head_repo) == ("octo", "feature/x", "acme-org/acme-app")
    await r.pr_full(1712)
    assert route.call_count == 2   # uncached


@respx.mock
async def test_comments_all_pages_and_shapes():
    inline = respx.get(f"{BASE}/pulls/1712/comments")
    inline.side_effect = [
        httpx.Response(200, json=[{"id": i, "user": {"login": "octo2"}, "path": "app/Cart.kt", "line": i + 1,
                                   "original_line": 1, "body": f"c{i}", "diff_hunk": "@@"} for i in range(100)]),
        httpx.Response(200, json=[{"id": 100, "user": None, "path": "app/Sync.kt", "line": None,
                                   "original_line": 8, "body": "outdated", "diff_hunk": "@@"}])]
    respx.get(f"{BASE}/pulls/1712/reviews").respond(200, json=[
        {"user": {"login": "octo2"}, "state": "CHANGES_REQUESTED", "body": "See inline"},
        {"user": {"login": "octo3"}, "state": "APPROVED", "body": ""},
        {"user": None, "state": "COMMENTED", "body": "lgtm"}])
    respx.get(f"{BASE}/issues/1712/comments").respond(200, json=[
        {"user": {"login": "octo-author"}, "body": "Ready"}, {"user": None, "body": "old"}])
    c = await reader().comments(1712)
    assert [call.request.url.params["page"] for call in inline.calls] == ["1", "2"]
    assert len(c.inline) == 101
    assert c.inline[0] == {"login": "octo2", "path": "app/Cart.kt", "line": 1, "id": 0, "body": "c0"}
    assert c.inline[100] == {"login": "ghost", "path": "app/Sync.kt", "line": 8, "id": 100, "body": "outdated"}
    assert c.reviews == ({"login": "octo2", "state": "CHANGES_REQUESTED", "body": "See inline"},
                         {"login": "ghost", "state": "COMMENTED", "body": "lgtm"})
    assert c.conversation == ({"login": "octo-author", "body": "Ready"}, {"login": "ghost", "body": "old"})


@respx.mock
async def test_pr_full_401_is_forge_error():
    respx.get(f"{BASE}/pulls/1712").respond(401, json={"message": "Bad credentials tok"})
    with pytest.raises(ForgeError, match="^GitHub 401$"):
        await reader().pr_full(1712)


# --- Code-review fix wave 2 ---

@pytest.mark.parametrize("respond, text, unavailable", [
    (lambda route: route.respond(502, text="upstream"), "GitHub 502", True),
    (lambda route: route.respond(404, json={"message": "Not Found"}), "GitHub 404", False),
    (lambda route: route.mock(side_effect=httpx.ReadTimeout("timed out")), "ReadTimeout", True),
    (lambda route: route.mock(side_effect=httpx.ConnectError("refused")), "ConnectError", True),
    (lambda route: route.respond(200, text="<html>not json</html>"), "JSONDecodeError", False),
])
@respx.mock
async def test_forge_error_tells_whether_github_is_unavailable(monkeypatch, respond, text, unavailable):
    monkeypatch.setattr("integrations.github.github_adapter.asyncio.sleep", AsyncMock())
    respond(respx.get(f"{BASE}/pulls/9"))
    with pytest.raises(ForgeError, match=f"^{text}$") as e:
        await reader().pr_full(9)
    assert e.value.unavailable is unavailable


# --- Posting: reviews, review comments, approve, closed_at ---

HEAD = "a" * 40


def review_json(i, state="COMMENTED", **fields):
    return {"id": i, "user": {"login": "octo"}, "state": state, "commit_id": HEAD,
            "submitted_at": "2026-11-20T10:00:00Z", "body": f"r{i}",
            "html_url": f"https://github.com/acme-org/acme-app/pull/1712#pullrequestreview-{i}", **fields}


@respx.mock
async def test_reviews_all_pages_and_shapes():
    def page(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[review_json(i) for i in range(100)])
        return httpx.Response(200, json=[review_json(100, "PENDING", user=None, commit_id=None, submitted_at=None,
                                                     body=None)])

    route = respx.get(f"{BASE}/pulls/1712/reviews").mock(side_effect=page)
    r = reader()
    reviews = await r.reviews(1712)
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
    assert len(reviews) == 101
    assert reviews[0] == ForgeReview(
        id=0, login="octo", state="COMMENTED", commit_id=HEAD, submitted_at="2026-11-20T10:00:00Z", body="r0",
        url="https://github.com/acme-org/acme-app/pull/1712#pullrequestreview-0")
    assert reviews[100] == ForgeReview(
        id=100, login="ghost", state="PENDING", commit_id="", submitted_at="", body="",
        url="https://github.com/acme-org/acme-app/pull/1712#pullrequestreview-100")
    with pytest.raises(dataclasses.FrozenInstanceError):
        reviews[0].state = "APPROVED"
    await r.reviews(1712)
    assert route.call_count == 4   # uncached


@respx.mock
async def test_review_comments():
    def page(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"id": i, "user": {"login": "octo"}, "path": "app/Cart.kt",
                                              "line": i + 1, "original_line": 1, "body": f"c{i}",
                                              "diff_hunk": "@@"} for i in range(100)])
        return httpx.Response(200, json=[{"id": 100, "user": None, "path": "app/Sync.kt", "line": None,
                                          "original_line": 7, "body": "outdated", "diff_hunk": "@@"}])

    route = respx.get(f"{BASE}/pulls/1712/reviews/55/comments").mock(side_effect=page)
    comments = await reader().review_comments(1712, 55)
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
    assert len(comments) == 101
    assert comments[0] == {"path": "app/Cart.kt", "line": 1, "body": "c0"}
    assert comments[100] == {"path": "app/Sync.kt", "line": 7, "body": "outdated"}


@respx.mock
async def test_approve_posts_once():
    route = respx.post(f"{BASE}/pulls/1712/reviews").respond(200, json=review_json(77, "APPROVED", body=""))
    review = await reader().approve(1712, HEAD)
    assert review == ForgeReview(
        id=77, login="octo", state="APPROVED", commit_id=HEAD, submitted_at="2026-11-20T10:00:00Z", body="",
        url="https://github.com/acme-org/acme-app/pull/1712#pullrequestreview-77")
    assert json.loads(route.calls[0].request.content) == {"commit_id": HEAD, "event": "APPROVE"}
    assert route.call_count == 1


@pytest.mark.parametrize("respond, text, unavailable", [
    (lambda route: route.mock(side_effect=httpx.ReadTimeout("timed out tok")), "ReadTimeout", True),
    (lambda route: route.mock(side_effect=httpx.ConnectError("refused tok")), "ConnectError", True),
    (lambda route: route.respond(502, text="upstream tok"), "GitHub 502", True),
    # A 2xx that cannot be read: the approve may have landed.
    (lambda route: route.respond(200, text="<html>not json tok</html>"), "JSONDecodeError", True),
    (lambda route: route.respond(200, json={}), "KeyError", True),
    # Refused: definitely not posted. A redirect is not followed.
    (lambda route: route.respond(301, headers={"Location": "https://api.github.com/x"}), "GitHub 301", False),
    (lambda route: route.respond(422, json={"message": "Unprocessable tok"}), "GitHub 422", False),
])
@respx.mock
async def test_approve_never_retries(monkeypatch, respond, text, unavailable):
    monkeypatch.setattr("integrations.github.github_adapter.asyncio.sleep", AsyncMock())
    route = respx.post(f"{BASE}/pulls/1712/reviews")
    respond(route)
    with pytest.raises(ForgeError, match=f"^{text}$") as e:
        await reader().approve(1712, HEAD)
    assert e.value.unavailable is unavailable
    assert route.call_count == 1


@pytest.mark.parametrize("closed_at, expected", [("2026-11-01T10:00:00Z", "2026-11-01T10:00:00Z"), (None, "")])
@respx.mock
async def test_pr_full_closed_at(closed_at, expected):
    respx.get(f"{BASE}/pulls/1712").respond(200, json={
        **PR, "body": "", "base": {"ref": "develop", "sha": "b" * 40}, "additions": 1,
        "deletions": 1, "closed_at": closed_at})
    assert (await reader().pr_full(1712)).closed_at == expected


@respx.mock
async def test_pr_full_head_repo_gone():
    respx.get(f"{BASE}/pulls/1712").respond(200, json={
        **PR, "body": "", "base": {"ref": "develop", "sha": "b" * 40}, "additions": 1, "deletions": 1,
        "head": {"sha": "a" * 40, "ref": "feature/x", "repo": None}})
    assert (await reader().pr_full(1712)).head_repo == ""


GRAPHQL = "https://api.github.com/graphql"


@respx.mock
async def test_request_reviewers_posts_once():
    route = respx.post(f"{BASE}/pulls/1712/requested_reviewers").respond(201, json={})
    await reader().request_reviewers(1712, ["rev1", "rev2"])
    assert json.loads(route.calls[0].request.content) == {"reviewers": ["rev1", "rev2"]}
    route.respond(502, json={"message": "tok"})
    with pytest.raises(ForgeError, match="^GitHub 502$") as e:
        await reader().request_reviewers(1712, ["rev1"])
    assert e.value.unavailable and route.call_count == 2  # never retried: a retry could notify twice


@respx.mock
async def test_merge_sends_head_and_method():
    route = respx.put(f"{BASE}/pulls/1712/merge").respond(200, json={"merged": True})
    await reader().merge(1712, "a" * 40, "squash")
    assert json.loads(route.calls[0].request.content) == {"sha": "a" * 40, "merge_method": "squash"}
    route.respond(405, json={"message": "Pull Request is not mergeable"})
    with pytest.raises(ForgeError, match="^GitHub 405$") as e:
        await reader().merge(1712, "a" * 40, "merge")
    assert not e.value.unavailable and route.call_count == 2


@respx.mock
async def test_merge_unanswered_is_unavailable():
    respx.put(f"{BASE}/pulls/1712/merge").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ForgeError) as e:
        await reader().merge(1712, "a" * 40, "merge")
    assert e.value.unavailable


@respx.mock
async def test_merge_methods_in_order():
    respx.get(BASE).respond(200, json={"allow_merge_commit": False, "allow_squash_merge": True,
                                       "allow_rebase_merge": True})
    assert await reader().merge_methods() == ["squash", "rebase"]
    respx.get(BASE).respond(200, json={})  # the fields are missing without push access: all are offered
    assert await reader().merge_methods() == ["merge", "squash", "rebase"]


def _candidates_answer(**overrides):
    user = lambda login: {"__typename": "User", "login": login}  # noqa: E731
    data = {"repository": {"pullRequest": {
        "author": {"login": "octo-author"},
        "suggestedReviewers": [{"reviewer": {"login": "sugg"}}, {"reviewer": {"login": "octo-author"}}],
        "reviewRequests": {"nodes": [{"requestedReviewer": user("asked")}]},
    }}, "search": {"nodes": [
        {"reviews": {"nodes": [{"author": user("often")}, {"author": user("often")}, {"author": user("once")},
                               {"author": {"__typename": "Bot", "login": "ci-bot"}}, {"author": user("octo-author")},
                               {"author": user("asked")}]}},
        {"reviews": {"nodes": [{"author": user("often")}, {"author": user("sugg")}]}},
        {},  # an issue in the search: no PR fields
    ]}}
    data.update(overrides)
    return {"data": data}


@respx.mock
async def test_review_candidates_ranked():
    route = respx.post(GRAPHQL).respond(200, json=_candidates_answer())
    assert await reader().review_candidates(1712, "octo-author") == [
        {"login": "sugg", "reason": "suggested by GitHub"},
        {"login": "often", "reason": "reviewed 2 of the author's recent PRs"},
        {"login": "once", "reason": "reviewed 1 of the author's recent PRs"},
    ]
    assert route.call_count == 1  # one query: the PR, and the author's recent PRs
    variables = json.loads(route.calls[0].request.content)["variables"]
    assert variables == {"owner": "acme-org", "name": "acme-app", "number": 1712,
                         "q": "repo:acme-org/acme-app is:pr author:octo-author sort:updated-desc"}


@respx.mock
async def test_review_candidates_graphql_error():
    respx.post(GRAPHQL).respond(200, json={"errors": [{"message": "Something tok"}]})
    with pytest.raises(ForgeError, match="^GitHub GraphQL error$"):
        await reader().review_candidates(1712, "octo-author")


@respx.mock
async def test_mark_ready_error_is_forge_error():
    respx.get(f"{BASE}/pulls/1712").respond(200, json={**PR, "draft": True, "node_id": "PR_1"})
    respx.post(GRAPHQL).respond(200, json={"errors": [{"message": "no tok"}]})
    with pytest.raises(ForgeError, match="^GitHub GraphQL error$"):  # the service says "GitHub refused it (…)"
        await reader().mark_ready(1712)



@respx.mock
async def test_review_candidates_null_fields():
    """GraphQL answers null for a connection it could not resolve: no candidates from it, never a crash."""
    respx.post(GRAPHQL).respond(200, json={"data": {"repository": {"pullRequest": {
        "author": None, "suggestedReviewers": None, "reviewRequests": None}}, "search": None}})
    assert await reader().review_candidates(1712, "octo-author") == []
    respx.post(GRAPHQL).respond(200, json={"data": {"repository": None, "search": {"nodes": [
        {"reviews": None}, {"reviews": {"nodes": [{"author": None}]}}]}}})
    assert await reader().review_candidates(1712, "octo-author") == []


def _merge_state_answer(rollup="default", **pr):
    if rollup == "default":
        rollup = {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "build", "status": "QUEUED", "conclusion": None},
            {"__typename": "CheckRun", "name": "scan", "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "guard", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "e2e", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"__typename": "StatusContext", "context": "ci/old", "state": "PENDING"},
            {"__typename": "StatusContext", "context": "ci/required", "state": "EXPECTED"},
            {"__typename": "StatusContext", "context": "ci/ok", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ci/err", "state": "ERROR"},
        ]}}
    node = {"mergeStateStatus": "BLOCKED", "baseRefName": "develop", "headRefName": "feature/x",
            "headRefOid": "a" * 40, "isCrossRepository": False, "reviewDecision": "APPROVED",
            "commits": {"nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": rollup}}]}}
    node.update(pr)
    return {"data": {"repository": {"pullRequest": node}}}


@respx.mock
async def test_merge_state_reads_the_checks():
    route = respx.post(GRAPHQL).respond(200, json=_merge_state_answer())
    state = await reader().merge_state(1712)
    assert (state.status, state.base, state.head_ref, state.head_sha, state.cross_repo, state.review) == (
        "BLOCKED", "develop", "feature/x", "a" * 40, False, "APPROVED")
    assert [(c.name, c.state, c.word) for c in state.checks] == [
        ("build", "pending", "queued"), ("scan", "pending", "in progress"), ("lint", "passed", "success"),
        ("guard", "passed", "skipped"), ("tests", "failed", "failure"), ("e2e", "failed", "cancelled"),
        ("ci/old", "pending", "pending"), ("ci/required", "pending", "expected"), ("ci/ok", "passed", "success"),
        ("ci/err", "failed", "error"),
    ]
    sent = json.loads(route.calls[0].request.content)
    assert sent["variables"] == {"owner": "acme-org", "name": "acme-app", "number": 1712}
    for field in ("mergeStateStatus", "baseRefName", "headRefName", "headRefOid", "isCrossRepository",
                  "reviewDecision", "statusCheckRollup", "contexts(first: 100)", "... on CheckRun { name status "
                  "conclusion }", "... on StatusContext { context state }"):
        assert field in sent["query"], field


@respx.mock
async def test_merge_state_without_checks_or_review():
    respx.post(GRAPHQL).respond(200, json=_merge_state_answer(rollup=None, reviewDecision=None))
    state = await reader().merge_state(1712)
    assert state.checks is None and state.review == ""
    respx.post(GRAPHQL).respond(200, json=_merge_state_answer(commits={"nodes": []}))
    assert (await reader().merge_state(1712)).checks is None  # no commit: nothing ran


@respx.mock
@pytest.mark.parametrize("answer", [
    {"errors": [{"message": "Something tok"}]},
    {"data": {"repository": {"pullRequest": None}}},
    {"data": {"repository": None}},
])
async def test_merge_state_errors(answer):
    respx.post(GRAPHQL).respond(200, json=answer)
    with pytest.raises(ForgeError, match="^GitHub GraphQL error$"):
        await reader().merge_state(1712)


@respx.mock
async def test_merge_state_unanswered_is_unavailable():
    respx.post(GRAPHQL).respond(502, json={"message": "tok"})
    with pytest.raises(ForgeError, match="^GitHub 502$") as e:
        await reader().merge_state(1712)
    assert e.value.unavailable


@respx.mock
async def test_empty_commit_keeps_the_tree_and_moves_the_branch_without_force():
    get = respx.get(f"{BASE}/git/commits/{'a' * 40}").respond(200, json={"tree": {"sha": "t" * 40}})
    post = respx.post(f"{BASE}/git/commits").respond(201, json={"sha": "c" * 40})
    respx.get(f"{BASE}/git/ref/heads/feature/x-1").respond(200, json={"object": {"sha": "a" * 40}})
    patch = respx.patch(f"{BASE}/git/refs/heads/feature/x-1").respond(200, json={})
    assert await reader().empty_commit("feature/x-1", "a" * 40, "ABC-1: re-run CI") == "c" * 40
    assert get.call_count == 1
    assert json.loads(post.calls[0].request.content) == {
        "message": "ABC-1: re-run CI", "tree": "t" * 40, "parents": ["a" * 40]}
    assert json.loads(patch.calls[0].request.content) == {"sha": "c" * 40, "force": False}


@respx.mock
@pytest.mark.parametrize("status, unavailable", [(422, False), (502, True)])
async def test_empty_commit_ref_refused_or_unanswered(status, unavailable):
    respx.get(f"{BASE}/git/commits/{'a' * 40}").respond(200, json={"tree": {"sha": "t" * 40}})
    respx.post(f"{BASE}/git/commits").respond(201, json={"sha": "c" * 40})
    respx.get(f"{BASE}/git/ref/heads/b").respond(200, json={"object": {"sha": "a" * 40}})
    patch = respx.patch(f"{BASE}/git/refs/heads/b").respond(status, json={"message": "Update is not a fast forward"})
    with pytest.raises(ForgeError, match=f"^GitHub {status}$") as e:
        await reader().empty_commit("b", "a" * 40, "Re-run CI")
    assert e.value.unavailable is unavailable and patch.call_count == 1  # never retried


@respx.mock
async def test_empty_commit_stops_when_the_commit_is_refused():
    respx.get(f"{BASE}/git/commits/{'a' * 40}").respond(200, json={"tree": {"sha": "t" * 40}})
    respx.post(f"{BASE}/git/commits").respond(403, json={"message": "tok"})
    patch = respx.patch(f"{BASE}/git/refs/heads/b").respond(200, json={})
    with pytest.raises(ForgeError, match="^GitHub 403$"):
        await reader().empty_commit("b", "a" * 40, "Re-run CI")
    assert patch.call_count == 0



@respx.mock
async def test_empty_commit_moves_nothing_when_the_branch_is_not_at_the_head():
    """A branch force-pushed back to an ancestor: moving it to a child of the old head would be a fast-forward that
    brings the removed commits back. The branch is read just before the move."""
    respx.get(f"{BASE}/git/commits/{'a' * 40}").respond(200, json={"tree": {"sha": "t" * 40}})
    respx.post(f"{BASE}/git/commits").respond(201, json={"sha": "c" * 40})
    respx.get(f"{BASE}/git/ref/heads/b").respond(200, json={"object": {"sha": "e" * 40}})
    patch = respx.patch(f"{BASE}/git/refs/heads/b").respond(200, json={})
    with pytest.raises(ForgeError, match="^branch moved$") as e:
        await reader().empty_commit("b", "a" * 40, "Re-run CI")
    assert not e.value.unavailable and patch.call_count == 0


@respx.mock
async def test_pr_files_all_pages_with_their_status():
    route = respx.get(f"{BASE}/pulls/1712/files")
    route.side_effect = [httpx.Response(200, json=[{"filename": f"f{i}.kt", "status": "modified"} for i in range(100)]),
                         httpx.Response(200, json=[{"filename": "new.kt", "status": "added"}])]
    files = await reader().pr_files(1712)
    assert len(files) == 101 and files[0] == ("f0.kt", "modified") and files[-1] == ("new.kt", "added")


@respx.mock
async def test_prior_review_comments_come_from_the_prs_that_last_touched_the_files():
    respx.get(f"{BASE}/commits", params={"path": "app/A.kt"}).respond(200, json=[{"sha": "s1"}, {"sha": "s2"}])
    respx.get(f"{BASE}/commits", params={"path": "app/B.kt"}).respond(200, json=[{"sha": "s1"}])
    respx.get(f"{BASE}/commits/s1/pulls").respond(200, json=[{"number": 1653, "title": "Week page"}])
    respx.get(f"{BASE}/commits/s2/pulls").respond(200, json=[{"number": 1712, "title": "This PR"},
                                                             {"number": 1600, "title": "Old"}])
    respx.get(f"{BASE}/pulls/1653/comments").respond(200, json=[
        {"user": {"login": "octo"}, "path": "app/A.kt", "line": 5, "original_line": 5, "body": "Size the root"},
        {"user": {"login": "octo"}, "path": "app/Other.kt", "line": 1, "body": "Not a file of this PR"}])
    respx.get(f"{BASE}/pulls/1600/comments").respond(200, json=[
        {"user": None, "path": "app/B.kt", "line": None, "original_line": 9, "body": "Old\nnote"}])
    got = await reader().prior_review_comments(1712, ["app/A.kt", "app/B.kt"])
    assert got == [
        {"pr": 1653, "title": "Week page", "login": "octo", "path": "app/A.kt", "line": 5, "body": "Size the root"},
        {"pr": 1600, "title": "Old", "login": "ghost", "path": "app/B.kt", "line": 9, "body": "Old\nnote"},
    ]


@respx.mock
async def test_prior_review_comments_read_only_the_newest_prs(monkeypatch):
    monkeypatch.setattr("orchestrator.reviewer.forge_github._PRIOR_MAX_PRS", 1)
    respx.get(f"{BASE}/commits", params={"path": "app/A.kt"}).respond(200, json=[{"sha": "s1"}])
    respx.get(f"{BASE}/commits/s1/pulls").respond(200, json=[{"number": 1600, "title": "Old"},
                                                             {"number": 1653, "title": "New"}])
    newest = respx.get(f"{BASE}/pulls/1653/comments").respond(200, json=[])
    oldest = respx.get(f"{BASE}/pulls/1600/comments").respond(200, json=[])
    assert await reader().prior_review_comments(1712, ["app/A.kt"]) == []
    assert (newest.called, oldest.called) == (True, False)

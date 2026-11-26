"""GitHub reads for the reviews board, review jobs and posting: open PRs, PR details, whether a login has
reviewed, the full PR, its comments, its reviews and one review's comments; the one approve call; and the board's
moves on a PR: request reviewers (and who to ask), mark ready, merge (and the methods the repo allows, and what
blocks a merge), and an empty commit that starts the PR's checks again."""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from integrations.github.github_adapter import GitHubAdapter
from orchestrator.reviewer.models import Check, ForgeReview, MergeState, PRComments, PRDetails, PRFull

_PAGE = 100
_MAX_PAGES = 100
_STATUS = re.compile(r"→ (\d{3})")
_MERGE_METHODS = (("merge", "allow_merge_commit"), ("squash", "allow_squash_merge"), ("rebase", "allow_rebase_merge"))
_MAX_CANDIDATES = 10
# The review comments of earlier PRs on a PR's files: the last commits of each file, and of the PRs they came from
# only the newest, so a PR of hundreds of files stays within a few hundred reads.
_PRIOR_COMMITS_PER_PATH = 3
_PRIOR_MAX_PRS = 10
# The PR's author, GitHub's suggestions and who is asked already; and the reviews of the author's newest PRs.
_CANDIDATES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $q: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      author { login }
      suggestedReviewers { reviewer { login } }
      reviewRequests(first: 20) { nodes { requestedReviewer { __typename ... on User { login } } } }
    }
  }
  search(query: $q, type: ISSUE, first: 20) {
    nodes { ... on PullRequest { reviews(first: 30) { nodes { author { __typename login } } } } }
  }
}
"""

# What GitHub says of merging the PR, and the checks on its head (at most 100: a check past them is not read).
_MERGE_STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      mergeStateStatus baseRefName headRefName headRefOid isCrossRepository reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
        __typename
        ... on CheckRun { name status conclusion }
        ... on StatusContext { context state }
      } } } } } }
    }
  }
}
"""
_PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")


def _check(node: dict) -> Check:
    """A check run (pending until completed, then passed or failed by its conclusion) or a commit status (pending
    while PENDING or EXPECTED)."""
    if node.get("__typename") == "CheckRun":
        name, status, conclusion = node.get("name") or "", node.get("status") or "", node.get("conclusion") or ""
        if status != "COMPLETED":
            state, word = "pending", status
        else:
            state, word = ("passed" if conclusion in _PASSED else "failed"), conclusion or "unknown"
    else:
        name, word = node.get("context") or "", node.get("state") or ""
        state = "pending" if word in ("PENDING", "EXPECTED") else "passed" if word == "SUCCESS" else "failed"
    return Check(name, state, word.lower().replace("_", " "))


class ForgeError(Exception):
    """A failed GitHub read, as its HTTP status ("GitHub 404") or the exception's class. unavailable: GitHub
    answered 5xx or was not reached (a timeout or a connection error), so the read may succeed later."""

    def __init__(self, text: str, unavailable: bool = False) -> None:
        super().__init__(text)
        self.unavailable = unavailable


# GitHub not reached, or no answer from it: httpx's timeouts and network errors, and the builtin ones should any
# get past httpx.
_UNREACHED = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, TimeoutError, ConnectionError)


def _forge_error(e: Exception) -> ForgeError:
    # Never the response body: it is GitHub's text, not ours, and may echo request data.
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
    elif isinstance(e, RuntimeError) and (m := _STATUS.search(str(e))):
        status = int(m.group(1))
    else:
        return ForgeError(type(e).__name__, unavailable=isinstance(e, _UNREACHED))
    return ForgeError(f"GitHub {status}", unavailable=status >= 500)


def _login(item: dict) -> str:
    # The user is null when the account was deleted; GitHub shows it as "ghost".
    return (item.get("user") or {"login": "ghost"})["login"]


def _review(r: dict) -> ForgeReview:
    # submitted_at is null while the review is pending; commit_id is null when the commit is gone from git.
    return ForgeReview(id=r["id"], login=_login(r), state=r["state"], commit_id=r["commit_id"] or "",
                       submitted_at=r.get("submitted_at") or "", body=r.get("body") or "", url=r["html_url"])


class GitHubForgeReader:
    def __init__(self, adapter: GitHubAdapter) -> None:
        self._adapter = adapter
        # One entry per PR (per PR and login), valid while its updated_at stamp matches.
        self._details: dict[int, tuple[str, PRDetails]] = {}
        self._reviewed: dict[tuple[int, str], tuple[str, bool]] = {}

    async def _get(self, path: str, **kwargs) -> dict | list:
        try:
            return await self._adapter._request("GET", path, **kwargs)
        except Exception as e:
            raise _forge_error(e) from None

    async def _all_pages(self, path: str, **params) -> list:
        items: list = []
        # Capped: a server that ignores `page` would otherwise be read forever.
        for page in range(1, _MAX_PAGES + 1):
            batch = await self._get(path, params={**params, "per_page": _PAGE, "page": page})
            items.extend(batch)
            if len(batch) < _PAGE:
                return items
        raise ForgeError("GitHub pagination did not end")

    async def open_prs(self) -> dict[int, str]:
        prs = await self._all_pages(f"{self._adapter._repo_path}/pulls", state="open")
        return {pr["number"]: pr["updated_at"] for pr in prs}

    async def details(self, number: int, updated_at: str) -> PRDetails:
        cached = self._details.get(number)
        if cached is None or cached[0] != updated_at:
            pr = await self._get(f"{self._adapter._repo_path}/pulls/{number}")
            # Stamped with the response's own updated_at: a read that lags the list
            # keeps the older stamp and is fetched again on the next call.
            cached = self._details[number] = (pr["updated_at"], PRDetails(
                number=pr["number"],
                title=pr["title"],
                url=pr["html_url"],
                author=pr["user"]["login"],
                state=pr["state"],
                head_sha=pr["head"]["sha"],
                draft=pr["draft"],
                assignees=tuple(a["login"] for a in pr["assignees"]),
                requested_reviewers=tuple(r["login"] for r in pr["requested_reviewers"]),
                changed_files=pr["changed_files"],
                updated_at=pr["updated_at"],
            ))
        return cached[1]

    async def reviewed_by(self, number: int, login: str, updated_at: str) -> bool:
        key = (number, login)
        cached = self._reviewed.get(key)
        if cached is None or cached[0] != updated_at:
            reviews = await self._all_pages(f"{self._adapter._repo_path}/pulls/{number}/reviews")
            # A review's user is null when the account was deleted.
            done = any(
                (r.get("user") or {}).get("login") == login and r["state"] != "PENDING"
                for r in reviews
            )
            cached = self._reviewed[key] = (updated_at, done)
        return cached[1]

    async def pr_full(self, number: int) -> PRFull:
        # Uncached: a job reads the PR once, right before it starts.
        pr = await self._get(f"{self._adapter._repo_path}/pulls/{number}")
        return PRFull(
            number=pr["number"],
            title=pr["title"],
            body=pr["body"] or "",
            url=pr["html_url"],
            state=pr["state"],
            head_sha=pr["head"]["sha"],
            base=pr["base"]["ref"],
            additions=pr["additions"],
            deletions=pr["deletions"],
            changed_files=pr["changed_files"],
            closed_at=pr["closed_at"] or "",
            author=_login(pr),
            head_ref=pr["head"].get("ref", ""),
            # The head's repo is null when the fork it lived in was deleted.
            head_repo=(pr["head"].get("repo") or {}).get("full_name", ""),
        )

    async def comments(self, number: int) -> PRComments:
        repo = self._adapter._repo_path
        inline = await self._all_pages(f"{repo}/pulls/{number}/comments")
        reviews = await self._all_pages(f"{repo}/pulls/{number}/reviews")
        conversation = await self._all_pages(f"{repo}/issues/{number}/comments")
        return PRComments(
            # An outdated comment has no line on the current diff; original_line is where it was made.
            inline=tuple({"login": _login(c), "path": c["path"], "line": c.get("line") or c.get("original_line"),
                          "id": c["id"], "body": c["body"]} for c in inline),
            reviews=tuple({"login": _login(r), "state": r["state"], "body": r["body"]}
                          for r in reviews if r.get("body")),
            conversation=tuple({"login": _login(c), "body": c["body"]} for c in conversation),
        )

    async def pr_files(self, number: int) -> list[tuple[str, str]]:
        """Each changed file of the PR as (path, GitHub's status: added, modified, removed, renamed…)."""
        files = await self._all_pages(f"{self._adapter._repo_path}/pulls/{number}/files")
        return [(f["filename"], f["status"]) for f in files]

    async def prior_review_comments(self, number: int, paths: list[str]) -> list[dict]:
        """The inline review comments that earlier PRs got on these paths, newest PR first: the PRs the last commits
        of each path came from, but this one. Each {"pr", "title", "login", "path", "line", "body"}; line is where
        an outdated comment was made."""
        repo = self._adapter._repo_path
        titles: dict[int, str] = {}
        seen: set[str] = set()
        for path in paths:
            commits = await self._get(f"{repo}/commits", params={"path": path, "per_page": _PRIOR_COMMITS_PER_PATH})
            for sha in (c["sha"] for c in commits if c["sha"] not in seen):
                seen.add(sha)
                for pr in await self._get(f"{repo}/commits/{sha}/pulls"):
                    if pr["number"] != number:
                        titles.setdefault(pr["number"], pr["title"])
        wanted = set(paths)
        found = []
        for pr_number in sorted(titles, reverse=True)[:_PRIOR_MAX_PRS]:
            for c in await self._all_pages(f"{repo}/pulls/{pr_number}/comments"):
                if c["path"] in wanted:
                    found.append({"pr": pr_number, "title": titles[pr_number], "login": _login(c), "path": c["path"],
                                  "line": c.get("line") or c.get("original_line"), "body": c["body"]})
        return found

    async def reviews(self, number: int) -> list[ForgeReview]:
        # Uncached: posting compares the reviews before and after a post.
        reviews = await self._all_pages(f"{self._adapter._repo_path}/pulls/{number}/reviews")
        return [_review(r) for r in reviews]

    async def review_comments(self, number: int, review_id: int) -> list[dict]:
        comments = await self._all_pages(f"{self._adapter._repo_path}/pulls/{number}/reviews/{review_id}/comments")
        # An outdated comment has no line on the current diff; original_line is where it was made.
        return [{"path": c["path"], "line": c.get("line") or c.get("original_line"), "body": c["body"]}
                for c in comments]

    async def approve(self, number: int, head: str) -> ForgeReview:
        """Approve the PR at `head` with exactly one POST, never retried.

        A ForgeError's `unavailable` is the outcome. True: the approve may have landed (no answer, a 5xx, or a
        2xx that could not be read), so verify on GitHub and never retry. False: GitHub refused it (a 3xx, which
        is not followed, or a 4xx), so it was definitely not posted.
        """
        # One request, never _request: its retries after a timeout or a 5xx could approve twice.
        try:
            response = await self._adapter._client.request(
                "POST", f"{self._adapter._repo_path}/pulls/{number}/reviews",
                json={"commit_id": head, "event": "APPROVE"})
            if response.is_success:
                return _review(response.json())
        except Exception as e:
            # Never the message: it may echo request data.
            raise ForgeError(type(e).__name__, unavailable=True) from None
        raise ForgeError(f"GitHub {response.status_code}", unavailable=response.status_code >= 500)

    async def _write(self, method: str, path: str, body: dict) -> dict:
        """One write, never retried (a retry after a timeout could notify twice, or answer a merge for another), and
        GitHub's answer ({} when it is not a JSON object): ForgeError("GitHub <status>") when GitHub refuses it,
        unavailable when it may have landed (no answer, a 5xx)."""
        try:
            response = await self._adapter._client.request(method, f"{self._adapter._repo_path}{path}", json=body)
        except Exception as e:
            # Never the message: it may echo request data.
            raise ForgeError(type(e).__name__, unavailable=True) from None
        if not response.is_success:
            raise ForgeError(f"GitHub {response.status_code}", unavailable=response.status_code >= 500)
        try:
            answer = response.json()
        except ValueError:
            return {}
        return answer if isinstance(answer, dict) else {}

    async def request_reviewers(self, number: int, logins: list[str]) -> None:
        """Ask `logins` to review the PR (again, for one who reviewed before)."""
        await self._write("POST", f"/pulls/{number}/requested_reviewers", {"reviewers": logins})

    async def merge(self, number: int, head: str, method: str) -> None:
        """Merge the PR with `method` (merge, squash, rebase) only while its head is `head`."""
        await self._write("PUT", f"/pulls/{number}/merge", {"sha": head, "merge_method": method})

    async def mark_ready(self, number: int) -> None:
        """Mark a draft PR ready for review (one that is not a draft is left alone)."""
        try:
            await self._adapter.mark_ready(number)
        except RuntimeError as e:
            if _STATUS.search(str(e)):
                raise _forge_error(e) from None
            raise ForgeError("GitHub GraphQL error") from None  # GitHub's text is not ours to show
        except Exception as e:
            raise _forge_error(e) from None

    async def merge_state(self, number: int) -> MergeState:
        """What GitHub says of merging the PR (MergeState): one GraphQL read. ForgeError when GitHub refuses it or
        answers no PR."""
        variables = {"owner": self._adapter._owner, "name": self._adapter._repo, "number": number}
        try:
            answer = await self._adapter._graphql_request(_MERGE_STATE_QUERY, variables)
        except Exception as e:
            raise _forge_error(e) from None
        pr = (((answer.get("data") or {}).get("repository") or {}).get("pullRequest")
              if not answer.get("errors") else None)
        if not pr:
            raise ForgeError("GitHub GraphQL error")  # GitHub's text is not ours to show
        commits = (pr.get("commits") or {}).get("nodes") or []
        rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") if commits else None
        checks = None if rollup is None else tuple(
            _check(node) for node in (rollup.get("contexts") or {}).get("nodes") or [] if node)
        return MergeState(status=pr.get("mergeStateStatus") or "", base=pr.get("baseRefName") or "",
                          head_ref=pr.get("headRefName") or "", head_sha=pr.get("headRefOid") or "",
                          cross_repo=bool(pr.get("isCrossRepository")), review=pr.get("reviewDecision") or "",
                          checks=checks)

    async def empty_commit(self, branch: str, head: str, message: str) -> str:
        """Push an empty commit to `branch`, whose head is `head`: a commit with the head's tree and the head as its
        only parent, then the branch moved to it without force. The branch is read just before the move:
        ForgeError("branch moved") when it is not at `head` (a branch force-pushed back to an ancestor would take
        the move as a fast-forward and get the removed commits back); GitHub refuses the move itself (422) when the
        branch moved to a commit that is not an ancestor. Each write once; the new commit's sha. ForgeError as
        _write's (a commit made but not reached by the branch is left to GitHub's garbage collection)."""
        tree = ((await self._get(f"{self._adapter._repo_path}/git/commits/{head}")).get("tree") or {}).get("sha")
        if not isinstance(tree, str):
            raise ForgeError("GitHub answer unreadable")
        made = await self._write("POST", "/git/commits", {"message": message, "tree": tree, "parents": [head]})
        sha = made.get("sha")
        if not isinstance(sha, str):
            raise ForgeError("GitHub answer unreadable")
        ref = f"heads/{quote(branch, safe='/')}"
        at = ((await self._get(f"{self._adapter._repo_path}/git/ref/{ref}")).get("object") or {}).get("sha")
        if at != head:
            raise ForgeError("branch moved")
        await self._write("PATCH", f"/git/refs/{ref}", {"sha": sha, "force": False})
        return sha

    async def merge_methods(self) -> list[str]:
        """The merge methods the repo allows, in the order merge, squash, rebase. GitHub leaves the fields out for a
        token without push access: each missing one counts as allowed, GitHub refuses the merge if it is not."""
        repo = await self._get(self._adapter._repo_path)
        return [method for method, field in _MERGE_METHODS if repo.get(field, True)]

    async def review_candidates(self, number: int, author: str) -> list[dict]:
        """Who to ask for a review of the PR: GitHub's suggestions first, then who reviewed the author's 20 newest
        PRs, most reviews first; never the author, a bot or one asked already. At most 10, each with its reason."""
        owner, name = self._adapter._owner, self._adapter._repo
        variables = {"owner": owner, "name": name, "number": number,
                     "q": f"repo:{owner}/{name} is:pr author:{author} sort:updated-desc"}
        try:
            answer = await self._adapter._graphql_request(_CANDIDATES_QUERY, variables)
        except Exception as e:
            raise _forge_error(e) from None
        if answer.get("errors") or not answer.get("data"):
            raise ForgeError("GitHub GraphQL error")
        pr = (answer["data"].get("repository") or {}).get("pullRequest") or {}
        # GraphQL answers null for a connection it could not resolve: each is read as empty.
        requests = (pr.get("reviewRequests") or {}).get("nodes") or []
        asked = {(n.get("requestedReviewer") or {}).get("login") for n in requests}
        skip = {author, (pr.get("author") or {}).get("login"), *asked} - {None}
        out: dict[str, str] = {}
        for item in pr.get("suggestedReviewers") or []:
            login = (item.get("reviewer") or {}).get("login")
            if login and login not in skip:
                out.setdefault(login, "suggested by GitHub")
        counts: dict[str, int] = {}
        for node in (answer["data"].get("search") or {}).get("nodes") or []:
            authors = [review.get("author") or {} for review in (node.get("reviews") or {}).get("nodes") or []]
            reviewers = {who.get("login") for who in authors if who.get("__typename") == "User"}
            for login in reviewers - skip:  # PRs, not reviews: one PR reviewed in three rounds counts once
                counts[login] = counts.get(login, 0) + 1
        for login, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            out.setdefault(login, f"reviewed {count} of the author's recent PRs")
        return [{"login": login, "reason": reason} for login, reason in out.items()][:_MAX_CANDIDATES]

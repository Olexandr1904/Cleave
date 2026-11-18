"""GitHub reads for the reviews board, review jobs and posting: open PRs, PR details, whether a login has
reviewed, the full PR, its comments, its reviews and one review's comments; and the one approve call."""
from __future__ import annotations

import re

import httpx

from integrations.github.github_adapter import GitHubAdapter
from orchestrator.reviewer.models import ForgeReview, PRComments, PRDetails, PRFull

_PAGE = 100
_MAX_PAGES = 100
_STATUS = re.compile(r"→ (\d{3})")


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

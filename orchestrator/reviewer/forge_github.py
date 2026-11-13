"""GitHub reads for the reviews board: open PRs, PR details, whether a login has reviewed."""
from __future__ import annotations

import re

import httpx

from integrations.github.github_adapter import GitHubAdapter
from orchestrator.reviewer.models import PRDetails

_PAGE = 100
_MAX_PAGES = 100
_STATUS = re.compile(r"→ (\d{3})")


class ForgeError(Exception):
    pass


def _forge_error(e: Exception) -> ForgeError:
    # Never the response body: it is GitHub's text, not ours, and may echo request data.
    if isinstance(e, httpx.HTTPStatusError):
        return ForgeError(f"GitHub {e.response.status_code}")
    if isinstance(e, RuntimeError) and (m := _STATUS.search(str(e))):
        return ForgeError(f"GitHub {m.group(1)}")
    return ForgeError(type(e).__name__)


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

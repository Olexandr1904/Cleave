"""GitLab adapter implementing VCSInterface (REST API v4 + git CLI)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from integrations.base.vcs import PRComment, PRStatus, VCSInterface

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]
TIMEOUT = 30
SUBPROCESS_TIMEOUT = 300


class GitLabAdapter(VCSInterface):
    """GitLab REST API v4 + git CLI adapter."""

    def __init__(
        self, token: str, project_id: str, url: str = "https://gitlab.com",
    ) -> None:
        self._token = token
        self._project_id = project_id
        self._url = (url or "https://gitlab.com").rstrip("/")
        self._project_path = quote(str(project_id), safe="")
        self._client = httpx.AsyncClient(
            base_url=f"{self._url}/api/v4",
            headers={"Private-Token": token, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        # note_id -> discussion_id, keyed by mr_iid; populated lazily by
        # get_pr_comments and used by reply_to_comment / resolve_comment.
        self._discussion_cache: dict[int, dict[int, str]] = {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict | list:
        """HTTP request with retries; surfaces response body on final failure."""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code in (401, 403):
                    raise httpx.HTTPStatusError(
                        f"Authentication failed: {response.status_code}",
                        request=response.request, response=response,
                    )
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                return response.json()
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                    raise
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    logger.warning(
                        "GitLab request retry %d/%d for %s %s: %s",
                        attempt + 1, MAX_RETRIES, method, path, e,
                    )
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response is not None:
            body = last_error.response.text[:1500]
            logger.error(
                "GitLab %s %s failed after %d retries → %d: %s",
                method, path, MAX_RETRIES, last_error.response.status_code, body[:500],
            )
            raise RuntimeError(
                f"GitLab {method} {path} → {last_error.response.status_code}: {body[:500]}"
            ) from last_error
        raise last_error  # type: ignore[misc]

    # --- VCSInterface methods (filled in by later tasks) ----------------

    async def clone_repo(self, url: str, dest: str, depth: int = 0) -> None:
        raise NotImplementedError

    async def create_branch(self, repo_dir: str, branch_name: str) -> None:
        raise NotImplementedError

    async def push(
        self, repo_dir: str, branch_name: str,
        force: bool = False, skip_hooks: bool = False,
    ) -> None:
        raise NotImplementedError

    async def open_pr(
        self, title: str, body: str, head_branch: str, base_branch: str,
    ) -> tuple[int, str]:
        raise NotImplementedError

    async def find_pr_by_branch(self, branch: str) -> tuple[int, str] | None:
        raise NotImplementedError

    async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
        raise NotImplementedError

    async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> None:
        raise NotImplementedError

    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        raise NotImplementedError

    async def check_pr_status(self, pr_number: int) -> PRStatus:
        raise NotImplementedError

    async def close_pr(self, pr_number: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        await self._client.aclose()

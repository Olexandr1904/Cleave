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

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

    @staticmethod
    async def _run_git(repo_dir: str, *args: str) -> tuple[str, str]:
        """Run a git command inside repo_dir. Returns (stdout, stderr)."""
        cmd = ["git", "-C", repo_dir] + list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=SUBPROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Git command timed out after {SUBPROCESS_TIMEOUT}s: {' '.join(cmd)}"
            )
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{stderr.strip()}")
        return stdout, stderr

    async def clone_repo(self, url: str, dest: str, depth: int = 0) -> None:
        cmd = ["git", "clone"]
        if depth > 0:
            cmd.extend(["--depth", str(depth)])
        cmd.extend([url, dest])

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=SUBPROCESS_TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"Git clone failed: {stderr.decode().strip()}")

    async def create_branch(self, repo_dir: str, branch_name: str) -> None:
        await self._run_git(repo_dir, "checkout", "-b", branch_name)
        logger.info("Created branch: %s", branch_name)

    async def push(
        self, repo_dir: str, branch_name: str,
        force: bool = False, skip_hooks: bool = False,
    ) -> None:
        """Push branch to origin. Rewrites origin URL with current token
        first so workspaces cloned before a rotation still authenticate.

        Uses GitLab's oauth2 username form for token-in-URL auth:
            https://oauth2:<token>@<host>/<namespace>/<project>.git
        """
        host = self._url.replace("https://", "").replace("http://", "")
        canonical_url = (
            f"https://oauth2:{self._token}@{host}/{self._project_id}.git"
        )
        await self._run_git(repo_dir, "remote", "set-url", "origin", canonical_url)

        args = ["push", "-u", "origin", branch_name]
        if force:
            args.insert(1, "--force")
        if skip_hooks:
            args.insert(1, "--no-verify")
        await self._run_git(repo_dir, *args)
        suffix = ""
        if force:
            suffix += " (force)"
        if skip_hooks:
            suffix += " (no-verify)"
        logger.info("Pushed branch: %s%s", branch_name, suffix)

    @property
    def _mr_path(self) -> str:
        return f"/projects/{self._project_path}/merge_requests"

    async def open_pr(
        self, title: str, body: str, head_branch: str, base_branch: str,
    ) -> tuple[int, str]:
        """Open a merge request. Returns (iid, web_url)."""
        data = await self._request(
            "POST",
            self._mr_path,
            json={
                "source_branch": head_branch,
                "target_branch": base_branch,
                "title": title,
                "description": body,
            },
        )
        iid = data["iid"]
        web_url = data["web_url"]
        logger.info("Opened MR !%d: %s", iid, web_url)
        return iid, web_url

    async def find_pr_by_branch(self, branch: str) -> tuple[int, str] | None:
        """Find an open MR with the given source_branch."""
        try:
            data = await self._request(
                "GET", self._mr_path,
                params={"source_branch": branch, "state": "opened"},
            )
            if data and isinstance(data, list) and len(data) > 0:
                mr = data[0]
                return mr["iid"], mr["web_url"]
        except Exception as e:
            logger.warning("Failed to find MR for branch %s: %s", branch, e)
        return None

    async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
        """Fetch all diff-position notes across the MR's discussions.

        General MR notes (no `position` field) are skipped — only diff-anchored
        review comments are surfaced, matching the GitHub adapter's behavior
        of pulling from /pulls/:n/comments (not /issues/:n/comments).

        Populates self._discussion_cache[pr_number] so reply_to_comment and
        resolve_comment can look up a note's owning discussion_id without
        another round-trip.
        """
        cache: dict[int, str] = {}
        all_comments: list[PRComment] = []
        page = 1
        while True:
            data = await self._request(
                "GET",
                f"/projects/{self._project_path}/merge_requests/{pr_number}/discussions",
                params={"per_page": 100, "page": page},
            )
            if not data:
                break
            for disc in data:
                disc_id = disc.get("id", "")
                for note in disc.get("notes", []) or []:
                    if note.get("position") is None:
                        continue
                    note_id = int(note["id"])
                    cache[note_id] = disc_id
                    pos = note.get("position") or {}
                    all_comments.append(PRComment(
                        id=note_id,
                        body=note.get("body", ""),
                        path=pos.get("new_path") or pos.get("old_path") or "",
                        line=pos.get("new_line") or pos.get("old_line"),
                        author=(note.get("author") or {}).get("username", ""),
                    ))
            if len(data) < 100:
                break
            page += 1

        self._discussion_cache[pr_number] = cache
        return all_comments

    async def _lookup_discussion(self, pr_number: int, note_id: int) -> str:
        """Return discussion_id for a note. Cache → refetch once → raise."""
        cached = self._discussion_cache.get(pr_number, {}).get(note_id)
        if cached:
            return cached
        # Refetch the MR's discussions (also refreshes the cache as a side effect).
        await self.get_pr_comments(pr_number)
        cached = self._discussion_cache.get(pr_number, {}).get(note_id)
        if cached:
            return cached
        raise RuntimeError(
            f"GitLab note {note_id} not found in MR !{pr_number} discussions; "
            f"cannot reply/resolve."
        )

    async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> None:
        """Reply to a note by posting to its owning discussion."""
        disc_id = await self._lookup_discussion(pr_number, comment_id)
        await self._request(
            "POST",
            f"/projects/{self._project_path}/merge_requests/{pr_number}/discussions/{disc_id}/notes",
            json={"body": body},
        )

    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        """Mark a discussion thread resolved. Idempotent."""
        disc_id = await self._lookup_discussion(pr_number, comment_id)
        await self._request(
            "PUT",
            f"/projects/{self._project_path}/merge_requests/{pr_number}/discussions/{disc_id}",
            params={"resolved": "true"},
        )

    async def check_pr_status(self, pr_number: int) -> PRStatus:
        raise NotImplementedError

    async def close_pr(self, pr_number: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        await self._client.aclose()

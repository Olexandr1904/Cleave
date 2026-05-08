"""GitHub adapter implementing VCSInterface."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from integrations.base.vcs import PRComment, PRStatus, VCSInterface

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]
TIMEOUT = 30
SUBPROCESS_TIMEOUT = 300


class GitHubAdapter(VCSInterface):
    """GitHub REST API + git CLI adapter."""

    def __init__(self, token: str, owner: str, repo: str) -> None:
        self._owner = owner
        self._repo = repo
        # _token is read by _graphql_request; httpx headers cover REST only.
        self._token = token
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=TIMEOUT,
        )

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self._owner}/{self._repo}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict | list:
        """Make an HTTP request with retries."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code in (401, 403):
                    raise httpx.HTTPStatusError(
                        f"Authentication failed: {response.status_code}",
                        request=response.request,
                        response=response,
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
                        "GitHub request retry %d/%d for %s %s: %s",
                        attempt + 1, MAX_RETRIES, method, path, e,
                    )
        # All retries exhausted. For HTTP errors, surface the response body —
        # the default httpx error message strips it, so callers see "422
        # Unprocessable Entity" with no clue why GitHub rejected the request.
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response is not None:
            body = last_error.response.text[:1500]
            logger.error(
                "GitHub %s %s failed after %d retries → %d: %s",
                method, path, MAX_RETRIES, last_error.response.status_code, body[:500],
            )
            raise RuntimeError(
                f"GitHub {method} {path} → {last_error.response.status_code}: {body[:500]}"
            ) from last_error
        raise last_error  # type: ignore[misc]

    @staticmethod
    async def _run_git(repo_dir: str, *args: str) -> tuple[str, str]:
        """Run a git command in the given repo directory.

        Async so the event loop isn't blocked for the duration of the git
        operation (which can be up to SUBPROCESS_TIMEOUT seconds for `push`).
        """
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
        """Clone a repository."""
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
        """Create and checkout a new branch."""
        await self._run_git(repo_dir, "checkout", "-b", branch_name)
        logger.info("Created branch: %s", branch_name)

    async def push(
        self, repo_dir: str, branch_name: str,
        force: bool = False, skip_hooks: bool = False,
    ) -> None:
        """Push branch to origin.

        Rewrites the `origin` URL with the adapter's current token before
        pushing so workspaces cloned with an older token still authenticate
        after a token rotation. Without this, a stale token baked into the
        existing remote URL is what reaches GitHub at push time.
        """
        canonical_url = (
            f"https://{self._token}@github.com/{self._owner}/{self._repo}.git"
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

    async def open_pr(
        self, title: str, body: str, head_branch: str, base_branch: str
    ) -> tuple[int, str]:
        """Open a pull request."""
        data = await self._request(
            "POST",
            f"{self._repo_path}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            },
        )
        pr_number = data["number"]
        pr_url = data["html_url"]
        logger.info("Opened PR #%d: %s", pr_number, pr_url)
        return pr_number, pr_url

    async def find_pr_by_branch(self, branch: str) -> tuple[int, str] | None:
        """Find an open PR for the given head branch."""
        try:
            data = await self._request(
                "GET", f"{self._repo_path}/pulls",
                params={"head": f"{self._owner}:{branch}", "state": "open"},
            )
            if data and isinstance(data, list) and len(data) > 0:
                pr = data[0]
                return pr["number"], pr["html_url"]
        except Exception as e:
            logger.warning("Failed to find PR for branch %s: %s", branch, e)
        return None

    async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
        """Get all review comments on a PR (handles pagination)."""
        all_comments = []
        page = 1
        while True:
            data = await self._request(
                "GET", f"{self._repo_path}/pulls/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if not data:
                break
            all_comments.extend(data)
            if len(data) < 100:
                break
            page += 1
        return [
            PRComment(
                id=c["id"],
                body=c.get("body", ""),
                path=c.get("path", ""),
                line=c.get("line"),
                author=c.get("user", {}).get("login", ""),
                in_reply_to_id=c.get("in_reply_to_id"),
            )
            for c in all_comments
        ]

    async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> None:
        """Reply to a review comment."""
        await self._request(
            "POST",
            f"{self._repo_path}/pulls/{pr_number}/comments/{comment_id}/replies",
            json={"body": body},
        )

    async def _graphql_request(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GitHub GraphQL request."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        response = await self._client.post(
            "https://api.github.com/graphql",
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        response.raise_for_status()
        return response.json()

    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        """Resolve a PR review comment thread via GraphQL."""
        # Get the comment's node_id via REST
        try:
            data = await self._request(
                "GET", f"{self._repo_path}/pulls/comments/{comment_id}",
            )
        except Exception as e:
            logger.warning("Cannot fetch comment %d for resolve: %s", comment_id, e)
            return
        node_id = data.get("node_id")
        if not node_id:
            logger.warning("No node_id for comment %d", comment_id)
            return

        # Navigate directly from the comment to its thread (works regardless of
        # whether the comment is part of a formal review submission or standalone)
        query = """
        query($nodeId: ID!) {
          node(id: $nodeId) {
            ... on PullRequestReviewComment {
              pullRequestThread {
                id
                isResolved
              }
            }
          }
        }
        """
        try:
            result = await self._graphql_request(query, {"nodeId": node_id})
        except Exception as e:
            logger.warning("GraphQL query failed for comment %d: %s", comment_id, e)
            return

        thread = (
            result.get("data", {}).get("node", {})
            .get("pullRequestThread") or {}
        )
        if not thread or thread.get("isResolved"):
            return

        thread_id = thread.get("id")
        if not thread_id:
            return

        mutation = """
        mutation($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { id isResolved }
          }
        }
        """
        try:
            await self._graphql_request(mutation, {"threadId": thread_id})
        except Exception as e:
            logger.warning("Failed to resolve thread for comment %d: %s", comment_id, e)

    async def check_pr_status(self, pr_number: int) -> PRStatus:
        """Check CI status for a PR."""
        pr_data = await self._request(
            "GET", f"{self._repo_path}/pulls/{pr_number}"
        )
        head_sha = pr_data["head"]["sha"]

        checks_data = await self._request(
            "GET", f"{self._repo_path}/commits/{head_sha}/check-runs"
        )
        check_runs = checks_data.get("check_runs", [])

        all_passing = all(
            cr.get("conclusion") == "success"
            for cr in check_runs
            if cr.get("status") == "completed"
        ) and len(check_runs) > 0

        return PRStatus(
            all_passing=all_passing,
            checks=[
                {"name": cr.get("name"), "status": cr.get("status"),
                 "conclusion": cr.get("conclusion")}
                for cr in check_runs
            ],
        )

    async def close_pr(self, pr_number: int) -> None:
        """Close a PR without merging."""
        await self._request(
            "PATCH",
            f"{self._repo_path}/pulls/{pr_number}",
            json={"state": "closed"},
        )
        logger.info("Closed PR #%d", pr_number)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

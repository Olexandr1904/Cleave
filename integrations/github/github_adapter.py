"""GitHub adapter implementing VCSInterface."""

from __future__ import annotations

import asyncio
import logging
import subprocess
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
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _run_git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
        """Run a git command in the given repo directory."""
        cmd = ["git", "-C", repo_dir] + list(args)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{result.stderr.strip()}")
        return result

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
        self._run_git(repo_dir, "checkout", "-b", branch_name)
        logger.info("Created branch: %s", branch_name)

    async def push(self, repo_dir: str, branch_name: str, force: bool = False) -> None:
        """Push branch to origin."""
        args = ["push", "-u", "origin", branch_name]
        if force:
            args.insert(1, "--force")
        self._run_git(repo_dir, *args)
        logger.info("Pushed branch: %s%s", branch_name, " (force)" if force else "")

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
        """Get all review comments on a PR."""
        data = await self._request(
            "GET", f"{self._repo_path}/pulls/{pr_number}/comments"
        )
        return [
            PRComment(
                id=c["id"],
                body=c.get("body", ""),
                path=c.get("path", ""),
                line=c.get("line"),
                author=c.get("user", {}).get("login", ""),
                in_reply_to_id=c.get("in_reply_to_id"),
            )
            for c in data
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

        # Find the review thread containing this comment
        query = """
        query($nodeId: ID!) {
          node(id: $nodeId) {
            ... on PullRequestReviewComment {
              pullRequestReview {
                pullRequest {
                  reviewThreads(last: 100) {
                    nodes {
                      id
                      isResolved
                      comments(first: 1) {
                        nodes { id }
                      }
                    }
                  }
                }
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

        threads = (
            result.get("data", {}).get("node", {})
            .get("pullRequestReview", {}).get("pullRequest", {})
            .get("reviewThreads", {}).get("nodes", [])
        )
        thread_id = None
        for thread in threads:
            if thread.get("isResolved"):
                continue
            comment_nodes = thread.get("comments", {}).get("nodes", [])
            if any(c.get("id") == node_id for c in comment_nodes):
                thread_id = thread["id"]
                break

        if not thread_id:
            return  # Already resolved or not found

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

    async def merge_pr(self, pr_number: int, merge_method: str = "squash") -> None:
        """Merge a pull request."""
        await self._request(
            "PUT",
            f"{self._repo_path}/pulls/{pr_number}/merge",
            json={"merge_method": merge_method},
        )
        logger.info("Merged PR #%d via %s", pr_number, merge_method)

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

"""Abstract VCS interface for version control and PR management."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PRComment:
    """A review comment on a pull request."""
    id: int
    body: str
    path: str = ""
    line: int | None = None
    author: str = ""
    in_reply_to_id: int | None = None


@dataclass
class PRStatus:
    """Status of a pull request's CI checks."""
    all_passing: bool
    checks: list[dict[str, Any]] = field(default_factory=list)


class VCSInterface(ABC):
    """Abstract interface for VCS operations (GitHub, GitLab, etc.)."""

    @abstractmethod
    async def clone_repo(self, url: str, dest: str, depth: int = 0) -> None:
        """Clone a repository to a destination directory."""

    @abstractmethod
    async def create_branch(self, repo_dir: str, branch_name: str) -> None:
        """Create and checkout a new branch."""

    @abstractmethod
    async def push(self, repo_dir: str, branch_name: str, force: bool = False) -> None:
        """Push the current branch to origin."""

    @abstractmethod
    async def open_pr(
        self, title: str, body: str, head_branch: str, base_branch: str
    ) -> tuple[int, str]:
        """Open a pull request. Returns (pr_number, pr_url)."""

    @abstractmethod
    async def find_pr_by_branch(self, branch: str) -> tuple[int, str] | None:
        """Find an open PR for the given branch. Returns (pr_number, pr_url) or None."""

    @abstractmethod
    async def get_pr_comments(self, pr_number: int) -> list[PRComment]:
        """Get all review comments on a PR."""

    @abstractmethod
    async def reply_to_comment(self, pr_number: int, comment_id: int, body: str) -> None:
        """Post a reply to a specific review comment."""

    @abstractmethod
    async def resolve_comment(self, pr_number: int, comment_id: int) -> None:
        """Mark a PR review comment thread as resolved."""

    @abstractmethod
    async def check_pr_status(self, pr_number: int) -> PRStatus:
        """Check whether all CI checks are passing."""

    @abstractmethod
    async def close_pr(self, pr_number: int) -> None:
        """Close a pull request without merging."""

"""Safeguard utilities — file write monitoring, protected path checks."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ProtectedFileViolation(Exception):
    """Raised when an agent attempts to write to a protected file."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Protected file violation: {path} — {reason}")


# Default protected file patterns
PROTECTED_PATTERNS = [
    "arch-rules.md",
    "architecture-rules.md",
    ".detekt.yml",
    ".detekt.yaml",
    "detekt.yml",
    "detekt.yaml",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.json",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    "Jenkinsfile",
    ".github/workflows/",
    ".gitlab-ci.yml",
    "bitbucket-pipelines.yml",
]


def check_protected_files(
    workspace_repo_dir: str | Path,
    changed_files: list[str],
    extra_protected: list[str] | None = None,
) -> list[ProtectedFileViolation]:
    """Check if any changed files are protected.

    AC1: Any agent attempting to write to arch-rules.md or lint config
    paths triggers immediate abort.

    Args:
        workspace_repo_dir: Path to the workspace's repo directory.
        changed_files: List of file paths (relative to repo root) that were changed.
        extra_protected: Additional protected file patterns from config.

    Returns:
        List of violations found.
    """
    protected = list(PROTECTED_PATTERNS)
    if extra_protected:
        protected.extend(extra_protected)

    violations = []
    for changed in changed_files:
        normalized = changed.lstrip("/")
        for pattern in protected:
            if pattern.endswith("/"):
                # Directory pattern — check prefix
                if normalized.startswith(pattern):
                    violations.append(ProtectedFileViolation(
                        path=changed,
                        reason=f"File is inside protected directory: {pattern}",
                    ))
                    break
            else:
                # Exact filename or basename match
                if normalized == pattern or normalized.endswith(f"/{pattern}") or Path(normalized).name == pattern:
                    violations.append(ProtectedFileViolation(
                        path=changed,
                        reason=f"File matches protected pattern: {pattern}",
                    ))
                    break

    return violations


def get_changed_files(repo_dir: str | Path, default_branch: str = "main") -> list[str]:
    """Get list of changed files relative to the default branch.

    Uses git diff to find files changed on the current branch.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"origin/{default_branch}...HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git diff failed: %s", result.stderr)
            return []
        return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Failed to get changed files: %s", e)
        return []

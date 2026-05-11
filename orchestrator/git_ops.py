"""Git helper functions used across the pipeline.

These were previously @staticmethod helpers on the Orchestrator class. They
take a Workspace because they operate on workspace.source_dir.
"""
from __future__ import annotations

import subprocess

from workspace.workspace import Workspace


def git_diff_files(workspace: Workspace, since_sha: str = "") -> set[str]:
    """Return the set of files changed in `<since_sha>..HEAD` (or HEAD~1)."""
    diff_arg = f"{since_sha}..HEAD" if since_sha else "HEAD~1"
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.source_dir),
             "diff", diff_arg, "--name-only"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return set(result.stdout.strip().splitlines())
    except Exception:
        pass
    return set()


def git_head_sha(workspace: Workspace) -> str:
    """Return the current HEAD sha, or 'unknown' on error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.source_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"

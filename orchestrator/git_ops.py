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


def git_diff_files_vs_base(workspace: Workspace, default_branch: str = "") -> list[str]:
    """Files changed on this branch vs its base, computed by us, not by the agent.

    Agents have derived this themselves with a mistaken pathspec and then
    reasoned from the wrong answer: QA on PR #1530 concluded the branch never
    touched a file the PR modifies. Base-ref resolution mirrors
    `stage_verifier._commits_ahead_of_base` — the configured branch first, then
    the `origin/HEAD` symref. Returns [] when neither resolves, which callers
    must read as "unknown", not "nothing changed".
    """
    candidates: list[str] = []
    if default_branch:
        candidates.append(f"origin/{default_branch}")
    candidates.append("origin/HEAD")
    for base_ref in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.source_dir),
                 "diff", f"{base_ref}...HEAD", "--name-only"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0:
            return sorted(p for p in result.stdout.strip().splitlines() if p)
    return []


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

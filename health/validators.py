"""Pure validators for project health checks.

Every validator returns a ValidatorResult. Validators MUST NOT raise;
unexpected failures are caught and returned as ok=False with the
exception class in `reason`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from integrations.config import config_tools


@dataclass
class ValidatorResult:
    """Structured result of a single health check.

    Attributes:
        ok: True if the check passed.
        name: Validator identifier (e.g. "jira", "github", "git_identity").
        target: What was checked (e.g. "ACME project", "/ws/acme/acme-mobile").
        reason: Human-readable error if ok=False, empty string otherwise.
        fix_hint: Copyable command or instruction to resolve the failure,
            empty string if ok or no actionable fix.
    """
    ok: bool
    name: str
    target: str
    reason: str
    fix_hint: str


async def check_jira(url: str, email: str, token: str, project_key: str) -> ValidatorResult:
    """Check Jira API access for a project."""
    try:
        result = await config_tools.validate_jira(
            url=url, token=token, email=email, project_key=project_key,
        )
    except Exception as e:
        return ValidatorResult(
            ok=False, name="jira", target=project_key,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check Jira URL, email, and token in project config",
        )
    if result.get("success"):
        return ValidatorResult(ok=True, name="jira", target=project_key, reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="jira", target=project_key,
        reason=result.get("error", "Jira check failed"),
        fix_hint="Verify Jira URL, email, token, and project key in project config",
    )


async def check_github(token: str, owner: str, repo: str) -> ValidatorResult:
    """Check GitHub API access for a repo."""
    target = f"{owner}/{repo}"
    try:
        result = await config_tools.validate_github(token=token, owner=owner, repo=repo)
    except Exception as e:
        return ValidatorResult(
            ok=False, name="github", target=target,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check GitHub token and repo name in project config",
        )
    if result.get("success"):
        return ValidatorResult(ok=True, name="github", target=target, reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="github", target=target,
        reason=result.get("error", "GitHub check failed"),
        fix_hint=f"Verify GitHub token has access to {target}",
    )


async def check_gitlab(token: str, project_id: str, url: str = "https://gitlab.com") -> ValidatorResult:
    """Check GitLab API access for a project."""
    try:
        result = await config_tools.validate_gitlab(token=token, project_id=project_id, url=url)
    except Exception as e:
        return ValidatorResult(
            ok=False, name="gitlab", target=project_id,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check GitLab token, URL, and project id in project config",
        )
    if result.get("success"):
        return ValidatorResult(ok=True, name="gitlab", target=project_id, reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="gitlab", target=project_id,
        reason=result.get("error", "GitLab check failed"),
        fix_hint=f"Verify GitLab token has access to project {project_id}",
    )


def _git_config(workspace_root: Path, key: str) -> tuple[bool, str]:
    """Run `git -C <workspace> config <key>` and return (ok, value_or_error)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "config", key],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return False, "git binary not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "git config timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if result.returncode == 0:
        return True, result.stdout.strip()
    stderr = result.stderr.strip() or f"exit {result.returncode}"
    return False, stderr


def check_git_identity(workspace_root: Path) -> ValidatorResult:
    """Check that git will accept commits in the given workspace.

    Reads user.name and user.email with `git config` (which resolves
    local → global → system in the same order git commit does).
    """
    target = str(workspace_root)
    fix_hint = (
        'git config --global user.name "Your Name" && '
        'git config --global user.email <you@company.com>'
    )

    if not workspace_root.exists():
        return ValidatorResult(
            ok=False, name="git_identity", target=target,
            reason="workspace directory does not exist", fix_hint=fix_hint,
        )

    name_ok, name_val = _git_config(workspace_root, "user.name")
    email_ok, email_val = _git_config(workspace_root, "user.email")

    if not name_ok or not name_val:
        return ValidatorResult(
            ok=False, name="git_identity", target=target,
            reason=f"git user.name not set ({name_val or 'empty'})",
            fix_hint=fix_hint,
        )
    if not email_ok or not email_val:
        return ValidatorResult(
            ok=False, name="git_identity", target=target,
            reason=f"git user.email not set ({email_val or 'empty'})",
            fix_hint=fix_hint,
        )
    return ValidatorResult(ok=True, name="git_identity", target=target, reason="", fix_hint="")


def check_git_remote(workspace_root: Path, remote: str = "origin") -> ValidatorResult:
    """Check that the configured git remote accepts read auth.

    Uses `git ls-remote <remote> HEAD` — this is a lightweight operation
    that validates network reach + auth without fetching any refs.
    """
    target = f"{workspace_root}:{remote}"
    fix_hint = (
        "Verify the remote is set and authentication works: "
        "git -C <workspace> remote -v && git -C <workspace> ls-remote origin HEAD"
    )

    if not workspace_root.exists():
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason="workspace directory does not exist", fix_hint=fix_hint,
        )

    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "ls-remote", remote, "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason="git binary not found on PATH", fix_hint=fix_hint,
        )
    except subprocess.TimeoutExpired:
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason=f"git ls-remote {remote} timed out", fix_hint=fix_hint,
        )
    except Exception as e:
        return ValidatorResult(
            ok=False, name="git_remote", target=target,
            reason=f"{type(e).__name__}: {e}", fix_hint=fix_hint,
        )

    if result.returncode == 0:
        return ValidatorResult(ok=True, name="git_remote", target=target, reason="", fix_hint="")

    stderr = result.stderr.strip().splitlines()
    first_line = stderr[0] if stderr else f"exit {result.returncode}"
    return ValidatorResult(
        ok=False, name="git_remote", target=target,
        reason=first_line, fix_hint=fix_hint,
    )

"""Pure validators for project health checks.

Every validator returns a ValidatorResult. Validators MUST NOT raise;
unexpected failures are caught and returned as ok=False with the
exception class in `reason`.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from integrations.config import config_tools


@dataclass
class ValidatorResult:
    """Structured result of a single health check.

    Attributes:
        ok: True if the check passed.
        name: Validator identifier (e.g. "jira", "github", "git_identity").
        target: What was checked (e.g. "ACME project", "/ws/acme/acme-app").
        reason: Human-readable error if ok=False, empty string otherwise.
        fix_hint: Copyable command or instruction to resolve the failure,
            empty string if ok or no actionable fix.
    """
    ok: bool
    name: str
    target: str
    reason: str
    fix_hint: str
    extra: dict[str, Any] = field(default_factory=dict)


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
        extra={"status": result.get("status")},
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
        extra={"status": result.get("status")},
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
        extra={"status": result.get("status")},
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


def read_git_identity(workspace_root: Path) -> tuple[str, str]:
    """Return (user_name, user_email), empty strings if unset or unreadable."""
    name_ok, name_val = _git_config(workspace_root, "user.name")
    email_ok, email_val = _git_config(workspace_root, "user.email")
    return (name_val if name_ok else "", email_val if email_ok else "")


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


async def check_trello(api_key: str, token: str, board_id: str) -> ValidatorResult:
    """Check Trello API access for a board."""
    try:
        result = await config_tools.validate_trello(
            api_key=api_key, token=token, board_id=board_id,
        )
    except Exception as e:
        return ValidatorResult(
            ok=False, name="trello", target=board_id,
            reason=f"{type(e).__name__}: {e}",
            fix_hint="Check Trello API key, token, and board URL/ID",
        )
    if result.get("success"):
        return ValidatorResult(
            ok=True, name="trello", target=board_id,
            reason="", fix_hint="",
            extra={"lists": result.get("lists", [])},
        )
    return ValidatorResult(
        ok=False, name="trello", target=board_id,
        reason=result.get("error", "Trello check failed"),
        fix_hint="Verify token has access to the board",
        extra={"status": result.get("status")},
    )


async def check_telegram(token: str) -> ValidatorResult:
    """Check that the Telegram bot token is accepted (getMe).

    The Bot API URL carries the token, so failures report only the HTTP
    status or the exception class name, never the exception text.
    """
    try:
        async with httpx.AsyncClient(timeout=config_tools.VALIDATE_TIMEOUT) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as e:
        return ValidatorResult(
            ok=False, name="telegram", target="bot",
            reason=type(e).__name__,
            fix_hint="Check the Telegram bot token and network access",
            extra={"status": None},
        )
    if response.status_code == 200:
        try:
            body_ok = bool(response.json().get("ok"))
        except Exception:
            body_ok = False
        if body_ok:
            return ValidatorResult(ok=True, name="telegram", target="bot", reason="", fix_hint="")
    return ValidatorResult(
        ok=False, name="telegram", target="bot",
        reason=f"Telegram returned HTTP {response.status_code}",
        fix_hint="Check the Telegram bot token",
        extra={"status": response.status_code},
    )


# CLAUDE_CONFIG_DIR selects the login profile, so the check sees the pipeline's login.
CLAUDE_ENV_KEYS = ("HOME", "PATH", "LANG", "TMPDIR", "USER", "CLAUDE_CONFIG_DIR")


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and anything it spawned (own session, so pgid == pid)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def check_claude_login(claude_bin: str, timeout_seconds: float = 30.0) -> ValidatorResult:
    """Check the `claude` CLI login via `claude auth status --json`.

    Runs with a scrubbed environment (no API key) and parses stdout whatever
    the exit code: a logged-out CLI exits 1 but still prints JSON.
    """
    def _unknown(reason: str) -> ValidatorResult:
        return ValidatorResult(
            ok=False, name="claude_login", target="host", reason=reason,
            fix_hint="Run `claude` on the host and log in with /login",
            extra={"logged_in": None, "expires_at": None, "status": None},
        )

    env = {k: os.environ[k] for k in CLAUDE_ENV_KEYS if k in os.environ}
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "auth", "status", "--json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return _unknown("claude not found")
    except Exception as e:
        return _unknown(type(e).__name__)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return _unknown("claude auth status timed out")
    except BaseException:
        _kill_group(proc)
        await proc.wait()
        raise
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        logged_in = data["loggedIn"]
        if not isinstance(logged_in, bool):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return _unknown("unexpected claude auth status output")
    if logged_in:
        expires_at = data.get("loginExpiresAt")
        return ValidatorResult(
            ok=True, name="claude_login", target="host", reason="", fix_hint="",
            extra={
                "logged_in": True,
                "expires_at": expires_at if isinstance(expires_at, str) else None,
                "status": None,
            },
        )
    return ValidatorResult(
        ok=False, name="claude_login", target="host", reason="not logged in",
        fix_hint="Run `claude` on the host and log in with /login",
        extra={"logged_in": False, "expires_at": None, "status": None},
    )

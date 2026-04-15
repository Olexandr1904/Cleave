"""Pure validators for project health checks.

Every validator returns a ValidatorResult. Validators MUST NOT raise;
unexpected failures are caught and returned as ok=False with the
exception class in `reason`.
"""

from __future__ import annotations

from dataclasses import dataclass

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

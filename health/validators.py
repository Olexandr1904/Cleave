"""Pure validators for project health checks.

Every validator returns a ValidatorResult. Validators MUST NOT raise;
unexpected failures are caught and returned as ok=False with the
exception class in `reason`.
"""

from __future__ import annotations

from dataclasses import dataclass


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

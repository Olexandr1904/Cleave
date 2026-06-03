"""Mechanical post-stage verification.

Each pipeline stage with an expected side-effect has a verifier that
asserts the effect actually happened (e.g. dev must produce a new
commit). The orchestrator calls `verify(...)` after the agent finishes
and transitions the workspace to BLOCKED if the check fails — regardless
of what the agent said in text output.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.constants import RUNTIME_OUTPUT_QA, RUNTIME_OUTPUT_SCOPE_GUARD

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    ok: bool
    stage_id: str
    reason: str


@dataclass
class ActionResult:
    """Structured result of an action-stage execution.

    Returned by action methods; the handler decides transitions.
    """
    success: bool
    next_state: str
    error: str
    metadata: dict[str, Any]
    skipped: bool = False


_GIT_STAGES = {"dev", "push"}


def _git_rev_parse(source_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("git rev-parse failed in %s: %s", source_dir, e)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def capture_stage_start(workspace: Any, stage_id: str) -> str | None:
    """Capture HEAD before a stage runs. Returns None for non-verifiable stages."""
    if stage_id not in _GIT_STAGES:
        return None
    return _git_rev_parse(Path(workspace.source_dir))


def _commits_ahead_of_base(source: Path, default_branch: str | None) -> int | None:
    """Count commits HEAD is ahead of the base branch.

    Tries the configured base branch first (``origin/<default_branch>``), then
    falls back to the ``origin/HEAD`` symref. Returns None when neither base ref
    can be resolved (e.g. no remote, or the symref isn't set in the clone) —
    callers must treat None as "unknown", not "zero".
    """
    candidates: list[str] = []
    if default_branch:
        candidates.append(f"origin/{default_branch}")
    candidates.append("origin/HEAD")
    for base_ref in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "rev-list", "--count", f"{base_ref}..HEAD"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        try:
            return int(result.stdout.strip())
        except ValueError:
            continue
    return None


def verify(
    stage_id: str,
    workspace: Any,
    stage_start_commit: str | None,
    duration_seconds: float | None = None,
    default_branch: str | None = None,
) -> VerifyResult:
    """Run the mechanical verifier for the given stage."""
    if stage_id == "dev":
        return _verify_dev(workspace, stage_start_commit, duration_seconds, default_branch)
    if stage_id == "scope_check":
        return _verify_report_exists("scope_check", workspace, RUNTIME_OUTPUT_SCOPE_GUARD)
    if stage_id == "qa":
        return _verify_report_exists("qa", workspace, RUNTIME_OUTPUT_QA)
    if stage_id == "push":
        return _verify_push(workspace)
    if stage_id == "pr_review":
        return _verify_pr_review(workspace)
    return VerifyResult(ok=True, stage_id=stage_id, reason="")


def _verify_report_exists(stage_id: str, workspace: Any, filename: str) -> VerifyResult:
    reports_dir = Path(workspace.reports_dir)
    if not (reports_dir / filename).exists():
        return VerifyResult(
            ok=False, stage_id=stage_id,
            reason=f"{filename} was not produced in ai_pipeline/<ticket>/",
        )
    return VerifyResult(ok=True, stage_id=stage_id, reason="")


def _verify_push(workspace: Any) -> VerifyResult:
    source = Path(workspace.source_dir)
    branch = getattr(workspace.state, "branch", None)
    if not branch:
        return VerifyResult(
            ok=False, stage_id="push",
            reason="no branch set on workspace state",
        )

    local = _git_rev_parse(source)
    if local is None:
        return VerifyResult(
            ok=False, stage_id="push",
            reason="could not read local HEAD",
        )

    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-remote", "origin", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"git ls-remote failed: {type(e).__name__}: {e}",
        )
    if result.returncode != 0:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"git ls-remote origin failed: {result.stderr.strip()[:200]}",
        )

    line = result.stdout.strip()
    if not line:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"remote has no ref refs/heads/{branch} (branch not pushed)",
        )
    remote_sha = line.split()[0]
    if remote_sha != local:
        return VerifyResult(
            ok=False, stage_id="push",
            reason=f"remote branch {branch} is at {remote_sha[:8]}, local at {local[:8]}",
        )
    return VerifyResult(ok=True, stage_id="push", reason="")


def _verify_pr_review(workspace: Any) -> VerifyResult:
    pr_number = getattr(workspace.state, "pr_number", None)
    if not pr_number:
        return VerifyResult(
            ok=False, stage_id="pr_review",
            reason="workspace state has no pr_number (PR not created)",
        )
    return VerifyResult(ok=True, stage_id="pr_review", reason="")


def _verify_dev(
    workspace: Any,
    stage_start_commit: str | None,
    duration_seconds: float | None = None,
    default_branch: str | None = None,
) -> VerifyResult:
    source = Path(workspace.source_dir)
    current = _git_rev_parse(source)
    if current is None:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason="could not read git HEAD from workspace source_dir",
        )

    # Strongest signal: HEAD advanced during this run. Only meaningful when the
    # pre-stage commit was actually captured.
    if stage_start_commit is not None and current != stage_start_commit:
        return VerifyResult(ok=True, stage_id="dev", reason="")

    # HEAD didn't advance this run — or the start commit was never captured
    # (transient git failure at stage entry). Either way, fall back to the
    # durable question: does the feature branch carry commits the base branch
    # doesn't? This catches commits made in a prior run AND real work that the
    # start-capture missed. None means "couldn't resolve a base ref" (no remote
    # / unset origin/HEAD), which we must not treat as zero.
    ahead = _commits_ahead_of_base(source, default_branch)
    if ahead is not None and ahead > 0:
        return VerifyResult(ok=True, stage_id="dev", reason="")

    if stage_start_commit is None:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason="stage start commit was not captured and the branch has no "
                   "commits ahead of the base branch",
        )
    if duration_seconds is not None and duration_seconds < 60:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason=(
                f"dev-agent completed in {duration_seconds:.0f}s with no changes"
                " — likely could not map plan to code. Escalating for human review."
            ),
        )
    return VerifyResult(
        ok=False, stage_id="dev",
        reason=f"no new commit on feature branch (HEAD still at {current[:8]})",
    )

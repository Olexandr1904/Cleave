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


def verify(stage_id: str, workspace: Any, stage_start_commit: str | None) -> VerifyResult:
    """Run the mechanical verifier for the given stage."""
    if stage_id == "dev":
        return _verify_dev(workspace, stage_start_commit)
    if stage_id == "scope_check":
        return _verify_report_exists("scope_check", workspace, "scope-guard-agent-output.md")
    if stage_id == "qa":
        return _verify_report_exists("qa", workspace, "qa-agent-output.md")
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
            reason=f"{filename} was not produced in reports/",
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


def _verify_dev(workspace: Any, stage_start_commit: str | None) -> VerifyResult:
    source = Path(workspace.source_dir)
    current = _git_rev_parse(source)
    if current is None:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason="could not read git HEAD from workspace source_dir",
        )
    if stage_start_commit is None:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason="stage start commit was not captured",
        )
    if current != stage_start_commit:
        return VerifyResult(ok=True, stage_id="dev", reason="")

    # HEAD didn't change this run — but maybe the commit was made in a prior run.
    # Check if the feature branch has commits ahead of the default branch.
    branch = getattr(workspace.state, "branch", None)
    if branch:
        try:
            # Find the fork point: how many commits on this branch that aren't
            # on any remote branch (i.e., local work)
            result = subprocess.run(
                ["git", "-C", str(source), "log", "--oneline", f"origin/HEAD..HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                local_commits = [l for l in result.stdout.strip().splitlines() if l]
                if len(local_commits) > 0:
                    return VerifyResult(ok=True, stage_id="dev", reason="")
        except (subprocess.TimeoutExpired, OSError):
            pass

    return VerifyResult(
        ok=False, stage_id="dev",
        reason=f"no new commit on feature branch (HEAD still at {current[:8]})",
    )

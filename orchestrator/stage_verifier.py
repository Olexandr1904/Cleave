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
    return VerifyResult(ok=True, stage_id=stage_id, reason="")


def _verify_dev(workspace: Any, stage_start_commit: str | None) -> VerifyResult:
    current = _git_rev_parse(Path(workspace.source_dir))
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
    if current == stage_start_commit:
        return VerifyResult(
            ok=False, stage_id="dev",
            reason=f"no new commit on feature branch (HEAD still at {current[:8]})",
        )
    return VerifyResult(ok=True, stage_id="dev", reason="")

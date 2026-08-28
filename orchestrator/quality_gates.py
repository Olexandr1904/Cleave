"""Run a repo's own quality-gate commands and report exit codes.

The QA agent reports gate results in prose, and that prose has been wrong: on
PR #1530 it narrated "PASS *for ticket scope*" over a non-zero `detektDebug`
exit, and derived the diff with a pathspec that missed a modified file. This
module gives the orchestrator its own answer, independent of the narrative.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OUTPUT_TAIL_CHARS = 4000


@dataclass
class GateResult:
    name: str          # "tests" | "lint" | "build"
    command: str
    exit_code: int
    hard_gate: bool
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def run_gates(
    source_dir: Path,
    repo_config: Any,
    timeout: int = 3600,
) -> list[GateResult]:
    """Run every configured gate command in source_dir. Unconfigured gates are skipped."""
    specs = (
        ("tests", repo_config.testing.run_command, repo_config.testing.hard_gate),
        ("lint", repo_config.linting.run_command, repo_config.linting.hard_gate),
        ("build", repo_config.build.check_command, repo_config.build.hard_gate),
    )
    results: list[GateResult] = []
    for name, command, hard_gate in specs:
        if not command.strip():
            continue
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(source_dir),
                capture_output=True, text=True, timeout=timeout,
            )
            exit_code = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            exit_code = 124
            output = f"gate '{name}' timed out after {timeout}s"
        except OSError as e:
            exit_code = 126
            output = f"gate '{name}' could not start: {e}"
        logger.info("Gate %s exited %d (command: %s)", name, exit_code, command)
        results.append(GateResult(
            name=name, command=command, exit_code=exit_code,
            hard_gate=hard_gate, output_tail=output[-_OUTPUT_TAIL_CHARS:],
        ))
    return results


def failing_hard_gates(results: list[GateResult]) -> list[GateResult]:
    """Gates that block the pipeline: configured as hard and exited non-zero."""
    return [r for r in results if r.hard_gate and not r.passed]

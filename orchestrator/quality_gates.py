"""Run a repo's own quality-gate commands and report exit codes.

The QA agent reports gate results in prose, and that prose has been wrong: on
PR #1530 it narrated "PASS *for ticket scope*" over a non-zero `detektDebug`
exit, and derived the diff with a pathspec that missed a modified file. This
module gives the orchestrator its own answer, independent of the narrative.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OUTPUT_TAIL_CHARS = 4000

# Exit codes of a command that never got to check anything.
_DID_NOT_RUN = {124: "timed out", 126: "could not start", 127: "command not found"}

# Output of a build tool that failed before, or instead of, checking the
# branch: the command names a task it does not have, or it ran out of memory.
# Gradle's words; another stack's go here too.
_COULD_NOT_RUN = re.compile(
    r"^.*(?:Cannot locate tasks that match|Task '[^']+' not found in|OutOfMemoryError"
    r"|Gradle build daemon disappeared unexpectedly).*$",
    re.MULTILINE,
)


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

    @property
    def could_not_run(self) -> str:
        """Why the gate did not do its job, or "" if it did (passed, or failed on what it checks).

        A gate that could not run says nothing about the branch. Counted as a failed gate, it sent a ticket
        whose tests and build had passed back to dev: the configured task name was ambiguous between the
        repo's two build flavours, and nothing dev can change in the branch fixes a gate's command.
        """
        if self.passed:
            return ""
        if self.exit_code in _DID_NOT_RUN:
            return _DID_NOT_RUN[self.exit_code]
        match = _COULD_NOT_RUN.search(self.output_tail)
        return match.group(0).strip()[:300] if match else ""


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

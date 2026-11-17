"""Cleave's own gates for the skill flow: the repo's test, lint and build commands, run in the ticket's worktree
after the session committed and judged by exit code, and the source-reading test check on the changed files."""
from __future__ import annotations

from pathlib import Path

from config.schemas import RepoConfig
from orchestrator.fake_test_detector import detect_source_reading_tests
from orchestrator.quality_gates import GateResult
from orchestrator.reviewer.commands import run_command

GATE_TIMEOUT = 3600.0
_ORDER = ("test", "lint", "build")
_OUTPUT_TAIL = 4000
_TIMEOUT_EXIT = 124   # as timeout(1) and the staged flow's gates report it
FAKE_TESTS = "fake-tests"   # the gate name of the source-reading test check
_FAKE_TESTS_COMMAND = "Cleave's source-reading test check"
_FAKE_TESTS_HEADER = "These tests read source files as text instead of testing behaviour:"


def gate_commands(repo_cfg: RepoConfig) -> dict[str, str]:
    """skill_flow.gates, else the staged flow's commands; an empty command is not configured."""
    staged = {"test": repo_cfg.testing.run_command, "lint": repo_cfg.linting.run_command,
              "build": repo_cfg.build.check_command}
    return _configured(repo_cfg.skill_flow.gates) or _configured(staged)


async def run_gates(worktree: Path, commands: dict[str, str], env: dict[str, str], *,
                    changed_files: list[str], timeout: float = GATE_TIMEOUT) -> list[GateResult]:
    """Run each configured command in the worktree (test, lint, build; all hard) with exactly env, then check
    the changed files for tests that read source text."""
    results: list[GateResult] = []
    for name in _ORDER:
        command = commands.get(name, "")
        if not command.strip():
            continue
        result = await run_command(["/bin/sh", "-c", command], cwd=worktree, env=env, timeout=timeout)
        if result.timed_out:
            exit_code, output = _TIMEOUT_EXIT, f"gate '{name}' timed out after {timeout:g} s"
        else:
            exit_code, output = result.returncode, result.stdout + result.stderr
        results.append(GateResult(name=name, command=command, exit_code=exit_code, hard_gate=True,
                                  output_tail=output[-_OUTPUT_TAIL:]))
    if offenders := detect_source_reading_tests(worktree, changed_files):
        results.append(GateResult(name=FAKE_TESTS, command=_FAKE_TESTS_COMMAND, exit_code=1, hard_gate=True,
                                  output_tail="\n".join([_FAKE_TESTS_HEADER, *offenders])))
    return results


def failing(results: list[GateResult]) -> list[GateResult]:
    return [r for r in results if not r.passed]


def _configured(commands: dict[str, str]) -> dict[str, str]:
    return {name: command for name, command in commands.items() if command.strip()}

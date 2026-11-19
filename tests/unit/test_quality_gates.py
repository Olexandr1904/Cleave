"""Tests for the orchestrator-owned quality gate runner."""

from pathlib import Path

from config.schemas import BuildConfig, LintConfig, RepoConfig
from config.schemas import TestConfig as GateTestConfig  # aliased: pytest tries to collect Test*
from orchestrator.quality_gates import GateResult, failing_hard_gates, run_gates


def _config(test_cmd: str = "", lint_cmd: str = "", build_cmd: str = "") -> RepoConfig:
    cfg = RepoConfig()
    cfg.testing = GateTestConfig(run_command=test_cmd, hard_gate=True)
    cfg.linting = LintConfig(run_command=lint_cmd, hard_gate=True)
    cfg.build = BuildConfig(check_command=build_cmd, hard_gate=True)
    return cfg


def test_empty_commands_produce_no_gates(tmp_path: Path):
    assert run_gates(tmp_path, _config()) == []


def test_exit_code_is_recorded(tmp_path: Path):
    results = run_gates(tmp_path, _config(test_cmd="exit 3"))
    assert len(results) == 1
    assert results[0].name == "tests"
    assert results[0].exit_code == 3


def test_failing_hard_gates_filters_by_exit_code(tmp_path: Path):
    results = run_gates(tmp_path, _config(test_cmd="true", lint_cmd="false"))
    failing = failing_hard_gates(results)
    assert [g.name for g in failing] == ["lint"]


def test_soft_gate_failure_is_not_reported_as_blocking(tmp_path: Path):
    cfg = _config(lint_cmd="false")
    cfg.linting.hard_gate = False
    assert failing_hard_gates(run_gates(tmp_path, cfg)) == []


def test_output_tail_is_captured_and_bounded(tmp_path: Path):
    results = run_gates(
        tmp_path, _config(test_cmd="printf 'x%.0s' $(seq 1 9000); exit 1")
    )
    assert 0 < len(results[0].output_tail) <= 4000


# --- a gate that could not do its job says nothing about the branch ---
# The gates of a two-flavour Android repo named tasks without a flavour. Gradle refused the test task in 6 s
# ("is ambiguous"), the build of both flavours at once ran out of heap, and a ticket whose own tests and build
# had passed was sent back to dev for it. The outputs below are Gradle's, shortened.

AMBIGUOUS_TASK = """
FAILURE: Build failed with an exception.

* What went wrong:
Cannot locate tasks that match ':app:testDebugUnitTest' as task 'testDebugUnitTest' is ambiguous in project ':app'. Candidates are: 'testChinaDebugUnitTest', 'testGlobalDebugUnitTest'.

BUILD FAILED in 6s
"""

OUT_OF_MEMORY = """
> Task :app:compileGlobalDebugKotlin FAILED
e: java.lang.OutOfMemoryError: Java heap space

* What went wrong:
Execution failed for task ':app:compileGlobalDebugKotlin'.

BUILD FAILED in 9m 17s
"""

FAILING_TESTS = """
> Task :app:testGlobalDebugUnitTest FAILED

LoginViewModelTest > shows an error on a wrong password FAILED
    java.lang.AssertionError at LoginViewModelTest.kt:41

* What went wrong:
Execution failed for task ':app:testGlobalDebugUnitTest'.
> There were failing tests. See the report at: file:///build/reports/tests/index.html

BUILD FAILED in 4m 2s
"""


def _result(output: str, exit_code: int = 1) -> GateResult:
    return GateResult(name="tests", command="./gradlew :app:testDebugUnitTest", exit_code=exit_code,
                      hard_gate=True, output_tail=output)


def test_a_task_the_build_tool_does_not_have_is_a_gate_that_could_not_run():
    assert "is ambiguous in project ':app'" in _result(AMBIGUOUS_TASK).could_not_run


def test_a_build_tool_out_of_memory_is_a_gate_that_could_not_run():
    assert "OutOfMemoryError" in _result(OUT_OF_MEMORY).could_not_run


def test_failing_tests_are_a_gate_that_ran():
    assert _result(FAILING_TESTS).could_not_run == ""


def test_a_passed_gate_ran():
    assert _result(OUT_OF_MEMORY, exit_code=0).could_not_run == ""


def test_a_command_the_shell_cannot_find_could_not_run(tmp_path: Path):
    [result] = run_gates(tmp_path, _config(test_cmd="cleave-no-such-command-for-this-test"))
    assert result.exit_code == 127
    assert result.could_not_run

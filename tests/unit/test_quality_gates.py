"""Tests for the orchestrator-owned quality gate runner."""

from pathlib import Path

from config.schemas import BuildConfig, LintConfig, RepoConfig
from config.schemas import TestConfig as GateTestConfig  # aliased: pytest tries to collect Test*
from orchestrator.quality_gates import failing_hard_gates, run_gates


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

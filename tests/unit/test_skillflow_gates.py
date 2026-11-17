# tests/unit/test_skillflow_gates.py
from __future__ import annotations
import os
from config.schemas import BuildConfig, LintConfig, RepoConfig
from config.schemas import TestConfig as GateTestConfig  # aliased: pytest tries to collect Test*
from orchestrator.skillflow.gates import failing, gate_commands, run_gates

ENV = {"PATH": os.environ["PATH"]}

def test_gate_commands_default_to_the_staged_ones():
    cfg = RepoConfig(testing=GateTestConfig("./gradlew test"), linting=LintConfig("./gradlew detekt"),
                     build=BuildConfig(""))
    assert gate_commands(cfg) == {"test": "./gradlew test", "lint": "./gradlew detekt"}
    cfg.skill_flow.gates = {"build": "./gradlew assemble"}
    assert gate_commands(cfg) == {"build": "./gradlew assemble"}

async def test_gates_run_in_the_worktree_and_report_exit_codes(tmp_path):
    results = await run_gates(tmp_path, {"test": "pwd; exit 3", "lint": "true"}, ENV, changed_files=[])
    assert [(r.name, r.exit_code) for r in results] == [("test", 3), ("lint", 0)]
    assert str(tmp_path.resolve()) in results[0].output_tail and [r.name for r in failing(results)] == ["test"]

async def test_gate_sees_only_the_given_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "leak")
    results = await run_gates(tmp_path, {"test": 'test -z "$GITHUB_TOKEN"'}, ENV, changed_files=[])
    assert results[0].exit_code == 0

async def test_timeout_is_a_failure(tmp_path):
    results = await run_gates(tmp_path, {"test": "sleep 5"}, ENV, changed_files=[], timeout=0.2)
    assert results[0].exit_code == 124 and "timed out" in results[0].output_tail

async def test_a_source_reading_test_fails_its_own_gate(tmp_path):
    test_file = tmp_path / "app/src/test/GuardTest.kt"; test_file.parent.mkdir(parents=True)
    test_file.write_text('val text = File("app/src/main/Login.kt").readText()')
    results = await run_gates(tmp_path, {}, ENV, changed_files=["app/src/test/GuardTest.kt"])
    assert [(r.name, r.exit_code) for r in results] == [("fake-tests", 1)]
    assert "app/src/test/GuardTest.kt" in results[0].output_tail

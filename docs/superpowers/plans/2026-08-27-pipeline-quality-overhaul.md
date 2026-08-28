# Pipeline Quality Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Cleave pipeline own correctness the way a human owns it in an interactive session — by adding a correctness reviewer, deriving stage verdicts from machine facts instead of agent prose, and closing the PR-review loop that currently no-ops.

**Architecture:** Three complementary moves. (1) *Ground truth*: the orchestrator computes the diff and runs the quality gates itself; agent prose stops being the source of truth for PASS/FAIL. (2) *A correctness owner*: a new `code_review` stage between `scope_check` and `qa` whose only job is finding behavioural defects — races, cancellation, error paths — the thing no current stage does. (3) *Unblock the loops that exist but produce nothing*: PR-comment fixes, BA escalation, scope-guard churn.

**Tech Stack:** Python 3.11+, asyncio, pytest, PyYAML, Claude Code CLI 2.1.221 (`claude -p`), GitHub via `gh`/REST.

**Spec:** The Evidence section below. It supersedes `Cleave-agent-tooling-findings-2026-08-27.md`, whose headline finding (agents lack tools) was falsified — see Evidence §0.

## Global Constraints

- Follow `CLAUDE.md`: minimum code that solves the problem, surgical diffs, no speculative abstraction. Every changed line traces to a task step.
- No changes to `execute_in_workspace()`'s architecture, the watchdogs, heartbeat, `killpg`, or quota classification in `integrations/llm/claude_code_adapter.py`. They were written from real incidents.
- Python: `from __future__ import annotations`, dataclasses, `logging` (never `print`), type hints on public functions.
- Tests live in `tests/unit/` and run with `.venv/bin/python -m pytest`. Each task's tests must pass before its commit.
- One task per commit. Commit messages: `feat(pipeline): …` / `fix(pipeline): …`.
- Roll out one task at a time on a live ticket before starting the next. Changing several at once makes the effect unattributable.
- Do NOT add `Task`, `Skill`, `TodoWrite` to `TOOL_MAP` — verified already available (Evidence §0).

---

## Evidence

Gathered 2026-08-27 from 16 pipeline PRs on `acme-org/acme-mobile` (Apr–Jun 2026) that still carry committed `ai_pipeline/<ticket>/*.md` artifacts, plus local `*.stream.jsonl` logs for ACME-14795, plus live probes of `claude -p` 2.1.221.

**§0 — What is NOT the problem.** Under Cleave's exact dev-agent grant (`--allowedTools "Read,Write,Edit,LS,Glob,Grep,Bash"`), a live probe showed `TodoWrite`, `Skill` and `Agent`/`Task` all execute successfully; only `WebSearch` is denied. `--allowedTools` is a permission list, not an availability list. Independently confirmed by production logs: `qa-agent` called `TodoWrite` 7 times, and `ba-agent` — which has no `run_command` grant — called `Bash` 15 times with zero denials. Turn ceilings are not binding either: the busiest observed agent used 43 of 100 turns.

**§1 — Nobody owns correctness.** On PR #1509, Copilot found seven genuine defects post-merge-request: a race in `updateReplyDraftOnLatest` writing state onto the wrong row, `runCatching` swallowing `CancellationException` and surfacing a false "Failed", an uncaught exception inside a `goAsync()` window that can kill the process, O(n) DB writes per update, and an unpopulated schema column. Every internal gate was green.

**§2 — Verdicts are prose.** `orchestrator/pipeline/agent_stage.py::_looks_like_pass` decides PASS by substring match, and its list includes `"verdict: hold"`, `"advances to"` and `"merge as-is"`. It is checked *before* `_looks_like_fail`, so a report containing both resolves to PASS. QA on PR #1530 wrote "PASS *for ticket scope*" while `detektDebug` exited non-zero. `stage_verifier.verify()` for `qa` only asserts that `qa-agent-output.md` exists.

**§3 — Agents self-report diff facts and get them wrong.** QA on PR #1530 ran `git diff develop...HEAD -- '**/TasksUnitsFragment.kt'`, read it as empty, and concluded the branch never touches that file. The PR modifies it. Nothing cross-checked the claim. Same class of error on PRs #1517 and #1524, where scope-guard certificates contradict the diff.

**§4 — QA writes tests that cannot fail.** 10 of the 21 test files the pipeline authored read production source as text and assert on substrings (`PreviewAcmeAtomMigrationGuardTest`, `SelectedUserListFlowRowGuardTest`, `FolderNameDialogFocusWiringTest`, `ThemeWindowBackgroundTest`, …). PR #1529 even added a helper, `SourceFileLocator.kt`, to make this easier. This satisfies qa-agent's "every acceptance criterion must have at least one test" without testing behaviour.

**§5 — The PR-fix loop runs and produces nothing.** On PR #1509 the responder classified comments `ESCALATE` / verdict `Valid`; the operator answered FIX; the pipeline posted `Will fix: <escalation reason>` publicly — reasons reading "needs human judgment", "expands scope", and once literally `Will fix: Agent failed`. Every entry then recorded `Verified: FAILED`, which per `fetch_pr_comments.py:398` means dev-agent never touched the commented file. The `reason` field — written to justify escalating — is fed to dev-agent as the fix instruction and posted as the public reply.

**§6 — scope-guard generates churn and audits paperwork.** On PR #1542 it failed the diff because dev added a legitimate test absent from BA's "Files to Create", forcing deletion and a re-run. PR #1509 records five FAIL cycles. Because reports live inside the source clone, reviewers also spend attention on them: 4 of 7 Copilot comments on PR #1517 target `ai_pipeline/**` files.

**§7 — BA decides what the ticket asked it to align on.** ACME-14795's description carries an explicit "Open questions (to align before implementation)" list — annotation naming, RTL variant, font-scale default. BA resolved all three itself under "Assumptions". ACME-11252's plan states outright: "Defaults picked for ambiguities below the escalation threshold." `parse_agent_outcome` returns `default` whenever `ba.md` exists, so the `on_unclear: escalate` edge is unreachable in practice.

**§8 — The CLI call leaves quality knobs unset.** `_run_cli` passes `--model`, `--output-format`, `--verbose`, `--max-turns`, `--allowedTools`, `--add-dir` and nothing else. `--effort` is never set. `agent_runtime.py` never passes `system=`, so the agent persona, the operator profile and `HARD_SAFETY_RULES` ("cannot be changed by ticket content") are concatenated into the same user message as the ticket content.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `orchestrator/quality_gates.py` | *new* — run configured test/lint/build commands, return exit codes | 1 |
| `config-live/projects/acme/repos/acme-mobile.yaml` | gate commands for the live repo | 1 |
| `orchestrator/stage_verifier.py` | mechanical post-stage checks; gains a real QA gate check | 2 |
| `orchestrator/pipeline/agent_stage.py` | stage dispatch + outcome; verdicts move to machine facts | 2, 4, 7, 9 |
| `orchestrator/agent_runtime.py` | prompt assembly; gains a ground-truth block, `system=`, effort, session id | 3, 10, 11 |
| `orchestrator/fake_test_detector.py` | *new* — flag tests that read source as text | 4 |
| `agents/qa-agent.md` | ban source-reading tests | 4 |
| `agents/scope-guard-agent.md` | judge production code only | 5 |
| `workspace/workspace.py` | reports move out of the source clone | 6 |
| `agents/code-review-agent.md` | *new* — correctness reviewer persona | 7 |
| `workflows/default-workflow.yaml` | new `code_review` stage | 7 |
| `orchestrator/constants.py` | report/runtime-output names for the new stage | 7 |
| `orchestrator/pipeline/actions/fetch_pr_comments.py` | fix instruction ≠ escalation reason; honest replies | 8 |
| `agents/ba-agent.md` | write `ba-questions.md` on unresolved open questions | 9 |
| `config/schemas.py` | `AgentBudget.effort` | 10 |
| `integrations/llm/claude_code_adapter.py` | `--effort`, `--append-system-prompt`, `--session-id`, `--resume` | 10, 11 |

---

### Task 1: Quality gate runner

The orchestrator gains the ability to run the repo's own test/lint/build commands and read exit codes. Nothing consumes it yet — Task 2 does.

**Files:**
- Create: `orchestrator/quality_gates.py`
- Test: `tests/unit/test_quality_gates.py`
- Modify: `config-live/projects/acme/repos/acme-mobile.yaml`

**Interfaces:**
- Consumes: `config.schemas.RepoConfig` (`testing.run_command`, `linting.run_command`, `build.check_command`, each with `.hard_gate: bool`) — these fields already exist and are currently empty strings for every live repo.
- Produces: `GateResult`, `run_gates(source_dir, repo_config, timeout=3600) -> list[GateResult]`, `failing_hard_gates(results) -> list[GateResult]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quality_gates.py
from pathlib import Path

from config.schemas import BuildConfig, LintConfig, RepoConfig, TestConfig
from orchestrator.quality_gates import failing_hard_gates, run_gates


def _config(test_cmd: str = "", lint_cmd: str = "", build_cmd: str = "") -> RepoConfig:
    cfg = RepoConfig()
    cfg.testing = TestConfig(run_command=test_cmd, hard_gate=True)
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
    results = run_gates(tmp_path, _config(test_cmd="printf 'x%.0s' $(seq 1 9000); exit 1"))
    assert 0 < len(results[0].output_tail) <= 4000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_quality_gates.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.quality_gates'`

- [ ] **Step 3: Write minimal implementation**

```python
# orchestrator/quality_gates.py
"""Run a repo's own quality-gate commands and report exit codes.

The QA agent reports gate results in prose, and that prose has been wrong
(a PASS narrative over a non-zero detekt exit, a diff claim that missed a
modified file). This module gives the orchestrator its own answer.
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
    return [r for r in results if r.hard_gate and not r.passed]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_quality_gates.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Configure the live repo's gate commands**

Append to `config-live/projects/acme/repos/acme-mobile.yaml`. Commands copied from what qa-agent has been running successfully on this host (see `ai_pipeline/ACME-11252/qa.md`), including the heap caps that fit the host's RAM:

```yaml
testing:
  run_command: "./gradlew :app:testDebugUnitTest --no-daemon"
  hard_gate: true

linting:
  run_command: "./gradlew :app:detektDebug --no-daemon"
  hard_gate: false   # develop currently carries pre-existing violations; see Evidence §2

build:
  check_command: "./gradlew :app:assembleDebug --no-daemon"
  hard_gate: true
```

- [ ] **Step 6: Verify config loads**

`load_config()` returns a `(GlobalConfig, dict[str, LoadedProject])` tuple, and it
raises `ConfigError` unless the tracker/token env vars are present — source `.env` first.

Run:
```bash
set -a && . ./.env && set +a && .venv/bin/python -c "
from config.config_loader import load_config
_, projects = load_config('config-live')
r = projects['acme'].repos['acme-mobile']
print(r.testing.run_command, '|', r.linting.hard_gate, '|', r.build.check_command)"
```
Expected: prints the three configured values, `linting.hard_gate` is `False`.

- [ ] **Step 7: Commit**

`config-live/` is gitignored (`.gitignore:45`) because it holds real credentials —
the gate config stays a local, uncommitted change on the host. The shape is already
documented in `config-live.example/projects/example-project/repos/example-repo.yaml`,
so nothing needs adding there.

```bash
git add orchestrator/quality_gates.py tests/unit/test_quality_gates.py
git commit -m "feat(pipeline): add quality gate runner with exit-code results"
```

---

### Task 2: QA verdict comes from exit codes, not prose

**Files:**
- Modify: `orchestrator/pipeline/agent_stage.py` (`parse_agent_outcome`, and the QA branch of `handle_agent_stage`)
- Test: `tests/unit/test_agent_stage.py`

**Interfaces:**
- Consumes: `orchestrator.quality_gates.run_gates`, `failing_hard_gates`, `GateResult` (Task 1).
- Produces: `parse_agent_outcome(stage_id, output, workspace, gate_results=None) -> str` — the new optional fourth parameter. Existing three-argument callers keep working.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_stage.py  (append)
from orchestrator.pipeline.agent_stage import parse_agent_outcome
from orchestrator.quality_gates import GateResult


def _gate(name: str, exit_code: int, hard: bool = True) -> GateResult:
    return GateResult(name=name, command="x", exit_code=exit_code,
                      hard_gate=hard, output_tail="")


def test_qa_fails_when_hard_gate_fails_even_if_agent_says_pass(tmp_workspace):
    (tmp_workspace.reports_dir / "qa-agent-output.md").write_text("## Status: PASS")
    outcome = parse_agent_outcome(
        "qa", "Status: PASS", tmp_workspace,
        gate_results=[_gate("tests", 1)],
    )
    assert outcome == "fail"


def test_qa_passes_when_all_hard_gates_pass_even_if_prose_is_ambiguous(tmp_workspace):
    (tmp_workspace.reports_dir / "qa-agent-output.md").write_text("verdict: hold")
    outcome = parse_agent_outcome(
        "qa", "verdict: hold", tmp_workspace,
        gate_results=[_gate("tests", 0), _gate("lint", 1, hard=False)],
    )
    assert outcome == "pass"


def test_qa_falls_back_to_prose_when_no_gates_configured(tmp_workspace):
    (tmp_workspace.reports_dir / "qa-agent-output.md").write_text("## Status: FAIL")
    assert parse_agent_outcome("qa", "", tmp_workspace, gate_results=[]) == "fail"
```

Use the existing `tmp_workspace` fixture in `tests/unit/test_agent_stage.py`; if the file has no such fixture, add one that builds a `Workspace` under `tmp_path` and returns it.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_stage.py -k qa_ -v`
Expected: FAIL — `parse_agent_outcome() got an unexpected keyword argument 'gate_results'`

- [ ] **Step 3: Write minimal implementation**

In `orchestrator/pipeline/agent_stage.py`, change the signature and the `qa` branch:

```python
def parse_agent_outcome(
    stage_id: str,
    output: str,
    workspace: Workspace,
    gate_results: list[GateResult] | None = None,
) -> str:
    """Parse agent output to determine outcome for routing.

    For `qa`, configured gate exit codes win over the agent's narrative: the
    agent has written "PASS for ticket scope" over a non-zero exit (see
    docs/superpowers/plans/2026-08-27-pipeline-quality-overhaul.md, Evidence §2).
    """
    output_lower = output.lower()

    if stage_id == "qa" and gate_results:
        return "fail" if failing_hard_gates(gate_results) else "pass"

    # ... existing body unchanged ...
```

Add the import at the top: `from orchestrator.quality_gates import GateResult, failing_hard_gates, run_gates`.

In `handle_agent_stage`, after the `verify_result` block and before `outcome = parse_agent_outcome(...)`:

```python
    gate_results: list[GateResult] = []
    if stage_id == "qa" and repo_config is not None:
        gate_results = run_gates(workspace.source_dir, repo_config)
        _write_gate_report(workspace, gate_results)

    outcome = parse_agent_outcome(stage_id, result.output, workspace, gate_results)
```

And a module-level helper so the next dev iteration sees the machine result rather than re-deriving it:

```python
def _write_gate_report(workspace: Workspace, results: list[GateResult]) -> None:
    """Write the orchestrator's own gate results next to the agent reports."""
    if not results:
        return
    lines = ["# Quality Gates (measured by orchestrator)", ""]
    for r in results:
        status = "PASS" if r.passed else f"FAIL (exit {r.exit_code})"
        kind = "hard" if r.hard_gate else "soft"
        lines.append(f"## {r.name} — {status} [{kind}]")
        lines.append(f"`{r.command}`")
        if not r.passed:
            lines.append("```")
            lines.append(r.output_tail)
            lines.append("```")
        lines.append("")
    (workspace.reports_dir / "qa-gates.md").write_text("\n".join(lines), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_stage.py -v`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/pipeline/agent_stage.py tests/unit/test_agent_stage.py
git commit -m "fix(pipeline): derive QA verdict from gate exit codes, not agent prose"
```

- [ ] **Step 6: Live check**

Run one real ticket. Confirm `ai_pipeline/<ticket>/qa-gates.md` exists and its verdict matches the stage transition in `pipeline-log.md`.

---

### Task 3: Feed agents the diff as ground truth

**Files:**
- Modify: `orchestrator/git_ops.py`, `orchestrator/agent_runtime.py` (`assemble_prompt`, line ~205 — the method that appends `HARD_SAFETY_RULES` at its end)
- Test: `tests/unit/test_git_diff_range.py`, `tests/unit/test_agent_runtime.py`

**Interfaces:**
- Consumes: nothing new. The existing `git_ops.git_diff_files(workspace, since_sha="")` diffs `<sha>..HEAD` or `HEAD~1` and returns a `set` — it cannot answer "what changed on this branch", so this task adds a sibling rather than bending it.
- Produces: `git_ops.git_diff_files_vs_base(workspace, default_branch: str = "") -> list[str]` — sorted, repo-relative; base ref resolved as `origin/<default_branch>` then `origin/HEAD`, matching `stage_verifier._commits_ahead_of_base`. Empty list when no base ref resolves.
- Produces: a `## Ground Truth` section in the prompt returned by `assemble_prompt(agent: AgentEntry, workspace: Workspace, extra_context=None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_git_diff_range.py  (append)
from orchestrator.git_ops import git_diff_files_vs_base


def test_diff_vs_base_lists_branch_changes(git_workspace):
    # git_workspace: a workspace whose source_dir is a repo with an
    # origin/develop base and one commit on a feature branch touching Foo.kt
    assert git_diff_files_vs_base(git_workspace, "develop") == ["Foo.kt"]


def test_diff_vs_base_returns_empty_when_base_unresolvable(git_workspace_no_remote):
    assert git_diff_files_vs_base(git_workspace_no_remote, "develop") == []
```

```python
# tests/unit/test_agent_runtime.py  (append)
def test_prompt_contains_ground_truth_changed_files(runtime, tmp_workspace, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.agent_runtime.git_diff_files_vs_base",
        lambda workspace, default_branch="": ["app/src/main/java/Foo.kt",
                                              "app/src/test/java/FooTest.kt"],
    )
    agent = runtime._registry.get_agent("dev-agent")
    prompt = runtime.assemble_prompt(agent, tmp_workspace)
    assert "## Ground Truth" in prompt
    assert "app/src/main/java/Foo.kt" in prompt
    assert "app/src/test/java/FooTest.kt" in prompt


def test_ground_truth_absent_when_no_changes(runtime, tmp_workspace, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.agent_runtime.git_diff_files_vs_base",
        lambda workspace, default_branch="": [],
    )
    agent = runtime._registry.get_agent("ba-agent")
    assert "## Ground Truth" not in runtime.assemble_prompt(agent, tmp_workspace)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_git_diff_range.py tests/unit/test_agent_runtime.py -k "vs_base or ground_truth" -v`
Expected: FAIL — `ImportError: cannot import name 'git_diff_files_vs_base'`

- [ ] **Step 3: Write minimal implementation**

```python
# orchestrator/git_ops.py
def git_diff_files_vs_base(workspace: Workspace, default_branch: str = "") -> list[str]:
    """Files changed on this branch vs its base, computed by us, not by the agent.

    Agents have derived this themselves with mistaken pathspecs and reasoned
    from the wrong answer (Evidence §3). Base ref resolution mirrors
    stage_verifier._commits_ahead_of_base: configured branch first, then the
    origin/HEAD symref. Returns [] when neither resolves — "unknown", not "none".
    """
    candidates = ([f"origin/{default_branch}"] if default_branch else []) + ["origin/HEAD"]
    for base_ref in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.source_dir),
                 "diff", f"{base_ref}...HEAD", "--name-only"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0:
            return sorted(p for p in result.stdout.strip().splitlines() if p)
    return []
```

Import it in `orchestrator/agent_runtime.py` and insert into `assemble_prompt`, immediately before the `HARD_SAFETY_RULES` append:

```python
        changed = git_diff_files_vs_base(workspace, self._default_branch)
        if changed:
            listing = "\n".join(f"- {p}" for p in changed)
            prompt_body += (
                "\n\n## Ground Truth\n\n"
                "Files changed on this branch, computed by the orchestrator. "
                "This list is authoritative — do not re-derive it with git commands "
                "and do not contradict it.\n\n"
                f"{listing}\n"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_runtime.py -v`
Expected: PASS

`self._default_branch` does not exist yet on `AgentRuntime` — thread the repo's
`vcs.default_branch` in the same way `handle_agent_stage` already does when it
calls `stage_verifier.verify(..., default_branch=...)`, or pass `""` and let the
`origin/HEAD` fallback resolve it.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/git_ops.py orchestrator/agent_runtime.py tests/unit/test_git_diff_range.py tests/unit/test_agent_runtime.py
git commit -m "feat(pipeline): give agents an authoritative changed-file list"
```

---

### Task 4: Reject tests that read source as text

**Files:**
- Create: `orchestrator/fake_test_detector.py`
- Test: `tests/unit/test_fake_test_detector.py`
- Modify: `orchestrator/pipeline/agent_stage.py` (QA branch), `agents/qa-agent.md`

**Interfaces:**
- Consumes: `git_ops.git_diff_files_vs_base` (Task 3), `workspace.source_dir`.
- Produces: `detect_source_reading_tests(source_dir: Path, changed_files: list[str]) -> list[str]` returning offending repo-relative test paths.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_fake_test_detector.py
from pathlib import Path

from orchestrator.fake_test_detector import detect_source_reading_tests

FAKE = '''
class PreviewGuardTest {
    @Test fun `annotation is used`() {
        val src = File("app/src/main/java/Button.kt").readText()
        assertTrue(src.contains("@PreviewAcmeAtom"))
    }
}
'''

REAL = '''
class ButtonTest {
    @Test fun `disabled button ignores clicks`() {
        val button = Button(enabled = false)
        button.click()
        assertEquals(0, button.clickCount)
    }
}
'''


def _write(tmp_path: Path, rel: str, body: str) -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return rel


def test_flags_test_reading_source_as_text(tmp_path):
    rel = _write(tmp_path, "app/src/test/java/PreviewGuardTest.kt", FAKE)
    assert detect_source_reading_tests(tmp_path, [rel]) == [rel]


def test_ignores_behavioural_test(tmp_path):
    rel = _write(tmp_path, "app/src/test/java/ButtonTest.kt", REAL)
    assert detect_source_reading_tests(tmp_path, [rel]) == []


def test_ignores_non_test_files(tmp_path):
    rel = _write(tmp_path, "app/src/main/java/FileReader.kt", FAKE)
    assert detect_source_reading_tests(tmp_path, [rel]) == []


def test_flags_xml_parsing_test(tmp_path):
    body = "val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(f)"
    rel = _write(tmp_path, "app/src/test/java/ThemeTest.kt", body)
    assert detect_source_reading_tests(tmp_path, [rel]) == [rel]


def test_missing_file_is_skipped(tmp_path):
    assert detect_source_reading_tests(tmp_path, ["app/src/test/java/Gone.kt"]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_fake_test_detector.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Write minimal implementation**

```python
# orchestrator/fake_test_detector.py
"""Detect tests that assert on source text instead of behaviour.

10 of the 21 test files the pipeline authored read production sources as
strings and grep them (Evidence §4). Such a test cannot fail: the agent that
wrote the source wrote the assertion in the same run.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_TEST_PATH_MARKERS = ("/src/test/", "/src/androidTest/", "/tests/")

_SOURCE_READ_PATTERNS = (
    re.compile(r"\breadText\s*\("),
    re.compile(r"\breadLines\s*\("),
    re.compile(r"\bFile\s*\(\s*[\"']"),
    re.compile(r"\bPaths\.get\s*\("),
    re.compile(r"\bDocumentBuilderFactory\b"),
)


def detect_source_reading_tests(source_dir: Path, changed_files: list[str]) -> list[str]:
    """Return the changed test files that read source/resources as text."""
    offenders: list[str] = []
    for rel in changed_files:
        if not any(marker in f"/{rel}" for marker in _TEST_PATH_MARKERS):
            continue
        path = source_dir / rel
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(p.search(content) for p in _SOURCE_READ_PATTERNS):
            offenders.append(rel)
    return offenders
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_fake_test_detector.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Wire into the QA stage**

In `handle_agent_stage`, inside the `stage_id == "qa"` block added in Task 2, after `gate_results`:

```python
        changed = git_diff_files_vs_base(workspace, default_branch)
        fake_tests = detect_source_reading_tests(workspace.source_dir, changed)
        if fake_tests:
            listing = "\n".join(f"- {p}" for p in fake_tests)
            (workspace.reports_dir / "qa-rejected-tests.md").write_text(
                "# Rejected tests\n\n"
                "These tests assert on source text instead of executing production code. "
                "Rewrite them to call the code under test, or delete them and cover the "
                "acceptance criterion another way.\n\n" + listing + "\n",
                encoding="utf-8",
            )
            _log_pipeline(workspace, f"qa: rejected {len(fake_tests)} source-reading test(s)")
            outcome = "fail"
```

Place this so it overrides the `parse_agent_outcome` result for QA.

- [ ] **Step 6: Add the rule to the agent prompt**

In `agents/qa-agent.md`, under `core_principles`, add:

```yaml
  - "A test must execute production code. NEVER assert on the text of a source file — no readText/File(...)/DocumentBuilderFactory over src/. A test that greps source cannot fail and does not count as coverage."
```

- [ ] **Step 7: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add orchestrator/fake_test_detector.py tests/unit/test_fake_test_detector.py orchestrator/pipeline/agent_stage.py agents/qa-agent.md
git commit -m "fix(qa): reject tests that assert on source text"
```

---

### Task 5: scope-guard judges production code only

Prompt-only change: there is no code path to unit-test, so it is verified on a live ticket. Keep the diff to the agent file.

**Files:**
- Modify: `agents/scope-guard-agent.md`

- [ ] **Step 1: Read the current agent file end to end**

Run: `cat agents/scope-guard-agent.md`
Note where "Every changed file must be in the implementation plan's allowed list" appears — that principle produced the churn in Evidence §6.

- [ ] **Step 2: Narrow the mandate**

Replace that principle with:

```yaml
  - "Audit production code only. Test files and pipeline reports are always in scope — never fail a diff for adding a test."
  - "Every changed production file must map to a ticket requirement or the implementation plan"
```

And in the body, add an explicit exclusion list next to the existing scope rules:

```markdown
## Out of audit scope

Never raise a violation for these — they are not product changes:
- test sources (`**/src/test/**`, `**/src/androidTest/**`, `tests/**`)
- pipeline reports written by agents
- files the plan lists under "Files to Modify" that the dev agent touched as planned

A missing entry in the plan's "Files to Create" is NOT a violation when the
added file is a test. Report it as a note, not a FAIL.
```

- [ ] **Step 3: Verify the agent still loads**

Run: `.venv/bin/python -c "from config.resource_registry import ResourceRegistry; r = ResourceRegistry('.'); a = r.get_agent('scope-guard-agent'); print(a.id, len(a.tools))"`
Expected: prints `scope-guard-agent` and its tool count without raising.

- [ ] **Step 4: Commit**

```bash
git add agents/scope-guard-agent.md
git commit -m "fix(scope-guard): audit production code only, stop failing on added tests"
```

- [ ] **Step 5: Live check**

Next real ticket: confirm `scope-guard.md` no longer lists a test file as a violation, and that `pipeline-log.md` shows no scope_check → dev bounce caused by tests.

---

### Task 6: Keep pipeline reports out of the PR diff

**Revised during execution.** The plan first proposed moving `reports_dir` to
`meta/reports/`, which meant rewriting ~90 `ai_pipeline/` references across 25
files including every agent prompt, and switching those prompts to absolute
paths (agents run with `cwd=source/`, so a relative write outside it would not
resolve). A missed reference would silently break stage verification and block
tickets.

The goal — "PR diff contains only code" — is reached by two small changes
instead. `commit_pipeline_artifacts()` in `push_and_open_pr.py` was the only
thing putting reports into the PR, and `.git/info/exclude` is local to the
clone, so nothing in the product repo is touched. Reports stay where every
consumer already looks for them.

**Files:**
- Modify: `workspace/workspace_manager.py` (new `_exclude_pipeline_artifacts`)
- Modify: `orchestrator/pipeline/actions/push_and_open_pr.py` (delete `commit_pipeline_artifacts` + 2 call sites)
- Modify: `orchestrator/pr_creation.py` (drop the "ignore ai_pipeline/" plea from the PR body)
- Test: `tests/unit/test_workspace_manager.py`, `tests/unit/test_action_stage.py`

- [x] **Step 1: Write the failing test** — `TestPipelineArtifactsExcluded`: after
      `manager.create(...)`, `source/.git/info/exclude` contains `ai_pipeline/`;
      and calling the helper twice does not duplicate the entry.
- [x] **Step 2: Run to verify it fails** — 2 failed.
- [x] **Step 3: Write `_exclude_pipeline_artifacts(source_dir)`** and call it
      right after the clone succeeds. Appends a comment plus `ai_pipeline/`,
      idempotently; an `OSError` is logged, not raised — worst case the
      artifacts appear in the diff again.
- [x] **Step 4: Delete `commit_pipeline_artifacts`** (64 lines) and its two call
      sites, and drop the PR-body scope note that existed only to ask reviewers
      to ignore the artifacts.
- [x] **Step 5: Fix the two tests** that patched the deleted function.
- [x] **Step 6: Full suite** — 1240 passed, 4 pre-existing failures unchanged.
- [x] **Step 7: Commit**

**Live check:** next ticket's PR diff contains no `ai_pipeline/` paths, and the
reports are still present on disk under `source/ai_pipeline/<ticket>/` for the
dashboard and escalation views.

### Task 7: Correctness review stage

The load-bearing change. A stage whose sole job is finding behavioural defects, placed where a failure is still cheap — before QA writes tests against wrong behaviour.

**Files:**
- Create: `agents/code-review-agent.md`
- Modify: `workflows/default-workflow.yaml`, `orchestrator/constants.py`, `orchestrator/stage_verifier.py`, `orchestrator/pipeline/agent_stage.py`
- Test: `tests/unit/test_agent_stage.py`, `tests/unit/test_workflow_router.py`

**Interfaces:**
- Consumes: the prompt's Ground Truth block (Task 3); `workflow_router.load_workflow` / `get_next_stage`.
- Produces: stage id `code_review`; `REPORT_CODE_REVIEW = "code-review.md"`; `RUNTIME_OUTPUT_CODE_REVIEW = "code-review-agent-output.md"`; both registered in `STAGE_REPORT_FILE` and `STAGE_RUNTIME_OUTPUT`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_stage.py  (append)
def test_code_review_fail_verdict_routes_back_to_dev(tmp_workspace):
    (tmp_workspace.reports_dir / "code-review-agent-output.md").write_text("## Status: FAIL")
    assert parse_agent_outcome("code_review", "", tmp_workspace) == "fail"


def test_code_review_pass_verdict(tmp_workspace):
    (tmp_workspace.reports_dir / "code-review-agent-output.md").write_text("## Status: PASS")
    assert parse_agent_outcome("code_review", "", tmp_workspace) == "pass"
```

And in `tests/unit/test_workflow_router.py` (helpers `load_workflow(workflow_path)` and `get_next_stage(stage_id, workflow, outcome)` from `orchestrator/workflow_router.py`):

```python
def test_scope_check_pass_goes_to_code_review():
    workflow = load_workflow("workflows/default-workflow.yaml")
    assert get_next_stage("scope_check", workflow, "pass") == "code_review"


def test_code_review_pass_goes_to_qa():
    workflow = load_workflow("workflows/default-workflow.yaml")
    assert get_next_stage("code_review", workflow, "pass") == "qa"


def test_code_review_fail_goes_to_dev():
    workflow = load_workflow("workflows/default-workflow.yaml")
    assert get_next_stage("code_review", workflow, "fail") == "dev"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit -k code_review -v`
Expected: FAIL — stage unknown, returns `None`

- [ ] **Step 3: Add constants**

In `orchestrator/constants.py`:

```python
REPORT_CODE_REVIEW = "code-review.md"
RUNTIME_OUTPUT_CODE_REVIEW = "code-review-agent-output.md"
```

Add `"code_review": REPORT_CODE_REVIEW` to `STAGE_REPORT_FILE` and `"code_review": RUNTIME_OUTPUT_CODE_REVIEW` to `STAGE_RUNTIME_OUTPUT`.

- [ ] **Step 4: Add the stage to the workflow**

In `workflows/default-workflow.yaml`, change `scope_check`'s `on_pass` from `"qa"` to `"code_review"` and insert after it:

```yaml
  - id: "code_review"
    agent: "code-review-agent"
    description: "Review the diff for behavioural defects before tests are written"
    on_pass: "qa"
    on_fail: "dev"
    max_iterations: 2
    on_max_iterations: "escalate"
```

- [ ] **Step 5: Register the stage in outcome parsing and verification**

In `orchestrator/pipeline/agent_stage.py`, change the membership test from `if stage_id in ("scope_check", "qa"):` to `if stage_id in ("scope_check", "code_review", "qa"):`.

In `orchestrator/stage_verifier.py`, add to `verify()`:

```python
    if stage_id == "code_review":
        return _verify_report_exists("code_review", workspace, RUNTIME_OUTPUT_CODE_REVIEW)
```

and import `RUNTIME_OUTPUT_CODE_REVIEW`.

- [ ] **Step 6: Write the agent**

Create `agents/code-review-agent.md`. Model the frontmatter on `agents/scope-guard-agent.md` — same tool grants (`read_file`, `list_directory`, `search_code`, `git_operation`), no write access:

```markdown
---
agent:
  id: "code-review-agent"
  name: "Critic"
  title: "Correctness Reviewer"

persona:
  role: "Senior Engineer reviewing a colleague's diff"
  style: "Concrete, evidence-based, no style nitpicking"
  identity: "Finds defects that compile, pass tests, and still break in production"

core_principles:
  - "Judge behaviour, not boundaries — scope-guard already owns scope"
  - "Every finding names a file, a line, and a concrete failure scenario"
  - "No style, naming, or formatting opinions"
  - "If the diff is correct, say PASS — do not invent findings to look useful"

tools:
  - read_file
  - list_directory
  - search_code
  - git_operation

inputs:
  - meta/reports/ba.md
  - meta/reports/developer.md

outputs:
  - meta/reports/code-review.md
---

# Correctness Reviewer

You review the diff on this branch before tests are written against it.

The changed-file list is in the prompt's Ground Truth section — it is authoritative.
Read those files and the diff (`git diff <base>...HEAD`).

## What to look for

Prioritise defects that survive compilation and a green test run:

1. **Concurrency** — races between callbacks, stale reads, "latest row" selection
   that a concurrent write can invalidate, work queued with data re-read at
   execution time.
2. **Cancellation** — `runCatching` / broad `catch` swallowing cancellation and
   converting it into a user-visible failure state.
3. **Error paths** — unchecked responses, uncaught exceptions on a background
   thread or inside a time-limited window, error branches that cannot be reached.
4. **Resources and lifecycle** — unbounded reads, per-item writes where one
   would do, listeners and jobs never released, ids that collide.
5. **Dead or unwired code** — fields persisted but never read, constants never
   used, branches that cannot execute.

Do NOT report: formatting, naming, import order, missing tests (QA owns that),
or files being out of scope (scope-guard owns that).

## Output

Write `meta/reports/code-review.md`:

```markdown
# Code Review — <TICKET>

## Status: PASS | FAIL

## Findings
1. **<file>:<line>** — <what is wrong>
   - Failure scenario: <concrete sequence of events producing the wrong result>
   - Suggested fix: <one or two sentences>
```

FAIL if any finding would produce incorrect behaviour, data loss, a crash, or a
misleading state for the user. Otherwise PASS. An empty findings list with a
PASS verdict is a valid and expected outcome for a clean diff.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add agents/code-review-agent.md workflows/default-workflow.yaml orchestrator/constants.py orchestrator/stage_verifier.py orchestrator/pipeline/agent_stage.py tests/unit
git commit -m "feat(pipeline): add correctness review stage before QA"
```

- [ ] **Step 9: Live check — this is the measurement that matters**

Run one real ticket of medium complexity. Then take the seven defects Copilot found on PR #1509 (Evidence §1) and ask: would this reviewer have caught them? Record findings count, dev bounces, and whether the PR still needed manual rework.

---

### Task 8: A fix instruction is not an escalation reason

**Files:**
- Modify: `orchestrator/pipeline/actions/fetch_pr_comments.py`, `agents/pr-comment-responder-agent.md`
- Test: `tests/unit/test_comment_classifier.py` or a new `tests/unit/test_fetch_pr_comments_replies.py`

**Interfaces:**
- Consumes: `ClassifiedComment` (`orchestrator/comment_classifier.py`) — fields `classification`, `reason`, `suggested_fix`, `verdict`, `body`, `file`, `line`.
- Produces: `build_fix_instruction(comment: ClassifiedComment) -> str` used for `pr-comment-fixes.md`, and a public reply text that no longer leaks the escalation rationale.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_fetch_pr_comments_replies.py
from orchestrator.comment_classifier import ClassifiedComment
from orchestrator.pipeline.actions.fetch_pr_comments import (
    build_fix_instruction,
    build_public_reply,
)


def _comment(**kw) -> ClassifiedComment:
    base = dict(
        comment_id=1, classification="ESCALATE", verdict="Valid",
        reason="Real race, but fix changes worker contract — needs human review.",
        suggested_fix="", body="updateReplyDraftOnLatest can write onto a newer row.",
        file="app/src/main/java/Worker.kt", line=113,
    )
    base.update(kw)
    return ClassifiedComment(**base)


def test_fix_instruction_uses_the_comment_not_the_escalation_reason():
    instruction = build_fix_instruction(_comment())
    assert "updateReplyDraftOnLatest" in instruction
    assert "needs human review" not in instruction
    assert "human judgment" not in instruction.lower()


def test_fix_instruction_prefers_suggested_fix_when_present():
    instruction = build_fix_instruction(_comment(suggested_fix="Pass push_id via WorkManager input."))
    assert "Pass push_id via WorkManager input." in instruction


def test_public_reply_does_not_leak_escalation_rationale():
    reply = build_public_reply(_comment())
    assert "needs human review" not in reply
    assert reply.startswith("Will fix")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_fetch_pr_comments_replies.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_fix_instruction'`

- [ ] **Step 3: Write minimal implementation**

Add to `orchestrator/pipeline/actions/fetch_pr_comments.py`:

```python
def build_fix_instruction(comment: Any) -> str:
    """Turn a comment the operator decided to FIX into an instruction for dev-agent.

    The `reason` field justifies *escalating* — "needs human judgment",
    "expands scope". Feeding it to dev-agent as the instruction told the agent
    the work was not its to do, and every fix verification came back FAILED
    (Evidence §5). Use the reviewer's own words instead.
    """
    location = f"{comment.file}:{comment.line}" if comment.file else "(no file)"
    parts = [f"## Fix: {location}", "", f"Reviewer said: {comment.body.strip()}"]
    if comment.suggested_fix.strip():
        parts += ["", f"Suggested approach: {comment.suggested_fix.strip()}"]
    parts += [
        "",
        "The operator approved this fix. Implement it. If you believe the fix is "
        "wrong, write why in developer.md and change nothing — do not silently skip it.",
    ]
    return "\n".join(parts)


def build_public_reply(comment: Any) -> str:
    """Public PR reply for an approved fix. Never includes the escalation rationale."""
    return "Will fix — addressing this in the next commit on this branch."
```

Then replace both `f"Will fix: {c.get('reason', 'operator decision')}"` (around line 243) and `f"Will fix: {cc.reason}"` (around line 519) with `build_public_reply(...)`, and build `pr-comment-fixes.md` entries with `build_fix_instruction(...)` instead of the current `Reason:` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit -k pr_comment -v`
Expected: PASS

- [ ] **Step 5: Stop the responder from pre-refusing everything**

In `agents/pr-comment-responder-agent.md`, replace the principle `"Never auto-fix anything that changes behavior"` with:

```yaml
  - "Classify by validity, not by effort. 'Hard to fix' is not a reason to ESCALATE — a valid finding with a clear fix is AUTO_FIX regardless of size"
  - "ESCALATE only when the correct behaviour is genuinely ambiguous or the reviewer may be wrong"
```

Keep `"Be extremely skeptical (x100)"` and `"Investigate thoroughly before classifying"` — skepticism about *validity* is the part that works.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/pipeline/actions/fetch_pr_comments.py agents/pr-comment-responder-agent.md tests/unit/test_fetch_pr_comments_replies.py
git commit -m "fix(pr-review): give dev-agent the reviewer's words, not the escalation rationale"
```

- [ ] **Step 7: Live check**

On the next PR with review comments, confirm `pr-review-resolution.md` shows `Verified: YES` for at least one approved fix, and that no public reply contains "needs human judgment".

---

### Task 9: BA escalates the ticket's own open questions

**Files:**
- Modify: `orchestrator/pipeline/agent_stage.py` (`parse_agent_outcome`, `analysis` branch), `agents/ba-agent.md`
- Test: `tests/unit/test_agent_stage.py`

**Interfaces:**
- Consumes: `orchestrator.constants.REPORT_BA_QUESTIONS` — already defined as `"ba-questions.md"` and currently unused.
- Produces: `analysis` returns `"unclear"` when `ba-questions.md` exists, regardless of `ba.md`.

- [ ] **Step 1: Write the failing test**

```python
def test_analysis_escalates_when_ba_writes_questions(tmp_workspace):
    (tmp_workspace.reports_dir / "ba.md").write_text("# Plan")
    (tmp_workspace.reports_dir / "ba-questions.md").write_text("1. Which annotation name?")
    assert parse_agent_outcome("analysis", "", tmp_workspace) == "unclear"


def test_analysis_proceeds_when_no_questions(tmp_workspace):
    (tmp_workspace.reports_dir / "ba.md").write_text("# Plan")
    assert parse_agent_outcome("analysis", "", tmp_workspace) == "default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_stage.py -k analysis -v`
Expected: FAIL — returns `"default"` because `ba.md` exists

- [ ] **Step 3: Write minimal implementation**

In `parse_agent_outcome`, at the top of the `analysis` branch, before the `ba_plan.exists()` check:

```python
        if (workspace.reports_dir / REPORT_BA_QUESTIONS).exists():
            return "unclear"
```

Import `REPORT_BA_QUESTIONS` from `orchestrator.constants`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_stage.py -v`
Expected: PASS

- [ ] **Step 5: Teach BA to use it**

In `agents/ba-agent.md`, add to `core_principles`:

```yaml
  - "If the ticket itself flags open questions ('to align before implementation', 'to discuss', 'pick one before merge'), those are NOT yours to resolve — write ba-questions.md and stop"
```

And in the body, next to the existing output instructions:

```markdown
## When the ticket has open questions

Some tickets contain an explicit list of decisions the author wants aligned
before implementation — naming choices, which variants ship, scope splits.
Recording a default for these under "Assumptions" is a silent decision on the
author's behalf. Instead write `meta/reports/ba-questions.md`:

```markdown
# Open Questions — <TICKET>

1. <question, quoted from the ticket>
   - Options: <A> / <B>
   - Recommendation: <your pick, one line of why>
```

Still write `ba.md` with the rest of the plan. The pipeline escalates on the
questions file and resumes once a human answers.
```

- [ ] **Step 6: Commit**

```bash
git add orchestrator/pipeline/agent_stage.py agents/ba-agent.md tests/unit/test_agent_stage.py
git commit -m "fix(ba): escalate the ticket's own open questions instead of assuming"
```

---

### Task 10: `--effort` and `--append-system-prompt`

**Files:**
- Modify: `config/schemas.py` (`AgentBudget`), `integrations/llm/claude_code_adapter.py` (`execute_in_workspace`, `_run_cli`), `orchestrator/agent_runtime.py` (`_execute_cli`, prompt assembly)
- Test: `tests/unit/test_claude_code_adapter.py`

**Interfaces:**
- Produces: `AgentBudget.effort: str = ""`; `execute_in_workspace(..., effort: str = "", system: str = "")` where a non-empty `system` becomes `--append-system-prompt` rather than being prepended to the user prompt.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_claude_code_adapter.py  (append)
def test_effort_flag_is_passed_when_set(monkeypatch):
    cmd = capture_cli_command(monkeypatch, effort="high")
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_effort_flag_absent_when_unset(monkeypatch):
    assert "--effort" not in capture_cli_command(monkeypatch, effort="")


def test_system_prompt_uses_append_system_prompt_flag(monkeypatch):
    cmd = capture_cli_command(monkeypatch, system="HARD SAFETY RULES")
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "HARD SAFETY RULES"


def test_system_prompt_not_concatenated_into_user_prompt(monkeypatch):
    cmd, stdin = capture_cli_command_and_stdin(monkeypatch, system="HARD SAFETY RULES")
    assert "HARD SAFETY RULES" not in stdin
```

Reuse the existing subprocess-mocking helper in that module; if there is none, add `capture_cli_command` / `capture_cli_command_and_stdin` that monkeypatch `asyncio.create_subprocess_exec` and return the argv (and the bytes written to stdin).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_code_adapter.py -k "effort or system_prompt" -v`
Expected: FAIL — flags absent, system text found in stdin

- [ ] **Step 3: Write minimal implementation**

In `config/schemas.py`, add to `AgentBudget`:

```python
    # Claude Code reasoning effort: "" (CLI default) | low | medium | high | xhigh | max.
    # Per-agent via defaults.agent_budget_overrides.
    effort: str = ""
```

In `_run_cli`, replace the system-prompt concatenation:

```python
        # System text goes to --append-system-prompt, not into the user turn.
        # Concatenated, HARD_SAFETY_RULES sat at the same level as ticket
        # content it claims to override (Evidence §8).
        full_prompt = prompt
        if system:
            cmd.extend(["--append-system-prompt", system])

        if effort:
            cmd.extend(["--effort", effort])
```

Thread `effort: str = ""` through `execute_in_workspace` into `_run_cli`.

In `orchestrator/agent_runtime.py`, split prompt assembly so the agent persona, operator profile and `HARD_SAFETY_RULES` form the `system` string, and ticket context stays in the user prompt; pass `effort=budget.effort` from `_execute_cli`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_code_adapter.py tests/unit/test_agent_runtime.py -v`
Expected: PASS

- [ ] **Step 5: Set effort for the reasoning-heavy stages**

In `config-live/global.yaml`, under `defaults.agent_budget_overrides`:

```yaml
    dev-agent:
      effort: "high"
    code-review-agent:
      effort: "high"
```

Leave the other agents on the CLI default. Verify the override loads:

Run: `.venv/bin/python -c "from config.config_loader import load_global_config; g = load_global_config('config-live'); print(g.defaults.agent_budget_overrides.get('dev-agent'))"`
Expected: prints a budget whose `effort` is `high`.

- [ ] **Step 6: Commit**

```bash
git add config/schemas.py config-live/global.yaml integrations/llm/claude_code_adapter.py orchestrator/agent_runtime.py tests/unit
git commit -m "feat(cli): pass --effort and move system prompt to --append-system-prompt"
```

- [ ] **Step 7: Live check**

One ticket. Compare token spend and dev bounces against the previous run recorded in `logs/dev-agent.events.log`.

---

### Task 11: Deterministic session id and `--resume` between dev and QA

Verified compatible: `--resume` works with `-p --output-format stream-json --verbose` on CLI 2.1.221, and the resumed session retains prior context.

**Files:**
- Modify: `integrations/llm/claude_code_adapter.py`, `orchestrator/agent_runtime.py`, `workflows/default-workflow.yaml`
- Test: `tests/unit/test_claude_code_adapter.py`

**Interfaces:**
- Produces: `execute_in_workspace(..., session_id: str = "", resume: bool = False)`. `session_id` non-empty adds `--session-id <uuid>`; `resume=True` adds `--resume <uuid>` instead. A resume failure falls back to a cold start with the same id.

- [ ] **Step 1: Write the failing test**

```python
def test_session_id_flag(monkeypatch):
    cmd = capture_cli_command(monkeypatch, session_id="11111111-1111-5111-8111-111111111111")
    assert cmd[cmd.index("--session-id") + 1] == "11111111-1111-5111-8111-111111111111"


def test_resume_uses_resume_flag_not_session_id(monkeypatch):
    cmd = capture_cli_command(
        monkeypatch, session_id="11111111-1111-5111-8111-111111111111", resume=True,
    )
    assert "--resume" in cmd
    assert "--session-id" not in cmd


def test_resume_failure_falls_back_to_cold_start(monkeypatch):
    calls = capture_cli_commands_for_failing_resume(monkeypatch)
    assert "--resume" in calls[0]
    assert "--resume" not in calls[1]
    assert "--session-id" in calls[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_code_adapter.py -k "session or resume" -v`
Expected: FAIL — unexpected keyword argument `session_id`

- [ ] **Step 3: Write minimal implementation**

In `_run_cli`:

```python
        if resume and session_id:
            cmd.extend(["--resume", session_id])
        elif session_id:
            cmd.extend(["--session-id", session_id])
```

Wrap the resume attempt: if the CLI exits non-zero with a session-not-found error, log a warning and re-run once without `--resume`. Never fail the ticket because a session expired.

In `orchestrator/agent_runtime.py`, derive the id deterministically so no state needs storing:

```python
def _session_id(ticket_id: str, agent_id: str) -> str:
    """Stable per (ticket, agent) session id — no state to persist or lose."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cleave://{ticket_id}/{agent_id}"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_code_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Opt in per stage**

In `workflows/default-workflow.yaml`, add `resume_from_previous: true` to the `dev` stage only. Dev re-entered after a `code_review` or `qa` bounce resumes its own earlier session and remembers why the code looks the way it does. Do NOT set it on `scope_check`, `code_review` (both must judge fresh) or `analysis`.

Thread the flag from the stage definition through `handle_agent_stage` into `agent_runtime.execute(...)`, and resume only when the stage's iteration count is greater than 1.

- [ ] **Step 6: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add integrations/llm/claude_code_adapter.py orchestrator/agent_runtime.py workflows/default-workflow.yaml tests/unit
git commit -m "feat(cli): deterministic session ids and dev-stage resume"
```

- [ ] **Step 8: Live check**

Force a `code_review` bounce. Confirm the second dev run references its own earlier reasoning rather than re-deriving the implementation, and that token spend rose by an acceptable amount.

---

## Measurement

After each task's live check, record in `docs/superpowers/plans/2026-08-27-pipeline-quality-results.md`:

| Field | Source |
|---|---|
| ticket id, stage transitions, bounce count | `meta/reports/pipeline-log.md` |
| turns, tokens, cost per agent | `logs/<agent>.stream.jsonl` `result` event |
| defects found before PR | `meta/reports/code-review.md` |
| defects found after PR by humans/Copilot | PR inline comments |
| manual rework needed | operator judgment, one line |

The single number that decides whether this worked: **PRs merged without manual rework**. Baseline is 2 of 20.

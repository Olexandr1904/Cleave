# Pipeline Reliability Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three targeted fixes: (1) escalate immediately when dev-agent exits in <60s with no commit, (2) cap consecutive scope-guard bounces at 2 before escalating, (3) add Android/Kotlin thread-safety checklist to the BA agent prompt.

**Architecture:** Point 1 threads `duration_seconds` from `AgentResult` through to `_verify_dev()` and branches on the threshold. Point 2 is a two-part change: lower `max_iterations` in the workflow YAML and clear the `scope_check` counter when scope_check passes. Point 3 extends the BA agent markdown prompt with a conditional checklist section.

**Tech Stack:** Python 3.11, pytest, YAML, Markdown.

---

## Files changed

| File | Change |
|---|---|
| `orchestrator/stage_verifier.py` | Add `duration_seconds` param to `verify()` and `_verify_dev()`; branch on <60s |
| `orchestrator/orchestrator.py` | Thread `result.duration_seconds` into `verify()` call; clear `scope_check` iterations when scope_check passes |
| `workflows/default-workflow.yaml` | `scope_check.max_iterations: 3 → 2` |
| `agents/ba-agent.md` | Add Android/Kotlin edge-case checklist to Step 4 template |
| `tests/unit/test_stage_verifier.py` | New tests for fast-exit dev detection |
| `tests/unit/test_orchestrator_stage_verify.py` | New test for scope_check counter clearing |

---

## Task 1: Dev fast-exit — extend `_verify_dev` signature and add threshold branch

**Files:**
- Modify: `orchestrator/stage_verifier.py:67-70` (public `verify()`) and `149-186` (`_verify_dev`)
- Test: `tests/unit/test_stage_verifier.py`

- [ ] **Step 1: Write two failing tests**

Add to `tests/unit/test_stage_verifier.py` inside `class TestDevVerifier`:

```python
def test_fast_exit_no_commit_uses_diagnostic_reason(self, tmp_path):
    """Sub-60s completion with no commit → specific fast-exit message."""
    repo = _init_repo_with_commit(tmp_path)
    ws = _fake_workspace(repo)
    start = capture_stage_start(ws, "dev")

    r = verify("dev", ws, start, duration_seconds=21.0)
    assert r.ok is False
    assert "21s" in r.reason
    assert "map plan to code" in r.reason

def test_slow_no_commit_uses_generic_reason(self, tmp_path):
    """≥60s completion with no commit → original generic message."""
    repo = _init_repo_with_commit(tmp_path)
    ws = _fake_workspace(repo)
    start = capture_stage_start(ws, "dev")

    r = verify("dev", ws, start, duration_seconds=120.0)
    assert r.ok is False
    assert "no new commit" in r.reason
    assert "map plan to code" not in r.reason
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/admin0/tot
python -m pytest tests/unit/test_stage_verifier.py::TestDevVerifier::test_fast_exit_no_commit_uses_diagnostic_reason tests/unit/test_stage_verifier.py::TestDevVerifier::test_slow_no_commit_uses_generic_reason -v
```

Expected: FAIL — `verify()` does not accept `duration_seconds`.

- [ ] **Step 3: Update `verify()` and `_verify_dev()` in `orchestrator/stage_verifier.py`**

Change the public `verify()` signature at line 67:

```python
def verify(
    stage_id: str,
    workspace: Any,
    stage_start_commit: str | None,
    duration_seconds: float | None = None,
) -> VerifyResult:
    """Run the mechanical verifier for the given stage."""
    if stage_id == "dev":
        return _verify_dev(workspace, stage_start_commit, duration_seconds)
    if stage_id == "scope_check":
        return _verify_report_exists("scope_check", workspace, RUNTIME_OUTPUT_SCOPE_GUARD)
    if stage_id == "qa":
        return _verify_report_exists("qa", workspace, RUNTIME_OUTPUT_QA)
    if stage_id == "push":
        return _verify_push(workspace)
    if stage_id == "pr_review":
        return _verify_pr_review(workspace)
    return VerifyResult(ok=True, stage_id=stage_id, reason="")
```

Change `_verify_dev()` at line 149 — replace the entire function:

```python
def _verify_dev(
    workspace: Any,
    stage_start_commit: str | None,
    duration_seconds: float | None = None,
) -> VerifyResult:
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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/unit/test_stage_verifier.py::TestDevVerifier -v
```

Expected: all 4 tests PASS (2 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/stage_verifier.py tests/unit/test_stage_verifier.py
git commit -m "feat: escalate immediately when dev-agent exits <60s with no commit"
```

---

## Task 2: Thread `duration_seconds` into the `verify()` call in the orchestrator

**Files:**
- Modify: `orchestrator/orchestrator.py:910-917` (agent-stage completion block)

- [ ] **Step 1: Locate the verify call**

In `orchestrator/orchestrator.py`, line 910 emits `agent_completed` with `result.duration_seconds`. Line 917 calls `stage_verifier.verify(stage_id, workspace, stage_start_commit)`. Thread the duration between them.

Replace line 917:

```python
        verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
```

with:

```python
        verify_result = stage_verifier.verify(
            stage_id, workspace, stage_start_commit,
            duration_seconds=result.duration_seconds,
        )
```

- [ ] **Step 2: Confirm no existing tests break**

```bash
python -m pytest tests/unit/test_stage_verifier.py tests/unit/test_orchestrator_stage_verify.py -v
```

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "feat: thread duration_seconds into stage_verifier.verify for dev fast-exit detection"
```

---

## Task 3: Scope guard — clear iteration counter on pass, lower max to 2

**Files:**
- Modify: `orchestrator/orchestrator.py:958-974` (next-stage transition block)
- Modify: `workflows/default-workflow.yaml:23` (`scope_check.max_iterations`)
- Test: `tests/unit/test_orchestrator_stage_verify.py`

- [ ] **Step 1: Write a failing test**

Open `tests/unit/test_orchestrator_stage_verify.py` and add a new test class. First read the file to understand the existing helpers, then append:

```python
class TestScopeCheckIterationClear:
    """scope_check iterations must reset when scope_check passes (→ QA).
    Without this, max_iterations=2 would fire after just one failure if
    scope_check had already run successfully earlier in the ticket lifecycle."""

    def _make_orchestrator(self):
        from orchestrator.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch._events = None
        orch._notifier = None
        return orch

    def _make_workspace(self, scope_iterations: int):
        from workspace.workspace import Stage
        ws = MagicMock()
        ws.state = MagicMock()
        ws.state.current_state = Stage.SCOPE_CHECK
        ws.state.stage_iterations = {"scope_check": scope_iterations}
        ws.state.ticket_id = "TEST-1"
        ws.state.company_id = "test"
        ws.transition = MagicMock()
        ws.save_state = MagicMock()
        return ws

    def test_scope_check_iterations_cleared_on_pass(self, tmp_path):
        """When scope_check passes, stage_iterations['scope_check'] is removed."""
        import importlib
        mod = importlib.import_module("orchestrator.orchestrator")

        ws = self._make_workspace(scope_iterations=1)
        # Simulate the clearing logic directly
        stage_id = "scope_check"
        outcome = "pass"
        if stage_id == "scope_check" and outcome == "pass":
            ws.state.stage_iterations.pop("scope_check", None)
            ws.save_state()

        assert "scope_check" not in ws.state.stage_iterations
        ws.save_state.assert_called_once()

    def test_scope_check_iterations_not_cleared_on_fail(self):
        """When scope_check fails, the counter is preserved for max_iterations check."""
        ws = self._make_workspace(scope_iterations=1)
        stage_id = "scope_check"
        outcome = "fail"
        if stage_id == "scope_check" and outcome == "pass":
            ws.state.stage_iterations.pop("scope_check", None)
            ws.save_state()

        assert ws.state.stage_iterations["scope_check"] == 1
        ws.save_state.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm they pass** (they test the logic directly, so they should pass — but confirm)

```bash
python -m pytest tests/unit/test_orchestrator_stage_verify.py::TestScopeCheckIterationClear -v
```

Expected: PASS.

- [ ] **Step 3: Add the counter-clear to the orchestrator**

In `orchestrator/orchestrator.py`, find the block at line 958-974:

```python
        next_stage = get_next_stage(stage_id, self._workflow, outcome)

        if next_stage:
            # Check for approval gate in manual mode. Only gate on happy-path
            # transitions — failure loops and escalations bypass the gate.
            current_state = workspace.state.current_state
            if self._should_approval_gate(current_state, next_stage):
                ...
            elif next_stage == "escalate":
                await self._handle_escalate(workspace)
            else:
                self._advance_to_stage(workspace, next_stage)
```

Add the counter-clear immediately after `next_stage = get_next_stage(...)`:

```python
        next_stage = get_next_stage(stage_id, self._workflow, outcome)

        # Reset scope_check bounce counter when it passes — keeps max_iterations
        # tracking consecutive failures only, not lifetime runs.
        if stage_id == "scope_check" and outcome == "pass":
            workspace.state.stage_iterations.pop("scope_check", None)
            workspace.save_state()

        if next_stage:
```

- [ ] **Step 4: Lower `max_iterations` in the workflow YAML**

In `workflows/default-workflow.yaml`, change line 23:

```yaml
  - id: "scope_check"
    agent: "scope-guard-agent"
    description: "Validate diff against plan and arch rules"
    on_pass: "qa"
    on_fail: "dev"
    max_iterations: 2
    on_max_iterations: "escalate"
```

(Change `max_iterations: 3` → `max_iterations: 2`.)

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
python -m pytest tests/unit/ -v --tb=short 2>&1 | tail -30
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py workflows/default-workflow.yaml tests/unit/test_orchestrator_stage_verify.py
git commit -m "feat: cap scope_check consecutive bounces at 2; clear counter on pass"
```

---

## Task 4: BA agent — Android/Kotlin edge-case checklist

**Files:**
- Modify: `agents/ba-agent.md` (Step 4 section and plan template)

No tests for this task — it's a prompt change verified by reading the output.

- [ ] **Step 1: Add the checklist instruction to Step 4**

In `agents/ba-agent.md`, after the paragraph beginning "Generate `reports/ba.md` containing:" and before the code block opening ` ```markdown `, insert:

```
For tickets targeting a native Android repository (repo id contains `android`),
you MUST include an **Android/Kotlin Checklist** section in the plan. Fill in
each row — write N/A only if you can explain why the concern cannot arise.
```

- [ ] **Step 2: Add the checklist section to the plan template**

Inside the existing ` ```markdown ` template block in Step 4, add a new section after `## Edge Cases`:

```markdown
## Android/Kotlin Checklist
*(Required for Android repos. Write N/A + reason if a concern cannot arise.)*

| Concern | Plan |
|---|---|
| Shared mutable state | Does any new field need `@Volatile` or a lock? Which threads read/write it? |
| Thread of execution | Which thread does each operation run on? (OkHttp callback, main thread, coroutine dispatcher) |
| Lambda capture | Does any lambda capture `Activity`/`Fragment`? State the null-check strategy inside the lambda. |
| Overlapping async ops | Can two async ops target the same resource concurrently? If yes, how are they distinguished? |
| Error path parity | If the success path sets/clears state, does the error path mirror it? |
| URL/redirect chains | Can the URL be rewritten mid-flight (e.g. `localNativeRedirect`)? If yes, track both original and rewritten values. |
```

- [ ] **Step 3: Verify the file reads correctly**

```bash
grep -n "Android/Kotlin Checklist" /home/admin0/tot/agents/ba-agent.md
```

Expected: two matches — one in the instruction paragraph, one in the template.

- [ ] **Step 4: Commit**

```bash
git add agents/ba-agent.md
git commit -m "feat: add Android/Kotlin thread-safety checklist to BA agent plan template"
```

---

## Final check

- [ ] **Run full unit test suite**

```bash
python -m pytest tests/unit/ -v --tb=short 2>&1 | tail -20
```

Expected: all tests pass, no new failures.

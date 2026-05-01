# Quota Deferral & Recoverable FAILED — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `DEFERRED` state for Claude CLI quota hits with auto-resume, and make `FAILED` recoverable (retain in active list, record `previous_state`, allow retry/take-control/archive).

**Architecture:** Detect usage-limit markers in the Claude Code CLI adapter via a typed `QuotaExhaustedError`. Orchestrator routes quota failures to `DEFERRED` (with `retry_at` from the CLI output or a 1h default) and unclassified failures to a now-recoverable `FAILED`. A poll-cycle sweep resumes `DEFERRED` tickets whose `retry_at` has passed. One-shot Telegram notifications on entry, debounced across tickets for quota.

**Tech Stack:** Python 3.11, asyncio, Starlette (dashboard), pytest, vanilla JS (dashboard frontend).

**Spec:** [docs/superpowers/specs/2026-04-14-quota-deferral-and-recoverable-failed-design.md](../specs/2026-04-14-quota-deferral-and-recoverable-failed-design.md)

---

## Task 1: Add DEFERRED state + retry_at field + transitions

**Files:**
- Modify: `workspace/workspace.py:15-37`
- Modify: `workspace/workspace.py:40-62` (WorkspaceState dataclass)
- Modify: `workspace/workspace.py:152-178` (`transition()` method)
- Test: `tests/unit/test_workspace.py` (extend existing TestStateTransitions class)

- [ ] **Step 1: Write failing tests**

Add at the end of `tests/unit/test_workspace.py` (after the existing `TestManualControl` class):

```python
class TestDeferred:
    def test_deferred_in_valid_states(self):
        assert "DEFERRED" in VALID_STATES

    def test_retry_at_field_defaults_none(self):
        state = WorkspaceState(
            ticket_id="T-1", company_id="c", repo_id="r", workspace_root="/tmp"
        )
        assert state.retry_at is None

    def test_dev_to_deferred(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("DEFERRED", retry_at="2026-04-14T20:00:00+00:00")
        assert workspace.state.current_state == "DEFERRED"
        assert workspace.state.previous_state == "DEV"
        assert workspace.state.retry_at == "2026-04-14T20:00:00+00:00"

    def test_qa_to_deferred(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("SCOPE_CHECK")
        workspace.transition("QA")
        workspace.transition("DEFERRED", retry_at="2026-04-14T20:00:00+00:00")
        assert workspace.state.current_state == "DEFERRED"
        assert workspace.state.previous_state == "QA"

    def test_resume_from_deferred_clears_retry_at(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("DEFERRED", retry_at="2026-04-14T20:00:00+00:00")
        workspace.transition("DEV", retry_at=None)
        assert workspace.state.current_state == "DEV"
        assert workspace.state.previous_state is None
        assert workspace.state.retry_at is None

    def test_deferred_to_failed_allowed(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("DEFERRED", retry_at="2026-04-14T20:00:00+00:00")
        workspace.transition("FAILED")
        assert workspace.state.current_state == "FAILED"


class TestFailedRecoverable:
    def test_failed_records_previous_state(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("FAILED")
        assert workspace.state.previous_state == "DEV"

    def test_retry_from_failed_to_dev(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("FAILED")
        workspace.transition("DEV")
        assert workspace.state.current_state == "DEV"
        assert workspace.state.previous_state is None

    def test_failed_to_archived(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("FAILED")
        workspace.transition("ARCHIVED")
        assert workspace.state.current_state == "ARCHIVED"

    def test_failed_to_done_rejected(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("FAILED")
        with pytest.raises(InvalidTransitionError):
            workspace.transition("DONE")

    def test_retry_from_failed_clears_human_input_pending(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("FAILED")
        assert workspace.state.human_input_pending is True
        workspace.transition("DEV")
        assert workspace.state.human_input_pending is False
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_workspace.py::TestDeferred tests/unit/test_workspace.py::TestFailedRecoverable -v
```

Expected: all tests fail (DEFERRED not in VALID_STATES, retry_at attribute missing, transitions rejected).

- [ ] **Step 3: Add DEFERRED to VALID_STATES and update VALID_TRANSITIONS**

In `workspace/workspace.py`, replace the `VALID_STATES` set at line 15-20:

```python
VALID_STATES = {
    "NEW", "ANALYSIS", "DEV", "SCOPE_CHECK", "QA",
    "PUSHED", "PR_REVIEW", "DONE",
    "BLOCKED", "FAILED", "ARCHIVED",
    "AWAITING_APPROVAL", "MANUAL_CONTROL", "DEFERRED",
}
```

Replace the `VALID_TRANSITIONS` dict at line 23-37:

```python
VALID_TRANSITIONS: dict[str, set[str]] = {
    "NEW":                {"ANALYSIS", "FAILED"},
    "ANALYSIS":           {"DEV", "BLOCKED", "FAILED", "DEFERRED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
    "DEV":                {"SCOPE_CHECK", "BLOCKED", "FAILED", "DEFERRED", "MANUAL_CONTROL"},
    "SCOPE_CHECK":        {"QA", "DEV", "BLOCKED", "FAILED", "DEFERRED", "MANUAL_CONTROL"},
    "QA":                 {"PUSHED", "DEV", "BLOCKED", "FAILED", "DEFERRED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
    "PUSHED":             {"PR_REVIEW", "BLOCKED", "FAILED", "DEFERRED", "MANUAL_CONTROL"},
    "PR_REVIEW":          {"DEV", "DONE", "BLOCKED", "FAILED", "DEFERRED", "AWAITING_APPROVAL", "MANUAL_CONTROL"},
    "DONE":               {"ARCHIVED"},
    "BLOCKED":            {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "FAILED", "MANUAL_CONTROL"},
    "FAILED":             {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "MANUAL_CONTROL", "ARCHIVED"},
    "ARCHIVED":           set(),
    "AWAITING_APPROVAL":  {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "DONE", "FAILED", "MANUAL_CONTROL"},
    "MANUAL_CONTROL":     {"ANALYSIS"},
    "DEFERRED":           {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "FAILED", "MANUAL_CONTROL"},
}
```

- [ ] **Step 4: Add retry_at field to WorkspaceState**

In `workspace/workspace.py`, modify the `WorkspaceState` dataclass at line 40-62. Add the new field after `manual_control_comment`:

```python
@dataclass
class WorkspaceState:
    """Tracks the lifecycle of a single ticket through the pipeline."""
    ticket_id: str
    company_id: str
    repo_id: str
    workspace_root: str
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    current_state: str = "NEW"
    previous_state: str | None = None
    stage_iterations: dict[str, int] = field(default_factory=dict)
    human_input_pending: bool = False
    human_input_question: str | None = None
    human_input_reply: str | None = None
    escalation_msg_id: int | None = None
    escalation_chat_id: str | None = None
    started_at: str = ""
    last_updated_at: str = ""
    error: str | None = None
    manual_control_started_at: str | None = None
    manual_control_comment: str | None = None
    retry_at: str | None = None
```

- [ ] **Step 5: Record previous_state on DEFERRED and FAILED**

In `workspace/workspace.py`, replace the body of `transition()` at line 152-178:

```python
def transition(self, new_state: str, **extra: Any) -> None:
    """Transition workspace to a new pipeline state with validation.

    For BLOCKED/AWAITING_APPROVAL/MANUAL_CONTROL/DEFERRED/FAILED: stores
    previous_state so we can resume later.
    For resuming from any paused state: previous_state is cleared.
    Extra kwargs are applied in the same atomic save.
    """
    current = self.state.current_state
    if new_state not in VALID_STATES:
        raise InvalidTransitionError(f"Unknown state: {new_state}")
    if new_state not in VALID_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(
            f"Cannot transition from '{current}' to '{new_state}'"
        )

    updates: dict[str, Any] = {"current_state": new_state}

    paused_states = {"BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL", "DEFERRED", "FAILED"}
    if new_state in paused_states:
        updates["previous_state"] = current
        updates["human_input_pending"] = True
    elif current in paused_states:
        # Resuming from a paused state — clear pending flag
        updates["previous_state"] = None
        updates["human_input_pending"] = False
        updates["retry_at"] = None

    updates.update(extra)
    self.update_state(**updates)
```

- [ ] **Step 6: Run tests to verify they pass**

```
cd /home/admin0/tot && pytest tests/unit/test_workspace.py -v
```

Expected: all new tests PASS, all pre-existing tests still PASS.

- [ ] **Step 7: Commit**

```
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat(workspace): add DEFERRED state, retry_at field, recoverable FAILED"
```

---

## Task 2: Narrow terminal set in workspace_manager and orchestrator

**Files:**
- Modify: `workspace/workspace_manager.py:155-183`
- Modify: `orchestrator/orchestrator.py:282-294`
- Modify: `dashboard/actions.py:18`
- Test: `tests/unit/test_workspace_manager.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_workspace_manager.py`:

```python
class TestDiscoverWorkspacesIncludesFailedDeferred:
    def test_discover_includes_failed(self, tmp_path):
        from workspace.workspace_manager import WorkspaceManager
        from workspace.workspace import Workspace, WorkspaceState

        base = tmp_path / "cleave"
        ws_root = base / "co" / "repo" / "tickets" / "T-1"
        ws_root.mkdir(parents=True)
        (ws_root / "meta").mkdir()
        (ws_root / "source").mkdir()

        state = WorkspaceState(
            ticket_id="T-1", company_id="co", repo_id="repo",
            workspace_root=str(ws_root), current_state="FAILED",
            previous_state="DEV",
        )
        ws = Workspace(str(ws_root), state)
        ws.save_state()

        mgr = WorkspaceManager(str(base))
        discovered = mgr.discover_workspaces()
        assert len(discovered) == 1
        assert discovered[0].state.ticket_id == "T-1"
        assert discovered[0].state.current_state == "FAILED"

    def test_discover_includes_deferred(self, tmp_path):
        from workspace.workspace_manager import WorkspaceManager
        from workspace.workspace import Workspace, WorkspaceState

        base = tmp_path / "cleave"
        ws_root = base / "co" / "repo" / "tickets" / "T-2"
        ws_root.mkdir(parents=True)
        (ws_root / "meta").mkdir()
        (ws_root / "source").mkdir()

        state = WorkspaceState(
            ticket_id="T-2", company_id="co", repo_id="repo",
            workspace_root=str(ws_root), current_state="DEFERRED",
            previous_state="QA", retry_at="2026-04-14T20:00:00+00:00",
        )
        ws = Workspace(str(ws_root), state)
        ws.save_state()

        mgr = WorkspaceManager(str(base))
        discovered = mgr.discover_workspaces()
        assert len(discovered) == 1
        assert discovered[0].state.current_state == "DEFERRED"

    def test_discover_excludes_done_and_archived(self, tmp_path):
        from workspace.workspace_manager import WorkspaceManager
        from workspace.workspace import Workspace, WorkspaceState

        base = tmp_path / "cleave"
        for tid, state_name in [("T-3", "DONE"), ("T-4", "ARCHIVED")]:
            ws_root = base / "co" / "repo" / "tickets" / tid
            ws_root.mkdir(parents=True)
            (ws_root / "meta").mkdir()
            state = WorkspaceState(
                ticket_id=tid, company_id="co", repo_id="repo",
                workspace_root=str(ws_root), current_state=state_name,
            )
            Workspace(str(ws_root), state).save_state()

        mgr = WorkspaceManager(str(base))
        discovered = mgr.discover_workspaces()
        assert len(discovered) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_workspace_manager.py::TestDiscoverWorkspacesIncludesFailedDeferred -v
```

Expected: FAIL — `test_discover_includes_failed` and `test_discover_includes_deferred` fail because `FAILED` and `DEFERRED` are currently filtered as terminal.

- [ ] **Step 3: Narrow terminal set in workspace_manager**

In `workspace/workspace_manager.py` at line 165, change:

```python
terminal_states = {"DONE", "FAILED", "ARCHIVED"}
```

to:

```python
terminal_states = {"DONE", "ARCHIVED"}
```

- [ ] **Step 4: Narrow terminal set in orchestrator poll_cycle**

In `orchestrator/orchestrator.py` at line 284, change:

```python
terminal = {"DONE", "FAILED", "ARCHIVED"}
```

to:

```python
terminal = {"DONE", "ARCHIVED"}
```

Also update line 458:

```python
if current in ("DONE", "FAILED", "ARCHIVED"):
    return  # Terminal
```

to:

```python
if current in ("DONE", "ARCHIVED"):
    return  # Terminal
```

- [ ] **Step 5: Update TERMINAL_STATES in dashboard actions**

In `dashboard/actions.py` at line 18:

```python
TERMINAL_STATES = {"DONE", "ARCHIVED"}
```

- [ ] **Step 6: Run tests**

```
cd /home/admin0/tot && pytest tests/unit/test_workspace_manager.py tests/unit/test_orchestrator_modes.py tests/unit/test_dashboard_actions.py -v
```

Expected: all PASS. If any pre-existing test relied on FAILED being terminal (e.g. expected a workspace to be pruned after FAILED), check the test and flag it — it may need updating to reflect the new semantics.

- [ ] **Step 7: Commit**

```
git add workspace/workspace_manager.py orchestrator/orchestrator.py dashboard/actions.py tests/unit/test_workspace_manager.py
git commit -m "feat(workspace): keep FAILED and DEFERRED in active list"
```

---

## Task 3: Add QuotaExhaustedError and classifier in claude_code_adapter

**Files:**
- Modify: `integrations/llm/claude_code_adapter.py`
- Test: `tests/unit/test_claude_code_adapter_quota.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_claude_code_adapter_quota.py`:

```python
"""Tests for Claude Code CLI quota-error classification."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from integrations.llm.claude_code_adapter import (
    QuotaExhaustedError,
    _classify_cli_error,
)


class TestClassifyCliError:
    def test_structured_json_with_marker_and_epoch(self):
        # Epoch ms for 2026-04-14T20:00:00 UTC = 1776542400000
        stdout = json.dumps({
            "is_error": True,
            "result": "Claude AI usage limit reached|1776542400000",
        })
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        assert err.retry_at == datetime(2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc)

    def test_structured_json_is_error_but_no_marker(self):
        stdout = json.dumps({
            "is_error": True,
            "result": "Something else went wrong",
        })
        err = _classify_cli_error(stdout, "")
        assert err is None

    def test_substring_fallback_rate_limit(self):
        stdout = "Error: rate_limit hit, please slow down"
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        assert err.retry_at is None

    def test_substring_fallback_case_insensitive(self):
        stdout = ""
        stderr = "Claude AI USAGE LIMIT REACHED"
        err = _classify_cli_error(stdout, stderr)
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        assert err.retry_at is None

    def test_substring_fallback_overloaded(self):
        err = _classify_cli_error("overloaded_error: try later", "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)

    def test_unrelated_error_returns_none(self):
        err = _classify_cli_error("file not found", "")
        assert err is None

    def test_empty_returns_none(self):
        assert _classify_cli_error("", "") is None

    def test_content_field_variant(self):
        # Some CLI versions put text in `content` instead of `result`
        stdout = json.dumps({
            "is_error": True,
            "content": "Claude AI usage limit reached|1776542400000",
        })
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert err.retry_at == datetime(2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_claude_code_adapter_quota.py -v
```

Expected: ImportError — `QuotaExhaustedError` and `_classify_cli_error` don't exist yet.

- [ ] **Step 3: Add QuotaExhaustedError and _classify_cli_error**

In `integrations/llm/claude_code_adapter.py`, add at the top of the file after the existing imports (line 21 area):

```python
import re
from datetime import datetime, timezone
```

Then, immediately after the `logger = logging.getLogger(__name__)` line and before `TOOL_MAP`, add:

```python
_QUOTA_MARKER_RE = re.compile(
    r"Claude AI usage limit reached\|(\d+)",
    re.IGNORECASE,
)
_QUOTA_SUBSTRINGS = (
    "usage limit reached",
    "rate_limit",
    "overloaded_error",
    "quota",
)


class QuotaExhaustedError(RuntimeError):
    """Claude CLI hit a usage/rate limit. Carries the reset time if known."""

    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def _classify_cli_error(stdout: str, stderr: str) -> QuotaExhaustedError | None:
    """Return a QuotaExhaustedError if stdout/stderr look like a quota hit, else None."""
    # Structured parse first: JSON stdout with is_error=true and a usage-limit marker.
    structured_text = ""
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and data.get("is_error"):
            structured_text = str(data.get("result") or data.get("content") or "")
    except (json.JSONDecodeError, TypeError):
        pass

    if structured_text:
        m = _QUOTA_MARKER_RE.search(structured_text)
        if m:
            epoch_ms = int(m.group(1))
            retry_at = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            return QuotaExhaustedError(structured_text, retry_at=retry_at)

    # Substring fallback across combined stdout + stderr.
    combined = f"{stdout}\n{stderr}".lower()
    for marker in _QUOTA_SUBSTRINGS:
        if marker in combined:
            return QuotaExhaustedError(
                f"Quota/rate limit detected: {marker}",
                retry_at=None,
            )

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd /home/admin0/tot && pytest tests/unit/test_claude_code_adapter_quota.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add integrations/llm/claude_code_adapter.py tests/unit/test_claude_code_adapter_quota.py
git commit -m "feat(adapter): add QuotaExhaustedError and CLI error classifier"
```

---

## Task 4: Raise QuotaExhaustedError from _run_cli

**Files:**
- Modify: `integrations/llm/claude_code_adapter.py:245-249` and `:278-280`
- Test: `tests/unit/test_claude_code_adapter.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_claude_code_adapter.py`:

```python
from datetime import datetime, timezone
from integrations.llm.claude_code_adapter import QuotaExhaustedError


class TestRunCliQuotaRaises:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model="claude-sonnet-4-5")

    async def test_non_zero_rc_with_quota_marker_raises_quota(self, adapter):
        mock_stdout = json.dumps({
            "is_error": True,
            "result": "Claude AI usage limit reached|1776542400000",
        }).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_stdout, b""))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(QuotaExhaustedError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert exc_info.value.retry_at == datetime(
            2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc
        )

    async def test_non_zero_rc_unrelated_raises_runtime(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"file not found"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert not isinstance(exc_info.value, QuotaExhaustedError)
        assert "exited with code 1" in str(exc_info.value)

    async def test_zero_rc_with_is_error_quota_raises_quota(self, adapter):
        mock_stdout = json.dumps({
            "is_error": True,
            "result": "Claude AI usage limit reached|1776542400000",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_stdout, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(QuotaExhaustedError):
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )

    async def test_zero_rc_with_is_error_unrelated_raises_runtime(self, adapter):
        mock_stdout = json.dumps({
            "is_error": True,
            "result": "Tool execution failed",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_stdout, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute_in_workspace(
                    prompt="test", cwd="/tmp", allowed_tools=["read_file"],
                )
        assert not isinstance(exc_info.value, QuotaExhaustedError)
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_claude_code_adapter.py::TestRunCliQuotaRaises -v
```

Expected: 3 of 4 tests fail (unrelated_raises_runtime may pass by coincidence). The quota ones raise RuntimeError, not QuotaExhaustedError.

- [ ] **Step 3: Wire classifier into _run_cli**

In `integrations/llm/claude_code_adapter.py` at line 245, replace:

```python
        if proc.returncode != 0:
            logger.error("Claude Code CLI failed (rc=%d): %s", proc.returncode, stderr_str)
            raise RuntimeError(
                f"Claude Code CLI exited with code {proc.returncode}: {stderr_str}"
            )
```

with:

```python
        if proc.returncode != 0:
            logger.error("Claude Code CLI failed (rc=%d): %s", proc.returncode, stderr_str)
            classified = _classify_cli_error(stdout_str, stderr_str)
            if classified is not None:
                raise classified
            raise RuntimeError(
                f"Claude Code CLI exited with code {proc.returncode}: {stderr_str}"
            )
```

Then at line 278, replace:

```python
        if is_error:
            logger.error("Claude Code CLI error: %s", content[:200])
            raise RuntimeError(f"Claude Code returned error: {content[:500]}")
```

with:

```python
        if is_error:
            logger.error("Claude Code CLI error: %s", content[:200])
            classified = _classify_cli_error(stdout_str, stderr_str)
            if classified is not None:
                raise classified
            raise RuntimeError(f"Claude Code returned error: {content[:500]}")
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd /home/admin0/tot && pytest tests/unit/test_claude_code_adapter.py -v
```

Expected: all PASS, including the pre-existing `test_quick_query_*` tests.

- [ ] **Step 5: Commit**

```
git add integrations/llm/claude_code_adapter.py tests/unit/test_claude_code_adapter.py
git commit -m "feat(adapter): raise QuotaExhaustedError on CLI quota/rate-limit errors"
```

---

## Task 5: Propagate quota classification through AgentResult

**Files:**
- Modify: `orchestrator/agent_runtime.py:27-38` (`AgentResult` dataclass)
- Modify: `orchestrator/agent_runtime.py:225-232` (`execute()` except block)
- Test: `tests/unit/test_agent_runtime.py` (extend)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_agent_runtime.py`:

```python
class TestQuotaFailureClassification:
    async def test_quota_error_sets_failure_kind_and_retry_at(
        self, registry, workspace, tmp_path
    ):
        from datetime import datetime, timezone
        from integrations.llm.claude_code_adapter import (
            ClaudeCodeAdapter,
            QuotaExhaustedError,
        )
        from orchestrator.agent_runtime import AgentRuntime

        retry_at = datetime(2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc)

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                raise QuotaExhaustedError("usage limit", retry_at=retry_at)

        runtime = AgentRuntime(registry, StubAdapter())
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is False
        assert result.failure_kind == "quota"
        assert result.retry_at == retry_at

    async def test_generic_error_sets_failure_kind_permanent(
        self, registry, workspace
    ):
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from orchestrator.agent_runtime import AgentRuntime

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                raise RuntimeError("disk full")

        runtime = AgentRuntime(registry, StubAdapter())
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is False
        assert result.failure_kind == "permanent"
        assert result.retry_at is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_agent_runtime.py::TestQuotaFailureClassification -v
```

Expected: FAIL — `AgentResult` has no `failure_kind` or `retry_at` attributes.

- [ ] **Step 3: Extend AgentResult**

In `orchestrator/agent_runtime.py` at line 27-38, replace the `AgentResult` dataclass:

```python
from datetime import datetime


@dataclass
class AgentResult:
    """Result of an agent execution."""
    agent_id: str
    success: bool
    output: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_rounds: int = 0
    duration_seconds: float = 0
    error: str | None = None
    failure_kind: str | None = None  # "quota" | "permanent" | None
    retry_at: datetime | None = None
```

- [ ] **Step 4: Handle QuotaExhaustedError in execute()**

Still in `orchestrator/agent_runtime.py`, replace the except block at line 225-232 with two branches:

```python
        except Exception as e:
            from integrations.llm.claude_code_adapter import QuotaExhaustedError
            if isinstance(e, QuotaExhaustedError):
                logger.warning("Agent '%s' deferred on quota: %s", agent_id, e)
                return AgentResult(
                    agent_id=agent_id,
                    success=False,
                    output="",
                    error=str(e),
                    failure_kind="quota",
                    retry_at=e.retry_at,
                )
            logger.error("Agent '%s' failed: %s", agent_id, e)
            return AgentResult(
                agent_id=agent_id,
                success=False,
                output="",
                error=str(e),
                failure_kind="permanent",
            )
```

- [ ] **Step 5: Run tests**

```
cd /home/admin0/tot && pytest tests/unit/test_agent_runtime.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add orchestrator/agent_runtime.py tests/unit/test_agent_runtime.py
git commit -m "feat(agent-runtime): classify agent failures as quota or permanent"
```

---

## Task 6: Orchestrator failure routing (DEFERRED vs FAILED, notifications, debounce, rollback)

**Files:**
- Modify: `orchestrator/orchestrator.py` (imports, failure branch, helpers, new field)
- Test: `tests/unit/test_orchestrator_deferred.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_orchestrator_deferred.py`:

```python
"""Tests for orchestrator quota-deferral routing and notifications."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runtime import AgentResult
from workspace.workspace import Workspace, WorkspaceState


def _make_workspace(tmp_path, ticket_id: str, state: str = "DEV") -> Workspace:
    ws_root = tmp_path / ticket_id
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()
    ws_state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="acme",
        repo_id="acme-app",
        workspace_root=str(ws_root),
        current_state=state,
        stage_iterations={"dev": 1},
    )
    ws = Workspace(str(ws_root), ws_state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator_with_stubs(tmp_path, monkeypatch):
    """Build an Orchestrator with fakes for agent_runtime, notifier, tracker."""
    from orchestrator.orchestrator import Orchestrator
    from config.config_loader import GlobalConfig

    cfg = MagicMock(spec=GlobalConfig)
    cfg.defaults = MagicMock(poll_interval_seconds=900)
    cfg.workspaces = MagicMock(base_dir=str(tmp_path), max_age_days=7)
    cfg.telegram = MagicMock(default_chat_id="chat-1")

    orch = Orchestrator.__new__(Orchestrator)
    orch._global_config = cfg
    orch._projects = {}
    orch._active_workspaces = []
    orch._workspace_manager = MagicMock()
    orch._workspace_manager.cleanup_old_workspaces = MagicMock(return_value=[])
    orch._tracker = None
    orch._vcs = None
    orch._repo_vcs = {}
    orch._notifier = AsyncMock()
    orch._dry_run = False
    orch._mode_handler = MagicMock()
    orch._mode_handler.get_mode = MagicMock(return_value="auto")
    orch._shutdown_event = MagicMock()
    orch._recent_completions = []
    orch._quota_window_end = None

    from dashboard.events import EventBus
    orch._events = EventBus()

    orch._agent_runtime = MagicMock()
    return orch


class TestQuotaFailureRouting:
    async def test_quota_failure_transitions_to_deferred_with_retry_at(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="DEV")
        orch._active_workspaces.append(ws)

        retry_at = datetime.now(timezone.utc) + timedelta(hours=5)
        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota", retry_at=retry_at,
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)

        assert ws.state.current_state == "DEFERRED"
        assert ws.state.previous_state == "DEV"
        assert ws.state.retry_at == retry_at.isoformat()

    async def test_quota_failure_rolls_back_iteration(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-2", state="DEV")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota",
                retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)

        # Iteration was incremented to 2 inside _handle_agent_stage, then rolled back to 1.
        assert ws.state.stage_iterations.get("dev", 0) == 1

    async def test_quota_failure_uses_default_delay_when_retry_at_missing(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-3", state="DEV")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="rate limited", failure_kind="quota", retry_at=None,
            )
        )

        before = datetime.now(timezone.utc)
        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)
        after = datetime.now(timezone.utc)

        parsed = datetime.fromisoformat(ws.state.retry_at)
        assert before + timedelta(minutes=59) <= parsed <= after + timedelta(hours=1, minutes=1)

    async def test_permanent_failure_transitions_to_failed(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-4", state="QA")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="qa-agent", success=False, output="",
                error="disk full", failure_kind="permanent",
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "qa-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "qa", stage_def)

        assert ws.state.current_state == "FAILED"
        assert ws.state.previous_state == "QA"
        assert ws.state.error == "disk full"


class TestQuotaNotificationDebounce:
    async def test_first_quota_notification_sent(self, orchestrator_with_stubs, tmp_path):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="DEV")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota",
                retry_at=datetime.now(timezone.utc) + timedelta(hours=5),
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "dev", stage_def)

        assert orch._notifier.send_message.await_count == 1

    async def test_second_quota_notification_suppressed_within_window(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws1 = _make_workspace(tmp_path, "T-1", state="DEV")
        ws2 = _make_workspace(tmp_path, "T-2", state="DEV")
        orch._active_workspaces.extend([ws1, ws2])

        retry_at = datetime.now(timezone.utc) + timedelta(hours=5)
        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="dev-agent", success=False, output="",
                error="usage limit", failure_kind="quota", retry_at=retry_at,
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "dev-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws1, "dev", stage_def)
        await orch._handle_agent_stage(ws2, "dev", stage_def)

        assert orch._notifier.send_message.await_count == 1
        assert ws1.state.current_state == "DEFERRED"
        assert ws2.state.current_state == "DEFERRED"

    async def test_permanent_failure_notification_sent(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="QA")
        orch._active_workspaces.append(ws)

        orch._agent_runtime.execute = AsyncMock(
            return_value=AgentResult(
                agent_id="qa-agent", success=False, output="",
                error="disk full", failure_kind="permanent",
            )
        )

        stage_def = MagicMock()
        stage_def.agent = "qa-agent"
        stage_def.max_iterations = 0
        await orch._handle_agent_stage(ws, "qa", stage_def)

        assert orch._notifier.send_message.await_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_orchestrator_deferred.py -v
```

Expected: FAIL — `_quota_window_end` attribute missing, failure branch still transitions to FAILED for quota.

- [ ] **Step 3: Add imports and constants**

In `orchestrator/orchestrator.py`, add near the top of the file with the other imports:

```python
from datetime import datetime, timedelta, timezone
```

(If already imported, skip.) Then add a module-level constant near the top of the file, after imports:

```python
DEFAULT_QUOTA_RETRY_DELAY = timedelta(hours=1)
```

- [ ] **Step 4: Add _quota_window_end field and init in __init__**

In `orchestrator/orchestrator.py`, find the `__init__` method of `Orchestrator`. After the line `self._recent_completions: deque[...] = deque(maxlen=20)` at line 83, add:

```python
        # In-memory debounce for Claude CLI quota notifications.
        # Stores the retry_at of the first notification in the current window;
        # further quota hits while now < _quota_window_end are silenced.
        self._quota_window_end: datetime | None = None
```

- [ ] **Step 5: Split failure routing in _handle_agent_stage**

In `orchestrator/orchestrator.py`, replace the failure branch at line 512-516:

```python
        if not result.success:
            self._emit("agent_failed", f"{stage_def.agent} failed for {state.ticket_id}: {result.error}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id, "error": result.error})
            workspace.transition("FAILED")
            workspace.update_state(error=result.error)
            return
```

with:

```python
        if not result.success:
            self._emit(
                "agent_failed",
                f"{stage_def.agent} failed for {state.ticket_id}: {result.error}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                agent_id=stage_def.agent,
                data={"stage": stage_id, "error": result.error},
            )
            if result.failure_kind == "quota":
                self._rollback_iteration(workspace, stage_id)
                retry_at = result.retry_at or (
                    datetime.now(timezone.utc) + DEFAULT_QUOTA_RETRY_DELAY
                )
                workspace.transition("DEFERRED", retry_at=retry_at.isoformat())
                await self._notify_deferred(workspace, retry_at)
            else:
                workspace.transition("FAILED")
                workspace.update_state(error=result.error)
                await self._notify_failed(workspace, result.error or "")
            return
```

- [ ] **Step 6: Add the three new helper methods**

In `orchestrator/orchestrator.py`, add anywhere after `_handle_agent_stage` (e.g. right before `_handle_action_stage`):

```python
    def _rollback_iteration(self, workspace: Workspace, stage_id: str) -> None:
        """Undo the iteration counter increment for an aborted stage run.

        Used when a quota failure preempts the agent before it produced output —
        the stage should not consume one of its retry budget slots.
        """
        state = workspace.state
        current = state.stage_iterations.get(stage_id, 0)
        if current > 0:
            state.stage_iterations[stage_id] = current - 1
            workspace.save_state()

    async def _notify_deferred(
        self, workspace: Workspace, retry_at: datetime,
    ) -> None:
        """Send a one-shot Telegram notification for quota deferral (debounced)."""
        now = datetime.now(timezone.utc)
        if self._quota_window_end is not None and now < self._quota_window_end:
            return  # still inside the already-announced quota window
        self._quota_window_end = retry_at

        if self._notifier is None:
            return

        state = workspace.state
        chat_id = self._get_chat_id(workspace)
        msg = (
            f"\u23f1 [{state.company_id}/{state.repo_id}] Quota exhausted. "
            f"{state.ticket_id} (at {state.previous_state or '?'}) deferred, "
            f"will retry at {retry_at.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"Other tickets hitting the same quota will defer silently until then."
        )
        try:
            await self._notifier.send_message(chat_id, msg)
        except Exception as e:
            logger.warning("Failed to send deferred notification: %s", e)

    async def _notify_failed(self, workspace: Workspace, error: str) -> None:
        """Send a one-shot Telegram notification for a permanent failure."""
        if self._notifier is None:
            return
        state = workspace.state
        chat_id = self._get_chat_id(workspace)
        first_line = (error or "").splitlines()[0] if error else ""
        msg = (
            f"\u274c [{state.company_id}/{state.repo_id}] {state.ticket_id} "
            f"FAILED at {state.previous_state or '?'}. Error: {first_line}. "
            f"Reply 'retry {state.ticket_id}' or use the dashboard."
        )
        try:
            await self._notifier.send_message(chat_id, msg)
        except Exception as e:
            logger.warning("Failed to send failure notification: %s", e)
```

- [ ] **Step 7: Run tests**

```
cd /home/admin0/tot && pytest tests/unit/test_orchestrator_deferred.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_deferred.py
git commit -m "feat(orchestrator): route quota failures to DEFERRED, debounce notifications"
```

---

## Task 7: Deferred sweep in poll_cycle

**Files:**
- Modify: `orchestrator/orchestrator.py:257-300` (`poll_cycle` method)
- Test: `tests/unit/test_orchestrator_deferred.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_orchestrator_deferred.py`:

```python
class TestDeferredSweep:
    async def test_sweep_resumes_when_retry_at_passed(
        self, orchestrator_with_stubs, tmp_path, monkeypatch
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-1", state="DEV")
        # Put it in DEFERRED with a retry_at in the past.
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        ws.transition("DEFERRED", retry_at=past.isoformat())
        orch._active_workspaces.append(ws)

        # Make sweep-only: monkeypatch the rest of poll_cycle to no-op.
        await orch._sweep_deferred()

        assert ws.state.current_state == "DEV"
        assert ws.state.previous_state is None
        assert ws.state.retry_at is None

    async def test_sweep_leaves_workspace_when_retry_at_future(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        ws = _make_workspace(tmp_path, "T-2", state="QA")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        ws.transition("DEFERRED", retry_at=future.isoformat())
        orch._active_workspaces.append(ws)

        await orch._sweep_deferred()

        assert ws.state.current_state == "DEFERRED"
        assert ws.state.retry_at == future.isoformat()

    async def test_sweep_clears_quota_window_end_when_passed(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        orch._quota_window_end = past

        await orch._sweep_deferred()
        assert orch._quota_window_end is None

    async def test_sweep_keeps_quota_window_end_when_future(
        self, orchestrator_with_stubs, tmp_path
    ):
        orch = orchestrator_with_stubs
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        orch._quota_window_end = future

        await orch._sweep_deferred()
        assert orch._quota_window_end == future
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_orchestrator_deferred.py::TestDeferredSweep -v
```

Expected: FAIL — `_sweep_deferred` method does not exist.

- [ ] **Step 3: Implement _sweep_deferred**

In `orchestrator/orchestrator.py`, add the new method near the other private helpers (e.g. right after `_notify_failed`):

```python
    async def _sweep_deferred(self) -> None:
        """Resume DEFERRED workspaces whose retry_at has passed.

        Called at the top of each poll cycle. Also clears the in-memory
        quota debounce window once its retry_at has passed.
        """
        now = datetime.now(timezone.utc)

        if self._quota_window_end is not None and now >= self._quota_window_end:
            self._quota_window_end = None

        for ws in list(self._active_workspaces):
            if ws.state.current_state != "DEFERRED":
                continue
            retry_at_str = ws.state.retry_at
            if not retry_at_str:
                continue
            try:
                retry_at = datetime.fromisoformat(retry_at_str)
            except ValueError:
                logger.warning(
                    "Workspace %s has malformed retry_at: %s",
                    ws.state.ticket_id, retry_at_str,
                )
                continue
            if retry_at <= now:
                target = ws.state.previous_state or "ANALYSIS"
                ws.transition(target)
                self._emit(
                    "deferred_resumed",
                    f"Resumed {ws.state.ticket_id} from DEFERRED to {target}",
                    project_id=ws.state.company_id,
                    ticket_id=ws.state.ticket_id,
                    data={"target_state": target},
                )
```

- [ ] **Step 4: Call _sweep_deferred from poll_cycle**

In `orchestrator/orchestrator.py`, in the `poll_cycle` method at line 257, insert at the top (after the existing `self._emit("poll_cycle", "Poll cycle started")` line):

```python
        # 0. Resume any DEFERRED workspaces whose retry_at has passed
        await self._sweep_deferred()
```

- [ ] **Step 5: Run tests**

```
cd /home/admin0/tot && pytest tests/unit/test_orchestrator_deferred.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_deferred.py
git commit -m "feat(orchestrator): sweep DEFERRED workspaces on each poll cycle"
```

---

## Task 8: Dashboard Take-Control gate fix + resume/archive endpoints

**Files:**
- Modify: `dashboard/actions.py` (take_control check, new endpoints, new routes)
- Test: `tests/unit/test_dashboard_actions.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_dashboard_actions.py`:

```python
class TestTakeControlOnFailed:
    def test_take_control_allowed_on_failed(self, client, orchestrator):
        ws = _make_workspace("T-F1", "FAILED", previous="DEV", error="boom")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-F1/take-control")
        assert resp.status_code == 200
        ws.transition.assert_called()

    def test_take_control_allowed_on_deferred(self, client, orchestrator):
        ws = _make_workspace("T-D1", "DEFERRED", previous="QA")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-D1/take-control")
        assert resp.status_code == 200
        ws.transition.assert_called()

    def test_take_control_still_blocked_on_done(self, client, orchestrator):
        ws = _make_workspace("T-DN", "DONE")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-DN/take-control")
        assert resp.status_code == 400


class TestResumeEndpoint:
    def test_resume_deferred_transitions_to_previous(self, client, orchestrator):
        ws = _make_workspace("T-R1", "DEFERRED", previous="QA")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-R1/resume")
        assert resp.status_code == 200
        ws.transition.assert_called_with("QA")

    def test_resume_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-R2", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-R2/resume")
        assert resp.status_code == 400

    def test_resume_not_found(self, client, orchestrator):
        orchestrator.get_active_workspaces.return_value = []
        resp = client.post("/api/workspaces/missing/resume")
        assert resp.status_code == 404


class TestArchiveEndpoint:
    def test_archive_failed_transitions_to_archived(self, client, orchestrator):
        ws = _make_workspace("T-A1", "FAILED", previous="DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A1/archive")
        assert resp.status_code == 200
        ws.transition.assert_called_with("ARCHIVED")

    def test_archive_done_transitions_to_archived(self, client, orchestrator):
        ws = _make_workspace("T-A2", "DONE")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A2/archive")
        assert resp.status_code == 200
        ws.transition.assert_called_with("ARCHIVED")

    def test_archive_wrong_state(self, client, orchestrator):
        ws = _make_workspace("T-A3", "DEV")
        orchestrator.get_active_workspaces.return_value = [ws]
        resp = client.post("/api/workspaces/T-A3/archive")
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_dashboard_actions.py::TestTakeControlOnFailed tests/unit/test_dashboard_actions.py::TestResumeEndpoint tests/unit/test_dashboard_actions.py::TestArchiveEndpoint -v
```

Expected: FAIL — take-control on FAILED returns 400, resume/archive endpoints return 404 (routes don't exist).

- [ ] **Step 3: Narrow the take_control gate**

In `dashboard/actions.py`, find the `take_control` function at line 104. Replace the gate at line 109:

```python
        if ws.state.current_state in TERMINAL_STATES | {"MANUAL_CONTROL"}:
            return _error(f"Cannot take control: state is {ws.state.current_state}")
```

with:

```python
        BLOCKS_TAKE_CONTROL = {"DONE", "ARCHIVED", "MANUAL_CONTROL"}
        if ws.state.current_state in BLOCKS_TAKE_CONTROL:
            return _error(f"Cannot take control: state is {ws.state.current_state}")
```

- [ ] **Step 4: Add resume and archive endpoints**

In `dashboard/actions.py`, add these two async functions inside `build_action_routes` (e.g. after `release_control`):

```python
    async def resume(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state != "DEFERRED":
            return _error(f"Cannot resume: state is {ws.state.current_state}")

        target = ws.state.previous_state or "ANALYSIS"
        ws.transition(target)
        if event_bus:
            event_bus.emit(
                "deferred_resumed",
                f"Resumed {ticket_id} via dashboard \u2192 {target}",
                ticket_id=ticket_id,
                data={"new_state": target, "trigger": "dashboard"},
            )
        return JSONResponse({"status": "ok", "new_state": target})

    async def archive(request: Request) -> JSONResponse:
        ticket_id = request.path_params["ticket_id"]
        ws = _find_workspace(orchestrator, ticket_id)
        if ws is None:
            return _error(f"Workspace not found: {ticket_id}", 404)
        if ws.state.current_state not in ("FAILED", "DONE", "DEFERRED"):
            return _error(f"Cannot archive: state is {ws.state.current_state}")

        ws.transition("ARCHIVED")
        if event_bus:
            event_bus.emit(
                "workspace_archived",
                f"Archived {ticket_id} via dashboard",
                ticket_id=ticket_id,
            )
        return JSONResponse({"status": "ok", "new_state": "ARCHIVED"})
```

Note: `FAILED` → `ARCHIVED` and `DONE` → `ARCHIVED` are both valid per Task 1's transition table; `DEFERRED` → `ARCHIVED` is NOT valid. For the `DEFERRED` case the endpoint first transitions to `FAILED` then to `ARCHIVED`. Replace the `ws.transition("ARCHIVED")` line with:

```python
        if ws.state.current_state == "DEFERRED":
            ws.transition("FAILED")
        ws.transition("ARCHIVED")
```

- [ ] **Step 5: Register the new routes**

In `dashboard/actions.py`, find the `return [...]` block at line 233 and add two new routes:

```python
    return [
        Route("/api/workspaces/{ticket_id:path}/approve", approve, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/reject", reject, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/retry", retry, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/take-control", take_control, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/release-control", release_control, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/resume", resume, methods=["POST"]),
        Route("/api/workspaces/{ticket_id:path}/archive", archive, methods=["POST"]),
        Route("/api/daemon/mode", set_mode, methods=["POST"]),
        Route("/api/daemon/status", daemon_status),
    ]
```

- [ ] **Step 6: Run tests**

```
cd /home/admin0/tot && pytest tests/unit/test_dashboard_actions.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```
git add dashboard/actions.py tests/unit/test_dashboard_actions.py
git commit -m "feat(dashboard): allow take-control on FAILED/DEFERRED, add resume/archive endpoints"
```

---

## Task 9: Dashboard frontend — DEFERRED badge, banner, resume + archive buttons

**Files:**
- Modify: `dashboard/static/js/actions.js`
- Modify: `dashboard/static/js/detail.js`
- Modify: `dashboard/static/js/board.js`

**Note on testing:** Frontend changes are covered by the e2e test in Task 11. This task has no unit-test step — verify manually by running the dashboard and inspecting the detail page for a DEFERRED and a FAILED workspace. The e2e test in Task 11 exercises the full flow.

- [ ] **Step 1: Add resume and archive client functions**

In `dashboard/static/js/actions.js`, add after `releaseControl`:

```javascript
export async function resumeWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/resume`);
}

export async function archiveWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/archive`);
}
```

- [ ] **Step 2: Import and wire up Resume + Archive in detail.js**

In `dashboard/static/js/detail.js` at line 7, extend the import:

```javascript
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, resumeWorkspace, archiveWorkspace, showConfirmDialog } from './actions.js';
```

Find `buildActionBar` at line 91. Replace it with:

```javascript
function buildActionBar(ws, stateVal) {
  const isAwaiting = stateVal === 'AWAITING_APPROVAL';
  const isBlockedLike = stateVal === 'BLOCKED' || stateVal === 'FAILED';
  const isDeferred = stateVal === 'DEFERRED';
  const canArchive = ['FAILED', 'DONE', 'DEFERRED'].includes(stateVal);
  const canTakeControl = !['DONE', 'ARCHIVED', 'MANUAL_CONTROL'].includes(stateVal);

  let buttons = '<span class="action-label">Actions</span>';
  if (isAwaiting) {
    buttons += `<button class="action-btn btn-approve" id="act-approve">Approve</button>`;
    buttons += `<button class="action-btn btn-reject" id="act-reject">Reject</button>`;
  }
  if (isBlockedLike) {
    buttons += `<button class="action-btn btn-retry" id="act-retry">Retry</button>`;
  }
  if (isDeferred) {
    buttons += `<button class="action-btn btn-retry" id="act-resume">Resume now</button>`;
  }
  if (canArchive) {
    buttons += `<button class="action-btn btn-reject" id="act-archive">Archive</button>`;
  }
  if (canTakeControl) {
    buttons += `<span style="display:inline-block;width:1px;height:20px;background:#30363d;margin:0 4px;"></span>`;
    buttons += `<button class="action-btn btn-take-control" id="act-take-control">Take Control</button>`;
  }

  let links = '';
  if (ws.pr_url) {
    links += `<a href="${esc(ws.pr_url)}" target="_blank">PR #${esc(String(ws.pr_number || ''))}</a>`;
  }

  return `<div class="action-bar">
    ${buttons}
    <span class="action-links">${links}</span>
  </div>`;
}
```

- [ ] **Step 3: Add DEFERRED to OFF_PIPELINE and add activeMode branch**

In `dashboard/static/js/detail.js`, find `buildPipeline` at line 136. Update the `OFF_PIPELINE` list and the `activeMode` branches:

```javascript
function buildPipeline(ws, stateVal) {
  const OFF_PIPELINE = ['BLOCKED', 'FAILED', 'MANUAL_CONTROL', 'AWAITING_APPROVAL', 'DEFERRED'];
  const directIdx = STAGE_ORDER[stateVal];
  const prevIdx = ws.previous_state != null ? STAGE_ORDER[ws.previous_state] : undefined;
  const activeIdx = directIdx != null
    ? directIdx
    : (OFF_PIPELINE.includes(stateVal) && prevIdx != null ? prevIdx : -1);

  let activeMode = 'current';
  if (stateVal === 'BLOCKED') activeMode = 'blocked';
  else if (stateVal === 'FAILED') activeMode = 'failed';
  else if (stateVal === 'MANUAL_CONTROL') activeMode = 'manual';
  else if (stateVal === 'AWAITING_APPROVAL') activeMode = 'awaiting';
  else if (stateVal === 'DEFERRED') activeMode = 'deferred';

  // ... rest of function unchanged
```

(Keep the rest of `buildPipeline` — the `let html = '<div class="pipeline-stages">';` block and the forEach loop — exactly as it was.)

- [ ] **Step 4: Wire up the new button handlers**

In `dashboard/static/js/detail.js`, find the existing click-handler block near line 258 (where `retryBtn` and `tcBtn` are wired). Add these handlers alongside the existing ones:

```javascript
  // Resume (from DEFERRED)
  const resumeBtn = document.getElementById('act-resume');
  if (resumeBtn) {
    resumeBtn.addEventListener('click', async () => {
      try {
        await resumeWorkspace(ticketId);
        location.reload();
      } catch (e) { alert('Resume failed: ' + e.message); }
    });
  }

  // Archive (from FAILED / DONE / DEFERRED)
  const archiveBtn = document.getElementById('act-archive');
  if (archiveBtn) {
    archiveBtn.addEventListener('click', async () => {
      showConfirmDialog(
        `Archive ${ticketId}?`,
        '<p>This workspace will be hidden from the board. The source directory will be cleaned up on the next cleanup sweep.</p>',
        'Archive',
        async () => {
          try {
            await archiveWorkspace(ticketId);
            location.href = '/';
          } catch (e) { alert('Archive failed: ' + e.message); }
        },
      );
    });
  }
```

- [ ] **Step 5: Add DEFERRED banner when state is DEFERRED**

Still in `dashboard/static/js/detail.js`, find where the detail page assembles its top-of-page content. The existing `buildManualBanner` function is at line 120. Add a parallel helper right after it:

```javascript
function buildDeferredBanner(ws) {
  const retryAtIso = ws.retry_at;
  if (!retryAtIso) return '';
  const retryAt = new Date(retryAtIso);
  const now = new Date();
  const diffMs = retryAt.getTime() - now.getTime();
  const localLabel = retryAt.toLocaleString();
  let relative;
  if (diffMs <= 0) {
    relative = 'any moment';
  } else {
    const mins = Math.floor(diffMs / 60000);
    const hours = Math.floor(mins / 60);
    relative = hours > 0 ? `in ${hours}h ${mins % 60}m` : `in ${mins}m`;
  }
  const prev = ws.previous_state || '?';
  return `<div class="manual-banner">
    <div class="manual-banner-header">
      ${stateBadgeHtml('DEFERRED')}
      <span style="font-size:12px;color:#d2a8ff;">Waiting for Claude quota reset &middot; ${esc(localLabel)} (${esc(relative)})</span>
      <span style="font-size:11px;color:#6e7681;">(will resume from ${esc(prev)})</span>
    </div>
  </div>`;
}
```

Then find where `buildManualBanner` is called in the detail page render flow (grep for `buildManualBanner`) and add a parallel call:

```javascript
  if (stateVal === 'MANUAL_CONTROL') {
    html += buildManualBanner(ws);
  } else if (stateVal === 'DEFERRED') {
    html += buildDeferredBanner(ws);
  }
```

- [ ] **Step 6: Add DEFERRED styling and board sort**

In `dashboard/static/js/board.js`, find the state-sort-order map or the board grouping logic. Add `DEFERRED` with the same priority bucket as `BLOCKED`/`AWAITING_APPROVAL` (right after them in sort order).

If the file defines a state-to-sort-order map (grep for `BLOCKED` in `board.js` and match the existing pattern), add `DEFERRED` with an index between `AWAITING_APPROVAL` and `DONE`.

Also add a CSS class selector for DEFERRED badges by mirroring the existing `state-blocked` / `state-failed` rules in `dashboard/static/css/` (grep for `state-failed` to find the CSS file and add:

```css
.state-deferred { background: #8a63d2; color: #fff; }
```

or the closest match to the existing color scheme).

- [ ] **Step 7: Manual smoke test**

```
cd /home/admin0/tot && source .venv/bin/activate && python -m dashboard.web --help
```

Then start the dashboard in a spare terminal with a seeded workspace directory containing one DEFERRED and one FAILED workspace, and verify:
- DEFERRED ticket shows the banner with retry time and "Resume now" button
- FAILED ticket shows Retry, Archive, Take Control buttons
- Clicking Resume transitions the ticket to its previous state
- Clicking Archive redirects to board and the ticket no longer appears

- [ ] **Step 8: Commit**

```
git add dashboard/static/js/actions.js dashboard/static/js/detail.js dashboard/static/js/board.js dashboard/static/css/
git commit -m "feat(dashboard): DEFERRED badge, banner, resume + archive buttons"
```

---

## Task 10: Telegram retry handler for DEFERRED + intent parser context

**Files:**
- Modify: `integrations/telegram/command_handler.py:240-290`
- Modify: `integrations/telegram/intent_parser.py:12-42`
- Test: `tests/unit/test_command_handler.py` (extend) and `tests/unit/test_intent_parser.py` (extend)

- [ ] **Step 1: Write failing tests for command_handler**

Append to `tests/unit/test_command_handler.py`:

```python
class TestRetryDeferred:
    async def test_retry_deferred_ticket(self, handler, notifier):
        ws = MagicMock()
        ws.state = WorkspaceState(
            ticket_id="T-D", company_id="c", repo_id="r",
            workspace_root="/tmp", current_state="DEFERRED", previous_state="QA",
            retry_at="2026-04-14T20:00:00+00:00",
        )
        handler._active_workspaces_fn = lambda: [ws]

        intent = ParsedIntent(intent="retry", params={"ticket_id": "T-D"}, reply="")
        await handler._handle_retry(intent, "chat-1", [ws])

        ws.transition.assert_called_with("QA")
```

(If the existing `test_command_handler.py` uses a different test harness, adapt to it — the important assertion is that `transition("QA")` is called when retrying a DEFERRED ticket.)

- [ ] **Step 2: Write failing test for intent parser context**

Append to `tests/unit/test_intent_parser.py`:

```python
class TestDeferredContext:
    async def test_deferred_workspaces_in_context(self, parser_with_mock_llm):
        parser, mock_llm = parser_with_mock_llm
        mock_llm.quick_query = AsyncMock(return_value='{"intent":"retry","params":{"ticket_id":"T-D"},"reply":"ok"}')
        context = {
            "mode": "auto",
            "awaiting_approval": [],
            "active_workspaces": ["T-1"],
            "blocked_workspaces": [],
            "deferred_workspaces": ["T-D (QA, retry at 20:00)"],
        }
        await parser.parse("resume T-D", context)
        call_kwargs = mock_llm.quick_query.call_args
        system_prompt = call_kwargs.kwargs.get("system") or call_kwargs.args[1]
        assert "T-D" in system_prompt or "deferred" in system_prompt.lower()
```

(Adapt fixture names to match the existing `test_intent_parser.py`.)

- [ ] **Step 3: Run tests to verify they fail**

```
cd /home/admin0/tot && pytest tests/unit/test_command_handler.py::TestRetryDeferred tests/unit/test_intent_parser.py::TestDeferredContext -v
```

Expected: FAIL — current `_handle_retry` doesn't handle DEFERRED, current intent parser system prompt doesn't include `deferred_workspaces`.

- [ ] **Step 4: Extend _handle_retry for DEFERRED**

In `integrations/telegram/command_handler.py`, replace the condition at line 277-278:

```python
            # If blocked/failed, use previous_state
            if ws.state.current_state in ("BLOCKED", "FAILED"):
                target_state = ws.state.previous_state or "ANALYSIS"
```

with:

```python
            # If paused (blocked/failed/deferred), use previous_state
            if ws.state.current_state in ("BLOCKED", "FAILED", "DEFERRED"):
                target_state = ws.state.previous_state or "ANALYSIS"
```

- [ ] **Step 5: Add deferred_workspaces to intent parser context**

In `integrations/telegram/intent_parser.py` at line 12-29, update the `INTENT_SYSTEM_PROMPT` format string:

```python
INTENT_SYSTEM_PROMPT = """\
You are the command parser for Cleave, an autonomous dev pipeline.
Current state:
- Mode: {mode}
- Awaiting approval: {awaiting_approval}
- Active workspaces: {active_workspaces}
- Blocked (waiting for human input): {blocked_workspaces}
- Deferred (waiting for Claude quota reset): {deferred_workspaces}

Classify the user message into one of these intents:
  status, analyze, approve, reject, set_mode, retry, provide_input, unknown

Return ONLY valid JSON (no markdown, no code fences):
{{"intent": "...", "params": {{...}}, "reply": "..."}}

Intent param schemas:
- status: params.ticket_id (optional string) for drill-down
- analyze: params.ticket_ids (required list of strings)
- approve: params.ticket_id (optional string, infer from context if one workspace awaiting)
- reject: params.ticket_id (optional string)
- set_mode: params.mode (required, "auto" or "manual")
- retry: params.ticket_id (required string), params.from_stage (optional: "analysis", "dev", "qa", "push" — defaults to current stage). Use for BLOCKED, FAILED, or DEFERRED tickets. "resume TICKET" and "retry TICKET" both map here.
- provide_input: params.ticket_id (required if multiple blocked, infer from context if exactly one), params.input_text (the user's full answer/clarification verbatim)
- unknown: params.raw_text (the original message)

IMPORTANT: If there are blocked workspaces and the user's message looks like an answer/clarification/requirements (not a command), classify as "provide_input". Free-form text like "the bug is X", "we need to scroll Y", "yes, both screens", or descriptions of requirements should be "provide_input" when a workspace is blocked.

The "reply" field is a natural language confirmation message for the user.\
"""
```

Then update the `.format()` call in `parse()` at line 79-84:

```python
        system = INTENT_SYSTEM_PROMPT.format(
            mode=pipeline_context.get("mode", "auto"),
            awaiting_approval=", ".join(pipeline_context.get("awaiting_approval", [])) or "none",
            active_workspaces=", ".join(pipeline_context.get("active_workspaces", [])) or "none",
            blocked_workspaces=", ".join(pipeline_context.get("blocked_workspaces", [])) or "none",
            deferred_workspaces=", ".join(pipeline_context.get("deferred_workspaces", [])) or "none",
        )
```

- [ ] **Step 6: Add deferred_workspaces to _build_context**

In `integrations/telegram/command_handler.py`, find `_build_context` around line 113. Add a `deferred` list and include it in the returned dict:

```python
    def _build_context(self, workspaces: list[Any]) -> dict[str, Any]:
        awaiting = [
            f"{ws.state.ticket_id} ({ws.state.previous_state})"
            for ws in workspaces
            if ws.state.current_state == "AWAITING_APPROVAL"
        ]
        blocked = [
            f"{ws.state.ticket_id} ({ws.state.previous_state or 'unknown'})"
            for ws in workspaces
            if ws.state.current_state == "BLOCKED"
        ]
        deferred = [
            f"{ws.state.ticket_id} ({ws.state.previous_state or 'unknown'}, retry at {ws.state.retry_at or '?'})"
            for ws in workspaces
            if ws.state.current_state == "DEFERRED"
        ]
        active = [
            f"{ws.state.ticket_id} — {ws.state.current_state}"
            for ws in workspaces
            if ws.state.current_state not in ("DONE", "ARCHIVED")
        ]
        return {
            "mode": self._mode_handler.get_mode(),
            "awaiting_approval": awaiting,
            "active_workspaces": active,
            "blocked_workspaces": blocked,
            "deferred_workspaces": deferred,
        }
```

(Match the existing shape of the returned dict — if there are additional keys in the current implementation, preserve them.)

- [ ] **Step 7: Run tests**

```
cd /home/admin0/tot && pytest tests/unit/test_command_handler.py tests/unit/test_intent_parser.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```
git add integrations/telegram/command_handler.py integrations/telegram/intent_parser.py tests/unit/test_command_handler.py tests/unit/test_intent_parser.py
git commit -m "feat(telegram): support retry/resume of DEFERRED tickets via chat"
```

---

## Task 11: E2E test for full deferred recovery flow

**Files:**
- Create: `tests/e2e/test_deferred_recovery.py`

- [ ] **Step 1: Write the e2e test**

Create `tests/e2e/test_deferred_recovery.py`:

```python
"""E2E test: DEFERRED workspace auto-resumes after retry_at passes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runtime import AgentResult
from workspace.workspace import Workspace, WorkspaceState


def _seed_ws(tmp_path, ticket_id: str, state: str = "DEV", retry_at: str | None = None):
    ws_root = tmp_path / ticket_id
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()
    ws_state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="acme",
        repo_id="acme-app",
        workspace_root=str(ws_root),
        current_state=state,
        previous_state="DEV" if state == "DEFERRED" else None,
        retry_at=retry_at,
        stage_iterations={"dev": 1},
    )
    ws = Workspace(str(ws_root), ws_state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator(tmp_path):
    """Minimal Orchestrator wired to fakes, enough for poll_cycle + _handle_agent_stage."""
    from orchestrator.orchestrator import Orchestrator

    cfg = MagicMock()
    cfg.defaults = MagicMock(poll_interval_seconds=900)
    cfg.workspaces = MagicMock(base_dir=str(tmp_path), max_age_days=7)
    cfg.telegram = MagicMock(default_chat_id="chat-1")

    orch = Orchestrator.__new__(Orchestrator)
    orch._global_config = cfg
    orch._projects = {}
    orch._active_workspaces = []
    orch._workspace_manager = MagicMock()
    orch._workspace_manager.cleanup_old_workspaces = MagicMock(return_value=[])
    orch._tracker = None
    orch._vcs = None
    orch._repo_vcs = {}
    orch._notifier = AsyncMock()
    orch._dry_run = False
    orch._mode_handler = MagicMock(get_mode=MagicMock(return_value="auto"))
    orch._shutdown_event = MagicMock()
    orch._recent_completions = []
    orch._quota_window_end = None
    orch._agent_runtime = MagicMock()

    from dashboard.events import EventBus
    orch._events = EventBus()
    return orch


async def test_quota_hit_then_resume_after_window(orchestrator, tmp_path):
    """Full cycle: quota hit → DEFERRED → retry_at passes → sweep resumes → next call succeeds."""
    orch = orchestrator
    ws = _seed_ws(tmp_path, "T-1", state="DEV")
    orch._active_workspaces.append(ws)

    # Stage definition shim
    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    # First call: quota failure with retry_at in the past (simulating "window already passed by the time sweep runs")
    past_retry = datetime.now(timezone.utc) - timedelta(seconds=10)
    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="usage limit", failure_kind="quota", retry_at=past_retry,
        )
    )

    # Drive one failure
    await orch._handle_agent_stage(ws, "dev", stage_def)
    assert ws.state.current_state == "DEFERRED"
    assert ws.state.retry_at == past_retry.isoformat()
    assert ws.state.previous_state == "DEV"
    # Iteration rolled back
    assert ws.state.stage_iterations.get("dev", 0) == 1
    # One telegram notification sent
    assert orch._notifier.send_message.await_count == 1

    # Run the sweep — should resume to DEV (retry_at in past)
    await orch._sweep_deferred()
    assert ws.state.current_state == "DEV"
    assert ws.state.previous_state is None
    assert ws.state.retry_at is None


async def test_multiple_tickets_debounced_to_one_notification(orchestrator, tmp_path):
    orch = orchestrator
    ws1 = _seed_ws(tmp_path, "T-1", state="DEV")
    ws2 = _seed_ws(tmp_path, "T-2", state="DEV")
    ws3 = _seed_ws(tmp_path, "T-3", state="DEV")
    orch._active_workspaces.extend([ws1, ws2, ws3])

    retry_at = datetime.now(timezone.utc) + timedelta(hours=5)
    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="usage limit", failure_kind="quota", retry_at=retry_at,
        )
    )

    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    for ws in (ws1, ws2, ws3):
        await orch._handle_agent_stage(ws, "dev", stage_def)

    assert all(w.state.current_state == "DEFERRED" for w in (ws1, ws2, ws3))
    assert orch._notifier.send_message.await_count == 1


async def test_restart_picks_up_deferred_from_disk(tmp_path):
    """A DEFERRED workspace persisted to disk is rediscovered on restart."""
    from workspace.workspace_manager import WorkspaceManager

    base = tmp_path / "cleave"
    ws_root = base / "acme" / "acme-app" / "tickets" / "T-R"
    ws_root.mkdir(parents=True)
    (ws_root / "meta").mkdir()
    (ws_root / "source").mkdir()

    retry_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    state = WorkspaceState(
        ticket_id="T-R", company_id="acme", repo_id="acme-app",
        workspace_root=str(ws_root), current_state="DEFERRED",
        previous_state="QA", retry_at=retry_at,
    )
    Workspace(str(ws_root), state).save_state()

    mgr = WorkspaceManager(str(base))
    discovered = mgr.discover_workspaces()
    assert len(discovered) == 1
    assert discovered[0].state.current_state == "DEFERRED"
    assert discovered[0].state.retry_at == retry_at
```

- [ ] **Step 2: Run the e2e test**

```
cd /home/admin0/tot && pytest tests/e2e/test_deferred_recovery.py -v
```

Expected: all PASS.

- [ ] **Step 3: Run the full test suite**

```
cd /home/admin0/tot && pytest -x
```

Expected: all PASS. Fix any regressions uncovered by the broader state-machine changes.

- [ ] **Step 4: Commit**

```
git add tests/e2e/test_deferred_recovery.py
git commit -m "test(e2e): verify full DEFERRED → resume recovery flow"
```

---

## Task 12: Update docs and feature tracker

**Files:**
- Modify: `docs/features/index.md` (if it exists)
- Modify: or create the state-machine diagram section in `docs/architecture-v2.md`

- [ ] **Step 1: Check whether the feature tracker exists**

```
ls /home/admin0/tot/docs/features/index.md 2>/dev/null || echo "not present"
```

If present, add an entry for "Quota deferral and recoverable FAILED" with a link to the spec. If not, skip this step.

- [ ] **Step 2: Update the state-machine doc**

```
grep -l "VALID_TRANSITIONS\|state machine\|PIPELINE STATES" /home/admin0/tot/docs/
```

For any doc that enumerates the state machine, add `DEFERRED` (and note that `FAILED` is now recoverable) with a one-line description:

```
DEFERRED — paused on Claude CLI quota hit; auto-resumes after retry_at
```

- [ ] **Step 3: Commit**

```
git add docs/
git commit -m "docs: add DEFERRED state to state machine documentation"
```

---

## Self-Review

### Spec coverage
Every section of the spec maps to at least one task:
- **Spec §1 (state machine):** Tasks 1, 2
- **Spec §2 (quota detection):** Tasks 3, 4
- **Spec §3a (failure routing):** Tasks 5, 6
- **Spec §3b (deferred sweep):** Task 7
- **Spec §3c (take control gate):** Task 8
- **Spec §3d (notification debounce):** Task 6
- **Spec §4 (dashboard):** Tasks 8, 9
- **Spec §5 (telegram):** Task 10
- **Spec §6 (persistence & restart):** Task 2 (discover change) + Task 11 (restart test)
- **Spec testing strategy:** Tasks 1, 3, 5, 6, 7, 8, 10, 11 (each has tests)

### Placeholder scan
Every code-changing step contains actual code; no "implement similar to…" or "TODO." The one "smoke test" step in Task 9 step 7 is explicit about what to verify manually.

### Type consistency
- `QuotaExhaustedError` — defined Task 3, referenced Tasks 4, 5, 6.
- `AgentResult.failure_kind` / `retry_at` — defined Task 5, consumed Task 6.
- `_sweep_deferred` — defined Task 7, called from Task 7.
- `_quota_window_end` / `_rollback_iteration` / `_notify_deferred` / `_notify_failed` — defined Task 6, consumed Tasks 6, 7.
- `retry_at` state field — defined Task 1, used throughout.
- `resume` / `archive` endpoints — defined Task 8, wired Task 9.

All identifiers consistent.

---

## Execution

**Plan complete and saved to** `docs/superpowers/plans/2026-04-14-quota-deferral.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

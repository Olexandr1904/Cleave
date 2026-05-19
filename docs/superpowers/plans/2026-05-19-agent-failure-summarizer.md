# Agent Failure Summarizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the generic "agent produced no output" / raw `result.error` block reasons with an LLM-generated two-block "Reason / Suggested action" summary, so operators can act on dashboard/Telegram alerts without tailing logs.

**Architecture:** A new module `orchestrator/agent_failure_summarizer.py` exposes `summarize_agent_failure(workspace, stage_id, *, reason_kind, raw_error)` plus a pure `extract_log_slice()` helper. The function feeds a curated slice of `logs/<stage>-agent.events.log` (errors + system events + tail) to Sonnet 4.6, parses the response, and returns the operator-facing string or `None` on any failure. `escalation.py::build_blocked_reason` and `agent_stage.py`'s FAILED path call the summarizer; on `None` they fall back to today's strings. Module-level `configure()` is called once from `main.py` so call sites don't need to thread a client through.

**Tech Stack:** Python 3.12, `anthropic` SDK (`AsyncAnthropic`), pytest, `pytest-asyncio`.

**Spec:** [docs/superpowers/specs/2026-05-19-agent-failure-summarizer-design.md](../specs/2026-05-19-agent-failure-summarizer-design.md)

---

## File Structure

**New files:**
- `orchestrator/agent_failure_summarizer.py` — summarizer module (configure, extract_log_slice, summarize_agent_failure, prompt template, validation)
- `tests/unit/test_agent_failure_summarizer.py` — extract_log_slice + summarize_agent_failure unit tests

**Modified files:**
- `workspace/workspace.py` — add `raw_error: str | None = None` field to `WorkspaceState`
- `config/schemas.py` — add `FailureSummarizerConfig` dataclass + field on `GlobalConfig`
- `config/config_loader.py` — parse `failure_summarizer:` block from `global.yaml`
- `orchestrator/escalation.py` — make `build_blocked_reason` async; call summarizer when runtime output is missing
- `orchestrator/pipeline/agent_stage.py` — call summarizer on `result.success is False`; persist `raw_error`
- `main.py` — call `configure_summarizer()` at startup
- `tests/unit/test_orchestrator_blocked_reason.py` — convert existing tests to async; add summarizer-path test cases

**Out of scope for this plan** (acknowledged in spec):
- `orchestrator/pipeline/action_stage.py` integration — defer to follow-up.
- Dashboard "summarize again" button — explicitly excluded.

---

### Task 1: Add `raw_error` field to `WorkspaceState`

**Files:**
- Modify: `workspace/workspace.py:57-84`
- Test: `tests/unit/test_workspace_state.py` (likely exists; verify path) or add to existing workspace tests

- [ ] **Step 1: Verify existing workspace-state test location**

Run: `grep -rln "class WorkspaceState\|test.*workspace.*state\|state.json" tests/ | head -5`

Expected: identifies a test file under `tests/` that round-trips `WorkspaceState` to/from JSON. If none, write the new test in `tests/unit/test_workspace_raw_error.py`.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_workspace_raw_error.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from workspace.workspace import Workspace, WorkspaceState


def test_workspace_state_raw_error_round_trip(tmp_path: Path) -> None:
    state = WorkspaceState(
        ticket_id="T-1",
        company_id="acme",
        repo_id="repo",
        workspace_root=str(tmp_path),
        error="operator-facing summary",
        raw_error="CLI exit 137: killed by signal SIGKILL",
    )
    ws = Workspace(str(tmp_path), state=state)
    ws.save_state()

    loaded = json.loads((tmp_path / "state.json").read_text())
    assert loaded["error"] == "operator-facing summary"
    assert loaded["raw_error"] == "CLI exit 137: killed by signal SIGKILL"

    reloaded = Workspace.load(str(tmp_path)).state
    assert reloaded.raw_error == "CLI exit 137: killed by signal SIGKILL"


def test_workspace_state_raw_error_defaults_to_none(tmp_path: Path) -> None:
    state = WorkspaceState(
        ticket_id="T-2",
        company_id="acme",
        repo_id="repo",
        workspace_root=str(tmp_path),
    )
    assert state.raw_error is None
```

If `Workspace.save_state()` or `Workspace.load()` have different names in the codebase, adjust the test to use the actual API. Run `grep -n "def save\|def load\|@classmethod" workspace/workspace.py` first to confirm.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_workspace_raw_error.py -v`

Expected: FAIL with `TypeError: WorkspaceState.__init__() got an unexpected keyword argument 'raw_error'`.

- [ ] **Step 4: Add the field**

In `workspace/workspace.py` after line 76 (`error: str | None = None`), add:

```python
    raw_error: str | None = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_workspace_raw_error.py -v`

Expected: PASS for both cases.

- [ ] **Step 6: Run full workspace test suite to catch regressions**

Run: `pytest tests/ -k workspace -v`

Expected: All previously-passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace_raw_error.py
git commit -m "feat(workspace): add raw_error field to WorkspaceState

Preserves the original CLI error string when agent_stage stores an
operator-facing summary in the error field. Optional, defaults to None."
```

---

### Task 2: Add `FailureSummarizerConfig` to config schema

**Files:**
- Modify: `config/schemas.py:99-129` (add dataclass + field on GlobalConfig)
- Modify: `config/config_loader.py` (parse the block)
- Test: `tests/unit/test_config_loader.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_config_loader.py` (or add a new test file `tests/unit/test_config_failure_summarizer.py` if the loader tests are too monolithic to extend):

```python
def test_failure_summarizer_defaults(tmp_path):
    from config.config_loader import load_global_config
    (tmp_path / "global.yaml").write_text(
        "claude:\n  api_key: test\n"
    )
    cfg = load_global_config(str(tmp_path))
    assert cfg.failure_summarizer.enabled is True
    assert cfg.failure_summarizer.model == "claude-sonnet-4-6"
    assert cfg.failure_summarizer.timeout_seconds == 30


def test_failure_summarizer_overrides(tmp_path):
    from config.config_loader import load_global_config
    (tmp_path / "global.yaml").write_text(
        "claude:\n  api_key: test\n"
        "failure_summarizer:\n"
        "  enabled: false\n"
        "  model: claude-haiku-4-5-20251001\n"
        "  timeout_seconds: 10\n"
    )
    cfg = load_global_config(str(tmp_path))
    assert cfg.failure_summarizer.enabled is False
    assert cfg.failure_summarizer.model == "claude-haiku-4-5-20251001"
    assert cfg.failure_summarizer.timeout_seconds == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_config_loader.py::test_failure_summarizer_defaults -v`

Expected: FAIL with `AttributeError: 'GlobalConfig' object has no attribute 'failure_summarizer'`.

- [ ] **Step 3: Add the dataclass**

In `config/schemas.py` after `DashboardConfig` (around line 115), add:

```python
@dataclass
class FailureSummarizerConfig:
    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    timeout_seconds: int = 30
```

In the `GlobalConfig` dataclass (line 119), add a field after `dashboard`:

```python
    failure_summarizer: FailureSummarizerConfig = field(
        default_factory=FailureSummarizerConfig
    )
```

- [ ] **Step 4: Parse the block in `config_loader.py`**

Open `config/config_loader.py`. Inside `load_global_config`, locate where other top-level sections are parsed (e.g., `claude`, `dashboard`). Add a parallel block. Run `grep -n "dashboard\b\|DashboardConfig" config/config_loader.py` to find the pattern; replicate it for `failure_summarizer`. Approximate shape:

```python
fs_data = data.get("failure_summarizer", {}) or {}
failure_summarizer = FailureSummarizerConfig(
    enabled=bool(fs_data.get("enabled", True)),
    model=str(fs_data.get("model", "claude-sonnet-4-6")),
    timeout_seconds=int(fs_data.get("timeout_seconds", 30)),
)
```

Then pass `failure_summarizer=failure_summarizer` into the `GlobalConfig(...)` construction.

Add the import: `from config.schemas import ..., FailureSummarizerConfig`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_config_loader.py -k failure_summarizer -v`

Expected: PASS for both `test_failure_summarizer_defaults` and `test_failure_summarizer_overrides`.

- [ ] **Step 6: Run full config test suite for regressions**

Run: `pytest tests/unit/test_config_loader.py tests/unit/test_config_cascade.py -v`

Expected: All previously-passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add config/schemas.py config/config_loader.py tests/unit/test_config_loader.py
git commit -m "feat(config): add failure_summarizer block to GlobalConfig

Three knobs: enabled (default true), model (default claude-sonnet-4-6),
timeout_seconds (default 30). Disabled mode short-circuits the
summarizer at call time."
```

---

### Task 3: Build `extract_log_slice` pure helper

**Files:**
- Create: `orchestrator/agent_failure_summarizer.py`
- Test: `tests/unit/test_agent_failure_summarizer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_agent_failure_summarizer.py`:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_failure_summarizer import extract_log_slice


def _make_ws(tmp_path: Path, stage_log_lines: list[str] | None = None,
             pipeline_log: str = "") -> SimpleNamespace:
    """Build a workspace-shaped namespace with logs_dir, reports_dir, state."""
    logs = tmp_path / "logs"
    logs.mkdir()
    reports = tmp_path / "source" / "ai_pipeline" / "T-1"
    reports.mkdir(parents=True)
    if stage_log_lines is not None:
        (logs / "qa-agent.events.log").write_text("\n".join(stage_log_lines) + "\n")
    if pipeline_log:
        (reports / "pipeline-log.md").write_text(pipeline_log)
    state = SimpleNamespace(
        ticket_id="T-1",
        title="Crash on submit",
        current_state="BLOCKED",
        previous_state="QA",
        stage_iterations={"qa": 2},
    )
    return SimpleNamespace(logs_dir=logs, reports_dir=reports, state=state)


def test_slice_contains_all_section_headers(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=["[t] event=heartbeat"],
                  pipeline_log="- step 1\n")
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    for header in ("=== TICKET ===", "=== STAGE HISTORY ===",
                   "=== OUTPUT FILES ===", "=== CRASH SIGNALS ===",
                   "=== LOG TAIL ==="):
        assert header in slice_, f"missing {header} in slice"


def test_slice_ticket_section_includes_metadata(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=["[t] x"])
    slice_ = extract_log_slice(ws, "qa", raw_error="exit code 137")
    assert "T-1" in slice_
    assert "Crash on submit" in slice_
    assert "qa" in slice_
    assert "qa: 2" in slice_ or "iteration" in slice_.lower()
    assert "exit code 137" in slice_


def test_crash_signals_extracts_errors_and_system_events(tmp_path: Path) -> None:
    log = [
        "[10:00] heartbeat elapsed=30s",
        "[10:01] event=tool_result is_error=False len=10",
        "[10:02] event=tool_result is_error=True len=200 preview='OutOfMemoryError'",
        "[10:03] event=system subtype=task_started",
        "[10:04] heartbeat elapsed=60s",
        "[10:05] Caused by: java.io.EOFException",
    ]
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    crash = slice_.split("=== CRASH SIGNALS ===")[1].split("=== LOG TAIL ===")[0]
    assert "is_error=True" in crash
    assert "OutOfMemoryError" in crash
    assert "task_started" in crash
    assert "EOFException" in crash
    # Heartbeats are noise — must not be in crash signals.
    assert "heartbeat elapsed" not in crash


def test_output_files_section_lists_presence(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=["[t] x"])
    (ws.reports_dir / "ba-agent-output.md").write_text("ok")
    (ws.reports_dir / "dev-agent-output.md").write_text("ok")
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    files = slice_.split("=== OUTPUT FILES ===")[1].split("=== CRASH SIGNALS ===")[0]
    assert "ba-agent-output.md: present" in files
    assert "dev-agent-output.md: present" in files
    assert "qa-agent-output.md: missing" in files


def test_slice_respects_6kb_cap(tmp_path: Path) -> None:
    log = [f"[t] line {i} " + "x" * 200 for i in range(200)]
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    assert len(slice_.encode("utf-8")) <= 6 * 1024


def test_crash_signals_preserved_when_truncating(tmp_path: Path) -> None:
    log = (
        [f"[t] event=tool_result is_error=True line {i}" for i in range(5)]
        + [f"[t] heartbeat padding line {i} " + "x" * 200 for i in range(200)]
    )
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    crash = slice_.split("=== CRASH SIGNALS ===")[1].split("=== LOG TAIL ===")[0]
    # All 5 error lines must survive even when LOG TAIL is head-truncated.
    for i in range(5):
        assert f"is_error=True line {i}" in crash


def test_missing_events_log_returns_skeleton(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=None)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    # No log file — sections still present, CRASH SIGNALS/LOG TAIL are empty.
    assert "=== TICKET ===" in slice_
    tail = slice_.split("=== LOG TAIL ===")[1].strip()
    assert tail in ("", "(no log file)") or len(tail) < 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_agent_failure_summarizer.py -v`

Expected: FAIL with `ImportError: cannot import name 'extract_log_slice' from 'orchestrator.agent_failure_summarizer'`.

- [ ] **Step 3: Write the minimal module skeleton + extract_log_slice**

Create `orchestrator/agent_failure_summarizer.py`:

```python
"""Summarize crashed-agent failures into operator-facing 'Reason / Suggested
action' strings using a short LLM call.

Used by:
- orchestrator.escalation.build_blocked_reason — when a stage runtime output
  is missing (agent crashed before writing its output file).
- orchestrator.pipeline.agent_stage — when an agent run exits with
  result.success is False.

Returns None on any failure mode; callers fall back to their existing
generic message. Never raises.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


SLICE_MAX_BYTES = 6 * 1024
LOG_TAIL_LINES = 50
CRASH_SIGNAL_MAX_ERROR_LINES = 20
CRASH_SIGNAL_MAX_SYSTEM_LINES = 10
CRASH_SIGNAL_MAX_KEYWORD_LINES = 10
KNOWN_OUTPUT_FILES = (
    "ba-agent-output.md",
    "scope-guard-agent-output.md",
    "dev-agent-output.md",
    "qa-agent-output.md",
)

_KEYWORD_RE = re.compile(
    r"Exception|OutOfMemory|BUILD FAILED|killed|timeout", re.IGNORECASE
)
_HEARTBEAT_RE = re.compile(r"\bheartbeat\b", re.IGNORECASE)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []


def _ticket_section(workspace: Any, stage_id: str, raw_error: str | None) -> str:
    state = workspace.state
    iterations = getattr(state, "stage_iterations", {}) or {}
    lines = [
        f"ticket_id: {getattr(state, 'ticket_id', '?')}",
        f"title: {getattr(state, 'title', None) or '(no title)'}",
        f"failing_stage: {stage_id}",
        f"previous_state: {getattr(state, 'previous_state', None) or '?'}",
        f"iterations[{stage_id}]: {iterations.get(stage_id, 0)}",
    ]
    if raw_error:
        lines.append(f"raw_error: {raw_error[:400]}")
    return "\n".join(lines)


def _stage_history_section(workspace: Any) -> str:
    log = workspace.reports_dir / "pipeline-log.md"
    if not log.exists():
        return "(no pipeline-log.md)"
    lines = _read_lines(log)
    return "\n".join(lines[-10:])


def _output_files_section(workspace: Any) -> str:
    reports = workspace.reports_dir
    out: list[str] = []
    for name in KNOWN_OUTPUT_FILES:
        present = (reports / name).exists()
        out.append(f"{name}: {'present' if present else 'missing'}")
    return "\n".join(out)


def _crash_signals_section(log_lines: list[str]) -> str:
    if not log_lines:
        return "(no log file)"
    errors: list[str] = []
    systems: list[str] = []
    keywords: list[str] = []
    for line in log_lines:
        if "is_error=True" in line:
            errors.append(line)
        elif "event=system" in line and not _HEARTBEAT_RE.search(line):
            systems.append(line)
        elif _KEYWORD_RE.search(line) and not _HEARTBEAT_RE.search(line):
            keywords.append(line)
    selected = (
        errors[-CRASH_SIGNAL_MAX_ERROR_LINES:]
        + systems[-CRASH_SIGNAL_MAX_SYSTEM_LINES:]
        + keywords[-CRASH_SIGNAL_MAX_KEYWORD_LINES:]
    )
    if not selected:
        return "(no crash signals found)"
    return "\n".join(selected)


def _log_tail_section(log_lines: list[str]) -> str:
    if not log_lines:
        return "(no log file)"
    return "\n".join(log_lines[-LOG_TAIL_LINES:])


def extract_log_slice(
    workspace: Any, stage_id: str, raw_error: str | None
) -> str:
    """Build the structured input slice the summarizer LLM sees.

    Sections are labeled with === HEADER === markers. The crash signals
    section is never truncated; the log tail is head-truncated if the
    total slice exceeds SLICE_MAX_BYTES.
    """
    log_path = workspace.logs_dir / f"{stage_id}-agent.events.log"
    log_lines = _read_lines(log_path) if log_path.exists() else []

    sections = {
        "TICKET": _ticket_section(workspace, stage_id, raw_error),
        "STAGE HISTORY": _stage_history_section(workspace),
        "OUTPUT FILES": _output_files_section(workspace),
        "CRASH SIGNALS": _crash_signals_section(log_lines),
        "LOG TAIL": _log_tail_section(log_lines),
    }

    def render(tail_lines: list[str]) -> str:
        sections_copy = dict(sections)
        sections_copy["LOG TAIL"] = "\n".join(tail_lines) if tail_lines else "(no log file)"
        return "\n\n".join(
            f"=== {name} ===\n{body}" for name, body in sections_copy.items()
        )

    rendered = render(log_lines[-LOG_TAIL_LINES:] if log_lines else [])
    if len(rendered.encode("utf-8")) <= SLICE_MAX_BYTES:
        return rendered

    # Head-truncate the LOG TAIL until under cap. CRASH SIGNALS untouched.
    tail = log_lines[-LOG_TAIL_LINES:] if log_lines else []
    while tail:
        tail = tail[1:]
        rendered = render(tail)
        if len(rendered.encode("utf-8")) <= SLICE_MAX_BYTES:
            return rendered
    return render([])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_agent_failure_summarizer.py -v`

Expected: All 7 `extract_log_slice` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/agent_failure_summarizer.py tests/unit/test_agent_failure_summarizer.py
git commit -m "feat(orchestrator): add extract_log_slice for agent failure summarizer

Pure helper that builds the structured input slice for the failure
summarizer LLM. Five labeled sections: TICKET, STAGE HISTORY, OUTPUT
FILES, CRASH SIGNALS, LOG TAIL. Crash signals never truncated; log
tail head-truncated if slice exceeds 6 KB."
```

---

### Task 4: Add `configure` and `summarize_agent_failure`

**Files:**
- Modify: `orchestrator/agent_failure_summarizer.py`
- Modify: `tests/unit/test_agent_failure_summarizer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_agent_failure_summarizer.py`:

```python
import pytest

from orchestrator.agent_failure_summarizer import (
    configure_summarizer,
    summarize_agent_failure,
)


class _FakeMessageBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeMessageBlock(text)]


class _FakeClient:
    """Minimal stand-in for anthropic.AsyncAnthropic."""

    def __init__(self, response_text: str | Exception = "") -> None:
        self._response_text = response_text
        self.calls: list[dict] = []

        class _Messages:
            def __init__(self, parent: "_FakeClient") -> None:
                self._parent = parent

            async def create(self, **kwargs):
                self._parent.calls.append(kwargs)
                if isinstance(self._parent._response_text, Exception):
                    raise self._parent._response_text
                return _FakeResponse(self._parent._response_text)

        self.messages = _Messages(self)


def _ws(tmp_path: Path) -> SimpleNamespace:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "qa-agent.events.log").write_text(
        "[10:00] event=tool_result is_error=True preview='OOM'\n"
    )
    reports = tmp_path / "source" / "ai_pipeline" / "T-1"
    reports.mkdir(parents=True)
    state = SimpleNamespace(
        ticket_id="T-1", title="x", previous_state="QA",
        stage_iterations={"qa": 2}, current_state="BLOCKED",
    )
    return SimpleNamespace(logs_dir=logs, reports_dir=reports, state=state)


@pytest.fixture(autouse=True)
def _reset_summarizer_state():
    # Reset to disabled between tests so state doesn't leak.
    configure_summarizer(client=None, model="", timeout_seconds=30, enabled=False)
    yield
    configure_summarizer(client=None, model="", timeout_seconds=30, enabled=False)


@pytest.mark.asyncio
async def test_summarize_returns_none_when_disabled(tmp_path: Path) -> None:
    client = _FakeClient(response_text="Reason: x\nSuggested action: y")
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=False)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_summarize_returns_string_on_success(tmp_path: Path) -> None:
    body = (
        "Reason: Kotlin daemon OOM during ./gradlew testDebugUnitTest.\n"
        "Suggested action: Raise -Xmx; retry the stage once resolved."
    )
    client = _FakeClient(response_text=body)
    configure_summarizer(client=client, model="claude-sonnet-4-6",
                         timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out == body
    # Slice was passed to the LLM.
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["model"] == "claude-sonnet-4-6"
    assert sent["max_tokens"] == 200
    assert sent["temperature"] == 0
    user_content = sent["messages"][0]["content"]
    assert "=== TICKET ===" in user_content
    assert "=== CRASH SIGNALS ===" in user_content


@pytest.mark.asyncio
async def test_summarize_returns_none_on_timeout(tmp_path: Path, caplog) -> None:
    import asyncio
    class _SlowClient:
        class _Messages:
            async def create(self, **kw):
                await asyncio.sleep(10)
        messages = _Messages()
    configure_summarizer(client=_SlowClient(), model="m",
                         timeout_seconds=0.05, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("timeout" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_summarize_returns_none_on_api_error(tmp_path: Path, caplog) -> None:
    client = _FakeClient(response_text=RuntimeError("rate limit"))
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("api_error" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_summarize_returns_none_on_malformed_output(tmp_path: Path, caplog) -> None:
    client = _FakeClient(response_text="here's what I think happened: stuff broke")
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("malformed" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_summarize_returns_none_when_too_long(tmp_path: Path, caplog) -> None:
    long_body = (
        "Reason: " + ("x" * 400) + "\nSuggested action: " + ("y" * 200)
    )
    client = _FakeClient(response_text=long_body)
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("too_long" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_summarize_passes_raw_error_for_agent_failed(tmp_path: Path) -> None:
    body = "Reason: a.\nSuggested action: b."
    client = _FakeClient(response_text=body)
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(
        _ws(tmp_path), "qa", reason_kind="agent_failed", raw_error="exit 137"
    )
    assert out == body
    sent_content = client.calls[0]["messages"][0]["content"]
    assert "raw_error: exit 137" in sent_content
```

The test file also needs `pytest-asyncio` mode. Add at the top of the file (only if not already present):

```python
pytestmark = pytest.mark.asyncio
```

or use `pyproject.toml` config — check first with `grep asyncio pyproject.toml`. If neither exists, add `asyncio_mode = "auto"` under `[tool.pytest.ini_options]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_agent_failure_summarizer.py -v`

Expected: FAIL on the new tests with `ImportError: cannot import name 'configure_summarizer'` (or similar) and `cannot import name 'summarize_agent_failure'`.

- [ ] **Step 3: Add `configure_summarizer`, `summarize_agent_failure`, prompt, and validator**

Append to `orchestrator/agent_failure_summarizer.py`:

```python
import asyncio
from typing import Literal


SUMMARIZER_PROMPT = """You are diagnosing a single pipeline-stage failure in an automated code-fix system. Your job is to tell the human operator WHAT broke and WHAT TO DO — in under 300 characters total.

Input below is divided into labeled sections:
- TICKET / STAGE HISTORY / OUTPUT FILES are CONTEXT ONLY.
- CRASH SIGNALS and LOG TAIL contain the actual failure evidence.
Do NOT infer failure causes from the ticket description or from prior stage outputs. Only the failing stage's log explains the failure.

Output EXACTLY this format, no preamble, no markdown headers, no code fences:

Reason: <one or two sentences naming the concrete failure mode observed in the log. If the log shows no clear failure, say 'No clear failure signal in stage log.' Do not guess.>

Suggested action: <one or two short bullet-style steps the operator can take. Prefer concrete config/command changes over vague advice. End with 'Retry the stage once resolved.' when retryable.>

Hard limits: max 300 chars total, no markdown, no emoji, plain text.
"""


MAX_OUTPUT_CHARS = 500  # accept up to 500 to leave headroom over the 300-char prompt ceiling

# Module-level singleton state, configured once at startup.
_client: Any = None
_model: str = "claude-sonnet-4-6"
_timeout_seconds: float = 30.0
_enabled: bool = False


def configure_summarizer(
    *,
    client: Any,
    model: str,
    timeout_seconds: float,
    enabled: bool,
) -> None:
    """Wire up the summarizer at startup. Safe to call multiple times.

    Tests should set enabled=False (or pass a fake client) to keep
    summarization deterministic.
    """
    global _client, _model, _timeout_seconds, _enabled
    _client = client
    _model = model
    _timeout_seconds = float(timeout_seconds)
    _enabled = bool(enabled)


def _validate_response(text: str) -> str | None:
    text = (text or "").strip()
    if "Reason:" not in text or "Suggested action:" not in text:
        return None
    if len(text) > MAX_OUTPUT_CHARS:
        return None
    return text


async def summarize_agent_failure(
    workspace: Any,
    stage_id: str,
    *,
    reason_kind: Literal["missing_output", "agent_failed"],
    raw_error: str | None = None,
) -> str | None:
    """Build a 'Reason / Suggested action' string from the failing stage's
    log via Sonnet 4.6. Returns None on any failure (caller falls back to
    its existing generic message). Never raises.
    """
    if not _enabled or _client is None:
        return None

    ticket_id = getattr(workspace.state, "ticket_id", "?")
    tag = f"ticket={ticket_id} stage={stage_id} kind={reason_kind}"

    try:
        slice_ = extract_log_slice(workspace, stage_id, raw_error)
    except Exception as exc:  # extract is best-effort; never raise into pipeline
        logger.warning("agent_failure_summarizer: slice_error %s: %s", tag, exc)
        return None

    user_message = f"{SUMMARIZER_PROMPT}\n=== INPUT ===\n{slice_}"

    try:
        response = await asyncio.wait_for(
            _client.messages.create(
                model=_model,
                max_tokens=200,
                temperature=0,
                messages=[{"role": "user", "content": user_message}],
            ),
            timeout=_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("agent_failure_summarizer: timeout %s", tag)
        return None
    except Exception as exc:
        logger.warning(
            "agent_failure_summarizer: api_error %s: %s",
            tag, type(exc).__name__,
        )
        return None

    text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")
    if len(text) > MAX_OUTPUT_CHARS + 100:
        logger.warning("agent_failure_summarizer: too_long %s len=%d", tag, len(text))
        return None
    validated = _validate_response(text)
    if validated is None:
        if text and len(text.strip()) > MAX_OUTPUT_CHARS:
            logger.warning("agent_failure_summarizer: too_long %s len=%d",
                           tag, len(text.strip()))
        else:
            logger.warning("agent_failure_summarizer: malformed %s preview=%r",
                           tag, text[:120])
        return None
    return validated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_agent_failure_summarizer.py -v`

Expected: All tests PASS (the 7 from Task 3 plus the 7 added here).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/agent_failure_summarizer.py tests/unit/test_agent_failure_summarizer.py
git commit -m "feat(orchestrator): add summarize_agent_failure with module-level config

Async function that feeds the smart log slice to Sonnet 4.6, validates
the two-block Reason/Suggested-action shape, and returns None on any
failure mode (disabled, timeout, API error, malformed, too long).
Module-level configure_summarizer wires the SDK client at startup."
```

---

### Task 5: Convert `build_blocked_reason` to async + call summarizer

**Files:**
- Modify: `orchestrator/escalation.py:40-95`
- Modify: `orchestrator/escalation.py:136` (call site inside `handle_escalate`)
- Modify: `tests/unit/test_orchestrator_blocked_reason.py` (existing tests + new ones)

- [ ] **Step 1: Convert existing tests to async**

In `tests/unit/test_orchestrator_blocked_reason.py`, add `import pytest` (if missing) and decorate every test:

```python
@pytest.mark.asyncio
async def test_analysis_prefers_ba_questions(tmp_path):
    ...
    reason = await build_blocked_reason(ws, "analysis")
    ...
```

Apply the same pattern to all 8 tests in the file: add `@pytest.mark.asyncio`, change `def` to `async def`, and prefix each `build_blocked_reason(...)` call with `await`.

- [ ] **Step 2: Add the new summarizer-path tests**

Append to the same test file:

```python
from unittest.mock import patch


@pytest.mark.asyncio
async def test_missing_output_uses_summarizer_when_returns_string(tmp_path):
    ws = _make_ws(tmp_path)  # no runtime output present
    summary = "Reason: Kotlin daemon OOM.\nSuggested action: raise -Xmx; retry."
    with patch(
        "orchestrator.escalation.summarize_agent_failure",
        return_value=summary,
    ) as mock_summarize:
        reason = await build_blocked_reason(ws, "qa")
    assert reason == summary
    mock_summarize.assert_awaited_once()
    args, kwargs = mock_summarize.call_args
    assert kwargs["reason_kind"] == "missing_output"


@pytest.mark.asyncio
async def test_missing_output_falls_back_when_summarizer_returns_none(tmp_path):
    ws = _make_ws(tmp_path)
    with patch(
        "orchestrator.escalation.summarize_agent_failure",
        return_value=None,
    ):
        reason = await build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()
    assert "no output" in reason.lower() or "may have timed out" in reason.lower()
```

`patch(...)` for an async function needs `AsyncMock`; use:

```python
from unittest.mock import AsyncMock, patch
```

and pass `new_callable=AsyncMock, return_value=summary` to `patch()`:

```python
with patch(
    "orchestrator.escalation.summarize_agent_failure",
    new_callable=AsyncMock, return_value=summary,
) as mock_summarize:
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_orchestrator_blocked_reason.py -v`

Expected: All 10 tests FAIL — the existing 8 because `build_blocked_reason` isn't async yet, the new 2 because the summarizer integration doesn't exist.

- [ ] **Step 4: Modify `build_blocked_reason`**

In `orchestrator/escalation.py`:

Add the import at the top:

```python
from orchestrator.agent_failure_summarizer import summarize_agent_failure
```

Change the signature on line 40 from `def build_blocked_reason(...) -> str:` to `async def build_blocked_reason(...) -> str:`.

Replace the missing-output branch (currently lines 65–71):

```python
    if runtime_filename:
        candidate = reports / runtime_filename
        if candidate.exists():
            stage_output = candidate
        else:
            summary = await summarize_agent_failure(
                workspace, stage_id, reason_kind="missing_output"
            )
            if summary:
                return summary
            return (
                f"{stage_id} agent produced no output "
                f"(may have timed out or crashed). Check pipeline logs."
            )
```

Update the call site on line 136 (inside `handle_escalate`, which is already `async`):

```python
    reason = tg_format.strip_markdown(
        await build_blocked_reason(workspace, stage_id_str)
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_blocked_reason.py -v`

Expected: All 10 tests PASS.

- [ ] **Step 6: Run related tests for regressions**

Run: `pytest tests/unit/test_orchestrator_notify_misc.py tests/unit/test_escalation_view.py -v`

Expected: All pass. If `test_orchestrator_notify_misc.py` calls `build_blocked_reason` directly, decorate as `@pytest.mark.asyncio` and `await` the call. Inspect first:

`grep -n "build_blocked_reason" tests/unit/test_orchestrator_notify_misc.py`

If callers exist outside escalation.py, update them too.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/escalation.py tests/unit/test_orchestrator_blocked_reason.py tests/unit/test_orchestrator_notify_misc.py
git commit -m "feat(escalation): summarize crashed-agent block reasons via LLM

build_blocked_reason is now async. When the runtime output file is
missing, it asks the failure summarizer for a 'Reason / Suggested
action' string; on None it falls back to the previous generic message."
```

---

### Task 6: Wire summarizer into `agent_stage.py` FAILED path

**Files:**
- Modify: `orchestrator/pipeline/agent_stage.py:218-220`
- Test: `tests/unit/test_agent_stage.py`

- [ ] **Step 1: Locate the existing agent_stage failure test**

Run: `grep -n "FAILED\|Stage.FAILED\|notify_failed_fn\|result\.success" tests/unit/test_agent_stage.py | head -20`

Expected: identifies one or more existing tests that drive the `result.success is False` branch with a fake `LLMResponse` or fake adapter. If none exist, skim the file (`pytest tests/unit/test_agent_stage.py -v --collect-only`) to find similar tests to model after.

- [ ] **Step 2: Write the failing test**

Add to `tests/unit/test_agent_stage.py`:

```python
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_agent_failed_uses_summary_when_available(tmp_path, ...):
    """When summarizer returns a string, that string is stored in state.error
    and the raw CLI error is preserved in state.raw_error."""
    # ... use existing fixtures in this file to build a workspace + workflow +
    # mock adapter that returns AgentResult(success=False, error="raw cli err")
    # Construct fakes matching the existing test patterns in this file.

    summary = "Reason: Kotlin daemon OOM.\nSuggested action: retry."
    notify_failed_fn = AsyncMock()
    with patch(
        "orchestrator.pipeline.agent_stage.summarize_agent_failure",
        new_callable=AsyncMock, return_value=summary,
    ):
        await run_agent_stage(
            stage_id="qa",
            workspace=ws,
            ...,
            notify_failed_fn=notify_failed_fn,
        )

    assert ws.state.current_state == "FAILED"
    assert ws.state.error == summary
    assert ws.state.raw_error == "raw cli err"
    notify_failed_fn.assert_awaited_once_with(ws, summary)


@pytest.mark.asyncio
async def test_agent_failed_falls_back_when_summarizer_returns_none(tmp_path, ...):
    notify_failed_fn = AsyncMock()
    with patch(
        "orchestrator.pipeline.agent_stage.summarize_agent_failure",
        new_callable=AsyncMock, return_value=None,
    ):
        await run_agent_stage(
            stage_id="qa", workspace=ws, ...,
            notify_failed_fn=notify_failed_fn,
        )
    assert ws.state.error == "raw cli err"
    assert ws.state.raw_error == "raw cli err"
    notify_failed_fn.assert_awaited_once_with(ws, "raw cli err")
```

The `...` placeholders represent existing fixtures and parameters in this test file. Read existing tests in `tests/unit/test_agent_stage.py` first and fill them in following the same patterns; do not invent new fixtures. If the existing tests in this file don't use `run_agent_stage` directly (they may use a different entry point), use whatever pattern is already established.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_agent_stage.py -k summarizer -v`

Expected: FAIL because (a) `summarize_agent_failure` is not yet imported in `agent_stage.py` and (b) `state.raw_error` is not written.

- [ ] **Step 4: Modify `agent_stage.py`**

Open `orchestrator/pipeline/agent_stage.py`. Add the import near the top with the other orchestrator imports:

```python
from orchestrator.agent_failure_summarizer import summarize_agent_failure
```

Replace lines 218–220:

```python
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=result.error)
            await notify_failed_fn(workspace, result.error or "")
```

With:

```python
            summary = await summarize_agent_failure(
                workspace, stage_id,
                reason_kind="agent_failed",
                raw_error=result.error,
            )
            final_error = summary or result.error or ""
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=final_error, raw_error=result.error)
            await notify_failed_fn(workspace, final_error)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_agent_stage.py -v`

Expected: New tests PASS; previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/pipeline/agent_stage.py tests/unit/test_agent_stage.py
git commit -m "feat(agent_stage): summarize agent_failed errors for operator

On result.success is False, ask the failure summarizer for a
'Reason / Suggested action' string. Store the summary in state.error
(falling back to the raw CLI error on None) and preserve the original
CLI error in state.raw_error for forensics."
```

---

### Task 7: Initialize summarizer at startup in `main.py`

**Files:**
- Modify: `main.py:266-275` (the ClaudeAdapter / ClaudeCodeAdapter selection block)
- Test: manual smoke test (no unit test — pure wiring)

- [ ] **Step 1: Inspect the surrounding wiring**

Read `main.py:200-310` to see the order of construction: config load, LLM adapter, workspace manager, agent runtime. The summarizer must be configured **after** `global_config` is loaded and the API key is known, and **before** the orchestrator starts processing tickets (so the first crash that fires escalation finds the summarizer wired up).

- [ ] **Step 2: Add the configure call**

Just after the `if global_config.claude.api_key: ... else: ...` block (around line 275), add:

```python
    # Wire the agent failure summarizer. It uses a dedicated Sonnet 4.6
    # client (configured separately from the pipeline's LLM adapter) so
    # crash summaries are cheap and don't share the agents' model choice.
    from orchestrator.agent_failure_summarizer import configure_summarizer
    import anthropic
    _fs_cfg = global_config.failure_summarizer
    if _fs_cfg.enabled and global_config.claude.api_key:
        configure_summarizer(
            client=anthropic.AsyncAnthropic(api_key=global_config.claude.api_key),
            model=_fs_cfg.model,
            timeout_seconds=_fs_cfg.timeout_seconds,
            enabled=True,
        )
        print(f"  Failure summarizer: enabled (model={_fs_cfg.model})")
    else:
        configure_summarizer(client=None, model="", timeout_seconds=30, enabled=False)
        print("  Failure summarizer: disabled")
```

- [ ] **Step 3: Smoke-test the daemon startup**

Run: `python main.py --help 2>&1 | head -5` to verify the import paths still resolve. Then start the daemon for a short window:

Run: `timeout 5 python main.py 2>&1 | grep -i "summarizer\|error" | head -10`

Expected: a line `Failure summarizer: enabled (model=claude-sonnet-4-6)` (or `disabled` if no api_key). No `ImportError`, no traceback.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(main): initialize agent failure summarizer at startup

Constructs an AsyncAnthropic client from global_config.claude.api_key
and wires it into the summarizer module. Disabled at runtime if the
config flag is off or no API key is configured."
```

---

### Task 8: End-to-end manual verification on the live blocked tickets

This task verifies the change against a real crash scenario without needing fresh ticket activity.

- [ ] **Step 1: Inspect one of the existing blocked tickets**

Run: `python3 -c "import json; d=json.load(open('/data/sickle/acme/acme-mobile/tickets/ACME-14812/state.json')); print('current:', d['current_state']); print('q:', d.get('human_input_question'))"`

Expected (before the fix is exercised): current state is `BLOCKED`, `human_input_question` contains the legacy "qa agent produced no output…" string.

- [ ] **Step 2: Reset the ticket to QA so the daemon re-triggers escalation**

Edit `/data/sickle/acme/acme-mobile/tickets/ACME-14812/state.json` — change `"current_state": "BLOCKED"` to `"current_state": "QA"`, clear `"human_input_question": null` and `"human_input_pending": false`. **Make a backup first:**

```bash
cp /data/sickle/acme/acme-mobile/tickets/ACME-14812/state.json \
   /tmp/ACME-14812.state.json.bak
```

- [ ] **Step 3: Restart the daemon and let it re-evaluate**

Stop the running daemon (`pkill -f "python main.py"` if appropriate; ask the operator first since this is a shared process). Start fresh:

`python main.py 2>&1 | tee /tmp/summarizer-smoke.log` (run for at most 90 seconds; the daemon discovers workspaces and re-runs escalation on the QA state).

- [ ] **Step 4: Inspect the resulting state.json**

After the daemon transitions ACME-14812 back to BLOCKED, re-read its `state.json`:

`python3 -c "import json; print(json.load(open('/data/sickle/acme/acme-mobile/tickets/ACME-14812/state.json'))['human_input_question'])"`

Expected: a two-block "Reason: ... / Suggested action: ..." string referencing Kotlin / Gradle / OOM — not the legacy generic message. If summarizer is disabled or fails, the legacy message is shown and `/tmp/summarizer-smoke.log` contains a `WARNING` line tagged `agent_failure_summarizer`.

- [ ] **Step 5: Restore the original state if anything went wrong**

If the daemon misbehaved or the summary is wrong: `cp /tmp/ACME-14812.state.json.bak /data/sickle/acme/acme-mobile/tickets/ACME-14812/state.json` and report findings before continuing.

- [ ] **Step 6: Commit any notes**

If you took notes during smoke-test, add them to a new file under `docs/superpowers/notes/` or to the spec doc's history. Otherwise skip this step.

---

## Self-Review Checklist (Plan Author)

This plan addresses each section of the spec:

- **Public API** (`summarize_agent_failure`) → Task 4
- **Smart-slice extractor** (`extract_log_slice` with 5 labeled sections + 6 KB cap + crash-signal preservation) → Task 3
- **Prompt template** (`SUMMARIZER_PROMPT` with explicit "context vs evidence" framing) → Task 4
- **Integration: BLOCKED missing-output** → Task 5
- **Integration: FAILED transition** → Task 6
- **`raw_error` state field** → Task 1
- **Configuration block + defaults** → Task 2
- **Startup wiring** → Task 7
- **Error handling matrix** (disabled, no log, timeout, API error, malformed, too long) → Task 4
- **Tests for `extract_log_slice`** → Task 3
- **Tests for `summarize_agent_failure`** (mocked LLM, all failure modes) → Task 4
- **Integration test for `build_blocked_reason`** → Task 5
- **Smoke verification** → Task 8

Out-of-scope items called out in the spec (`action_stage.py` integration, dashboard summarize-again button, async/streaming, AWAITING_APPROVAL summarization) are deliberately not in this plan.

**Risks deferred to execution time:**
- The `pyproject.toml` may already configure `asyncio_mode = "auto"`. Task 4 says to check; if not present, add it.
- `tests/unit/test_agent_stage.py` may use very different fixtures than the simple namespace pattern used in `test_orchestrator_blocked_reason.py`. Task 6 explicitly instructs the executor to read existing tests in that file first and follow established patterns.
- If `main.py` orders `global_config` loading after the orchestrator construction, Task 7's location may need to shift; the task instruction says to "inspect the surrounding wiring" before making the edit.

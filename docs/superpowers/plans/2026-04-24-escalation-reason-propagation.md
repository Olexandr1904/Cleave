# Escalation Reason Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the actual reason for BLOCKED transitions in Telegram escalation messages, TG `/status` drill-down, and for verification-failure transitions that currently go silent.

**Architecture:** One shared helper `Orchestrator._build_blocked_reason(workspace, stage_id)` extracts a readable reason (prefer `reports/ba-questions.md` for analysis, else strip boilerplate from the latest `*-output.md`). Three call sites consume the helper: `_handle_escalate` (drops Proceed/Retry buttons, stores the reason in `human_input_question`), a new `_notify_verification_blocked` method invoked from the verification-fail path in `_handle_agent_stage`, and `StatusHandler.format_drill_down` (renders `Blocked on: <reason>` when `human_input_question` is set).

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, python-telegram-bot (existing).

**Spec:** [docs/superpowers/specs/2026-04-24-escalation-reason-propagation-design.md](../specs/2026-04-24-escalation-reason-propagation-design.md)

---

## Files Touched

| File | Kind | Responsibility |
|---|---|---|
| `orchestrator/orchestrator.py` | Modify | Add `_build_blocked_reason`, rewrite `_handle_escalate` summary/buttons, add `_notify_verification_blocked`, call it from verification-fail branch |
| `integrations/telegram/handlers/status.py` | Modify | Render `Blocked on: <reason>` in `format_drill_down` when BLOCKED and `human_input_question` is set |
| `tests/unit/test_orchestrator_blocked_reason.py` | Create | Unit tests for `_build_blocked_reason` helper |
| `tests/unit/test_orchestrator_escalate.py` | Create | Unit tests for `_handle_escalate` (no buttons, reason stored correctly) |
| `tests/unit/test_orchestrator_stage_verify.py` | Modify | Add test that verification-fail path triggers TG notification and sets escalation fields |
| `tests/unit/test_handler_status.py` | Modify | Add test for BLOCKED drill-down with `human_input_question` |

---

## Task 1: `_build_blocked_reason` helper

**Files:**
- Create: `tests/unit/test_orchestrator_blocked_reason.py`
- Modify: `orchestrator/orchestrator.py` (new method near existing `_handle_escalate`)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_orchestrator_blocked_reason.py`:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_ws(tmp_path: Path) -> SimpleNamespace:
    reports = tmp_path / "reports"
    reports.mkdir()
    return SimpleNamespace(
        reports_dir=reports,
        state=SimpleNamespace(ticket_id="T-1"),
    )


def _orch() -> Orchestrator:
    return Orchestrator.__new__(Orchestrator)


def test_analysis_prefers_ba_questions(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "ba-questions.md").write_text(
        "## Questions for Human Review\n\n"
        "1. [AC2] What error types must be handled?\n"
        "2. [Scope] Is this a standalone view?\n"
    )
    # Also drop a later output file — should be ignored for analysis.
    (ws.reports_dir / "ba-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate — waiting_for_human\n"
    )

    reason = _orch()._build_blocked_reason(ws, "analysis")

    assert "Questions for Human Review" in reason
    assert "[AC2]" in reason
    assert "Attempt:" not in reason


def test_non_analysis_strips_boilerplate(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "qa-agent-output.md").write_text(
        "---\n"
        "**Attempt: 2026-04-24 14:00 UTC**\n"
        "## Decision: Escalate — waiting_for_human\n"
        "\n"
        "Tests fail because the fixture file is missing.\n"
        "See tests/data/fixture.json — expected but not found.\n"
    )

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert "Tests fail" in reason
    assert "Attempt:" not in reason
    assert "Decision:" not in reason
    assert not reason.startswith("---")


def test_non_analysis_uses_latest_output_by_mtime(tmp_path):
    import os
    import time
    ws = _make_ws(tmp_path)
    old = ws.reports_dir / "scope-guard-agent-output.md"
    old.write_text("Old content.\n")
    # Force older mtime.
    old_mtime = time.time() - 60
    os.utime(old, (old_mtime, old_mtime))

    new = ws.reports_dir / "qa-agent-output.md"
    new.write_text("New content that should win.\n")

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert "New content" in reason
    assert "Old content" not in reason


def test_truncates_long_reason(tmp_path):
    ws = _make_ws(tmp_path)
    body = "x" * 2000
    (ws.reports_dir / "qa-agent-output.md").write_text(body)

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert len(reason) <= 801  # 800 + the ellipsis char
    assert reason.endswith("…")


def test_empty_reports_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    reason = _orch()._build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()
    assert "reports" in reason.lower()


def test_missing_reports_dir_returns_fallback(tmp_path):
    ws = SimpleNamespace(
        reports_dir=tmp_path / "does-not-exist",
        state=SimpleNamespace(ticket_id="T-1"),
    )
    reason = _orch()._build_blocked_reason(ws, "analysis")
    assert "analysis" in reason.lower()


def test_analysis_falls_back_to_output_when_no_questions_file(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "ba-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate\n\nRepo label missing from ticket.\n"
    )
    reason = _orch()._build_blocked_reason(ws, "analysis")
    assert "Repo label missing" in reason


def test_boilerplate_only_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "qa-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 14:00 UTC**\n## Decision: Escalate\n---\n\n\n"
    )
    reason = _orch()._build_blocked_reason(ws, "qa")
    # Everything was boilerplate → fallback.
    assert "qa" in reason.lower()
    assert "reports" in reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_orchestrator_blocked_reason.py -v`
Expected: all tests fail with `AttributeError: 'Orchestrator' object has no attribute '_build_blocked_reason'`.

- [ ] **Step 3: Implement `_build_blocked_reason`**

Add this method to the `Orchestrator` class in `orchestrator/orchestrator.py`, immediately above `_handle_escalate` (around line 1646). Also add `import re` at the top of the file if not already present.

Check imports first:

```bash
grep -n "^import re" orchestrator/orchestrator.py
```

If no match, add `import re` in the stdlib import block near the top of the file (around line 6).

Then add the method:

```python
_BLOCKED_REASON_MAX_CHARS = 800

_BOILERPLATE_LINE_PATTERNS = (
    re.compile(r"^-{3,}$"),
    re.compile(r"^={3,}$"),
    re.compile(r"^\*\*Attempt.*\*\*$"),
    re.compile(r"^## Decision:"),
)


def _build_blocked_reason(self, workspace: Any, stage_id: str) -> str:
    """Extract a human-readable reason for why a workspace is blocked.

    For analysis: prefer reports/ba-questions.md (the BA agent's numbered
    questions). For other stages (or if ba-questions.md is absent): read the
    latest reports/*-output.md by mtime and strip header boilerplate.
    Falls back to a generic message if nothing useful is found.
    """
    reports = workspace.reports_dir
    if not reports.exists():
        return f"Pipeline stuck at {stage_id}. Check reports/ for details."

    if stage_id == "analysis":
        questions = reports / "ba-questions.md"
        if questions.exists():
            text = questions.read_text(encoding="utf-8").strip()
            if text:
                return self._truncate_reason(text)

    outputs = sorted(
        reports.glob("*-output.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not outputs:
        return f"Pipeline stuck at {stage_id}. Check reports/ for details."

    raw = outputs[0].read_text(encoding="utf-8")
    # Strip leading boilerplate and blank lines.
    lines = raw.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if any(p.match(stripped) for p in self._BOILERPLATE_LINE_PATTERNS):
            continue
        start = i
        break
    else:
        return f"Pipeline stuck at {stage_id}. Check reports/ for details."

    body = "\n".join(lines[start:]).strip()
    if not body:
        return f"Pipeline stuck at {stage_id}. Check reports/ for details."
    return self._truncate_reason(body)


@classmethod
def _truncate_reason(cls, text: str) -> str:
    if len(text) <= cls._BLOCKED_REASON_MAX_CHARS:
        return text
    return text[: cls._BLOCKED_REASON_MAX_CHARS] + "…"
```

The `_BOILERPLATE_LINE_PATTERNS` and `_BLOCKED_REASON_MAX_CHARS` live as class attributes, defined once above the method body.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_blocked_reason.py -v`
Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_blocked_reason.py
git commit -m "feat: add _build_blocked_reason helper extracts escalation context"
```

---

## Task 2: Rewrite `_handle_escalate` to use the helper and drop buttons

**Files:**
- Modify: `orchestrator/orchestrator.py:1646-1717`
- Create: `tests/unit/test_orchestrator_escalate.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_orchestrator_escalate.py`:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _make_workspace(tmp_path: Path) -> MagicMock:
    ws = MagicMock()
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="ANALYSIS",
        previous_state="ANALYSIS",
        escalation_msg_id=None,
        escalation_chat_id=None,
        human_input_question=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.save_state = MagicMock()
    return ws


def _make_orch(notifier) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = notifier
    orch._events = None
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._get_ticket_title = MagicMock(return_value="A ticket")
    orch._tg_header = MagicMock(return_value="🔔 [acme/acme-app] T-1\nTitle: A ticket")
    orch._emit = MagicMock()
    return orch


@pytest.mark.asyncio
async def test_escalate_sends_message_without_buttons(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws.reports_dir / "ba-questions.md").write_text(
        "## Questions for Human Review\n\n1. [AC2] What errors?\n"
    )
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=42)

    orch = _make_orch(notifier)
    await orch._handle_escalate(ws)

    notifier.send_message.assert_awaited_once()
    args, kwargs = notifier.send_message.call_args
    # Signature: send_message(chat_id, message, buttons=None, reply_to_message_id=None)
    assert kwargs.get("buttons") is None
    assert "buttons" not in kwargs or kwargs["buttons"] is None
    message = args[1] if len(args) >= 2 else kwargs.get("message", "")
    assert "[AC2]" in message
    assert "Questions for Human Review" in message


@pytest.mark.asyncio
async def test_escalate_stores_reason_only_in_human_input_question(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws.reports_dir / "ba-questions.md").write_text("Only the questions.\n")
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=99)

    orch = _make_orch(notifier)
    await orch._handle_escalate(ws)

    # update_state called with human_input_question=<reason only, no header, no hint>
    found = [c for c in ws.update_state.call_args_list if "human_input_question" in c.kwargs]
    assert found, "update_state must set human_input_question"
    stored = found[-1].kwargs["human_input_question"]
    assert "Only the questions." in stored
    assert "🔔" not in stored  # No header
    assert "↩️" not in stored  # No reply hint


@pytest.mark.asyncio
async def test_escalate_transitions_to_blocked_and_records_msg_id(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws.reports_dir / "ba-questions.md").write_text("Q.\n")
    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=123)

    orch = _make_orch(notifier)
    await orch._handle_escalate(ws)

    ws.transition.assert_called_once_with(Stage.BLOCKED)
    assert ws.state.escalation_msg_id == 123
    assert ws.state.escalation_chat_id == "chat-1"


@pytest.mark.asyncio
async def test_escalate_no_notifier_transitions_to_failed(tmp_path):
    ws = _make_workspace(tmp_path)
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = None
    orch._events = None

    await orch._handle_escalate(ws)

    ws.transition.assert_called_once_with(Stage.FAILED)


@pytest.mark.asyncio
async def test_escalate_no_chat_id_transitions_to_failed(tmp_path):
    ws = _make_workspace(tmp_path)
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)
    orch._get_chat_id = MagicMock(return_value="")

    await orch._handle_escalate(ws)

    ws.transition.assert_called_once_with(Stage.FAILED)
    notifier.send_message.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_orchestrator_escalate.py -v`
Expected:
- `test_escalate_sends_message_without_buttons` fails — current code passes `buttons=[Button(...), Button(...)]`.
- `test_escalate_stores_reason_only_in_human_input_question` fails — current code stores full message including header.
- `test_escalate_transitions_to_blocked_and_records_msg_id` may pass (existing behavior) — that's fine.
- `test_escalate_no_notifier_transitions_to_failed` / `test_escalate_no_chat_id_transitions_to_failed` likely already pass.

- [ ] **Step 3: Rewrite `_handle_escalate`**

In `orchestrator/orchestrator.py`, replace the existing `_handle_escalate` method (lines 1646-1717) with:

```python
async def _handle_escalate(self, workspace: Workspace) -> None:
    """Send escalation notification and block workspace."""
    state = workspace.state

    if not self._notifier:
        logger.warning("No notifier configured, cannot escalate %s", state.ticket_id)
        workspace.transition(Stage.FAILED)
        workspace.update_state(error="No notifier configured for escalation")
        return

    chat_id = self._get_chat_id(workspace)
    if not chat_id:
        logger.warning("No chat_id for escalation of %s", state.ticket_id)
        workspace.transition(Stage.FAILED)
        workspace.update_state(error="No Telegram chat_id configured")
        return

    stage = state.previous_state or state.current_state
    sep = "─" * 30
    title = self._get_ticket_title(workspace)
    hdr = self._tg_header("🔔", state, title)
    header = f"{hdr}\nStage: {stage}\n{sep}\n"

    reason = self._build_blocked_reason(workspace, stage.lower() if isinstance(stage, str) else stage)
    hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
    message = f"{header}\n{reason}{hint}"

    try:
        msg_id = await self._notifier.send_message(chat_id, message)
        workspace.transition(Stage.BLOCKED)
        workspace.update_state(human_input_question=reason)
        workspace.state.escalation_msg_id = msg_id
        workspace.state.escalation_chat_id = chat_id
        workspace.save_state()
        logger.info("Escalated %s via Telegram (msg_id=%d)", state.ticket_id, msg_id)
        self._emit(
            "escalation_sent",
            f"Escalated {workspace.state.ticket_id} to human",
            project_id=workspace.state.company_id,
            ticket_id=workspace.state.ticket_id,
            data={"reason": reason},
        )
    except Exception as e:
        logger.error("Telegram send failed for %s: %s", state.ticket_id, e)
        workspace.transition(Stage.FAILED)
        workspace.update_state(error=f"Telegram notification failed: {e}")
```

Notes:
- The `buttons=` argument is dropped entirely.
- `stage.lower()` converts `"ANALYSIS"` → `"analysis"` so the helper matches on the analysis branch.
- `human_input_question` stores only the reason (no header, no hint).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_escalate.py -v`
Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_escalate.py
git commit -m "refactor: _handle_escalate uses _build_blocked_reason, drops buttons"
```

---

## Task 3: Verification-fail path notifies via Telegram

**Files:**
- Modify: `orchestrator/orchestrator.py:794-807` (call new method) and add `_notify_verification_blocked`
- Modify: `tests/unit/test_orchestrator_stage_verify.py` (new test case)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_orchestrator_stage_verify.py` (end of file):

```python
@pytest.mark.asyncio
async def test_verification_fail_notifies_telegram_and_sets_escalation_fields(tmp_path):
    """When stage verification fails, the workspace goes BLOCKED and a TG
    notification is sent with escalation_msg_id/chat_id populated so the
    existing reply flow can unblock it."""
    from orchestrator.orchestrator import Orchestrator

    repo = _init_repo(tmp_path)

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "dev-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 14:00 UTC**\n## Decision: Proceed\n"
        "\nTests pass but no commit was made.\n"
    )

    ws = MagicMock()
    ws.source_dir = repo
    ws.reports_dir = reports_dir
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="DEV",
        previous_state="ANALYSIS",
        stage_iterations={},
        branch="feature/t-1",
        error=None,
        escalation_msg_id=None,
        escalation_chat_id=None,
        human_input_question=None,
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock()
    ws.save_state = MagicMock()

    workflow = MagicMock()
    stage_def = SimpleNamespace(agent="dev-agent", action=None, max_iterations=3)
    workflow.stages = {"dev": stage_def}

    notifier = MagicMock()
    notifier.send_message = AsyncMock(return_value=777)

    orch = Orchestrator.__new__(Orchestrator)
    orch._workflow = workflow
    orch._dry_run = False
    orch._events = None
    orch._notifier = notifier
    orch._agent_runtime = MagicMock()
    orch._agent_runtime.execute = AsyncMock(
        return_value=SimpleNamespace(
            success=True, output="Tests pass", duration_seconds=1.0,
            input_tokens=0, output_tokens=0, failure_kind=None, error=None, retry_at=None,
        )
    )
    orch._get_repo_config = MagicMock(return_value=None)
    orch._emit = MagicMock()
    orch._parse_agent_outcome = MagicMock(return_value="default")
    orch._should_approval_gate = MagicMock(return_value=False)
    orch._advance_to_stage = MagicMock()
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._get_ticket_title = MagicMock(return_value="A ticket")
    orch._tg_header = MagicMock(return_value="⚠️ [acme/acme-app] T-1")
    orch._log_pipeline = MagicMock()

    await orch._handle_agent_stage(ws, "dev", stage_def)

    # Workspace transitioned to BLOCKED.
    blocked_calls = [c for c in ws.transition.call_args_list if c.args and c.args[0] == "BLOCKED"]
    assert blocked_calls, "workspace must transition to BLOCKED"

    # TG notification sent.
    notifier.send_message.assert_awaited_once()
    args, kwargs = notifier.send_message.call_args
    message = args[1] if len(args) >= 2 else kwargs.get("message", "")
    assert "verification failed" in message.lower()
    assert kwargs.get("buttons") is None or "buttons" not in kwargs

    # Escalation fields populated on the workspace for reply routing.
    assert ws.state.escalation_msg_id == 777
    assert ws.state.escalation_chat_id == "chat-1"
    assert ws.state.human_input_question is not None
    assert "verification failed" in ws.state.human_input_question.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_orchestrator_stage_verify.py::test_verification_fail_notifies_telegram_and_sets_escalation_fields -v`
Expected: FAIL — current code does not call `notifier.send_message` on verification failure.

- [ ] **Step 3: Add `_notify_verification_blocked` method**

Insert this method into `orchestrator/orchestrator.py` immediately after `_handle_escalate`:

```python
async def _notify_verification_blocked(
    self, workspace: Workspace, stage_id: str, verify_reason: str,
) -> None:
    """Send a TG notification for a stage that just failed verification.

    Mirrors _handle_escalate semantics (populates escalation_msg_id so the
    reply flow in command_handler.handle_reply can unblock), but uses a
    distinct header to flag that this is a mechanical verification failure
    rather than an agent-requested escalation.
    """
    if not self._notifier:
        return
    chat_id = self._get_chat_id(workspace)
    if not chat_id:
        return

    sep = "─" * 30
    title = self._get_ticket_title(workspace)
    hdr = self._tg_header("⚠️", workspace.state, title)
    header = f"{hdr}\nStage: {stage_id} — verification failed\n{sep}\n"

    agent_reason = self._build_blocked_reason(workspace, stage_id)
    combined = f"Verification failed: {verify_reason}\n\n{agent_reason}"
    hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
    message = f"{header}\n{combined}{hint}"

    try:
        msg_id = await self._notifier.send_message(chat_id, message)
        workspace.update_state(human_input_question=combined)
        workspace.state.escalation_msg_id = msg_id
        workspace.state.escalation_chat_id = chat_id
        workspace.save_state()
        logger.info(
            "Verification-blocked %s via Telegram (msg_id=%d)",
            workspace.state.ticket_id, msg_id,
        )
    except Exception as e:
        logger.warning(
            "Failed to send verification-blocked notification for %s: %s",
            workspace.state.ticket_id, e,
        )
```

- [ ] **Step 4: Call it from the verification-fail branch**

In `orchestrator/orchestrator.py`, locate the verification-fail branch inside `_handle_agent_stage` (lines 794-807). Replace the block with:

```python
verify_result = stage_verifier.verify(stage_id, workspace, stage_start_commit)
if not verify_result.ok:
    agent_snippet = (result.output or "")[:200].replace("\n", " ")
    error_msg = f"{stage_id}: {verify_result.reason} (agent said: {agent_snippet})"
    workspace.transition(Stage.BLOCKED)
    workspace.update_state(error=error_msg)
    self._log_pipeline(workspace, f"BLOCKED — {stage_id} verification failed: {verify_result.reason}")
    self._emit(
        "stage_verification_failed",
        f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
        project_id=state.company_id, ticket_id=state.ticket_id,
        data={"stage": stage_id, "reason": verify_result.reason},
    )
    await self._notify_verification_blocked(workspace, stage_id, verify_result.reason)
    return
```

Only one line added: `await self._notify_verification_blocked(...)` before `return`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator_stage_verify.py -v`
Expected: all tests in the file pass (old ones still pass, new one passes).

Also re-run the earlier suites to confirm no regression:
Run: `pytest tests/unit/test_orchestrator_blocked_reason.py tests/unit/test_orchestrator_escalate.py tests/unit/test_orchestrator_stage_verify.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_stage_verify.py
git commit -m "feat: verification-fail path sends TG notification with escalation fields"
```

---

## Task 4: `/status` drill-down shows `Blocked on:` for BLOCKED workspaces

**Files:**
- Modify: `integrations/telegram/handlers/status.py:117-120`
- Modify: `tests/unit/test_handler_status.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_handler_status.py` (inside the `TestStatusHandler` class, after the existing drill-down tests — if none, add at the end of the class):

```python
    def test_drill_down_blocked_shows_blocked_on(self, handler):
        ws = _make_workspace("ACME-1", "BLOCKED")
        ws.state.human_input_question = (
            "## Questions for Human Review\n\n"
            "1. [AC2] What error types must be handled?\n"
        )
        ws.state.error = None

        result = handler.format_drill_down(ws)

        assert "Blocked on:" in result
        assert "[AC2]" in result
        assert "Last error: none" not in result

    def test_drill_down_blocked_truncates_long_reason(self, handler):
        ws = _make_workspace("ACME-1", "BLOCKED")
        ws.state.human_input_question = "x" * 2000
        ws.state.error = None

        result = handler.format_drill_down(ws)

        assert "Blocked on:" in result
        # 500 chars of x + ellipsis marker
        assert "…" in result

    def test_drill_down_blocked_without_question_falls_back_to_error(self, handler):
        ws = _make_workspace("ACME-1", "BLOCKED")
        ws.state.human_input_question = None
        ws.state.error = "some error"

        result = handler.format_drill_down(ws)

        assert "Last error: some error" in result
        assert "Blocked on:" not in result

    def test_drill_down_non_blocked_with_error_still_shows_last_error(self, handler):
        ws = _make_workspace("ACME-1", "FAILED")
        ws.state.human_input_question = "should not appear"
        ws.state.error = "timed out"

        result = handler.format_drill_down(ws)

        assert "Last error: timed out" in result
        assert "Blocked on:" not in result
```

Also update `_make_workspace` in that test file to default `human_input_question = None`. Find the existing helper (near the top) and add:

```python
    ws_state.human_input_question = None
```

just after `ws_state.human_input_pending = False`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_handler_status.py -v -k drill_down`
Expected: the four new tests fail — current code always emits `Last error:` line.

- [ ] **Step 3: Update `format_drill_down`**

In `integrations/telegram/handlers/status.py`, replace lines 117-120 (the `if s.error:` block at the end of `format_drill_down`) with:

```python
        if s.current_state == "BLOCKED" and getattr(s, "human_input_question", None):
            reason = s.human_input_question.strip()
            if len(reason) > 500:
                reason = reason[:500] + "…"
            lines.append(f"\nBlocked on: {reason}")
        elif s.error:
            lines.append(f"\nLast error: {s.error}")
        else:
            lines.append(f"\nLast error: none")
```

`getattr(..., None)` is a defensive guard: older workspace state files on disk may not have the field.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_handler_status.py -v`
Expected: all tests pass (existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/handlers/status.py tests/unit/test_handler_status.py
git commit -m "feat: /status drill-down shows Blocked on reason for BLOCKED tickets"
```

---

## Task 5: Full suite smoke + manual verification

**Files:** none modified — verification only.

- [ ] **Step 1: Run full unit suite**

Run: `pytest tests/unit/ -x --tb=short`
Expected: all tests pass. If any pre-existing test now fails because it asserted on the old escalation message format (header + first-3-lines summary + Proceed/Retry buttons), update it to match the new shape.

- [ ] **Step 2: Grep for other consumers of `human_input_question`**

Run: `grep -rn "human_input_question" --include="*.py"`
Expected: only these consumers — `orchestrator/orchestrator.py`, `integrations/telegram/handlers/status.py` (new), `integrations/telegram/command_handler.py` (reads it in `_build_input_context` or similar — verify it still makes sense with reason-only content), `workspace/workspace.py` (field definition), `tests/`.

If `command_handler.py` formats the stored value back into a message (e.g., for echoing the original question to the operator), confirm it still reads well when the value is just the reason (no header). If it needs tweaks, make them as a follow-up — do not expand scope here.

- [ ] **Step 3: Manual verification against a real workspace**

On a dev machine with a running Cleave instance:

1. Force an analysis escalation:
   - Create or adopt a ticket whose BA agent will escalate (ambiguous requirements, or no repo label).
   - Wait for the TG notification.
   - Confirm the message body contains the actual numbered questions from `reports/ba-questions.md`, not `**Attempt:**` / `## Decision:` boilerplate.
   - Confirm no `[Proceed]` / `[Retry]` buttons under the message.

2. Force a verification-fail BLOCKED:
   - Run a dev agent that claims success but makes no commit (existing bug path in `test_dev_stage_without_new_commit_goes_to_blocked`).
   - Confirm a TG message arrives with `⚠️` header and "verification failed" text.

3. Check `/status <ticket>` for the BLOCKED workspace:
   - Reply `status <TICKET-ID>` to the bot.
   - Confirm drill-down ends with `Blocked on: <reason>`, not `Last error: none`.

4. Test the reply round-trip:
   - Reply `skip` to the escalation message → workspace advances.
   - Reproduce, reply `retry` → workspace retries the stage.
   - Reproduce, reply with free text → workspace resumes with text stored in `meta/human-input.md` (already verified upstream).

- [ ] **Step 4: Update the feature index if needed**

`docs/features/telegram-notifications.md` — append to the change log:

```
| 2026-04-24 | Escalation messages now surface the actual reason (BA questions for analysis, extracted output for other stages); verification-failure BLOCKEDs now send TG notifications; `/status` drill-down shows `Blocked on:` for BLOCKED tickets; escalations no longer carry Proceed/Retry buttons (text-reply only, per inline-buttons spec). |
```

- [ ] **Step 5: Commit**

```bash
git add docs/features/telegram-notifications.md
git commit -m "docs: telegram-notifications changelog entry for escalation reason propagation"
```

---

## Self-Review Checklist

- **Spec coverage:** every Decision in the spec maps to a task —
  - "One helper, three call sites" → Task 1 (helper), Tasks 2/3/4 (call sites).
  - "Escalation messages stay text-reply only" → Task 2, tested by `test_escalate_sends_message_without_buttons`.
  - "Verification-failure BLOCKED now notifies" → Task 3.
  - "`human_input_question` stores the reason" → Task 2 (`test_escalate_stores_reason_only_in_human_input_question`) and Task 3 (assertion in verification test).
  - "No new workspace fields" → confirmed, plan reuses existing fields.
- **Placeholder scan:** no TBDs, no "similar to", no un-coded steps.
- **Type consistency:** helper is `_build_blocked_reason(workspace, stage_id)` everywhere. New method name `_notify_verification_blocked` is used consistently. `_BLOCKED_REASON_MAX_CHARS = 800` and `500` char truncation in status match spec.

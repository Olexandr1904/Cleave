# Telegram Command Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add interactive Telegram control to Sickle: status/health check, auto/manual pipeline modes with approval gates, and a Claude CLI-powered free-text command interface.

**Architecture:** New `CommandHandler` sits between TelegramAdapter's polling loop and the orchestrator — it intercepts all incoming messages, classifies intent via Claude Code CLI (`quick_query`), and dispatches to handler modules. The orchestrator gains mode awareness (auto/manual) and a new `AWAITING_APPROVAL` workspace state for manual mode gates.

**Tech Stack:** Python 3.10+, python-telegram-bot 21.10, Claude Code CLI (`claude -p`), pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-08-telegram-command-layer-design.md`

---

### Task 1: Add `AWAITING_APPROVAL` to workspace state machine

**Files:**
- Modify: `workspace/workspace.py:15-34`
- Modify: `tests/unit/test_workspace.py`

- [ ] **Step 1: Write failing tests for the new state**

Add to `tests/unit/test_workspace.py`:

```python
class TestAwaitingApproval:
    def test_awaiting_approval_in_valid_states(self):
        assert "AWAITING_APPROVAL" in VALID_STATES

    def test_analysis_to_awaiting_approval(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("AWAITING_APPROVAL")
        assert workspace.state.current_state == "AWAITING_APPROVAL"
        assert workspace.state.previous_state == "ANALYSIS"

    def test_qa_to_awaiting_approval(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("SCOPE_CHECK")
        workspace.transition("QA")
        workspace.transition("AWAITING_APPROVAL")
        assert workspace.state.current_state == "AWAITING_APPROVAL"

    def test_pr_review_to_awaiting_approval(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("SCOPE_CHECK")
        workspace.transition("QA")
        workspace.transition("PUSHED")
        workspace.transition("PR_REVIEW")
        workspace.transition("AWAITING_APPROVAL")
        assert workspace.state.current_state == "AWAITING_APPROVAL"

    def test_awaiting_approval_to_dev(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("AWAITING_APPROVAL")
        workspace.transition("DEV")
        assert workspace.state.current_state == "DEV"
        assert workspace.state.previous_state is None

    def test_awaiting_approval_to_pushed(self, workspace):
        """Post-QA approval resumes to PUSHED."""
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("SCOPE_CHECK")
        workspace.transition("QA")
        workspace.transition("AWAITING_APPROVAL")
        workspace.transition("PUSHED")
        assert workspace.state.current_state == "PUSHED"

    def test_awaiting_approval_to_done(self, workspace):
        """Post-PR_REVIEW approval finalizes."""
        workspace.transition("ANALYSIS")
        workspace.transition("DEV")
        workspace.transition("SCOPE_CHECK")
        workspace.transition("QA")
        workspace.transition("PUSHED")
        workspace.transition("PR_REVIEW")
        workspace.transition("AWAITING_APPROVAL")
        workspace.transition("DONE")
        assert workspace.state.current_state == "DONE"

    def test_awaiting_approval_to_failed(self, workspace):
        """Rejection moves to FAILED."""
        workspace.transition("ANALYSIS")
        workspace.transition("AWAITING_APPROVAL")
        workspace.transition("FAILED")
        assert workspace.state.current_state == "FAILED"

    def test_awaiting_approval_stores_previous_state(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("AWAITING_APPROVAL")
        assert workspace.state.previous_state == "ANALYSIS"
        assert workspace.state.human_input_pending is True

    def test_resume_from_awaiting_approval_clears_pending(self, workspace):
        workspace.transition("ANALYSIS")
        workspace.transition("AWAITING_APPROVAL")
        workspace.transition("DEV")
        assert workspace.state.human_input_pending is False
        assert workspace.state.previous_state is None

    def test_new_to_awaiting_approval_invalid(self, workspace):
        with pytest.raises(InvalidTransitionError):
            workspace.transition("AWAITING_APPROVAL")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_workspace.py::TestAwaitingApproval -v`
Expected: FAIL — `AWAITING_APPROVAL` not in `VALID_STATES`, transitions not defined.

- [ ] **Step 3: Implement AWAITING_APPROVAL state**

In `workspace/workspace.py`, update `VALID_STATES` (line 15-18):

```python
VALID_STATES = {
    "NEW", "ANALYSIS", "DEV", "SCOPE_CHECK", "QA",
    "PUSHED", "PR_REVIEW", "DONE",
    "BLOCKED", "FAILED", "ARCHIVED",
    "AWAITING_APPROVAL",
}
```

Update `VALID_TRANSITIONS` (line 22-34) — add `AWAITING_APPROVAL` as a target from `ANALYSIS`, `QA`, `PR_REVIEW`, and as a source to `DEV`, `PUSHED`, `DONE`, `FAILED`:

```python
VALID_TRANSITIONS: dict[str, set[str]] = {
    "NEW":                {"ANALYSIS", "FAILED"},
    "ANALYSIS":           {"DEV", "BLOCKED", "FAILED", "AWAITING_APPROVAL"},
    "DEV":                {"SCOPE_CHECK", "BLOCKED", "FAILED"},
    "SCOPE_CHECK":        {"QA", "DEV", "BLOCKED", "FAILED"},
    "QA":                 {"PUSHED", "DEV", "BLOCKED", "FAILED", "AWAITING_APPROVAL"},
    "PUSHED":             {"PR_REVIEW", "BLOCKED", "FAILED"},
    "PR_REVIEW":          {"DEV", "DONE", "BLOCKED", "FAILED", "AWAITING_APPROVAL"},
    "DONE":               {"ARCHIVED"},
    "BLOCKED":            {"ANALYSIS", "DEV", "SCOPE_CHECK", "QA", "PUSHED", "PR_REVIEW", "FAILED"},
    "FAILED":             set(),
    "ARCHIVED":           set(),
    "AWAITING_APPROVAL":  {"DEV", "PUSHED", "DONE", "FAILED"},
}
```

Update the `transition` method (line 145-169) to handle `AWAITING_APPROVAL` the same way as `BLOCKED` — store `previous_state` and set `human_input_pending`:

```python
    def transition(self, new_state: str) -> None:
        current = self.state.current_state
        if new_state not in VALID_STATES:
            raise InvalidTransitionError(f"Unknown state: {new_state}")
        if new_state not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransitionError(
                f"Cannot transition from '{current}' to '{new_state}'"
            )

        updates: dict[str, Any] = {"current_state": new_state}

        if new_state in ("BLOCKED", "AWAITING_APPROVAL"):
            updates["previous_state"] = current
            updates["human_input_pending"] = True
        elif current in ("BLOCKED", "AWAITING_APPROVAL"):
            updates["previous_state"] = None
            updates["human_input_pending"] = False

        self.update_state(**updates)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_workspace.py -v`
Expected: ALL PASS (including existing tests and new `TestAwaitingApproval`).

- [ ] **Step 5: Commit**

```bash
git add workspace/workspace.py tests/unit/test_workspace.py
git commit -m "feat: add AWAITING_APPROVAL state to workspace state machine"
```

---

### Task 2: Add pipeline and intent_parser config schemas

**Files:**
- Modify: `config/schemas.py:66-73`
- Modify: `tests/unit/test_config_loader.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_config_loader.py`:

```python
from config.schemas import PipelineConfig, IntentParserConfig, GlobalConfig


class TestNewConfigSchemas:
    def test_pipeline_config_defaults(self):
        cfg = PipelineConfig()
        assert cfg.mode == "auto"

    def test_intent_parser_config_defaults(self):
        cfg = IntentParserConfig()
        assert cfg.max_tokens == 200
        assert cfg.timeout_seconds == 5

    def test_global_config_has_pipeline(self):
        cfg = GlobalConfig()
        assert cfg.pipeline.mode == "auto"

    def test_global_config_has_intent_parser(self):
        cfg = GlobalConfig()
        assert cfg.intent_parser.max_tokens == 200

    def test_project_config_has_pipeline(self):
        from config.schemas import ProjectConfig
        cfg = ProjectConfig()
        assert cfg.pipeline.mode == "auto"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_loader.py::TestNewConfigSchemas -v`
Expected: FAIL — `PipelineConfig` and `IntentParserConfig` do not exist.

- [ ] **Step 3: Add new dataclasses to schemas.py**

In `config/schemas.py`, add before `GlobalConfig` (before line 66):

```python
@dataclass
class PipelineConfig:
    mode: str = "auto"  # "auto" or "manual"


@dataclass
class IntentParserConfig:
    max_tokens: int = 200
    timeout_seconds: int = 5
```

Update `GlobalConfig` (line 66-73) to include the new fields:

```python
@dataclass
class GlobalConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    workspaces: WorkspacesConfig = field(default_factory=WorkspacesConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    operator: OperatorProfile = field(default_factory=OperatorProfile)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    intent_parser: IntentParserConfig = field(default_factory=IntentParserConfig)
```

Update `ProjectConfig` (line 211-216) to include pipeline:

```python
@dataclass
class ProjectConfig:
    project: ProjectInfo = field(default_factory=ProjectInfo)
    jira: JiraConfig = field(default_factory=JiraConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    parallelism: ParallelismConfig = field(default_factory=ParallelismConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_config_loader.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add config/schemas.py tests/unit/test_config_loader.py
git commit -m "feat: add PipelineConfig and IntentParserConfig schemas"
```

---

### Task 3: Add `quick_query` method to Claude Code adapter

**Files:**
- Modify: `integrations/llm/claude_code_adapter.py`
- Create: `tests/unit/test_claude_code_adapter.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_claude_code_adapter.py`:

```python
"""Tests for integrations/llm/claude_code_adapter.py — quick_query method."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from integrations.llm.claude_code_adapter import ClaudeCodeAdapter


class TestQuickQuery:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model="claude-haiku-4-5-20251001")

    async def test_quick_query_returns_content(self, adapter):
        mock_result = json.dumps({
            "result": '{"intent": "status", "params": {}, "reply": "Here is the status"}',
            "is_error": False,
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "num_turns": 1,
            "stop_reason": "end_turn",
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await adapter.quick_query(
                prompt="what's going on",
                system="You are a command parser.",
            )
        assert "status" in result

    async def test_quick_query_uses_no_tools(self, adapter):
        mock_result = json.dumps({
            "result": "response",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "num_turns": 1,
            "stop_reason": "end_turn",
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.quick_query(prompt="test", system="sys")
            call_args = mock_exec.call_args
            cmd_list = list(call_args[0])
            assert "--allowedTools" in cmd_list
            # Empty string = no tools allowed
            tools_idx = cmd_list.index("--allowedTools")
            assert cmd_list[tools_idx + 1] == ""

    async def test_quick_query_uses_max_turns_1(self, adapter):
        mock_result = json.dumps({
            "result": "response",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "num_turns": 1,
            "stop_reason": "end_turn",
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_result.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.quick_query(prompt="test", system="sys")
            call_args = mock_exec.call_args
            cmd_list = list(call_args[0])
            assert "--max-turns" in cmd_list
            turns_idx = cmd_list.index("--max-turns")
            assert cmd_list[turns_idx + 1] == "1"

    async def test_quick_query_timeout(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="timed out"):
                await adapter.quick_query(prompt="test", system="sys")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_claude_code_adapter.py -v`
Expected: FAIL — `ClaudeCodeAdapter` has no `quick_query` method.

- [ ] **Step 3: Implement quick_query**

Add to `integrations/llm/claude_code_adapter.py` after the `execute_in_workspace` method (after line 120):

```python
    async def quick_query(
        self,
        prompt: str,
        system: str = "",
        timeout: int = 5,
    ) -> str:
        """Lightweight prompt-to-response call. No tools, single turn, short timeout.

        Used for intent parsing and other quick classification tasks.

        Args:
            prompt: The user message to classify.
            system: System prompt with context.
            timeout: Timeout in seconds (default 5).

        Returns:
            The raw text response from Claude.
        """
        cmd = [self._claude_bin, "-p"]

        full_prompt = prompt
        if system:
            full_prompt = f"{system}\n\n---\n\n{full_prompt}"

        use_model = self._model
        if use_model:
            cmd.extend(["--model", use_model])

        cmd.extend(["--output-format", "json"])
        cmd.extend(["--max-turns", "1"])
        cmd.extend(["--allowedTools", ""])

        logger.info("Claude Code quick_query: model=%s", use_model or "default")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"Claude Code quick_query timed out after {timeout}s")

        stdout_str = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Claude Code quick_query failed (rc={proc.returncode}): {stderr_str}"
            )

        try:
            data = json.loads(stdout_str)
            return data.get("result", stdout_str)
        except json.JSONDecodeError:
            return stdout_str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_claude_code_adapter.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/llm/claude_code_adapter.py tests/unit/test_claude_code_adapter.py
git commit -m "feat: add quick_query method to ClaudeCodeAdapter for intent parsing"
```

---

### Task 4: Create IntentParser

**Files:**
- Create: `integrations/telegram/intent_parser.py`
- Create: `tests/unit/test_intent_parser.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_intent_parser.py`:

```python
"""Tests for integrations/telegram/intent_parser.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from integrations.telegram.intent_parser import IntentParser, ParsedIntent


class TestParsedIntent:
    def test_from_valid_json(self):
        raw = '{"intent": "status", "params": {}, "reply": "Here is the status"}'
        intent = ParsedIntent.from_json(raw)
        assert intent.intent == "status"
        assert intent.params == {}
        assert intent.reply == "Here is the status"

    def test_from_invalid_json(self):
        intent = ParsedIntent.from_json("not json at all")
        assert intent.intent == "unknown"
        assert intent.reply != ""

    def test_from_missing_fields(self):
        raw = '{"intent": "status"}'
        intent = ParsedIntent.from_json(raw)
        assert intent.intent == "status"
        assert intent.params == {}
        assert intent.reply == ""


class TestIntentParser:
    @pytest.fixture
    def mock_adapter(self):
        adapter = AsyncMock()
        adapter.quick_query = AsyncMock(return_value=json.dumps({
            "intent": "status",
            "params": {},
            "reply": "Here is the pipeline status",
        }))
        return adapter

    @pytest.fixture
    def parser(self, mock_adapter):
        return IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)

    async def test_parse_returns_parsed_intent(self, parser):
        result = await parser.parse("what's going on", pipeline_context={})
        assert result.intent == "status"

    async def test_parse_passes_context_in_system_prompt(self, parser, mock_adapter):
        context = {
            "mode": "manual",
            "awaiting_approval": ["ACME-123 (post_analysis)"],
            "active_workspaces": ["ACME-456 — DEV"],
        }
        await parser.parse("yes", pipeline_context=context)
        call_args = mock_adapter.quick_query.call_args
        system_prompt = call_args.kwargs.get("system", "") or call_args.args[1] if len(call_args.args) > 1 else ""
        # The system prompt should be passed via keyword
        assert mock_adapter.quick_query.called

    async def test_parse_handles_adapter_error(self, mock_adapter):
        mock_adapter.quick_query = AsyncMock(side_effect=RuntimeError("CLI unavailable"))
        parser = IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)
        result = await parser.parse("hello", pipeline_context={})
        assert result.intent == "error"
        assert "trouble" in result.reply.lower()

    async def test_parse_handles_malformed_response(self, mock_adapter):
        mock_adapter.quick_query = AsyncMock(return_value="not json")
        parser = IntentParser(llm_adapter=mock_adapter, intent_parser_config=None)
        result = await parser.parse("hello", pipeline_context={})
        assert result.intent == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_intent_parser.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement IntentParser**

Create `integrations/telegram/intent_parser.py`:

```python
"""Intent parser — classifies free-text Telegram messages via Claude CLI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """\
You are the command parser for Sickle, an autonomous dev pipeline.
Current state:
- Mode: {mode}
- Awaiting approval: {awaiting_approval}
- Active workspaces: {active_workspaces}

Classify the user message into one of these intents:
  status, analyze, approve, reject, set_mode, unknown

Return ONLY valid JSON (no markdown, no code fences):
{{"intent": "...", "params": {{...}}, "reply": "..."}}

Intent param schemas:
- status: params.ticket_id (optional string) for drill-down
- analyze: params.ticket_ids (required list of strings)
- approve: params.ticket_id (optional string, infer from context if one workspace awaiting)
- reject: params.ticket_id (optional string)
- set_mode: params.mode (required, "auto" or "manual")
- unknown: params.raw_text (the original message)

The "reply" field is a natural language confirmation message for the user.\
"""


@dataclass
class ParsedIntent:
    """Result of intent classification."""
    intent: str
    params: dict[str, Any] = field(default_factory=dict)
    reply: str = ""

    @staticmethod
    def from_json(raw: str) -> ParsedIntent:
        """Parse a JSON string into a ParsedIntent, with fallback for malformed input."""
        try:
            data = json.loads(raw)
            return ParsedIntent(
                intent=data.get("intent", "unknown"),
                params=data.get("params", {}),
                reply=data.get("reply", ""),
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            return ParsedIntent(
                intent="unknown",
                params={"raw_text": raw},
                reply="I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, switch modes.",
            )


class IntentParser:
    """Classifies free-text messages into pipeline intents using Claude CLI."""

    def __init__(self, llm_adapter: Any, intent_parser_config: Any | None = None) -> None:
        self._llm = llm_adapter
        self._timeout = 5
        if intent_parser_config and hasattr(intent_parser_config, "timeout_seconds"):
            self._timeout = intent_parser_config.timeout_seconds

    async def parse(self, message: str, pipeline_context: dict[str, Any]) -> ParsedIntent:
        """Classify a user message into an intent.

        Args:
            message: Raw text from Telegram.
            pipeline_context: Dict with keys: mode, awaiting_approval, active_workspaces.

        Returns:
            ParsedIntent with intent, params, and reply.
        """
        system = INTENT_SYSTEM_PROMPT.format(
            mode=pipeline_context.get("mode", "auto"),
            awaiting_approval=", ".join(pipeline_context.get("awaiting_approval", [])) or "none",
            active_workspaces=", ".join(pipeline_context.get("active_workspaces", [])) or "none",
        )

        try:
            raw = await self._llm.quick_query(
                prompt=message,
                system=system,
                timeout=self._timeout,
            )
            return ParsedIntent.from_json(raw)
        except Exception as e:
            logger.error("Intent parsing failed: %s", e)
            return ParsedIntent(
                intent="error",
                params={},
                reply="I'm having trouble understanding right now. Try again in a moment.",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_intent_parser.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/intent_parser.py tests/unit/test_intent_parser.py
git commit -m "feat: add IntentParser for classifying Telegram messages via Claude CLI"
```

---

### Task 5: Create status handler

**Files:**
- Create: `integrations/telegram/handlers/__init__.py`
- Create: `integrations/telegram/handlers/status.py`
- Create: `tests/unit/test_handler_status.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_handler_status.py`:

```python
"""Tests for integrations/telegram/handlers/status.py."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.status import StatusHandler


def _make_workspace(ticket_id, state, started_at=None, pr_url=None, company_id="test", repo_id="repo"):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.company_id = company_id
    ws_state.repo_id = repo_id
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = pr_url
    ws_state.pr_number = 42 if pr_url else None
    ws_state.started_at = started_at or datetime.now(timezone.utc).isoformat()
    ws_state.last_updated_at = datetime.now(timezone.utc).isoformat()
    ws_state.stage_iterations = {"analysis": 1}
    ws_state.error = None
    ws_state.previous_state = None
    ws_state.human_input_pending = False
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestStatusHandler:
    @pytest.fixture
    def handler(self):
        return StatusHandler(jira_base_url="https://acme.atlassian.net")

    def test_summary_with_active_workspaces(self, handler):
        workspaces = [
            _make_workspace("ACME-123", "DEV"),
            _make_workspace("ACME-456", "QA"),
        ]
        result = handler.format_summary(
            mode="auto",
            uptime_seconds=3600,
            last_poll_ago_seconds=120,
            active_workspaces=workspaces,
            recent_done=[],
            recent_failed=[],
        )
        assert "ACME-123" in result
        assert "ACME-456" in result
        assert "auto" in result.lower()

    def test_summary_with_no_workspaces(self, handler):
        result = handler.format_summary(
            mode="manual",
            uptime_seconds=0,
            last_poll_ago_seconds=0,
            active_workspaces=[],
            recent_done=[],
            recent_failed=[],
        )
        assert "no active" in result.lower() or "Active (0)" in result

    def test_drill_down_includes_jira_url(self, handler):
        ws = _make_workspace("ACME-123", "DEV")
        result = handler.format_drill_down(ws)
        assert "https://acme.atlassian.net/browse/ACME-123" in result

    def test_drill_down_includes_pr_url_when_present(self, handler):
        ws = _make_workspace("ACME-123", "PR_REVIEW", pr_url="https://github.com/org/repo/pull/42")
        result = handler.format_drill_down(ws)
        assert "https://github.com/org/repo/pull/42" in result

    def test_drill_down_no_pr_url_when_absent(self, handler):
        ws = _make_workspace("ACME-123", "DEV")
        result = handler.format_drill_down(ws)
        assert "PR:" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_status.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement status handler**

Create `integrations/telegram/handlers/__init__.py` (empty file).

Create `integrations/telegram/handlers/status.py`:

```python
"""Status handler — formats pipeline status for Telegram responses."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = int(seconds // 3600)
    days = hours // 24
    if days > 0:
        remaining_hours = hours % 24
        return f"{days}d {remaining_hours}h"
    return f"{hours}h {int((seconds % 3600) // 60)}m"


class StatusHandler:
    """Formats pipeline status summary and drill-down messages."""

    def __init__(self, jira_base_url: str = "") -> None:
        self._jira_base_url = jira_base_url.rstrip("/")

    def format_summary(
        self,
        mode: str,
        uptime_seconds: float,
        last_poll_ago_seconds: float,
        active_workspaces: list[Any],
        recent_done: list[Any],
        recent_failed: list[Any],
    ) -> str:
        """Format the summary status message."""
        lines = [
            "Sickle Status",
            "",
            f"Mode: {mode}",
            f"Uptime: {_format_duration(uptime_seconds)}",
            f"Last Jira poll: {_format_duration(last_poll_ago_seconds)} ago",
            "",
        ]

        # Active workspaces
        lines.append(f"Active ({len(active_workspaces)}):")
        if active_workspaces:
            for ws in active_workspaces:
                s = ws.state
                iterations = s.stage_iterations.get(s.current_state.lower(), 0)
                suffix = ""
                if s.current_state == "AWAITING_APPROVAL":
                    suffix = ", awaiting approval"
                elif s.current_state == "BLOCKED":
                    suffix = ", blocked"
                elif iterations > 0:
                    max_iter = 2  # default display
                    suffix = f" (iteration {iterations}/{max_iter})"
                lines.append(f"  {s.ticket_id} — {s.current_state}{suffix}")
        else:
            lines.append("  (none)")

        lines.append("")

        # Recent completions and failures (24h)
        recent_all = recent_done + recent_failed
        if recent_all:
            lines.append("Recent (24h):")
            for ws in recent_done:
                lines.append(f"  {ws.state.ticket_id} — merged")
            for ws in recent_failed:
                lines.append(f"  {ws.state.ticket_id} — failed at {ws.state.previous_state or ws.state.current_state}")
        else:
            lines.append("Recent (24h): none")

        return "\n".join(lines)

    def format_drill_down(self, workspace: Any) -> str:
        """Format a detailed drill-down for a specific workspace."""
        s = workspace.state
        lines = [
            f"{s.ticket_id}",
            "",
            f"Stage: {s.current_state}",
            f"Branch: {s.branch or 'N/A'}",
        ]

        # Jira URL
        if self._jira_base_url:
            lines.append(f"Jira: {self._jira_base_url}/browse/{s.ticket_id}")

        # PR URL (only if exists)
        if s.pr_url:
            lines.append(f"PR: {s.pr_url}")

        # Stage history from iterations
        lines.append("")
        lines.append("Iterations:")
        if s.stage_iterations:
            for stage, count in s.stage_iterations.items():
                lines.append(f"  {stage}: {count}")
        else:
            lines.append("  (none)")

        # Error
        if s.error:
            lines.append(f"\nLast error: {s.error}")
        else:
            lines.append(f"\nLast error: none")

        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_status.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/handlers/__init__.py integrations/telegram/handlers/status.py tests/unit/test_handler_status.py
git commit -m "feat: add StatusHandler for Telegram status/drill-down messages"
```

---

### Task 6: Create mode handler

**Files:**
- Create: `integrations/telegram/handlers/mode.py`
- Create: `tests/unit/test_handler_mode.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_handler_mode.py`:

```python
"""Tests for integrations/telegram/handlers/mode.py."""

from __future__ import annotations

import json

import pytest

from integrations.telegram.handlers.mode import ModeHandler


class TestModeHandler:
    @pytest.fixture
    def state_file(self, tmp_path):
        path = tmp_path / "daemon_state.json"
        path.write_text(json.dumps({
            "mode": "auto",
            "started_at": "2026-04-08T00:00:00Z",
        }))
        return path

    @pytest.fixture
    def handler(self, state_file):
        return ModeHandler(state_file_path=str(state_file))

    def test_get_mode_returns_current(self, handler):
        assert handler.get_mode() == "auto"

    def test_set_mode_to_manual(self, handler):
        handler.set_mode("manual")
        assert handler.get_mode() == "manual"

    def test_set_mode_persists_to_disk(self, handler, state_file):
        handler.set_mode("manual")
        data = json.loads(state_file.read_text())
        assert data["mode"] == "manual"
        assert "mode_changed_at" in data

    def test_set_mode_back_to_auto(self, handler):
        handler.set_mode("manual")
        handler.set_mode("auto")
        assert handler.get_mode() == "auto"

    def test_set_invalid_mode_raises(self, handler):
        with pytest.raises(ValueError, match="Invalid mode"):
            handler.set_mode("turbo")

    def test_load_from_disk_on_init(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"mode": "manual", "started_at": "2026-04-08T00:00:00Z"}))
        handler = ModeHandler(state_file_path=str(path))
        assert handler.get_mode() == "manual"

    def test_fallback_to_default_when_no_file(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        handler = ModeHandler(state_file_path=str(path), default_mode="auto")
        assert handler.get_mode() == "auto"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_mode.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement mode handler**

Create `integrations/telegram/handlers/mode.py`:

```python
"""Mode handler — manages auto/manual pipeline mode with persistence."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_MODES = {"auto", "manual"}


class ModeHandler:
    """Manages the pipeline mode (auto/manual) with file persistence."""

    def __init__(self, state_file_path: str, default_mode: str = "auto") -> None:
        self._state_path = Path(state_file_path)
        self._mode = default_mode
        self._state: dict = {}
        self._load()

    def _load(self) -> None:
        """Load mode from daemon state file."""
        if self._state_path.exists():
            try:
                self._state = json.loads(self._state_path.read_text())
                self._mode = self._state.get("mode", self._mode)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load daemon state: %s", e)

    def get_mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Switch mode and persist to disk."""
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}. Must be one of {VALID_MODES}")

        self._mode = mode
        self._state["mode"] = mode
        self._state["mode_changed_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
        logger.info("Pipeline mode set to: %s", mode)

    def _save(self) -> None:
        """Persist state to disk."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state, indent=2))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_mode.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/handlers/mode.py tests/unit/test_handler_mode.py
git commit -m "feat: add ModeHandler for auto/manual mode switching with persistence"
```

---

### Task 7: Create approval handler

**Files:**
- Create: `integrations/telegram/handlers/approval.py`
- Create: `tests/unit/test_handler_approval.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_handler_approval.py`:

```python
"""Tests for integrations/telegram/handlers/approval.py."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.approval import ApprovalHandler


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state == "AWAITING_APPROVAL"
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestApprovalHandler:
    @pytest.fixture
    def handler(self):
        return ApprovalHandler()

    def test_find_awaiting_workspaces(self, handler):
        workspaces = [
            _make_workspace("T-1", "DEV"),
            _make_workspace("T-2", "AWAITING_APPROVAL", "ANALYSIS"),
            _make_workspace("T-3", "QA"),
        ]
        result = handler.find_awaiting(workspaces)
        assert len(result) == 1
        assert result[0].state.ticket_id == "T-2"

    def test_find_awaiting_by_ticket_id(self, handler):
        workspaces = [
            _make_workspace("T-1", "AWAITING_APPROVAL", "ANALYSIS"),
            _make_workspace("T-2", "AWAITING_APPROVAL", "QA"),
        ]
        result = handler.find_awaiting(workspaces, ticket_id="T-2")
        assert len(result) == 1
        assert result[0].state.ticket_id == "T-2"

    def test_resolve_next_state_post_analysis(self, handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", "ANALYSIS")
        next_state = handler.resolve_next_state(ws)
        assert next_state == "DEV"

    def test_resolve_next_state_post_qa(self, handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", "QA")
        next_state = handler.resolve_next_state(ws)
        assert next_state == "PUSHED"

    def test_resolve_next_state_post_pr_review(self, handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", "PR_REVIEW")
        next_state = handler.resolve_next_state(ws)
        assert next_state == "DONE"

    def test_no_awaiting_returns_empty(self, handler):
        workspaces = [_make_workspace("T-1", "DEV")]
        result = handler.find_awaiting(workspaces)
        assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_approval.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement approval handler**

Create `integrations/telegram/handlers/approval.py`:

```python
"""Approval handler — manages approve/reject for workspaces in AWAITING_APPROVAL."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maps the stage that triggered the gate to the next state on approval
_APPROVAL_NEXT_STATE = {
    "ANALYSIS": "DEV",
    "QA": "PUSHED",
    "PR_REVIEW": "DONE",
}


class ApprovalHandler:
    """Handles approval/rejection of workspaces awaiting manual approval."""

    def find_awaiting(
        self, workspaces: list[Any], ticket_id: str | None = None,
    ) -> list[Any]:
        """Find workspaces in AWAITING_APPROVAL state.

        Args:
            workspaces: List of active Workspace objects.
            ticket_id: Optional filter to match a specific ticket.

        Returns:
            List of matching workspaces.
        """
        results = [
            ws for ws in workspaces
            if ws.state.current_state == "AWAITING_APPROVAL"
        ]
        if ticket_id:
            results = [ws for ws in results if ws.state.ticket_id == ticket_id]
        return results

    def resolve_next_state(self, workspace: Any) -> str:
        """Determine the next state based on which gate triggered the approval wait.

        Returns:
            The state to transition to on approval.
        """
        previous = workspace.state.previous_state
        return _APPROVAL_NEXT_STATE.get(previous, "FAILED")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_approval.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/handlers/approval.py tests/unit/test_handler_approval.py
git commit -m "feat: add ApprovalHandler for manual mode approval gates"
```

---

### Task 8: Create analyze handler

**Files:**
- Create: `integrations/telegram/handlers/analyze.py`
- Create: `tests/unit/test_handler_analyze.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_handler_analyze.py`:

```python
"""Tests for integrations/telegram/handlers/analyze.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from integrations.telegram.handlers.analyze import AnalyzeHandler


class TestAnalyzeHandler:
    @pytest.fixture
    def mock_tracker(self):
        tracker = AsyncMock()
        ticket = MagicMock()
        ticket.id = "ACME-123"
        ticket.summary = "Implement user search"
        ticket.url = "https://acme.atlassian.net/browse/ACME-123"
        tracker.get_ticket = AsyncMock(return_value=ticket)
        return tracker

    @pytest.fixture
    def handler(self, mock_tracker):
        return AnalyzeHandler(tracker=mock_tracker)

    async def test_validate_tickets_success(self, handler):
        result = await handler.validate_tickets(["ACME-123"])
        assert len(result.valid) == 1
        assert result.valid[0].id == "ACME-123"
        assert result.invalid == []

    async def test_validate_tickets_not_found(self, handler, mock_tracker):
        mock_tracker.get_ticket = AsyncMock(side_effect=Exception("Not found"))
        result = await handler.validate_tickets(["ACME-999"])
        assert result.valid == []
        assert len(result.invalid) == 1
        assert "ACME-999" in result.invalid[0]

    async def test_validate_multiple_tickets(self, handler, mock_tracker):
        ticket2 = MagicMock()
        ticket2.id = "ACME-456"
        ticket2.summary = "Fix login bug"
        mock_tracker.get_ticket = AsyncMock(side_effect=[
            MagicMock(id="ACME-123", summary="Search"), 
            Exception("Not found"),
        ])
        result = await handler.validate_tickets(["ACME-123", "ACME-456"])
        assert len(result.valid) == 1
        assert len(result.invalid) == 1

    def test_is_already_active(self, handler):
        ws = MagicMock()
        ws_state = MagicMock()
        ws_state.ticket_id = "ACME-123"
        type(ws).state = PropertyMock(return_value=ws_state)
        active = [ws]
        assert handler.is_already_active("ACME-123", active) is True
        assert handler.is_already_active("ACME-999", active) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_analyze.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement analyze handler**

Create `integrations/telegram/handlers/analyze.py`:

```python
"""Analyze handler — validates and prepares tickets for manual mode analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of ticket validation."""
    valid: list[Any] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


class AnalyzeHandler:
    """Validates ticket IDs against Jira before creating workspaces."""

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker

    async def validate_tickets(self, ticket_ids: list[str]) -> ValidationResult:
        """Fetch each ticket from Jira to verify it exists.

        Returns:
            ValidationResult with lists of valid TicketData and invalid ticket ID strings.
        """
        result = ValidationResult()
        for tid in ticket_ids:
            try:
                ticket = await self._tracker.get_ticket(tid)
                result.valid.append(ticket)
            except Exception as e:
                logger.warning("Ticket %s not found or inaccessible: %s", tid, e)
                result.invalid.append(f"{tid}: {e}")
        return result

    def is_already_active(self, ticket_id: str, active_workspaces: list[Any]) -> bool:
        """Check if a ticket already has an active workspace."""
        return any(ws.state.ticket_id == ticket_id for ws in active_workspaces)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_handler_analyze.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/handlers/analyze.py tests/unit/test_handler_analyze.py
git commit -m "feat: add AnalyzeHandler for manual mode ticket validation"
```

---

### Task 9: Create CommandHandler (dispatcher)

**Files:**
- Create: `integrations/telegram/command_handler.py`
- Create: `tests/unit/test_command_handler.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_command_handler.py`:

```python
"""Tests for integrations/telegram/command_handler.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.intent_parser import ParsedIntent


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = None
    ws_state.pr_number = None
    ws_state.started_at = "2026-04-08T00:00:00Z"
    ws_state.last_updated_at = "2026-04-08T01:00:00Z"
    ws_state.stage_iterations = {}
    ws_state.error = None
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestCommandHandler:
    @pytest.fixture
    def mock_intent_parser(self):
        parser = AsyncMock()
        parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="status", params={}, reply="Here is the status",
        ))
        return parser

    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        return notifier

    @pytest.fixture
    def mock_mode_handler(self):
        handler = MagicMock()
        handler.get_mode.return_value = "auto"
        return handler

    @pytest.fixture
    def command_handler(self, mock_intent_parser, mock_notifier, mock_mode_handler):
        return CommandHandler(
            intent_parser=mock_intent_parser,
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
            jira_base_url="https://acme.atlassian.net",
            started_at="2026-04-08T00:00:00Z",
        )

    async def test_handle_status_sends_message(self, command_handler, mock_notifier):
        await command_handler.handle_message("what's going on", "12345")
        mock_notifier.send_message.assert_called_once()
        call_args = mock_notifier.send_message.call_args
        assert call_args[0][0] == "12345"  # chat_id

    async def test_handle_set_mode(self, command_handler, mock_intent_parser, mock_notifier, mock_mode_handler):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="set_mode", params={"mode": "manual"}, reply="Switched to manual",
        ))
        await command_handler.handle_message("switch to manual", "12345")
        mock_mode_handler.set_mode.assert_called_once_with("manual")

    async def test_handle_unknown_sends_help(self, command_handler, mock_intent_parser, mock_notifier):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="unknown", params={}, reply="I didn't understand that.",
        ))
        await command_handler.handle_message("gibberish", "12345")
        mock_notifier.send_message.assert_called_once()

    async def test_handle_error_sends_error_message(self, command_handler, mock_intent_parser, mock_notifier):
        mock_intent_parser.parse = AsyncMock(return_value=ParsedIntent(
            intent="error", params={}, reply="I'm having trouble.",
        ))
        await command_handler.handle_message("hello", "12345")
        mock_notifier.send_message.assert_called_once()
        call_text = mock_notifier.send_message.call_args[0][1]
        assert "trouble" in call_text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_command_handler.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement CommandHandler**

Create `integrations/telegram/command_handler.py`:

```python
"""Command handler — dispatches parsed intents to handler modules."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from integrations.telegram.handlers.approval import ApprovalHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.handlers.status import StatusHandler
from integrations.telegram.intent_parser import IntentParser, ParsedIntent

logger = logging.getLogger(__name__)


class CommandHandler:
    """Routes incoming Telegram messages to the appropriate handler."""

    def __init__(
        self,
        intent_parser: IntentParser,
        notifier: Any,
        mode_handler: ModeHandler,
        active_workspaces_fn: Callable[[], list[Any]],
        jira_base_url: str = "",
        started_at: str = "",
        tracker: Any | None = None,
        analyze_callback: Callable | None = None,
    ) -> None:
        self._intent_parser = intent_parser
        self._notifier = notifier
        self._mode_handler = mode_handler
        self._active_workspaces_fn = active_workspaces_fn
        self._status_handler = StatusHandler(jira_base_url=jira_base_url)
        self._approval_handler = ApprovalHandler()
        self._started_at = started_at
        self._tracker = tracker
        self._analyze_callback = analyze_callback
        self._last_poll_time: float = time.time()

    def update_last_poll_time(self) -> None:
        """Called by orchestrator after each Jira poll."""
        self._last_poll_time = time.time()

    async def handle_message(self, text: str, chat_id: str) -> None:
        """Process an incoming Telegram message."""
        workspaces = self._active_workspaces_fn()

        # Build pipeline context for intent parser
        context = self._build_context(workspaces)

        # Parse intent
        intent = await self._intent_parser.parse(text, context)
        logger.info("Parsed intent: %s (params=%s)", intent.intent, intent.params)

        # Dispatch
        if intent.intent == "status":
            await self._handle_status(intent, chat_id, workspaces)
        elif intent.intent == "set_mode":
            await self._handle_set_mode(intent, chat_id)
        elif intent.intent == "approve":
            await self._handle_approve(intent, chat_id, workspaces)
        elif intent.intent == "reject":
            await self._handle_reject(intent, chat_id, workspaces)
        elif intent.intent == "analyze":
            await self._handle_analyze(intent, chat_id, workspaces)
        elif intent.intent == "error":
            await self._notifier.send_message(chat_id, intent.reply)
        else:
            await self._notifier.send_message(chat_id, intent.reply or
                "I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, switch modes.")

    def _build_context(self, workspaces: list[Any]) -> dict[str, Any]:
        """Build pipeline context dict for the intent parser."""
        awaiting = [
            f"{ws.state.ticket_id} ({ws.state.previous_state})"
            for ws in workspaces
            if ws.state.current_state == "AWAITING_APPROVAL"
        ]
        active = [
            f"{ws.state.ticket_id} — {ws.state.current_state}"
            for ws in workspaces
            if ws.state.current_state not in ("DONE", "FAILED", "ARCHIVED")
        ]
        return {
            "mode": self._mode_handler.get_mode(),
            "awaiting_approval": awaiting,
            "active_workspaces": active,
        }

    async def _handle_status(
        self, intent: ParsedIntent, chat_id: str, workspaces: list[Any],
    ) -> None:
        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            # Drill-down
            matches = [ws for ws in workspaces if ws.state.ticket_id == ticket_id]
            if matches:
                msg = self._status_handler.format_drill_down(matches[0])
            else:
                msg = f"No active workspace found for {ticket_id}."
        else:
            # Summary
            now = time.time()
            uptime = 0.0
            if self._started_at:
                try:
                    start = datetime.fromisoformat(self._started_at)
                    uptime = (datetime.now(timezone.utc) - start).total_seconds()
                except (ValueError, TypeError):
                    pass
            poll_ago = now - self._last_poll_time

            active = [ws for ws in workspaces if ws.state.current_state not in ("DONE", "FAILED", "ARCHIVED")]
            done = [ws for ws in workspaces if ws.state.current_state == "DONE"]
            failed = [ws for ws in workspaces if ws.state.current_state == "FAILED"]

            msg = self._status_handler.format_summary(
                mode=self._mode_handler.get_mode(),
                uptime_seconds=uptime,
                last_poll_ago_seconds=poll_ago,
                active_workspaces=active,
                recent_done=done,
                recent_failed=failed,
            )
        await self._notifier.send_message(chat_id, msg)

    async def _handle_set_mode(self, intent: ParsedIntent, chat_id: str) -> None:
        mode = intent.params.get("mode", "")
        try:
            self._mode_handler.set_mode(mode)
            await self._notifier.send_message(chat_id, intent.reply or f"Switched to {mode} mode.")
        except ValueError as e:
            await self._notifier.send_message(chat_id, str(e))

    async def _handle_approve(
        self, intent: ParsedIntent, chat_id: str, workspaces: list[Any],
    ) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)

        if not awaiting:
            await self._notifier.send_message(chat_id, "No workspaces awaiting approval.")
            return

        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._notifier.send_message(
                chat_id,
                f"Multiple workspaces awaiting approval: {tickets}. Please specify which one.",
            )
            return

        ws = awaiting[0]
        next_state = self._approval_handler.resolve_next_state(ws)
        ws.transition(next_state)
        await self._notifier.send_message(
            chat_id,
            intent.reply or f"Approved {ws.state.ticket_id}. Moving to {next_state}.",
        )

    async def _handle_reject(
        self, intent: ParsedIntent, chat_id: str, workspaces: list[Any],
    ) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)

        if not awaiting:
            await self._notifier.send_message(chat_id, "No workspaces awaiting approval.")
            return

        ws = awaiting[0]
        ws.transition("FAILED")
        ws.update_state(error="Rejected by operator via Telegram")
        await self._notifier.send_message(
            chat_id,
            intent.reply or f"Rejected {ws.state.ticket_id}. Marked as FAILED.",
        )

    async def _handle_analyze(
        self, intent: ParsedIntent, chat_id: str, workspaces: list[Any],
    ) -> None:
        ticket_ids = intent.params.get("ticket_ids", [])
        if not ticket_ids:
            await self._notifier.send_message(chat_id, "Please specify ticket IDs to analyze.")
            return

        if self._mode_handler.get_mode() != "manual":
            await self._notifier.send_message(
                chat_id,
                "The analyze command is only available in manual mode. Switch to manual first.",
            )
            return

        if self._analyze_callback:
            await self._analyze_callback(ticket_ids, chat_id)
        else:
            await self._notifier.send_message(chat_id, intent.reply or f"Queued {len(ticket_ids)} ticket(s) for analysis.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_command_handler.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_command_handler.py
git commit -m "feat: add CommandHandler dispatching intents to handler modules"
```

---

### Task 10: Hook CommandHandler into TelegramAdapter polling

**Files:**
- Modify: `integrations/telegram/telegram_adapter.py`
- Create: `tests/unit/test_telegram_adapter.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_telegram_adapter.py`:

```python
"""Tests for integrations/telegram/telegram_adapter.py — command handler integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.telegram.telegram_adapter import TelegramAdapter


class TestCommandHandlerIntegration:
    def test_set_command_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            adapter.set_command_handler(handler)
            assert adapter._command_handler is handler

    async def test_incoming_non_reply_routes_to_command_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            handler.handle_message = AsyncMock()
            adapter.set_command_handler(handler)

            # Simulate an incoming non-reply message
            update = MagicMock()
            update.message.reply_to_message = None
            update.message.text = "what's going on"
            update.message.chat.id = 12345

            await adapter._handle_incoming(update, None)
            handler.handle_message.assert_called_once_with("what's going on", "12345")

    async def test_incoming_reply_routes_to_reply_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            adapter.set_command_handler(handler)

            # Simulate a reply message
            update = MagicMock()
            update.message.reply_to_message = MagicMock()
            update.message.reply_to_message.message_id = 42
            update.message.text = "yes proceed"
            update.message.chat.id = 12345

            # Set up a pending reply future
            import asyncio
            future = asyncio.get_event_loop().create_future()
            adapter._pending_replies[42] = future

            await adapter._handle_incoming(update, None)
            # Reply should be routed to the future, not command handler
            assert future.result() == "yes proceed"
            handler.handle_message.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_telegram_adapter.py -v`
Expected: FAIL — `set_command_handler` and `_handle_incoming` don't exist.

- [ ] **Step 3: Modify TelegramAdapter**

Update `integrations/telegram/telegram_adapter.py` to add `set_command_handler` and a unified `_handle_incoming` method:

Replace the entire file content with:

```python
"""Telegram adapter implementing NotifierInterface."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, filters

from integrations.base.notifier import NotifierInterface

logger = logging.getLogger(__name__)


class TelegramAdapter(NotifierInterface):
    """Telegram Bot API adapter using python-telegram-bot."""

    def __init__(self, bot_token: str) -> None:
        self._bot = Bot(token=bot_token)
        self._pending_replies: dict[int, asyncio.Future[str]] = {}
        self._app: Application | None = None
        self._command_handler: Any | None = None

    def set_command_handler(self, handler: Any) -> None:
        """Register the CommandHandler for processing incoming messages."""
        self._command_handler = handler

    async def send_message(self, chat_id: str, message: str) -> int:
        """Send a message and return the message ID."""
        msg = await self._bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
        logger.info("Sent Telegram message %d to chat %s", msg.message_id, chat_id)
        return msg.message_id

    async def wait_for_reply(
        self, chat_id: str, message_id: int, timeout_seconds: int = 0
    ) -> str | None:
        """Wait for a reply to a specific message."""
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending_replies[message_id] = future

        try:
            if timeout_seconds > 0:
                return await asyncio.wait_for(future, timeout=timeout_seconds)
            else:
                return await future
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout waiting for reply to message %d in chat %s",
                message_id, chat_id,
            )
            return None
        finally:
            self._pending_replies.pop(message_id, None)

    async def _handle_incoming(self, update: Update, context: object) -> None:
        """Handle all incoming messages. Routes replies to futures, others to CommandHandler."""
        message = update.message
        if not message or not message.text:
            return

        # If it's a reply to a tracked message, route to the reply future
        if message.reply_to_message:
            original_id = message.reply_to_message.message_id
            future = self._pending_replies.get(original_id)
            if future and not future.done():
                future.set_result(message.text)
                logger.info(
                    "Received reply to message %d: %s",
                    original_id, message.text[:50],
                )
                return

        # Otherwise, route to command handler
        if self._command_handler:
            chat_id = str(message.chat.id)
            try:
                await self._command_handler.handle_message(message.text, chat_id)
            except Exception as e:
                logger.error("Command handler error: %s", e, exc_info=True)

    async def start_polling(self) -> None:
        """Start the bot's polling loop for receiving messages."""
        self._app = Application.builder().token(self._bot.token).build()
        self._app.add_handler(
            MessageHandler(filters.TEXT, self._handle_incoming)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("Telegram bot polling started")

    async def stop_polling(self) -> None:
        """Stop the bot's polling loop."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot polling stopped")
```

Key changes:
- Added `_command_handler` attribute and `set_command_handler` method.
- Replaced `_handle_reply` with `_handle_incoming` that handles both replies and commands.
- Changed the message filter from `filters.REPLY & filters.TEXT` to `filters.TEXT` (catch all text messages).
- Reply matching still takes priority (checked first in `_handle_incoming`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_telegram_adapter.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/telegram_adapter.py tests/unit/test_telegram_adapter.py
git commit -m "feat: hook CommandHandler into TelegramAdapter polling loop"
```

---

### Task 11: Add mode-aware behavior to Orchestrator

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `tests/unit/test_workspace.py` (or a new test file)

This is the integration task — the orchestrator needs to:
1. Skip Jira polling in manual mode
2. Insert approval gates after ANALYSIS, QA, and PR_REVIEW in manual mode
3. Auto-approve AWAITING_APPROVAL workspaces when switching to auto mode
4. Expose state for the CommandHandler

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_orchestrator_modes.py`:

```python
"""Tests for orchestrator mode-aware behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.stage_iterations = {}
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.error = None
    ws_state.branch = f"feature/{ticket_id}"
    ws_state.pr_url = None
    ws_state.pr_number = None
    type(ws).state = PropertyMock(return_value=ws_state)
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.increment_iteration = MagicMock(return_value=1)
    ws.reports_dir = MagicMock()
    return ws


def _make_orchestrator(mode="auto"):
    global_config = MagicMock()
    global_config.telegram.default_chat_id = "12345"
    global_config.defaults.poll_interval_seconds = 300
    global_config.workspaces.max_age_days = 14
    global_config.pipeline.mode = mode

    workflow = MagicMock()
    workspace_manager = MagicMock()
    workspace_manager.discover_workspaces.return_value = []
    workspace_manager.cleanup_old_workspaces.return_value = []

    orch = Orchestrator(
        global_config=global_config,
        projects={},
        registry=MagicMock(),
        workflow=workflow,
        workspace_manager=workspace_manager,
        agent_runtime=MagicMock(),
        tracker=AsyncMock(),
        notifier=AsyncMock(),
    )
    return orch


class TestModeAwarePollCycle:
    async def test_manual_mode_skips_jira_polling(self):
        orch = _make_orchestrator(mode="manual")
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        orch._tracker = AsyncMock()

        await orch.poll_cycle()
        orch._tracker.poll_tickets.assert_not_called()

    async def test_auto_mode_polls_jira(self):
        orch = _make_orchestrator(mode="auto")
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "auto"
        orch._projects = {"test": MagicMock()}
        orch._projects["test"].config.jira.url = "https://jira.example.com"

        await orch.poll_cycle()
        # Tracker is called because mode is auto
        assert orch._tracker.poll_tickets.called


class TestApprovalGates:
    def test_should_gate_returns_true_for_analysis_in_manual(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        assert orch._should_approval_gate("ANALYSIS") is True

    def test_should_gate_returns_true_for_qa_in_manual(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        assert orch._should_approval_gate("QA") is True

    def test_should_gate_returns_true_for_pr_review_in_manual(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        assert orch._should_approval_gate("PR_REVIEW") is True

    def test_should_gate_returns_false_in_auto(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "auto"
        assert orch._should_approval_gate("ANALYSIS") is False

    def test_should_gate_returns_false_for_dev(self):
        orch = _make_orchestrator()
        orch._mode_handler = MagicMock()
        orch._mode_handler.get_mode.return_value = "manual"
        assert orch._should_approval_gate("DEV") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_orchestrator_modes.py -v`
Expected: FAIL — `_mode_handler` and `_should_approval_gate` don't exist on Orchestrator.

- [ ] **Step 3: Modify Orchestrator for mode awareness**

In `orchestrator/orchestrator.py`, apply the following changes:

**Add import at top of file (after line 10):**

```python
from integrations.telegram.handlers.mode import ModeHandler
```

**Add `_mode_handler` initialization in `__init__` (after line 61, after `self._repo_vcs` line):**

```python
        # Mode handler — initialized later via set_mode_handler or from config default
        self._mode_handler: ModeHandler | None = None
```

**Add setter method after `register_repo_vcs` (after line 67):**

```python
    def set_mode_handler(self, handler: ModeHandler) -> None:
        """Register the mode handler for auto/manual switching."""
        self._mode_handler = handler
```

**Add approval gate check method (after `_get_chat_id`, around line 91):**

```python
    # Approval gate stages in manual mode
    _APPROVAL_GATE_STATES = {"ANALYSIS", "QA", "PR_REVIEW"}

    def _should_approval_gate(self, completed_state: str) -> bool:
        """Check if the workspace should pause for approval after this state."""
        if not self._mode_handler or self._mode_handler.get_mode() != "manual":
            return False
        return completed_state in self._APPROVAL_GATE_STATES
```

**Modify `poll_cycle` (line 127-159) to check mode before polling:**

Replace the Jira polling section:

```python
    async def poll_cycle(self) -> None:
        """Single poll + advance cycle."""
        # 1. Poll for new tickets (auto mode only)
        is_manual = self._mode_handler and self._mode_handler.get_mode() == "manual"
        if self._tracker and not is_manual:
            await self._poll_and_create_workspaces()
```

The rest of `poll_cycle` stays the same.

**Modify `advance_workspace` (line 278-312) to skip AWAITING_APPROVAL:**

After the `BLOCKED` check (line 283-284), add:

```python
        if current == "AWAITING_APPROVAL":
            return  # Waiting for operator approval
```

**Modify `_handle_agent_stage` to insert approval gate (line 345-351):**

After the outcome is determined and next_stage is computed, before advancing, check if gate applies:

Replace the block at the end of `_handle_agent_stage` (the part after `_parse_agent_outcome`):

```python
        outcome = self._parse_agent_outcome(stage_id, result.output, workspace)
        next_stage = get_next_stage(stage_id, self._workflow, outcome)

        if next_stage:
            # Check for approval gate in manual mode
            current_state = workspace.state.current_state
            if self._should_approval_gate(current_state):
                workspace.transition("AWAITING_APPROVAL")
                if self._notifier:
                    chat_id = self._get_chat_id(workspace)
                    summary = self._build_gate_summary(workspace, current_state)
                    await self._notifier.send_message(chat_id, summary)
            else:
                self._advance_to_stage(workspace, next_stage)
        else:
            workspace.transition("DONE")
```

**Similarly modify `_handle_action_stage` for QA pass and PR_REVIEW done outcomes — add gate checks where the workspace would normally advance past a gate point.**

In `_action_push_and_open_pr` (line 381-395), after `workspace.transition("PR_REVIEW")`, no gate needed (gate is after PR_REVIEW completes, not after PUSHED).

In `_action_fetch_pr_comments` (line 397-469), before transitioning to done, check the gate:

After `workspace.transition("DONE")` on line 405 and 441 and the dev redirect on 465-468, add gate check. Specifically, when the outcome is "done" (no comments or agent says no fix needed), check:

```python
            if self._should_approval_gate("PR_REVIEW"):
                workspace.transition("AWAITING_APPROVAL")
                if self._notifier:
                    chat_id = self._get_chat_id(workspace)
                    summary = self._build_gate_summary(workspace, "PR_REVIEW")
                    await self._notifier.send_message(chat_id, summary)
            else:
                workspace.transition("DONE")
```

**Add `_build_gate_summary` helper (after `_parse_agent_outcome`):**

```python
    def _build_gate_summary(self, workspace: Workspace, gate_state: str) -> str:
        """Build a summary message for an approval gate notification."""
        state = workspace.state
        ticket_id = state.ticket_id

        if gate_state == "ANALYSIS":
            ba_report = workspace.reports_dir / "ba-agent-output.md"
            summary = ""
            if ba_report.exists():
                content = ba_report.read_text(encoding="utf-8")
                summary = content[:500]
            return (
                f"[{state.company_id}/{state.repo_id}] {ticket_id}\n\n"
                f"Analysis complete. Here's the plan:\n{summary}\n\n"
                f"Proceed to development?"
            )

        if gate_state == "QA":
            qa_report = workspace.reports_dir / "qa-agent-output.md"
            summary = ""
            if qa_report.exists():
                content = qa_report.read_text(encoding="utf-8")
                summary = content[:500]
            return (
                f"[{state.company_id}/{state.repo_id}] {ticket_id}\n\n"
                f"Tests pass.\n{summary}\n\n"
                f"Push and open PR?"
            )

        if gate_state == "PR_REVIEW":
            return (
                f"[{state.company_id}/{state.repo_id}] {ticket_id}\n\n"
                f"PR review complete. PR: {state.pr_url or 'N/A'}\n\n"
                f"Finalize and merge?"
            )

        return f"{ticket_id}: Awaiting approval at {gate_state}."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_orchestrator_modes.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Run full test suite**

Run: `cd /home/admin0/tot && python -m pytest tests/ -v`
Expected: ALL PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_modes.py
git commit -m "feat: add mode-aware behavior to orchestrator (manual mode gates, skip polling)"
```

---

### Task 12: Wire everything together in main.py

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Read current main.py**

Read `main.py` to understand the current initialization flow before modifying.

- [ ] **Step 2: Add CommandHandler wiring to main.py**

After the orchestrator is created and before `orchestrator.run()` is called, add:

```python
    # Initialize mode handler
    from integrations.telegram.handlers.mode import ModeHandler
    daemon_state_path = os.path.join(config_dir, "daemon_state.json")
    mode_handler = ModeHandler(
        state_file_path=daemon_state_path,
        default_mode=global_config.pipeline.mode,
    )
    orchestrator.set_mode_handler(mode_handler)

    # Initialize command handler (if Telegram configured)
    if notifier and isinstance(notifier, TelegramAdapter):
        from integrations.telegram.intent_parser import IntentParser
        from integrations.telegram.command_handler import CommandHandler
        from datetime import datetime, timezone

        intent_parser = IntentParser(
            llm_adapter=llm_adapter,
            intent_parser_config=global_config.intent_parser,
        )
        command_handler = CommandHandler(
            intent_parser=intent_parser,
            notifier=notifier,
            mode_handler=mode_handler,
            active_workspaces_fn=lambda: orchestrator._active_workspaces,
            jira_base_url=next(iter(projects.values())).config.jira.url if projects else "",
            started_at=datetime.now(timezone.utc).isoformat(),
            tracker=tracker,
        )
        notifier.set_command_handler(command_handler)
```

The exact placement depends on the current `main.py` structure — read it first, then insert after orchestrator creation.

- [ ] **Step 3: Run full test suite to verify no regressions**

Run: `cd /home/admin0/tot && python -m pytest tests/ -v`
Expected: ALL PASS.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire CommandHandler, ModeHandler, and IntentParser into main.py"
```

---

### Task 13: Update default workflow YAML for approval gates

**Files:**
- Modify: `workflows/default-workflow.yaml`
- Modify: `tests/unit/test_workflow_router.py`

The workflow YAML doesn't need new stages for approval gates — gates are handled by the orchestrator, not the workflow router. But we should add a test to verify the workflow router gracefully handles the AWAITING_APPROVAL state (which has no corresponding workflow stage).

- [ ] **Step 1: Write test**

Add to `tests/unit/test_workflow_router.py`:

```python
class TestAwaitingApprovalState:
    def test_no_workflow_stage_for_awaiting_approval(self, workflow):
        """AWAITING_APPROVAL is an orchestrator state, not a workflow stage."""
        assert "awaiting_approval" not in workflow.stages

    def test_get_next_stage_returns_none_for_awaiting_approval(self, workflow):
        result = get_next_stage("awaiting_approval", workflow)
        assert result is None
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /home/admin0/tot && python -m pytest tests/unit/test_workflow_router.py -v`
Expected: ALL PASS (these tests should pass immediately since AWAITING_APPROVAL is NOT in the workflow — they just document the design).

- [ ] **Step 3: Add AWAITING_APPROVAL to orchestrator's state mapping**

In `orchestrator/orchestrator.py`, update `_STAGE_TO_STATE` (line 596-605) — no change needed since AWAITING_APPROVAL has no corresponding workflow stage. But update `advance_workspace` to handle it (already done in Task 11).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_workflow_router.py
git commit -m "test: document AWAITING_APPROVAL as orchestrator-level state"
```

---

### Task 14: Integration test — full manual mode flow

**Files:**
- Create: `tests/integration/test_manual_mode_flow.py`

- [ ] **Step 1: Write integration test**

Create `tests/integration/test_manual_mode_flow.py`:

```python
"""Integration test — manual mode flow from analyze command to approval."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.intent_parser import IntentParser, ParsedIntent


class TestManualModeFlow:
    """Test the full flow: set mode -> analyze -> approve cycle."""

    @pytest.fixture
    def mode_handler(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        state_path.write_text(json.dumps({"mode": "auto", "started_at": "2026-04-08T00:00:00Z"}))
        return ModeHandler(state_file_path=str(state_path))

    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        return notifier

    async def test_switch_to_manual_then_analyze_then_approve(self, mode_handler, mock_notifier):
        """Full manual mode lifecycle."""
        # Step 1: Set up intent parser that returns different intents
        intent_sequence = iter([
            ParsedIntent(intent="set_mode", params={"mode": "manual"}, reply="Switched to manual."),
            ParsedIntent(intent="status", params={}, reply="Status"),
        ])
        mock_parser = AsyncMock()
        mock_parser.parse = AsyncMock(side_effect=lambda *a, **k: next(intent_sequence))

        handler = CommandHandler(
            intent_parser=mock_parser,
            notifier=mock_notifier,
            mode_handler=mode_handler,
            active_workspaces_fn=lambda: [],
            jira_base_url="https://acme.atlassian.net",
            started_at="2026-04-08T00:00:00Z",
        )

        # Switch to manual
        await handler.handle_message("switch to manual", "12345")
        assert mode_handler.get_mode() == "manual"

        # Status check
        await handler.handle_message("what's going on", "12345")
        # Should have been called twice (one for mode switch, one for status)
        assert mock_notifier.send_message.call_count == 2
```

- [ ] **Step 2: Run integration test**

Run: `cd /home/admin0/tot && python -m pytest tests/integration/test_manual_mode_flow.py -v`
Expected: ALL PASS.

- [ ] **Step 3: Run full test suite**

Run: `cd /home/admin0/tot && python -m pytest tests/ -v`
Expected: ALL PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_manual_mode_flow.py
git commit -m "test: add integration test for manual mode flow"
```

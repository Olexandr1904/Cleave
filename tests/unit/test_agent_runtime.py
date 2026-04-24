"""Tests for orchestrator/agent_runtime.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.resource_registry import discover_resources
from integrations.llm.llm_interface import LLMResponse, ToolUseRequest
from orchestrator.agent_runtime import AgentRuntime, HARD_SAFETY_RULES
from workspace.workspace import Workspace, WorkspaceState

PROJECT_ROOT = str(Path(__file__).parent.parent.parent)


@pytest.fixture
def registry():
    return discover_resources(PROJECT_ROOT)


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.send_message.return_value = LLMResponse(
        content="Agent output: implemented login feature",
        input_tokens=1000,
        output_tokens=500,
        model="claude-sonnet-4-5",
    )
    return llm


@pytest.fixture
def workspace(tmp_path):
    ws_root = tmp_path / "test-ws"
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source" / "reports").mkdir(parents=True)

    state = WorkspaceState(
        ticket_id="TEST-42",
        company_id="test-project",
        repo_id="test-repo",
        workspace_root=str(ws_root),
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


class TestAssemblePrompt:
    def test_includes_agent_body(self, registry, mock_llm, workspace):
        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "Dev Agent" in prompt
        assert "Hard Rules" in prompt

    def test_includes_safety_rules(self, registry, mock_llm, workspace):
        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "HARD SAFETY RULES" in prompt
        assert "NEVER modify architecture rules" in prompt

    def test_variable_substitution(self, registry, mock_llm, workspace):
        """AC4 (4.1): Template variables are replaced."""
        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace, {"ticket_id": "TEST-42"})

        # The {ticket_id} in the prompt body should be replaced
        assert "{ticket_id}" not in prompt or "TEST-42" in prompt

    def test_includes_context_files(self, registry, mock_llm, workspace):
        """AC3 (4.2): Workspace context files are injected."""
        (workspace.meta_dir / "ticket.json").write_text('{"id": "TEST-42"}')

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert '<context file="ticket.json">' in prompt
        assert '"TEST-42"' in prompt

    def test_includes_operator_profile(self, registry, mock_llm, workspace):
        """AC1 (4.2): Operator profile is injected."""
        runtime = AgentRuntime(registry, mock_llm, operator_profile="Role: Tech Lead")
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "Operator Profile" in prompt
        assert "Tech Lead" in prompt


class TestExecute:
    async def test_successful_execution(self, registry, mock_llm, workspace):
        """AC3 (4.3): Response written to workspace context."""
        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is True
        assert result.agent_id == "dev-agent"
        assert result.input_tokens == 1000
        assert result.output_tokens == 500
        assert result.duration_seconds > 0

        # Output written to reports
        output_file = workspace.reports_dir / "dev-agent-output.md"
        assert output_file.exists()
        assert "login feature" in output_file.read_text()

    async def test_execution_log(self, registry, mock_llm, workspace):
        """AC4 (4.3): Execution logged to workspace logs."""
        runtime = AgentRuntime(registry, mock_llm)
        await runtime.execute("dev-agent", workspace)

        log_file = workspace.logs_dir / "dev-agent.log"
        assert log_file.exists()
        log_content = log_file.read_text()
        assert "input_tokens=1000" in log_content
        assert "output_tokens=500" in log_content

    async def test_unknown_agent(self, registry, mock_llm, workspace):
        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute("nonexistent-agent", workspace)

        assert result.success is False
        assert "not found" in result.error

    async def test_llm_error(self, registry, mock_llm, workspace):
        """AC5 (4.3): API errors handled gracefully."""
        mock_llm.send_message.side_effect = Exception("API timeout")

        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is False
        assert "API timeout" in result.error


class TestToolUseExecution:
    """Test the tool_use multi-turn loop."""

    async def test_agent_with_tools_creates_sandbox(self, registry, mock_llm, workspace):
        """Agent with tools in metadata triggers tool_use flow."""
        # Mock LLM to return a tool_use request, then a final text response
        tool_response = LLMResponse(
            content="",
            input_tokens=500,
            output_tokens=200,
            model="claude-sonnet-4-5",
            tool_use=[
                ToolUseRequest(id="call_1", name="read_file", input={"path": "main.py"}),
            ],
            stop_reason="tool_use",
        )
        final_response = LLMResponse(
            content="I read the file and it contains a print statement.",
            input_tokens=800,
            output_tokens=100,
            model="claude-sonnet-4-5",
            stop_reason="end_turn",
        )
        mock_llm.send_message.return_value = tool_response
        mock_llm.send_tool_results.return_value = final_response

        # Create a source file for the sandbox to read
        (workspace.source_dir / "main.py").write_text("print('hello')\n")

        # Create a mock agent with tools in metadata
        from config.resource_registry import AgentEntry
        agent = AgentEntry(
            id="test-tool-agent",
            name="Test Tool Agent",
            resource_type="agents",
            file_path=str(Path(PROJECT_ROOT) / "agents" / "ba-agent.md"),
            metadata={"tools": ["read_file", "list_directory", "search_code"]},
        )
        registry.add("agents", agent)

        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute("test-tool-agent", workspace)

        assert result.success is True
        assert result.tool_calls == 1
        assert result.tool_rounds == 1
        assert result.input_tokens == 1300  # 500 + 800
        assert result.output_tokens == 300  # 200 + 100

        # Final output written
        output_path = workspace.reports_dir / "test-tool-agent-output.md"
        assert output_path.exists()
        assert "print statement" in output_path.read_text()

        # Tool log written
        tool_log = workspace.logs_dir / "test-tool-agent-tools.log"
        assert tool_log.exists()
        assert "read_file" in tool_log.read_text()

    async def test_max_tool_rounds_limit(self, registry, mock_llm, workspace):
        """Agent stops after max_tool_rounds even if LLM keeps requesting tools."""
        tool_response = LLMResponse(
            content="",
            input_tokens=100,
            output_tokens=50,
            model="claude-sonnet-4-5",
            tool_use=[
                ToolUseRequest(id="call_1", name="read_file", input={"path": "main.py"}),
            ],
            stop_reason="tool_use",
        )
        mock_llm.send_message.return_value = tool_response
        mock_llm.send_tool_results.return_value = tool_response  # keeps requesting tools

        (workspace.source_dir / "main.py").write_text("x = 1\n")

        from config.resource_registry import AgentEntry
        agent = AgentEntry(
            id="loop-agent",
            name="Loop Agent",
            resource_type="agents",
            file_path=str(Path(PROJECT_ROOT) / "agents" / "ba-agent.md"),
            metadata={"tools": ["read_file"]},
        )
        registry.add("agents", agent)

        runtime = AgentRuntime(registry, mock_llm, max_tool_rounds=3)
        result = await runtime.execute("loop-agent", workspace)

        assert result.success is True
        assert result.tool_rounds == 3
        assert result.tool_calls == 3  # one per round

    async def test_tool_error_sent_back_to_llm(self, registry, mock_llm, workspace):
        """Tool errors are sent back to LLM as error results."""
        tool_response = LLMResponse(
            content="",
            input_tokens=100,
            output_tokens=50,
            model="claude-sonnet-4-5",
            tool_use=[
                ToolUseRequest(id="call_1", name="read_file", input={"path": "nonexistent.py"}),
            ],
            stop_reason="tool_use",
        )
        final_response = LLMResponse(
            content="The file was not found.",
            input_tokens=200,
            output_tokens=30,
            model="claude-sonnet-4-5",
            stop_reason="end_turn",
        )
        mock_llm.send_message.return_value = tool_response
        mock_llm.send_tool_results.return_value = final_response

        from config.resource_registry import AgentEntry
        agent = AgentEntry(
            id="err-agent",
            name="Error Agent",
            resource_type="agents",
            file_path=str(Path(PROJECT_ROOT) / "agents" / "ba-agent.md"),
            metadata={"tools": ["read_file"]},
        )
        registry.add("agents", agent)

        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute("err-agent", workspace)

        assert result.success is True  # Agent itself succeeds
        assert "not found" in result.output

        # Verify the error was sent back to LLM via send_tool_results
        tool_result_call = mock_llm.send_tool_results.call_args
        messages = tool_result_call[1]["messages"] if "messages" in tool_result_call[1] else tool_result_call[0][0]
        # Find the user message with tool results (second-to-last, before final assistant)
        user_msgs = [m for m in messages if m["role"] == "user"]
        # The last user message (index -1) contains the tool results
        tool_result_msg = user_msgs[-1]
        assert tool_result_msg["content"][0]["is_error"] is True

    async def test_no_tools_uses_simple_path(self, registry, mock_llm, workspace):
        """Agent without tools uses simple single-call execution."""
        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is True
        assert result.tool_calls == 0
        assert result.tool_rounds == 0

    async def test_protected_files_passed_to_sandbox(self, registry, mock_llm, workspace):
        """Protected files from config are passed through to sandbox."""
        tool_response = LLMResponse(
            content="",
            input_tokens=100,
            output_tokens=50,
            model="claude-sonnet-4-5",
            tool_use=[
                ToolUseRequest(id="call_1", name="write_file", input={"path": "config.yaml", "content": "bad"}),
            ],
            stop_reason="tool_use",
        )
        final_response = LLMResponse(
            content="Could not write protected file.",
            input_tokens=200,
            output_tokens=30,
            model="claude-sonnet-4-5",
            stop_reason="end_turn",
        )
        mock_llm.send_message.return_value = tool_response
        mock_llm.send_tool_results.return_value = final_response

        from config.resource_registry import AgentEntry
        agent = AgentEntry(
            id="protect-agent",
            name="Protect Agent",
            resource_type="agents",
            file_path=str(Path(PROJECT_ROOT) / "agents" / "dev-agent.md"),
            metadata={"tools": ["write_file", "read_file"]},
        )
        registry.add("agents", agent)

        runtime = AgentRuntime(registry, mock_llm)
        result = await runtime.execute(
            "protect-agent", workspace, protected_files=["config.yaml"],
        )

        assert result.success is True
        # The error from protected file should have been sent back to LLM
        tool_result_call = mock_llm.send_tool_results.call_args
        messages = tool_result_call[1]["messages"] if "messages" in tool_result_call[1] else tool_result_call[0][0]
        user_msgs = [m for m in messages if m["role"] == "user"]
        tool_result_msg = user_msgs[-1]
        assert tool_result_msg["content"][0]["is_error"] is True


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

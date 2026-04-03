"""Tests for orchestrator/agent_runtime.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.resource_registry import discover_resources
from integrations.llm.llm_interface import LLMResponse
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
    (ws_root / "context").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "repo").mkdir()

    state = WorkspaceState(
        ticket_id="TEST-42",
        project_id="test-project",
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
        (workspace.context_dir / "ticket.json").write_text('{"id": "TEST-42"}')

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

        # Output written to context
        output_file = workspace.context_dir / "dev-agent-output.md"
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

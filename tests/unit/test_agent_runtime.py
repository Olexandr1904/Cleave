"""Tests for orchestrator/agent_runtime.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.resource_registry import discover_resources
from config.schemas import AgentBudget
from integrations.llm.llm_interface import LLMResponse, ToolUseRequest
from orchestrator.agent_runtime import AgentRuntime, HARD_SAFETY_RULES
from orchestrator.constants import RUNTIME_OUTPUT_DEV
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
    (ws_root / "source" / "ai_pipeline" / "TEST-42").mkdir(parents=True)

    state = WorkspaceState(
        ticket_id="TEST-42",
        company_id="test-project",
        repo_id="test-repo",
        workspace_root=str(ws_root),
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


def test_workspace_state_has_model_field_default_empty():
    """WorkspaceState exposes a `model` field defaulting to empty string.

    Empty string means: no per-ticket override; agent_runtime falls back to
    agent frontmatter / global default.
    """
    state = WorkspaceState(
        ticket_id="TEST-1",
        company_id="p",
        repo_id="r",
        workspace_root="/tmp/x",
    )
    assert state.model == ""


def test_workspace_state_model_field_settable():
    """The model field can be set to a Claude model id."""
    state = WorkspaceState(
        ticket_id="TEST-1",
        company_id="p",
        repo_id="r",
        workspace_root="/tmp/x",
        model="claude-opus-4-7",
    )
    assert state.model == "claude-opus-4-7"


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

    def test_attachments_manifest_lists_text_files_by_path(
        self, registry, mock_llm, workspace
    ):
        """Attachments are listed in a manifest by absolute path, not inlined
        as truncated bytes — a large crash log truncated to 5 KB is useless."""
        attachments_dir = workspace.meta_dir / "attachments"
        attachments_dir.mkdir()
        crash = attachments_dir / "crash.logcat"
        crash.write_text("FATAL: NullPointerException at Foo.kt:42\n" * 500)

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "## Ticket Attachments" in prompt
        assert str(crash) in prompt
        assert "text" in prompt

    def test_attachments_manifest_lists_images(
        self, registry, mock_llm, workspace
    ):
        """Image attachments appear in the manifest (the CLI Read tool can
        view them) instead of being silently dropped."""
        attachments_dir = workspace.meta_dir / "attachments"
        attachments_dir.mkdir()
        png = attachments_dir / "screenshot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\xfd")

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert str(png) in prompt
        assert "image" in prompt

    def test_no_manifest_when_no_attachments(
        self, registry, mock_llm, workspace
    ):
        """No attachments dir -> no manifest section."""
        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "## Ticket Attachments" not in prompt

    def test_truncated_meta_file_gets_full_path_pointer(
        self, registry, mock_llm, workspace
    ):
        """When an inlined meta file is truncated, the agent is told where the
        full file lives so it can read the rest with its tools."""
        from orchestrator.agent_runtime import _PER_FILE_CONTEXT_BYTES

        big = workspace.meta_dir / "comments.md"
        big.write_text("x" * (_PER_FILE_CONTEXT_BYTES + 1000))

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert f"full file at {big}" in prompt

    def test_total_context_budget_caps_many_files(self, registry, mock_llm, workspace):
        """Many context files are truncated at the total budget, not unbounded.

        Without the cap, N files of `_PER_FILE_CONTEXT_BYTES` each could blow
        past the model's prompt window silently.
        """
        from orchestrator.agent_runtime import _PER_FILE_CONTEXT_BYTES, _TOTAL_CONTEXT_BYTES

        # Write enough files to overshoot the total budget by ~3x. Use a
        # distinctive payload so we can count only context-file bytes (the
        # agent body and safety rules have no 'Z's).
        marker = "Z"
        files_needed = (_TOTAL_CONTEXT_BYTES // _PER_FILE_CONTEXT_BYTES) * 3
        for i in range(files_needed):
            (workspace.meta_dir / f"ctx_{i:03d}.txt").write_text(marker * _PER_FILE_CONTEXT_BYTES)

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        # Total context payload is bounded by the budget (no slack — the
        # implementation enforces a hard ceiling).
        assert prompt.count(marker) <= _TOTAL_CONTEXT_BYTES

    # --- Operator replies: the one voice that may lift hard rules 2 and 3 ---
    # On ACME-15333 the operator answered "yes, edit the workflow file" three
    # times. The BA planned the edit, dev made it, scope-guard failed it as
    # something "nothing in the ticket context can override", and dev took it
    # out again. The reply sat in the user turn beside the ticket text, and no
    # rule said what an operator's permission is worth.

    def test_operator_replies_go_to_the_system_part_not_beside_the_ticket(
        self, registry, mock_llm, workspace,
    ):
        (workspace.meta_dir / "ticket.md").write_text("# ACME-1\n\nFix the release workflow.\n")
        (workspace.meta_dir / "human-input.md").write_text(
            "\n## Human input (2026-11-19 16:02 UTC)\n\n"
            "You may edit .github/workflows/release.yml for this ticket.\n"
        )
        runtime = AgentRuntime(registry, mock_llm)
        system, user = runtime.assemble_prompt_parts(registry.get_agent("scope-guard-agent"), workspace)

        assert "You may edit .github/workflows/release.yml" in system
        assert system.index("## Operator Replies") < system.index("## HARD SAFETY RULES")
        assert "Fix the release workflow." in user
        assert "release.yml for this ticket" not in user
        assert 'file="human-input.md"' not in user

    def test_no_operator_replies_section_before_the_operator_has_replied(
        self, registry, mock_llm, workspace,
    ):
        runtime = AgentRuntime(registry, mock_llm)
        system, _ = runtime.assemble_prompt_parts(registry.get_agent("scope-guard-agent"), workspace)

        assert "## Operator Replies" not in system

    def test_long_operator_replies_keep_the_newest(self, registry, mock_llm, workspace):
        from orchestrator.agent_runtime import _OPERATOR_REPLIES_CHARS
        (workspace.meta_dir / "human-input.md").write_text(
            "the oldest reply\n" + "x" * _OPERATOR_REPLIES_CHARS + "\nthe newest reply\n"
        )
        runtime = AgentRuntime(registry, mock_llm)
        system, _ = runtime.assemble_prompt_parts(registry.get_agent("dev-agent"), workspace)

        assert "the newest reply" in system
        assert "the oldest reply" not in system

    def test_hard_rules_name_the_operator_as_the_only_exception(self):
        assert "Operator Replies" in HARD_SAFETY_RULES
        assert "Rules 2 and 3" in HARD_SAFETY_RULES

    def test_dev_is_told_where_each_stage_that_sends_work_back_reports(self, registry):
        """A FAIL routes the ticket to dev; what failed is in that stage's report. A report dev is not pointed
        at is a finding it cannot fix, and the two stages trade the ticket until the attempt cap."""
        from orchestrator.constants import STAGE_REPORT_FILE
        from orchestrator.workflow_router import load_workflow
        workflow = load_workflow(str(Path(PROJECT_ROOT) / "workflows" / "default-workflow.yaml"))
        dev_prompt = Path(registry.get_agent("dev-agent").file_path).read_text(encoding="utf-8")

        senders = [stage.id for stage in workflow.stages.values() if stage.on_fail == "dev"]
        assert senders == ["scope_check", "code_review", "qa"]
        for stage_id in senders:
            assert STAGE_REPORT_FILE[stage_id] in dev_prompt
        # The orchestrator's own findings at the qa stage, written for dev.
        assert "qa-gates.md" in dev_prompt and "qa-rejected-tests.md" in dev_prompt

    @pytest.mark.parametrize("agent_id", ["ba-agent", "dev-agent", "scope-guard-agent"])
    def test_agents_that_plan_make_or_judge_config_changes_know_the_exception(
        self, registry, mock_llm, workspace, agent_id,
    ):
        """One rule for all three: before, each improvised its own reading of an operator's yes."""
        runtime = AgentRuntime(registry, mock_llm)
        system, _ = runtime.assemble_prompt_parts(registry.get_agent(agent_id), workspace)
        own_instructions = system.split("## HARD SAFETY RULES")[0]

        assert "Operator Replies" in own_instructions

    # --- A permission given after the plan was approved ---
    # On ACME-14794 QA found the lint baseline out of date and the ticket blocked. The operator answered
    # "I allow editing config/detekt-baseline.xml" and dev made the two-line edit. Scope-guard failed it
    # twice: the exception asked for the file in the plan as well, the plan still listed it under "Files NOT
    # to Touch", and no stage between scope check and dev may amend a plan. The ticket blocked on a fix
    # nobody on its route could make.

    @pytest.mark.parametrize("agent_id", ["dev-agent", "scope-guard-agent"])
    def test_an_operators_permission_stands_without_the_plan(self, registry, mock_llm, workspace, agent_id):
        """The agent that makes the change, the one that judges it and the hard rules both are bound by."""
        runtime = AgentRuntime(registry, mock_llm)
        system, _ = runtime.assemble_prompt_parts(registry.get_agent(agent_id), workspace)
        own_instructions, hard_rules = system.split("## HARD SAFETY RULES")

        for rules in (own_instructions, hard_rules):
            assert "after the plan was approved" in rules
            assert "the question it answers" in rules  # what the change is measured against instead

    def test_scope_guard_does_not_send_an_allowed_config_file_back_for_a_plan_amendment(self, registry):
        prompt = Path(registry.get_agent("scope-guard-agent").file_path).read_text(encoding="utf-8")
        exception = prompt.split("**Operator-authorized exception")[1].split("### Step 4")[0]

        assert "checks 1, 2 and 3" in exception  # not in the plan, protected by it, a lint or CI config
        assert "Files NOT to Touch" in exception
        assert "both hold" not in exception  # the plan listing the file was the second condition


    def test_the_qa_agent_reads_the_gate_commands_in_its_own_instructions(self, registry, mock_llm, workspace):
        """It was told to run "`testing.run_command` from repo config": a config it is never shown."""
        runtime = AgentRuntime(registry, mock_llm)
        system, _ = runtime.assemble_prompt_parts(
            registry.get_agent("qa-agent"), workspace, {"quality_gates": "- tests (hard gate): `make test`"},
        )

        assert "- tests (hard gate): `make test`" in system
        assert "from repo config" not in system


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
        output_file = workspace.reports_dir / RUNTIME_OUTPUT_DEV
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

    async def test_token_budget_stops_loop(self, registry, mock_llm, workspace):
        """Total-token budget ends the tool loop before max_tool_rounds."""
        tool_response = LLMResponse(
            content="",
            input_tokens=600,
            output_tokens=400,  # 1000 tokens per round
            model="claude-sonnet-4-5",
            tool_use=[
                ToolUseRequest(id="c", name="read_file", input={"path": "main.py"}),
            ],
            stop_reason="tool_use",
        )
        mock_llm.send_message.return_value = tool_response
        mock_llm.send_tool_results.return_value = tool_response

        (workspace.source_dir / "main.py").write_text("x = 1\n")

        from config.resource_registry import AgentEntry
        agent = AgentEntry(
            id="token-budget-agent",
            name="Token Budget Agent",
            resource_type="agents",
            file_path=str(Path(PROJECT_ROOT) / "agents" / "ba-agent.md"),
            metadata={"tools": ["read_file"]},
        )
        registry.add("agents", agent)

        # Budget allows ~3 rounds before tokens exceed 2500 (first call adds
        # 1000, then each round adds 1000). Round cap is high so token cap
        # is what trips first.
        budget = AgentBudget(
            max_tool_rounds=100, wall_clock_seconds=60, max_total_tokens=2500,
        )
        runtime = AgentRuntime(registry, mock_llm, default_budget=budget)
        result = await runtime.execute("token-budget-agent", workspace)

        assert result.success is False
        assert result.failure_kind == "permanent"
        assert result.error is not None
        assert result.error.startswith("token_budget_exceeded")
        # We expect to break before hitting 100 rounds
        assert result.tool_rounds < 100

    async def test_per_agent_budget_override(self, registry, mock_llm, workspace):
        """Overrides keyed by agent id beat the default budget."""
        tool_response = LLMResponse(
            content="",
            input_tokens=100, output_tokens=50,
            model="claude-sonnet-4-5",
            tool_use=[ToolUseRequest(id="c", name="read_file", input={"path": "m.py"})],
            stop_reason="tool_use",
        )
        mock_llm.send_message.return_value = tool_response
        mock_llm.send_tool_results.return_value = tool_response

        (workspace.source_dir / "m.py").write_text("x = 1\n")

        from config.resource_registry import AgentEntry
        agent = AgentEntry(
            id="override-agent",
            name="Override Agent",
            resource_type="agents",
            file_path=str(Path(PROJECT_ROOT) / "agents" / "ba-agent.md"),
            metadata={"tools": ["read_file"]},
        )
        registry.add("agents", agent)

        # Default budget would allow 25 rounds; override caps this agent at 2.
        runtime = AgentRuntime(
            registry, mock_llm,
            default_budget=AgentBudget(max_tool_rounds=25),
            budget_overrides={
                "override-agent": AgentBudget(max_tool_rounds=2),
            },
        )
        result = await runtime.execute("override-agent", workspace)

        assert result.success is False
        assert result.error == "max_tool_rounds_exhausted"
        assert result.tool_rounds == 2

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

        # Hitting the round cap with a tool call still pending means the agent
        # never produced a final answer — the run is a failure, not a success.
        assert result.success is False
        assert result.failure_kind == "permanent"
        assert result.error == "max_tool_rounds_exhausted"
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

    async def test_cli_token_overrun_marks_permanent_failure(
        self, registry, workspace
    ):
        """CLI returns successfully but tokens exceed budget — flagged as
        permanent failure so dashboards/alerting catch the cost overrun."""
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from integrations.llm.llm_interface import LLMResponse
        from orchestrator.agent_runtime import AgentRuntime

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                return LLMResponse(
                    content="all good",
                    input_tokens=4_000_000,
                    output_tokens=50_000,
                    model="claude-sonnet-4-5",
                )

        runtime = AgentRuntime(
            registry,
            StubAdapter(),
            default_budget=AgentBudget(max_total_tokens=3_000_000),
        )
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is False
        assert result.failure_kind == "permanent"
        assert result.error is not None
        assert result.error.startswith("token_budget_exceeded")
        # We still wrote the output — we already paid for it.
        assert (workspace.reports_dir / RUNTIME_OUTPUT_DEV).exists()

    async def test_cli_timed_out_sets_failure_kind_quota_for_retry(
        self, registry, workspace
    ):
        """CLI 'timed out' errors must route to the auto-retry DEFERRED path,
        not FAILED. The actual error message says 'timed out', not 'timeout'."""
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from orchestrator.agent_runtime import AgentRuntime

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                raise RuntimeError("Claude Code CLI timed out after 2400s")

        runtime = AgentRuntime(registry, StubAdapter())
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is False
        assert result.failure_kind == "quota"  # reused for auto-retry
        assert result.retry_at is not None

    async def test_cli_stalled_sets_failure_kind_quota_for_retry(
        self, registry, workspace
    ):
        """A watchdog stall kill ('Claude Code CLI stalled: no activity ...')
        is a transient hang and must auto-retry via DEFERRED, not FAILED."""
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from orchestrator.agent_runtime import AgentRuntime

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                raise RuntimeError(
                    "Claude Code CLI stalled: no activity for >1800s "
                    "with no result event (last_tool='Read')"
                )

        runtime = AgentRuntime(registry, StubAdapter())
        result = await runtime.execute("dev-agent", workspace)

        assert result.success is False
        assert result.failure_kind == "quota"  # reused for auto-retry
        assert result.retry_at is not None


class TestCliAddDirs:
    async def test_execute_cli_passes_meta_dir_as_add_dir(self, registry, workspace):
        """The CLI agent runs with cwd=source/, so meta/ must be passed as an
        add-dir or the agent cannot read attachments and comments."""
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from integrations.llm.llm_interface import LLMResponse
        from orchestrator.agent_runtime import AgentRuntime

        captured: dict = {}

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                captured.update(kwargs)
                return LLMResponse(
                    content="ok", input_tokens=10, output_tokens=5,
                    model="claude-sonnet-4-5",
                )

        runtime = AgentRuntime(registry, StubAdapter())
        await runtime.execute("dev-agent", workspace)

        assert captured.get("add_dirs") == [str(workspace.meta_dir)]


class TestModelSelection:
    """agent_runtime dispatches with workspace.state.model — the single source of truth."""

    @pytest.mark.asyncio
    async def test_dispatches_with_state_model(
        self, registry, mock_llm, tmp_path
    ):
        """Whatever is on workspace.state.model is what reaches the LLM call."""
        ws_root = tmp_path / "snap-ws"
        ws_root.mkdir()
        (ws_root / "meta").mkdir()
        (ws_root / "logs").mkdir()
        (ws_root / "source" / "ai_pipeline" / "TEST-99").mkdir(parents=True)
        state = WorkspaceState(
            ticket_id="TEST-99",
            company_id="p",
            repo_id="r",
            workspace_root=str(ws_root),
            model="claude-opus-4-7",
        )
        ws = Workspace(str(ws_root), state)
        ws.save_state()

        runtime = AgentRuntime(registry, mock_llm)
        await runtime.execute("dev-agent", ws)

        assert mock_llm.send_message.called
        call_kwargs = mock_llm.send_message.call_args.kwargs
        assert call_kwargs.get("model") == "claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_empty_state_model_passes_through_unchanged(
        self, registry, mock_llm, workspace
    ):
        """If state.model is empty (legacy workspaces), agent_runtime passes
        the empty string through. Ticket workspaces created by the orchestrator
        always have state.model set, so this path only matters for old data."""
        assert workspace.state.model == ""

        runtime = AgentRuntime(registry, mock_llm)
        await runtime.execute("dev-agent", workspace)

        assert mock_llm.send_message.called
        call_kwargs = mock_llm.send_message.call_args.kwargs
        assert call_kwargs.get("model") == ""



class TestGroundTruthBlock:
    """The changed-file list is computed by the orchestrator, not the agent."""

    def test_prompt_contains_ground_truth_changed_files(
        self, registry, mock_llm, workspace, monkeypatch,
    ):
        monkeypatch.setattr(
            "orchestrator.agent_runtime.git_diff_files_vs_base",
            lambda workspace, default_branch="": [
                "app/src/main/java/Foo.kt", "app/src/test/java/FooTest.kt",
            ],
        )
        runtime = AgentRuntime(registry, mock_llm)
        prompt = runtime.assemble_prompt(registry.get_agent("dev-agent"), workspace)

        assert "## Ground Truth" in prompt
        assert "app/src/main/java/Foo.kt" in prompt
        assert "app/src/test/java/FooTest.kt" in prompt

    def test_ground_truth_absent_when_no_changes(
        self, registry, mock_llm, workspace, monkeypatch,
    ):
        monkeypatch.setattr(
            "orchestrator.agent_runtime.git_diff_files_vs_base",
            lambda workspace, default_branch="": [],
        )
        runtime = AgentRuntime(registry, mock_llm)
        prompt = runtime.assemble_prompt(registry.get_agent("ba-agent"), workspace)

        assert "## Ground Truth" not in prompt


class TestSessionId:
    def test_session_id_is_stable_per_ticket_and_agent(self):
        from orchestrator.agent_runtime import session_id_for

        a = session_id_for("ACME-14795", "dev-agent")
        assert a == session_id_for("ACME-14795", "dev-agent")
        assert a != session_id_for("ACME-14795", "qa-agent")
        assert a != session_id_for("ACME-14796", "dev-agent")

    def test_session_id_is_a_uuid(self):
        import uuid

        from orchestrator.agent_runtime import session_id_for

        assert str(uuid.UUID(session_id_for("T-1", "dev-agent"))) == session_id_for("T-1", "dev-agent")

    @staticmethod
    async def _dispatch(registry, workspace, **execute_kwargs) -> dict:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from integrations.llm.llm_interface import LLMResponse

        captured: dict = {}

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                captured.update(kwargs)
                return LLMResponse(content="ok", input_tokens=10, output_tokens=5, model="m")

        await AgentRuntime(registry, StubAdapter()).execute("dev-agent", workspace, **execute_kwargs)
        return captured

    async def test_run_that_is_never_resumed_does_not_name_its_session(self, registry, workspace):
        """The CLI keeps a named session after the run ends and refuses a second
        --session-id with the same name, so a cold stage re-entered under the
        derived id never starts ("Session ID ... is already in use")."""
        captured = await self._dispatch(registry, workspace)

        assert captured["session_id"] == ""

    async def test_resumable_run_names_its_session(self, registry, workspace):
        from orchestrator.agent_runtime import session_id_for

        captured = await self._dispatch(registry, workspace, resumable=True)

        assert captured["session_id"] == session_id_for(workspace.state.ticket_id, "dev-agent")


class TestAgentExecutionDetailAttribution:
    """Verify agent_execution_detail events carry correct ticket and project attribution."""

    async def test_agent_execution_detail_carries_correct_ticket_id(
        self, registry, mock_llm, workspace
    ) -> None:
        """The emitted event must carry ticket_id matching the workspace's ticket_id.

        This test catches value-level regressions: if ticket_id and project_id are
        swapped, or assigned the wrong workspace fields, the test fails.
        """
        from unittest.mock import Mock

        # Create a mock event bus to capture emitted events
        mock_bus = Mock()

        runtime = AgentRuntime(registry, mock_llm, event_bus=mock_bus)
        await runtime.execute("dev-agent", workspace)

        # Find the agent_execution_detail emit call
        emit_calls = [
            call for call in mock_bus.emit.call_args_list
            if call[0][0] == "agent_execution_detail"  # event type is first positional arg
        ]
        assert len(emit_calls) == 1, f"Expected exactly 1 agent_execution_detail emit, got {len(emit_calls)}"

        # Verify the keyword arguments
        call_kwargs = emit_calls[0][1]  # Get the kwargs dict from the call
        assert call_kwargs.get("ticket_id") == workspace.state.ticket_id, (
            f"ticket_id must be {workspace.state.ticket_id}, "
            f"got {call_kwargs.get('ticket_id')}"
        )
        assert call_kwargs.get("project_id") == workspace.state.company_id, (
            f"project_id must be {workspace.state.company_id}, "
            f"got {call_kwargs.get('project_id')}"
        )

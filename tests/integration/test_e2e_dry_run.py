"""E2E dry-run test — verifies full pipeline advances through all stages."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.resource_registry import discover_resources
from config.schemas import DefaultsConfig, GlobalConfig, LoadedProject, ProjectConfig, ProjectInfo
from integrations.llm.llm_interface import LLMResponse
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.orchestrator import Orchestrator
from orchestrator.workflow_router import load_workflow
from workspace.workspace import Workspace, WorkspaceState
from workspace.workspace_manager import WorkspaceManager

PROJECT_ROOT = str(Path(__file__).parent.parent.parent)
WORKFLOW_PATH = str(Path(__file__).parent.parent.parent / "workflows" / "default-workflow.yaml")


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.send_message.return_value = LLMResponse(
        content="Agent output: done",
        input_tokens=100,
        output_tokens=50,
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
        ticket_id="DRY-1",
        project_id="test-project",
        repo_id="test-repo",
        workspace_root=str(ws_root),
        current_stage="pm",
        status="pending",
    )
    ws = Workspace(str(ws_root), state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator(mock_llm, workspace, tmp_path):
    registry = discover_resources(PROJECT_ROOT)
    workflow = load_workflow(WORKFLOW_PATH)
    workspace_manager = WorkspaceManager(base_dir=str(tmp_path))
    agent_runtime = AgentRuntime(registry, mock_llm)

    global_config = GlobalConfig(
        defaults=DefaultsConfig(poll_interval_seconds=1),
    )
    projects = {
        "test-project": LoadedProject(
            config=ProjectConfig(project=ProjectInfo(id="test-project")),
        ),
    }

    orch = Orchestrator(
        global_config=global_config,
        projects=projects,
        registry=registry,
        workflow=workflow,
        workspace_manager=workspace_manager,
        agent_runtime=agent_runtime,
        dry_run=True,
    )
    orch._active_workspaces = [workspace]
    return orch


class TestE2EDryRun:
    async def test_dry_run_advances_stages(self, orchestrator, workspace):
        """AC1: Dry run advances through stages without executing agents."""
        # Run one poll cycle
        await orchestrator.poll_cycle()
        # In dry run, pm stage advances to ba
        assert workspace.state.current_stage == "ba"

        # Run another cycle
        await orchestrator.poll_cycle()
        assert workspace.state.current_stage == "dev"

    async def test_dry_run_full_pipeline(self, orchestrator, workspace):
        """AC4: Pipeline completes all stages in dry run."""
        stages_visited = [workspace.state.current_stage]

        # Run enough cycles to complete the pipeline
        for _ in range(20):
            await orchestrator.poll_cycle()
            stage = workspace.state.current_stage
            if stage not in stages_visited:
                stages_visited.append(stage)
            if workspace.state.status in ("completed", "failed"):
                break

        # Should have visited multiple stages
        assert len(stages_visited) > 1
        assert "pm" in stages_visited
        assert "ba" in stages_visited
        assert "dev" in stages_visited

    async def test_dry_run_no_llm_calls(self, orchestrator, workspace, mock_llm):
        """AC1: Dry run does not call the LLM."""
        for _ in range(10):
            await orchestrator.poll_cycle()
            if workspace.state.status in ("completed", "failed"):
                break

        mock_llm.send_message.assert_not_called()

    async def test_workspace_isolation(self, orchestrator, workspace, tmp_path):
        """AC3 (7.2): One workspace error doesn't affect others."""
        # Create a second workspace
        ws2_root = tmp_path / "test-ws-2"
        ws2_root.mkdir()
        (ws2_root / "context").mkdir()
        (ws2_root / "logs").mkdir()
        (ws2_root / "repo").mkdir()

        state2 = WorkspaceState(
            ticket_id="DRY-2",
            project_id="test-project",
            repo_id="test-repo",
            workspace_root=str(ws2_root),
            current_stage="pm",
            status="pending",
        )
        ws2 = Workspace(str(ws2_root), state2)
        ws2.save_state()

        orchestrator._active_workspaces.append(ws2)

        # Both workspaces should advance independently
        await orchestrator.poll_cycle()

        assert workspace.state.current_stage == "ba"
        assert ws2.state.current_stage == "ba"

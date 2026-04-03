"""Tests for orchestrator/workflow_router.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.workflow_router import (
    get_next_stage,
    load_workflow,
    should_escalate,
    WorkflowDefinition,
)

WORKFLOW_PATH = str(Path(__file__).parent.parent.parent / "workflows" / "default-workflow.yaml")


@pytest.fixture
def workflow():
    return load_workflow(WORKFLOW_PATH)


class TestLoadWorkflow:
    def test_loads_default_workflow(self, workflow):
        assert workflow.id == "default"
        assert workflow.name == "Standard Ticket Pipeline"
        assert len(workflow.stages) > 0

    def test_all_stages_present(self, workflow):
        expected = {"pm", "ba", "dev", "scope_guard", "pr_create",
                    "copilot_review", "fix", "qa", "merge", "escalate", "completed"}
        assert set(workflow.stages.keys()) == expected

    def test_stage_agent_mapping(self, workflow):
        assert workflow.stages["dev"].agent == "dev-agent"
        assert workflow.stages["ba"].agent == "ba-agent"
        assert workflow.stages["pm"].agent == "pm-agent"

    def test_stage_iterations(self, workflow):
        assert workflow.stages["scope_guard"].max_iterations == 3
        assert workflow.stages["fix"].max_iterations == 3
        assert workflow.stages["qa"].max_iterations == 2

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_workflow("/nonexistent/workflow.yaml")


class TestGetNextStage:
    def test_happy_path(self, workflow):
        """AC1+AC2 (4.4): Default workflow progression."""
        assert get_next_stage("pm", workflow) == "ba"
        assert get_next_stage("ba", workflow) == "dev"
        assert get_next_stage("dev", workflow) == "scope_guard"

    def test_scope_guard_pass(self, workflow):
        """Scope guard pass goes to PR create."""
        assert get_next_stage("scope_guard", workflow, "pass") == "pr_create"

    def test_scope_guard_fail(self, workflow):
        """AC3 (4.4): Scope guard fail loops back to dev."""
        assert get_next_stage("scope_guard", workflow, "fail") == "dev"

    def test_copilot_with_comments(self, workflow):
        assert get_next_stage("copilot_review", workflow, "comments") == "fix"

    def test_copilot_no_comments(self, workflow):
        assert get_next_stage("copilot_review", workflow, "no_comments") == "qa"

    def test_merge_success(self, workflow):
        assert get_next_stage("merge", workflow, "success") == "completed"

    def test_merge_conflict(self, workflow):
        assert get_next_stage("merge", workflow, "conflict") == "escalate"

    def test_max_iterations_escalates(self, workflow):
        """AC4 (4.4): Max iterations triggers escalation."""
        assert get_next_stage("scope_guard", workflow, "max_iterations") == "escalate"
        assert get_next_stage("fix", workflow, "max_iterations") == "escalate"
        assert get_next_stage("qa", workflow, "max_iterations") == "escalate"

    def test_escalate_reply_resumes(self, workflow):
        """AC6 (4.4): Human reply resumes previous stage."""
        assert get_next_stage("escalate", workflow, "reply") == "resume_previous"

    def test_unknown_stage_returns_none(self, workflow):
        assert get_next_stage("nonexistent", workflow) is None


class TestShouldEscalate:
    def test_under_cap(self, workflow):
        assert should_escalate("scope_guard", workflow, 2) is False

    def test_at_cap(self, workflow):
        """At max_iterations, should escalate."""
        assert should_escalate("scope_guard", workflow, 3) is True

    def test_over_cap(self, workflow):
        assert should_escalate("scope_guard", workflow, 5) is True

    def test_no_cap(self, workflow):
        """Stages without max_iterations never escalate."""
        assert should_escalate("dev", workflow, 100) is False

    def test_unknown_stage(self, workflow):
        assert should_escalate("nonexistent", workflow, 100) is False

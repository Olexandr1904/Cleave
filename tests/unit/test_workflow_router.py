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
        expected = {"analysis", "dev", "scope_check", "qa", "push",
                    "pr_review", "escalate", "done"}
        assert set(workflow.stages.keys()) == expected

    def test_stage_agent_mapping(self, workflow):
        assert workflow.stages["dev"].agent == "dev-agent"
        assert workflow.stages["analysis"].agent == "ba-agent"
        assert workflow.stages["scope_check"].agent == "scope-guard-agent"
        assert workflow.stages["qa"].agent == "qa-agent"

    def test_stage_action_mapping(self, workflow):
        assert workflow.stages["push"].action == "push_and_open_pr"
        assert workflow.stages["pr_review"].action == "fetch_pr_comments"
        assert workflow.stages["escalate"].action == "notify_human"
        assert workflow.stages["done"].action == "finalize"

    def test_stage_iterations(self, workflow):
        assert workflow.stages["scope_check"].max_iterations == 2
        assert workflow.stages["qa"].max_iterations == 2
        assert workflow.stages["pr_review"].max_iterations == 3

    def test_delay_minutes(self, workflow):
        assert workflow.stages["pr_review"].delay_minutes == 30
        assert workflow.stages["dev"].delay_minutes == 0

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_workflow("/nonexistent/workflow.yaml")


class TestGetNextStage:
    def test_happy_path(self, workflow):
        """Default workflow progression."""
        assert get_next_stage("analysis", workflow) == "dev"
        assert get_next_stage("dev", workflow) == "scope_check"

    def test_scope_check_pass(self, workflow):
        assert get_next_stage("scope_check", workflow, "pass") == "qa"

    def test_scope_check_fail(self, workflow):
        """Scope check fail loops back to dev."""
        assert get_next_stage("scope_check", workflow, "fail") == "dev"

    def test_qa_pass(self, workflow):
        assert get_next_stage("qa", workflow, "pass") == "push"

    def test_qa_fail(self, workflow):
        assert get_next_stage("qa", workflow, "fail") == "dev"

    def test_push_to_pr_review(self, workflow):
        assert get_next_stage("push", workflow) == "pr_review"

    def test_pr_review_fix_required(self, workflow):
        assert get_next_stage("pr_review", workflow, "fix_required") == "dev"

    def test_pr_review_done(self, workflow):
        assert get_next_stage("pr_review", workflow, "done") == "done"

    def test_max_iterations_escalates(self, workflow):
        assert get_next_stage("scope_check", workflow, "max_iterations") == "escalate"
        assert get_next_stage("qa", workflow, "max_iterations") == "escalate"
        assert get_next_stage("pr_review", workflow, "max_iterations") == "escalate"

    def test_escalate_reply_resumes(self, workflow):
        assert get_next_stage("escalate", workflow, "reply") == "resume_previous"

    def test_analysis_unclear(self, workflow):
        assert get_next_stage("analysis", workflow, "unclear") == "escalate"

    def test_unknown_stage_returns_none(self, workflow):
        assert get_next_stage("nonexistent", workflow) is None


class TestShouldEscalate:
    def test_under_cap(self, workflow):
        assert should_escalate("scope_check", workflow, 1) is False

    def test_at_cap(self, workflow):
        assert should_escalate("scope_check", workflow, 2) is True

    def test_over_cap(self, workflow):
        assert should_escalate("scope_check", workflow, 5) is True

    def test_no_cap(self, workflow):
        """Stages without max_iterations never escalate."""
        assert should_escalate("push", workflow, 100) is False

    def test_unknown_stage(self, workflow):
        assert should_escalate("nonexistent", workflow, 100) is False


class TestAwaitingApprovalState:
    """AWAITING_APPROVAL is an orchestrator-level state with no workflow stage."""

    def test_no_workflow_stage_for_awaiting_approval(self, workflow):
        assert "awaiting_approval" not in workflow.stages

    def test_get_next_stage_returns_none_for_awaiting_approval(self, workflow):
        assert get_next_stage("awaiting_approval", workflow) is None

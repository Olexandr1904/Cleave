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
        expected = {"analysis", "plan_review", "dev", "scope_check", "code_review", "qa",
                    "push", "pr_review", "escalate", "done"}
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
        """Default workflow progression — analysis now hands off to plan_review."""
        assert get_next_stage("analysis", workflow) == "plan_review"
        assert get_next_stage("plan_review", workflow, "pass") == "dev"
        assert get_next_stage("dev", workflow) == "scope_check"

    def test_plan_review_bounces_to_analysis(self, workflow):
        """A defective plan goes back to the BA, not forward to dev."""
        assert get_next_stage("plan_review", workflow, "fail") == "analysis"

    def test_plan_review_escalates_on_cap(self, workflow):
        assert workflow.stages["plan_review"].max_iterations == 2
        assert get_next_stage("plan_review", workflow, "max_iterations") == "escalate"

    def test_plan_review_is_a_cold_judge(self, workflow):
        """A judge that resumes the author's session inherits the author's excuses."""
        assert workflow.stages["plan_review"].resume_from_previous is False

    def test_plan_review_agent_mapping(self, workflow):
        assert workflow.stages["plan_review"].agent == "plan-review-agent"

    def test_scope_check_pass(self, workflow):
        """Scope check pass now hands off to code_review, which owns correctness."""
        assert get_next_stage("scope_check", workflow, "pass") == "code_review"

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


class TestCodeReviewStage:
    """A stage that owns correctness, between scope_check (boundaries) and qa.

    On PR #1509 Copilot found seven behavioural defects after the PR opened —
    races, swallowed cancellation, an uncaught exception in a goAsync window —
    while every internal gate was green. No stage was looking for them.
    """

    def test_scope_check_pass_goes_to_code_review(self, workflow):
        assert get_next_stage("scope_check", workflow, "pass") == "code_review"

    def test_code_review_pass_goes_to_qa(self, workflow):
        assert get_next_stage("code_review", workflow, "pass") == "qa"

    def test_code_review_fail_goes_back_to_dev(self, workflow):
        assert get_next_stage("code_review", workflow, "fail") == "dev"

    def test_code_review_uses_its_own_agent(self, workflow):
        assert workflow.stages["code_review"].agent == "code-review-agent"


class TestResumeFlag:
    def test_dev_opts_into_resume(self, workflow):
        assert workflow.stages["dev"].resume_from_previous is True

    def test_judging_stages_start_fresh(self, workflow):
        """scope_check and code_review must judge with a clean head."""
        assert workflow.stages["scope_check"].resume_from_previous is False
        assert workflow.stages["code_review"].resume_from_previous is False
        assert workflow.stages["analysis"].resume_from_previous is False


def test_workflow_description_names_every_agent_stage() -> None:
    """The description is the one-line answer to "what does this pipeline
    do". It silently omitted plan_review and code_review, both of which are
    full agent stages that bounce work backwards."""
    workflow = load_workflow(WORKFLOW_PATH)
    described = workflow.description.lower()
    missing = [
        stage_id for stage_id, stage in workflow.stages.items()
        if stage.agent and stage_id.replace("_", " ") not in described
    ]
    assert missing == [], f"not named in the workflow description: {missing}"

"""Workflow router — determines which agent to invoke next based on workspace state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class StageDefinition:
    """A single stage in a workflow."""
    id: str
    agent: str = ""
    action: str = ""
    description: str = ""
    next: str = ""
    on_pass: str = ""
    on_fail: str = ""
    on_unclear: str = ""
    on_comments: str = ""
    on_no_comments: str = ""
    on_success: str = ""
    on_conflict: str = ""
    on_reply: str = ""
    on_max_iterations: str = ""
    max_iterations: int = 0


@dataclass
class WorkflowDefinition:
    """A complete workflow definition."""
    id: str = ""
    name: str = ""
    description: str = ""
    stages: dict[str, StageDefinition] = field(default_factory=dict)


def load_workflow(workflow_path: str) -> WorkflowDefinition:
    """Load a workflow definition from a YAML file."""
    path = Path(workflow_path)
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {workflow_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    workflow_meta = data.get("workflow", {})
    stages_data = data.get("stages", [])

    stages: dict[str, StageDefinition] = {}
    for stage_data in stages_data:
        stage_id = stage_data["id"]
        stages[stage_id] = StageDefinition(
            id=stage_id,
            agent=stage_data.get("agent", ""),
            action=stage_data.get("action", ""),
            description=stage_data.get("description", ""),
            next=stage_data.get("next", ""),
            on_pass=stage_data.get("on_pass", ""),
            on_fail=stage_data.get("on_fail", ""),
            on_unclear=stage_data.get("on_unclear", ""),
            on_comments=stage_data.get("on_comments", ""),
            on_no_comments=stage_data.get("on_no_comments", ""),
            on_success=stage_data.get("on_success", ""),
            on_conflict=stage_data.get("on_conflict", ""),
            on_reply=stage_data.get("on_reply", ""),
            on_max_iterations=stage_data.get("on_max_iterations", ""),
            max_iterations=stage_data.get("max_iterations", 0),
        )

    return WorkflowDefinition(
        id=workflow_meta.get("id", ""),
        name=workflow_meta.get("name", ""),
        description=workflow_meta.get("description", ""),
        stages=stages,
    )


def get_next_stage(
    current_stage: str,
    workflow: WorkflowDefinition,
    outcome: str = "default",
) -> str | None:
    """Determine the next stage based on current stage and outcome.

    Args:
        current_stage: Current stage id.
        workflow: The workflow definition.
        outcome: The outcome of the current stage:
            "default" — normal completion, use 'next'
            "pass" — scope guard passed
            "fail" — scope guard / QA failed
            "unclear" — BA found unclear requirements
            "comments" — Copilot has valid comments
            "no_comments" — no valid Copilot comments
            "success" — merge succeeded
            "conflict" — merge conflict
            "max_iterations" — iteration cap reached
            "reply" — human replied

    Returns:
        Next stage id, or None if workflow is complete.
    """
    stage = workflow.stages.get(current_stage)
    if stage is None:
        logger.warning("Unknown stage: %s", current_stage)
        return None

    # Map outcome to the appropriate transition field
    outcome_map = {
        "default": stage.next,
        "pass": stage.on_pass or stage.next,
        "fail": stage.on_fail,
        "unclear": stage.on_unclear,
        "comments": stage.on_comments,
        "no_comments": stage.on_no_comments,
        "success": stage.on_success,
        "conflict": stage.on_conflict,
        "max_iterations": stage.on_max_iterations,
        "reply": stage.on_reply,
    }

    next_stage = outcome_map.get(outcome, stage.next)

    if not next_stage:
        # Fallback to 'next' if specific outcome isn't defined
        next_stage = stage.next

    return next_stage if next_stage else None


def should_escalate(
    current_stage: str,
    workflow: WorkflowDefinition,
    iterations: int,
) -> bool:
    """Check if the current stage has exceeded its iteration cap."""
    stage = workflow.stages.get(current_stage)
    if stage is None or stage.max_iterations == 0:
        return False
    return iterations >= stage.max_iterations

"""Agent stage execution: dispatch agent, verify, parse outcome, advance.

The biggest single function in the pipeline. Decides which agent to
invoke, handles success/failure/quota, runs post-stage verification,
parses agent outcome, and advances or escalates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from config.schemas import RepoConfig
from integrations.base.notifier import NotifierInterface
from orchestrator import stage_verifier, tg_format
from orchestrator.agent_runtime import AgentRuntime
from orchestrator.approval_gate import should_approval_gate
from orchestrator.constants import REPORT_BA, STAGE_RUNTIME_OUTPUT
from orchestrator.escalation import handle_escalate
from orchestrator.git_ops import git_head_sha
from orchestrator.workflow_router import WorkflowDefinition, get_next_stage
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_RETRY_DELAY = timedelta(hours=1)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _emit(event_bus: Any | None, event_type: str, message: str, **kwargs: Any) -> None:
    if event_bus is not None:
        event_bus.emit(event_type, message, **kwargs)


def _log_pipeline(workspace: Workspace, entry: str) -> None:
    """Append a timestamped entry to ai_pipeline/<ticket>/pipeline-log.md."""
    log_path = workspace.reports_dir / "pipeline-log.md"
    timestamp = datetime.now(timezone.utc).strftime("%H:%M")
    line = f"- **{timestamp}** {entry}\n"
    if log_path.exists():
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    else:
        log_path.write_text(
            f"# Pipeline Log — {workspace.state.ticket_id}\n\n{line}",
            encoding="utf-8",
        )


def _looks_like_pass(text: str) -> bool:
    """Check if agent output indicates a pass verdict."""
    return any(m in text for m in (
        "status: pass", "verdict: pass", "all gates passed",
        "qa pass", "qa complete", "verdict: hold",
        "scope audit complete. verdict: **pass",
        "scope audit complete. **status: pass",
        "advances to qa", "advances to",
        "merge as-is",
    ))


def _looks_like_fail(text: str) -> bool:
    """Check if agent output indicates a fail verdict."""
    return any(m in text for m in (
        "status: fail", "verdict: fail", "verdict: **fail",
        "status: blocked",
    ))


def rollback_iteration(workspace: Workspace, stage_id: str) -> None:
    """Undo the iteration counter increment for an aborted stage run.

    Used when a quota failure preempts the agent before it produced output —
    the stage should not consume one of its retry budget slots.
    """
    state = workspace.state
    current = state.stage_iterations.get(stage_id, 0)
    if current > 0:
        state.stage_iterations[stage_id] = current - 1
        workspace.save_state()


def parse_agent_outcome(stage_id: str, output: str, workspace: Workspace) -> str:
    """Parse agent output to determine outcome for routing."""
    output_lower = output.lower()

    if stage_id == "analysis":
        ba_plan = workspace.reports_dir / REPORT_BA
        # If ba.md exists, analysis is done — proceed regardless of keywords
        if ba_plan.exists():
            return "default"
        # No ba.md — check if agent is asking questions
        if "unclear" in output_lower or "questions" in output_lower:
            return "unclear"
        logger.warning(
            "%s: BA completed but ba.md missing in ai_pipeline/ — treating as unclear",
            workspace.state.ticket_id,
        )
        return "unclear"

    if stage_id in ("scope_check", "qa"):
        runtime_name = STAGE_RUNTIME_OUTPUT.get(stage_id)
        if runtime_name:
            report = workspace.reports_dir / runtime_name
            if report.exists():
                content = report.read_text().lower()
                if _looks_like_pass(content):
                    return "pass"
                if _looks_like_fail(content):
                    return "fail"
        if _looks_like_pass(output_lower):
            return "pass"
        return "fail"

    return "default"


async def handle_agent_stage(
    workspace: Workspace,
    stage_id: str,
    stage_def: Any,
    *,
    workflow: WorkflowDefinition,
    agent_runtime: AgentRuntime,
    repo_config: RepoConfig | None,
    notifier: NotifierInterface | None,
    mode_handler: Any,
    get_chat_id: Callable[[Workspace], str],
    dry_run: bool,
    event_bus: Any | None,
    advance_to_stage_fn: Callable[[Workspace, str], None],
    on_ticket_done_fn: Callable[[Workspace], Awaitable[None]],
    build_gate_summary_fn: Callable[[Workspace, str], tuple],
    notify_verification_blocked_fn: Callable[[Workspace, str, str], Awaitable[None]],
    notify_deferred_fn: Callable[..., Awaitable[None]],
    notify_failed_fn: Callable[[Workspace, str], Awaitable[None]],
) -> None:
    """Execute an agent stage."""
    state = workspace.state

    if dry_run:
        logger.info(
            "[DRY RUN] Would execute agent '%s' for %s",
            stage_def.agent, state.ticket_id,
        )
        next_stage = get_next_stage(stage_id, workflow)
        if next_stage:
            advance_to_stage_fn(workspace, next_stage)
        return

    workspace.increment_iteration(stage_id)
    stage_start_commit = stage_verifier.capture_stage_start(workspace, stage_id)

    protected = repo_config.architecture.protected_files if repo_config else []

    iteration_now = workspace.state.stage_iterations.get(stage_id, 0)
    max_iter = stage_def.max_iterations
    start_sha_short = (stage_start_commit or "unknown")[:8]
    logger.info(
        "Stage entry: stage=%s ticket=%s agent=%s iteration=%d/%s "
        "model=%s start_sha=%s protected_files=%d",
        stage_id, state.ticket_id, stage_def.agent, iteration_now,
        max_iter if max_iter > 0 else "uncapped",
        getattr(workspace.state, "model", "unknown"),
        start_sha_short, len(protected),
    )

    _emit(event_bus, "agent_dispatched", f"Dispatching {stage_def.agent} for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id})
    state_before = workspace.state.current_state
    result = await agent_runtime.execute(
        stage_def.agent, workspace, protected_files=protected,
    )
    logger.info(
        "Stage exit: stage=%s ticket=%s agent=%s success=%s "
        "duration=%.1fs in_tok=%d out_tok=%d tool_calls=%d "
        "tool_rounds=%d failure_kind=%s",
        stage_id, state.ticket_id, stage_def.agent,
        getattr(result, "success", False),
        getattr(result, "duration_seconds", 0.0),
        getattr(result, "input_tokens", 0),
        getattr(result, "output_tokens", 0),
        getattr(result, "tool_calls", 0),
        getattr(result, "tool_rounds", 0),
        getattr(result, "failure_kind", None),
    )

    # Operator intervened mid-flight (Pause / Take Control / etc.).
    # Discard the agent's transitions so we don't auto-advance out of the
    # operator-chosen state.
    state_after = workspace.state.current_state
    if state_after != state_before:
        logger.info(
            "State changed during %s for %s: %s -> %s, discarding transitions",
            stage_def.agent, state.ticket_id, state_before, state_after,
        )
        return

    if not result.success:
        _emit(
            event_bus,
            "agent_failed",
            f"{stage_def.agent} failed for {state.ticket_id}: {result.error}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            agent_id=stage_def.agent,
            data={"stage": stage_id, "error": result.error},
        )
        if result.failure_kind == "quota":
            rollback_iteration(workspace, stage_id)
            retry_at = result.retry_at or (
                datetime.now(timezone.utc) + DEFAULT_QUOTA_RETRY_DELAY
            )
            workspace.transition(Stage.DEFERRED, retry_at=retry_at.isoformat())
            await notify_deferred_fn(workspace, retry_at, reason=result.error)
        else:
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=result.error)
            await notify_failed_fn(workspace, result.error or "")
        return

    _emit(event_bus, "agent_completed", f"{stage_def.agent} completed for {state.ticket_id}", project_id=state.company_id, ticket_id=state.ticket_id, agent_id=stage_def.agent, data={"stage": stage_id, "duration": result.duration_seconds, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
    sha_info = ""
    if stage_id == "dev":
        sha = git_head_sha(workspace)
        if sha != "unknown":
            sha_info = f" Commit: {sha[:8]}."
    _log_pipeline(workspace, f"{stage_id} ({stage_def.agent}) completed.{sha_info} Output: `ai_pipeline/{state.ticket_id}/{stage_def.agent}-output.md`")
    verify_result = stage_verifier.verify(
        stage_id, workspace, stage_start_commit,
        duration_seconds=result.duration_seconds,
    )
    if not verify_result.ok:
        agent_snippet = (result.output or "")[:200].replace("\n", " ")
        error_msg = f"{stage_id}: {verify_result.reason} (agent said: {agent_snippet})"
        workspace.transition(Stage.BLOCKED)
        workspace.update_state(error=error_msg)
        _log_pipeline(workspace, f"BLOCKED — {stage_id} verification failed: {verify_result.reason}")
        _emit(
            event_bus,
            "stage_verification_failed",
            f"{stage_id} verification failed for {state.ticket_id}: {verify_result.reason}",
            project_id=state.company_id, ticket_id=state.ticket_id,
            data={"stage": stage_id, "reason": verify_result.reason},
        )
        await notify_verification_blocked_fn(workspace, stage_id, verify_result.reason)
        return
    # Determine outcome from agent output
    outcome = parse_agent_outcome(stage_id, result.output, workspace)

    # Warn if QA passed but couldn't compile/test
    if stage_id == "qa" and outcome == "pass" and notifier:
        output_lower = (result.output or "").lower()
        warnings = []
        if "sdk" in output_lower and "not found" in output_lower:
            warnings.append("Android SDK not installed — build not verified")
        if "java" in output_lower and ("not found" in output_lower or "command not found" in output_lower):
            warnings.append("JDK not installed — tests not run")
        if "hold" in output_lower or "could not" in output_lower:
            if not warnings:
                warnings.append("Quality gates could not run locally")
        if warnings:
            chat_id = get_chat_id(workspace)
            if chat_id:
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                await notifier.send_message(chat_id, (
                    f"{hdr}\n"
                    f"QA passed but with warnings:\n"
                    + "\n".join(f"  • {w}" for w in warnings)
                    + f"\n\nCI on GitHub will be the authoritative gate."
                ))

    next_stage = get_next_stage(stage_id, workflow, outcome)

    # Reset scope_check bounce counter on pass so max_iterations tracks
    # consecutive failures only, not lifetime runs.
    if stage_id == "scope_check" and outcome == "pass":
        workspace.state.stage_iterations.pop("scope_check", None)
        workspace.save_state()

    if next_stage:
        # Check for approval gate in manual mode. Only gate on happy-path
        # transitions — failure loops and escalations bypass the gate.
        current_state = workspace.state.current_state
        if should_approval_gate(mode_handler, current_state, next_stage):
            workspace.transition(Stage.AWAITING_APPROVAL)
            _emit(event_bus, "approval_requested", f"Awaiting approval for {state.ticket_id} after {current_state}", project_id=state.company_id, ticket_id=state.ticket_id, data={"gate": current_state})
            if notifier:
                chat_id = get_chat_id(workspace)
                summary, buttons = build_gate_summary_fn(workspace, current_state)
                await notifier.send_message(chat_id, summary, buttons=buttons)
        elif next_stage == "escalate":
            chat_id = get_chat_id(workspace) if notifier else ""
            await handle_escalate(
                workspace, notifier, chat_id,
                workflow=workflow,
                event_bus=event_bus,
            )
        else:
            advance_to_stage_fn(workspace, next_stage)
    else:
        workspace.transition(Stage.DONE)
        _log_pipeline(workspace, f"✅ DONE. PR: {workspace.state.pr_url or 'N/A'}")
        await on_ticket_done_fn(workspace)

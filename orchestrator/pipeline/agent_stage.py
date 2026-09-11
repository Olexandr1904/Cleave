"""Agent stage execution: dispatch agent, verify, parse outcome, advance.

The biggest single function in the pipeline. Decides which agent to
invoke, handles success/failure/quota, runs post-stage verification,
parses agent outcome, and advances or escalates.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from config.schemas import RepoConfig
from integrations.base.notifier import NotifierInterface
from orchestrator import stage_verifier, tg_format
from orchestrator.agent_runtime import AgentRuntime, last_attempt_block
from orchestrator.approval_gate import should_approval_gate
from orchestrator.constants import (
    REPORT_BA,
    REPORT_BA_QUESTIONS,
    RUNTIME_OUTPUT_PLAN_REVIEW,
    STAGE_RUNTIME_OUTPUT,
)
from orchestrator.escalation import handle_escalate
from orchestrator.git_ops import git_diff_files_vs_base, git_head_sha
from orchestrator.agent_failure_summarizer import summarize_agent_failure
from orchestrator.fake_test_detector import detect_source_reading_tests
from orchestrator.quality_gates import GateResult, failing_hard_gates, run_gates
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


def _write_gate_report(workspace: Workspace, results: list[GateResult]) -> None:
    """Write the orchestrator's own gate results next to the agent reports.

    The next dev iteration reads this instead of re-deriving the gate state
    from a narrative that may have redefined what PASS means.
    """
    if not results:
        return
    lines = ["# Quality Gates (measured by orchestrator)", ""]
    for r in results:
        status = "PASS" if r.passed else f"FAIL (exit {r.exit_code})"
        kind = "hard" if r.hard_gate else "soft"
        lines.append(f"## {r.name} — {status} [{kind}]")
        lines.append(f"`{r.command}`")
        if not r.passed:
            lines.append("```")
            lines.append(r.output_tail)
            lines.append("```")
        lines.append("")
    (workspace.reports_dir / "qa-gates.md").write_text(
        "\n".join(lines), encoding="utf-8",
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


# plan_review deliberately does NOT reuse _looks_like_pass: that matcher accepts
# "advances to" and treats "verdict: hold" as a pass, and parse_agent_outcome
# checks pass before fail. A plan-review report reading "the plan advances to dev
# only if..." would be read as PASS — the exact inversion this gate exists to
# prevent. Strict line match, and anything unparseable fails closed.
# Markdown decoration around the line is tolerated — agents emit `**Status:
# PASS**` and `## Status: PASS`, and `_looks_like_fail` above already carries
# `verdict: **fail` because it was seen in the wild. The verdict token itself
# stays strict: `\b` keeps prose like "status: passing this now" from matching.
_PLAN_REVIEW_VERDICT = re.compile(
    r"^[ \t]*[*#]*[ \t]*status[ \t]*\**[ \t]*:[ \t]*\**[ \t]*(pass|fail)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_plan_review_outcome(workspace: Workspace) -> str:
    """Return 'pass' only for an explicit `Status: PASS` line; else 'fail'.

    Reads the newest attempt block only: the report file accumulates one
    appended block per run, so scanning the whole file returns the *oldest*
    verdict and a plan rejected once could never pass on re-review.
    """
    report = workspace.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW
    if not report.exists():
        return "fail"
    block = last_attempt_block(report.read_text(encoding="utf-8"))
    match = _PLAN_REVIEW_VERDICT.search(block)
    if match is None:
        return "fail"
    return "pass" if match.group(1).lower() == "pass" else "fail"


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


def parse_agent_outcome(
    stage_id: str,
    output: str,
    workspace: Workspace,
    gate_results: list[GateResult] | None = None,
) -> str:
    """Parse agent output to determine outcome for routing.

    For `qa`, configured gate exit codes win over the agent's narrative: the
    agent has written "PASS for ticket scope" over a non-zero exit (PR #1530),
    and `_looks_like_pass` is a substring match that accepts "verdict: hold".
    """
    output_lower = output.lower()

    if stage_id == "qa" and gate_results:
        return "fail" if failing_hard_gates(gate_results) else "pass"

    if stage_id == "analysis":
        # BA flagged decisions the ticket asked to align on before work starts.
        # Checked before ba.md: a plan can be complete and still rest on a
        # choice that was the author's to make (ACME-14795 shipped three).
        if (workspace.reports_dir / REPORT_BA_QUESTIONS).exists():
            return "unclear"
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

    if stage_id == "plan_review":
        return _parse_plan_review_outcome(workspace)

    if stage_id in ("scope_check", "code_review", "qa"):
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
    if stage_id == "plan_review":
        # `_write_agent_output` only appends, so on re-entry the previous
        # attempt's report is still here — which makes stage_verifier's
        # existence check stop being evidence that *this* attempt produced
        # anything, and lets a run that writes nothing be scored on the stale
        # verdict. Clear it so "no report" (the fail-closed case) stays
        # reachable. Nothing downstream needs the history: the reviewer runs
        # cold by design, and the BA reads the defect list before the
        # reviewer is re-entered.
        (workspace.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW).unlink(missing_ok=True)
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
    # Resume only on re-entry: the first run of a stage has nothing to resume,
    # and passing --resume for a session that was never created just costs a
    # failed attempt plus a cold-start retry.
    resume = bool(getattr(stage_def, "resume_from_previous", False)) and iteration_now > 1
    result = await agent_runtime.execute(
        stage_def.agent, workspace, protected_files=protected, resume=resume,
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
            summary = await summarize_agent_failure(
                workspace, stage_id,
                reason_kind="agent_failed",
                raw_error=result.error,
            )
            final_error = summary or result.error or ""
            workspace.transition(Stage.FAILED)
            workspace.update_state(error=final_error, raw_error=result.error)
            await notify_failed_fn(workspace, final_error)
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
        default_branch=getattr(getattr(repo_config, "vcs", None), "default_branch", None),
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
    # Ground truth for QA: run the repo's configured gates ourselves. Runs in a
    # thread — a gradle build blocks for minutes and would stall the event loop
    # (heartbeats, Telegram, other tickets). The qa stage already holds the
    # build semaphore, so no concurrent gradle run can start here.
    gate_results: list[GateResult] = []
    if stage_id == "qa" and repo_config is not None:
        gate_results = await asyncio.to_thread(
            run_gates, workspace.source_dir, repo_config,
        )
        _write_gate_report(workspace, gate_results)
        if gate_results:
            _log_pipeline(workspace, "qa: gates measured — " + ", ".join(
                f"{r.name}={'PASS' if r.passed else f'FAIL({r.exit_code})'}"
                for r in gate_results
            ))

    # Determine outcome from agent output
    outcome = parse_agent_outcome(stage_id, result.output, workspace, gate_results)

    # A test that greps production source cannot fail. Reject them before the
    # PR exists — 10 of the 21 test files the pipeline has written were of
    # this shape. Overrides a PASS: green gates over a fake test is not green.
    if stage_id == "qa":
        fake_tests = detect_source_reading_tests(
            workspace.source_dir, git_diff_files_vs_base(workspace),
        )
        if fake_tests:
            listing = "\n".join(f"- {p}" for p in fake_tests)
            (workspace.reports_dir / "qa-rejected-tests.md").write_text(
                "# Rejected tests\n\n"
                "These tests assert on the text of a source file instead of "
                "executing production code, so they cannot fail. Rewrite them "
                "to call the code under test, or delete them and cover the "
                "acceptance criterion another way.\n\n" + listing + "\n",
                encoding="utf-8",
            )
            _log_pipeline(
                workspace,
                f"qa: rejected {len(fake_tests)} source-reading test(s) — "
                "see qa-rejected-tests.md",
            )
            outcome = "fail"

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

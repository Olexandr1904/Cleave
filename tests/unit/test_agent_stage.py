"""Tests for agent_stage FAILED path: failure summarizer integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.agent_runtime import AgentResult
from workspace.workspace import Stage, Workspace, WorkspaceState


def _make_workspace(tmp_path, ticket_id: str, state: str = Stage.DEV) -> Workspace:
    ws_root = tmp_path / ticket_id
    ws_root.mkdir()
    (ws_root / "meta").mkdir()
    (ws_root / "reports").mkdir()
    (ws_root / "logs").mkdir()
    (ws_root / "source").mkdir()
    ws_state = WorkspaceState(
        ticket_id=ticket_id,
        company_id="acme",
        repo_id="acme-app",
        workspace_root=str(ws_root),
        current_state=state,
        stage_iterations={"dev": 1},
    )
    ws = Workspace(str(ws_root), ws_state)
    ws.save_state()
    return ws


@pytest.fixture
def orchestrator_with_stubs(tmp_path, monkeypatch):
    """Minimal Orchestrator with fakes — mirrors test_orchestrator_deferred.py."""
    from orchestrator.orchestrator import Orchestrator
    from config.config_loader import GlobalConfig

    cfg = MagicMock(spec=GlobalConfig)
    cfg.defaults = MagicMock(poll_interval_seconds=900)
    cfg.workspaces = MagicMock(base_dir=str(tmp_path), max_age_days=7)
    cfg.telegram = MagicMock(default_chat_id="chat-1")

    orch = Orchestrator.__new__(Orchestrator)
    orch._global_config = cfg
    orch._projects = {}
    orch._active_workspaces = []
    orch._workspace_manager = MagicMock()
    orch._workspace_manager.cleanup_old_workspaces = MagicMock(return_value=[])
    orch._trackers = {}
    orch._vcs = None
    orch._repo_vcs = {}
    orch._notifier = AsyncMock()
    orch._dry_run = False
    orch._mode_handler = MagicMock()
    orch._mode_handler.get_mode = MagicMock(return_value="auto")
    orch._shutdown_event = MagicMock()
    orch._recent_completions = []
    orch._quota_window_end = None

    from dashboard.events import EventBus
    orch._events = EventBus()

    orch._agent_runtime = MagicMock()
    return orch


@pytest.mark.asyncio
async def test_agent_failed_uses_summary_when_available(
    orchestrator_with_stubs, tmp_path
):
    """When summarizer returns a string, state.error is set to that string and
    state.raw_error preserves the original CLI error."""
    orch = orchestrator_with_stubs
    ws = _make_workspace(tmp_path, "T-10", state=Stage.DEV)
    orch._active_workspaces.append(ws)

    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="raw cli err", failure_kind="permanent",
        )
    )

    summary = "Reason: Kotlin daemon OOM.\nSuggested action: retry."
    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    with patch(
        "orchestrator.pipeline.agent_stage.summarize_agent_failure",
        new_callable=AsyncMock, return_value=summary,
    ) as mock_summarize:
        await orch._handle_agent_stage(ws, "dev", stage_def)

    mock_summarize.assert_awaited_once_with(
        ws, "dev", reason_kind="agent_failed", raw_error="raw cli err"
    )
    assert ws.state.current_state == Stage.FAILED
    assert ws.state.error == summary
    assert ws.state.raw_error == "raw cli err"


@pytest.mark.asyncio
async def test_agent_failed_falls_back_when_summarizer_returns_none(
    orchestrator_with_stubs, tmp_path
):
    """When summarizer returns None, state.error falls back to the raw CLI error."""
    orch = orchestrator_with_stubs
    ws = _make_workspace(tmp_path, "T-11", state=Stage.DEV)
    orch._active_workspaces.append(ws)

    orch._agent_runtime.execute = AsyncMock(
        return_value=AgentResult(
            agent_id="dev-agent", success=False, output="",
            error="raw cli err", failure_kind="permanent",
        )
    )

    stage_def = MagicMock()
    stage_def.agent = "dev-agent"
    stage_def.max_iterations = 0

    with patch(
        "orchestrator.pipeline.agent_stage.summarize_agent_failure",
        new_callable=AsyncMock, return_value=None,
    ) as mock_summarize:
        await orch._handle_agent_stage(ws, "dev", stage_def)

    mock_summarize.assert_awaited_once_with(
        ws, "dev", reason_kind="agent_failed", raw_error="raw cli err"
    )
    assert ws.state.current_state == Stage.FAILED
    assert ws.state.error == "raw cli err"
    assert ws.state.raw_error == "raw cli err"


# --- QA verdict from gate exit codes, not prose ---------------------------
# Regression cover for PR #1530, where the agent narrated "PASS for ticket
# scope" over a non-zero detektDebug exit and the pipeline advanced.

from orchestrator.pipeline.agent_stage import parse_agent_outcome  # noqa: E402
from orchestrator.quality_gates import GateResult  # noqa: E402


def _gate(name: str, exit_code: int, hard: bool = True) -> GateResult:
    return GateResult(name=name, command="x", exit_code=exit_code,
                      hard_gate=hard, output_tail="")


def test_qa_fails_when_hard_gate_fails_even_if_agent_says_pass(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-1")
    (ws.reports_dir / "qa-agent-output.md").write_text("## Status: PASS")
    outcome = parse_agent_outcome(
        "qa", "Status: PASS", ws, gate_results=[_gate("tests", 1)],
    )
    assert outcome == "fail"


def test_qa_passes_when_all_hard_gates_pass_even_if_prose_is_ambiguous(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-2")
    (ws.reports_dir / "qa-agent-output.md").write_text("verdict: hold")
    outcome = parse_agent_outcome(
        "qa", "verdict: hold", ws,
        gate_results=[_gate("tests", 0), _gate("lint", 1, hard=False)],
    )
    assert outcome == "pass"


def _gate_that_could_not_run(name: str, hard: bool = True) -> GateResult:
    return GateResult(
        name=name, command="./gradlew :app:testDebugUnitTest", exit_code=1, hard_gate=hard,
        output_tail="Cannot locate tasks that match ':app:testDebugUnitTest' as task 'testDebugUnitTest' "
                    "is ambiguous in project ':app'. Candidates are: 'testChinaDebugUnitTest', "
                    "'testGlobalDebugUnitTest'.",
    )


def test_qa_with_a_hard_gate_that_could_not_run_is_not_a_fail(tmp_path):
    """Nothing in the branch made the gate fail, so there is nothing for dev to fix."""
    ws = _make_workspace(tmp_path, "ACME-20")
    outcome = parse_agent_outcome(
        "qa", "## Status: PASS", ws, gate_results=[_gate_that_could_not_run("tests"), _gate("build", 0)],
    )
    assert outcome == "gate_could_not_run"


def test_qa_with_only_a_soft_gate_that_could_not_run_still_passes(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-21")
    outcome = parse_agent_outcome(
        "qa", "## Status: PASS", ws,
        gate_results=[_gate("tests", 0), _gate_that_could_not_run("lint", hard=False)],
    )
    assert outcome == "pass"


@pytest.mark.asyncio
async def test_qa_gate_that_could_not_run_blocks_with_the_gate_named(orchestrator_with_stubs, tmp_path):
    """The operator gets the gate, its command and the build tool's own words; dev gets nothing."""
    orch = orchestrator_with_stubs
    ws = _make_workspace(tmp_path, "ACME-22", state=Stage.QA)
    (ws.reports_dir / "qa-agent-output.md").write_text("## Status: PASS")
    orch._agent_runtime.execute = AsyncMock(return_value=AgentResult(
        agent_id="qa-agent", success=True, output="## Status: PASS",
    ))
    orch._get_repo_config = MagicMock(return_value=MagicMock())
    stage_def = MagicMock()
    stage_def.agent = "qa-agent"
    stage_def.max_iterations = 2

    with patch(
        "orchestrator.pipeline.agent_stage.run_gates",
        return_value=[_gate_that_could_not_run("tests"), _gate("build", 0)],
    ), patch("orchestrator.pipeline.agent_stage.get_next_stage") as next_stage:
        await orch._handle_agent_stage(ws, "qa", stage_def)

    next_stage.assert_not_called()
    assert ws.state.current_state == Stage.BLOCKED
    assert "gate `tests` could not run" in ws.state.error
    assert "./gradlew :app:testDebugUnitTest" in ws.state.error
    assert "is ambiguous in project" in ws.state.error


def test_qa_falls_back_to_the_agents_verdict_when_no_gates_configured(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-3")
    (ws.reports_dir / "qa.md").write_text("## Status: FAIL")
    assert parse_agent_outcome("qa", "", ws, gate_results=[]) == "fail"


def test_gate_report_records_failing_command_output(tmp_path):
    from orchestrator.pipeline.agent_stage import _write_gate_report

    ws = _make_workspace(tmp_path, "ACME-4")
    _write_gate_report(ws, [
        _gate("tests", 0),
        GateResult(name="build", command="./gradlew assembleDebug",
                   exit_code=1, hard_gate=True, output_tail="compile error here"),
    ])
    report = (ws.reports_dir / "qa-gates.md").read_text()
    assert "## tests — PASS [hard]" in report
    assert "## build — FAIL (exit 1) [hard]" in report
    assert "compile error here" in report


def test_gate_report_not_written_when_no_gates_configured(tmp_path):
    from orchestrator.pipeline.agent_stage import _write_gate_report

    ws = _make_workspace(tmp_path, "ACME-5")
    _write_gate_report(ws, [])
    assert not (ws.reports_dir / "qa-gates.md").exists()


def test_code_review_fail_verdict_routes_back_to_dev(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-6")
    (ws.reports_dir / "code-review.md").write_text("## Status: FAIL")
    assert parse_agent_outcome("code_review", "", ws) == "fail"


def test_code_review_pass_verdict_advances(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-7")
    (ws.reports_dir / "code-review.md").write_text("## Status: PASS")
    assert parse_agent_outcome("code_review", "", ws) == "pass"


def test_analysis_escalates_when_ba_writes_questions(tmp_path):
    """ACME-14795 listed three "align before implementation" questions and BA
    resolved all three itself under Assumptions."""
    ws = _make_workspace(tmp_path, "ACME-8")
    (ws.reports_dir / "ba.md").write_text("# Plan")
    (ws.reports_dir / "ba-questions.md").write_text("1. Which annotation name?")
    assert parse_agent_outcome("analysis", "", ws) == "unclear"


def test_analysis_proceeds_when_no_questions(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-9")
    (ws.reports_dir / "ba.md").write_text("# Plan")
    assert parse_agent_outcome("analysis", "", ws) == "default"


# --- analysis re-entered after a human answer: the old questions file stays ---
# A human reply re-enters analysis with the previous attempt's ba-questions.md
# still on disk (the BA reads it to know what was answered and may not delete
# files). Only a file this attempt wrote is a new question.

def _questions_written_at(ws, mtime: float):
    import os

    questions = ws.reports_dir / "ba-questions.md"
    questions.write_text("1. May the pipeline edit the release workflow?")
    os.utime(questions, (mtime, mtime))


def test_analysis_ignores_questions_an_earlier_attempt_wrote(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-10")
    (ws.reports_dir / "ba.md").write_text("# Plan")
    _questions_written_at(ws, 1_000.0)

    assert parse_agent_outcome("analysis", "", ws, started_at=2_000.0) == "default"


def test_analysis_escalates_questions_this_attempt_wrote(tmp_path):
    ws = _make_workspace(tmp_path, "ACME-11")
    (ws.reports_dir / "ba.md").write_text("# Plan")
    _questions_written_at(ws, 3_000.0)

    assert parse_agent_outcome("analysis", "", ws, started_at=2_000.0) == "unclear"


@pytest.mark.asyncio
async def test_reentered_analysis_that_asks_nothing_new_advances(tmp_path):
    """Each answer re-ran the BA, which recorded it in ba.md, and the leftover
    questions file escalated the same question again."""
    import time

    from orchestrator.pipeline.agent_stage import handle_agent_stage
    from orchestrator.workflow_router import StageDefinition, WorkflowDefinition

    ws = _make_workspace(tmp_path, "ACME-12", state=Stage.ANALYSIS)
    _questions_written_at(ws, time.time() - 3600)

    async def _execute(*args, **kwargs):
        (ws.reports_dir / "ba.md").write_text("# Plan\n\nDecision: the human allowed the edit.")
        return AgentResult(agent_id="ba-agent", success=True, output="done")

    runtime = MagicMock()
    runtime.execute = _execute
    advanced: list[str] = []
    workflow = WorkflowDefinition(
        id="t", name="t",
        stages={"analysis": StageDefinition(
            id="analysis", agent="ba-agent", next="plan_review", on_unclear="escalate",
        )},
    )

    await handle_agent_stage(
        ws, "analysis", workflow.stages["analysis"],
        workflow=workflow, agent_runtime=runtime, repo_config=None,
        notifier=None, mode_handler=None, get_chat_id=lambda w: "",
        dry_run=False, event_bus=None,
        advance_to_stage_fn=lambda w, s: advanced.append(s),
        on_ticket_done_fn=AsyncMock(),
        build_gate_summary_fn=MagicMock(return_value=("", [])),
        notify_verification_blocked_fn=AsyncMock(),
        notify_deferred_fn=AsyncMock(),
        notify_failed_fn=AsyncMock(),
    )

    assert advanced == ["plan_review"]
    assert ws.state.current_state != Stage.BLOCKED


# --- plan_review: a stale report must not be read as this attempt's verdict ---
# `_write_agent_output` only ever appends, so on re-entry the previous
# attempt's report is still on disk and stage_verifier's existence check is
# no longer evidence that *this* attempt produced anything.

def _plan_review_stage_def():
    stage_def = MagicMock()
    stage_def.agent = "plan-review-agent"
    stage_def.max_iterations = 2
    stage_def.resume_from_previous = False
    return stage_def


async def _run_plan_review(ws, runtime, advanced, stage_def=None):
    from orchestrator.pipeline.agent_stage import handle_agent_stage
    await handle_agent_stage(
        ws, "plan_review", stage_def or _plan_review_stage_def(),
        workflow=MagicMock(), agent_runtime=runtime, repo_config=None,
        notifier=None, mode_handler=None, get_chat_id=lambda w: "",
        dry_run=False, event_bus=None,
        advance_to_stage_fn=lambda w, s: advanced.append(s),
        on_ticket_done_fn=AsyncMock(),
        build_gate_summary_fn=MagicMock(return_value=("", [])),
        notify_verification_blocked_fn=AsyncMock(),
        notify_deferred_fn=AsyncMock(),
        notify_failed_fn=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_plan_review_stale_report_is_not_reused_as_a_verdict(tmp_path):
    """A run that writes nothing must block, not inherit the old PASS."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW

    ws = _make_workspace(tmp_path, "T-20", state=Stage.PLAN_REVIEW)
    _write_agent_output(
        ws.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW,
        "Status: PASS\n\nStale verdict from the previous attempt.",
    )

    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=AgentResult(
        agent_id="plan-review-agent", success=True, output="done",
    ))
    advanced: list[str] = []

    await _run_plan_review(ws, runtime, advanced)

    assert advanced == []
    assert ws.state.current_state == Stage.BLOCKED


@pytest.mark.asyncio
async def test_plan_review_fresh_report_still_decides_the_outcome(tmp_path):
    """Control for the test above: a run that does write is parsed normally."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    from orchestrator.workflow_router import StageDefinition, WorkflowDefinition

    ws = _make_workspace(tmp_path, "T-21", state=Stage.PLAN_REVIEW)
    _write_agent_output(
        ws.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW,
        "Status: FAIL\n\n### Files to Modify\nNo file list.",
    )

    async def _execute(*args, **kwargs):
        _write_agent_output(
            ws.reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW,
            "Status: PASS\n\nThe file list is now concrete.",
        )
        return AgentResult(agent_id="plan-review-agent", success=True, output="done")

    runtime = MagicMock()
    runtime.execute = _execute
    advanced: list[str] = []

    from orchestrator.pipeline.agent_stage import handle_agent_stage
    workflow = WorkflowDefinition(
        id="t", name="t",
        stages={"plan_review": StageDefinition(
            id="plan_review", agent="plan-review-agent",
            on_pass="dev", on_fail="analysis", max_iterations=2,
        )},
    )
    await handle_agent_stage(
        ws, "plan_review", workflow.stages["plan_review"],
        workflow=workflow, agent_runtime=runtime, repo_config=None,
        notifier=None, mode_handler=None, get_chat_id=lambda w: "",
        dry_run=False, event_bus=None,
        advance_to_stage_fn=lambda w, s: advanced.append(s),
        on_ticket_done_fn=AsyncMock(),
        build_gate_summary_fn=MagicMock(return_value=("", [])),
        notify_verification_blocked_fn=AsyncMock(),
        notify_deferred_fn=AsyncMock(),
        notify_failed_fn=AsyncMock(),
    )

    assert advanced == ["dev"]


# --- CLI session naming: only a stage that resumes may name its session ---

@pytest.mark.asyncio
@pytest.mark.parametrize("resumes", [True, False])
async def test_stage_tells_the_runtime_whether_its_session_is_resumed(tmp_path, resumes):
    """The runtime names the CLI session only when a later entry will resume it:
    the CLI refuses to start a second run under a name it already holds."""
    ws = _make_workspace(tmp_path, "T-30", state=Stage.PLAN_REVIEW)
    stage_def = _plan_review_stage_def()
    stage_def.resume_from_previous = resumes
    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=AgentResult(
        agent_id="plan-review-agent", success=True, output="done",
    ))

    await _run_plan_review(ws, runtime, [], stage_def=stage_def)

    assert runtime.execute.call_args.kwargs["resumable"] is resumes

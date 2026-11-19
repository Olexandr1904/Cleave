from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.constants import (
    REPORT_BA,
    REPORT_BA_QUESTIONS,
    RUNTIME_OUTPUT_BA,
    RUNTIME_OUTPUT_QA,
    RUNTIME_OUTPUT_SCOPE_GUARD,
)
from orchestrator.escalation import build_blocked_reason


def _make_ws(tmp_path: Path) -> SimpleNamespace:
    reports = tmp_path / "reports"
    reports.mkdir()
    return SimpleNamespace(
        reports_dir=reports,
        state=SimpleNamespace(ticket_id="T-1"),
    )


async def test_analysis_prefers_ba_questions(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / REPORT_BA_QUESTIONS).write_text(
        "## Questions for Human Review\n\n"
        "1. [AC2] What error types must be handled?\n"
        "2. [Scope] Is this a standalone view?\n"
    )
    # Also drop a later runtime output — should be ignored for analysis.
    (ws.reports_dir / RUNTIME_OUTPUT_BA).write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate — waiting_for_human\n"
    )

    reason = await build_blocked_reason(ws, "analysis")

    assert "Questions for Human Review" in reason
    assert "[AC2]" in reason
    assert "Attempt:" not in reason


async def test_non_analysis_strips_boilerplate(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text(
        "---\n"
        "**Attempt: 2026-04-24 14:00 UTC**\n"
        "## Decision: Escalate — waiting_for_human\n"
        "\n"
        "Tests fail because the fixture file is missing.\n"
        "See tests/data/fixture.json — expected but not found.\n"
    )

    reason = await build_blocked_reason(ws, "qa")

    assert "Tests fail" in reason
    assert "Attempt:" not in reason
    assert "Decision:" not in reason
    assert not reason.startswith("---")


async def test_non_analysis_uses_stage_specific_runtime_output(tmp_path):
    """scope-guard runtime output must not bleed into a QA block reason."""
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_SCOPE_GUARD).write_text("Scope guard content.\n")
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text("QA failure details here.\n")

    reason = await build_blocked_reason(ws, "qa")

    assert "QA failure" in reason
    assert "Scope guard" not in reason


async def test_truncates_long_reason(tmp_path):
    ws = _make_ws(tmp_path)
    body = "x" * 2000
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text(body)

    reason = await build_blocked_reason(ws, "qa")

    assert len(reason) <= 801  # 800 + the ellipsis char
    assert reason.endswith("…")


async def test_empty_reports_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    reason = await build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()


async def test_missing_reports_dir_returns_fallback(tmp_path):
    ws = SimpleNamespace(
        reports_dir=tmp_path / "does-not-exist",
        state=SimpleNamespace(ticket_id="T-1"),
    )
    reason = await build_blocked_reason(ws, "analysis")
    assert "analysis" in reason.lower()


async def test_analysis_falls_back_to_runtime_output_when_no_questions_file(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_BA).write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate\n\nRepo label missing from ticket.\n"
    )
    reason = await build_blocked_reason(ws, "analysis")
    assert "Repo label missing" in reason


async def test_boilerplate_only_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text(
        "---\n**Attempt: 2026-04-24 14:00 UTC**\n## Decision: Escalate\n---\n\n\n"
    )
    reason = await build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()


async def test_missing_output_uses_summarizer_when_returns_string(tmp_path):
    ws = _make_ws(tmp_path)  # no runtime output present
    summary = "Reason: Kotlin daemon OOM.\nSuggested action: raise -Xmx; retry."
    with patch(
        "orchestrator.escalation.summarize_agent_failure",
        new_callable=AsyncMock, return_value=summary,
    ) as mock_summarize:
        reason = await build_blocked_reason(ws, "qa")
    assert reason == summary
    mock_summarize.assert_awaited_once()
    args, kwargs = mock_summarize.call_args
    assert kwargs["reason_kind"] == "missing_output"


async def test_missing_output_falls_back_when_summarizer_returns_none(tmp_path):
    ws = _make_ws(tmp_path)
    with patch(
        "orchestrator.escalation.summarize_agent_failure",
        new_callable=AsyncMock, return_value=None,
    ):
        reason = await build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()
    assert "no output" in reason.lower() or "may have timed out" in reason.lower()


async def test_reason_is_the_newest_attempt_not_the_first(tmp_path):
    """The reply file keeps every attempt. The operator was shown the first one — "the scope check failed on one
    file" — for a ticket whose last check had passed."""
    from orchestrator.agent_runtime import _write_agent_output
    ws = _make_ws(tmp_path)
    replies = ws.reports_dir / RUNTIME_OUTPUT_SCOPE_GUARD
    _write_agent_output(replies, "The scope check failed on one file.")
    _write_agent_output(replies, "Scope guard passes after the revert.")

    reason = await build_blocked_reason(ws, "scope_check")

    assert reason == "Scope guard passes after the revert."

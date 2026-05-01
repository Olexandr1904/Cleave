from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.constants import (
    REPORT_BA,
    REPORT_BA_QUESTIONS,
    RUNTIME_OUTPUT_BA,
    RUNTIME_OUTPUT_QA,
    RUNTIME_OUTPUT_SCOPE_GUARD,
)
from orchestrator.orchestrator import Orchestrator


def _make_ws(tmp_path: Path) -> SimpleNamespace:
    reports = tmp_path / "reports"
    reports.mkdir()
    return SimpleNamespace(
        reports_dir=reports,
        state=SimpleNamespace(ticket_id="T-1"),
    )


def _orch() -> Orchestrator:
    return Orchestrator.__new__(Orchestrator)


def test_analysis_prefers_ba_questions(tmp_path):
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

    reason = _orch()._build_blocked_reason(ws, "analysis")

    assert "Questions for Human Review" in reason
    assert "[AC2]" in reason
    assert "Attempt:" not in reason


def test_non_analysis_strips_boilerplate(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text(
        "---\n"
        "**Attempt: 2026-04-24 14:00 UTC**\n"
        "## Decision: Escalate — waiting_for_human\n"
        "\n"
        "Tests fail because the fixture file is missing.\n"
        "See tests/data/fixture.json — expected but not found.\n"
    )

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert "Tests fail" in reason
    assert "Attempt:" not in reason
    assert "Decision:" not in reason
    assert not reason.startswith("---")


def test_non_analysis_uses_stage_specific_runtime_output(tmp_path):
    """scope-guard runtime output must not bleed into a QA block reason."""
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_SCOPE_GUARD).write_text("Scope guard content.\n")
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text("QA failure details here.\n")

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert "QA failure" in reason
    assert "Scope guard" not in reason


def test_truncates_long_reason(tmp_path):
    ws = _make_ws(tmp_path)
    body = "x" * 2000
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text(body)

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert len(reason) <= 801  # 800 + the ellipsis char
    assert reason.endswith("…")


def test_empty_reports_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    reason = _orch()._build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()


def test_missing_reports_dir_returns_fallback(tmp_path):
    ws = SimpleNamespace(
        reports_dir=tmp_path / "does-not-exist",
        state=SimpleNamespace(ticket_id="T-1"),
    )
    reason = _orch()._build_blocked_reason(ws, "analysis")
    assert "analysis" in reason.lower()


def test_analysis_falls_back_to_runtime_output_when_no_questions_file(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_BA).write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate\n\nRepo label missing from ticket.\n"
    )
    reason = _orch()._build_blocked_reason(ws, "analysis")
    assert "Repo label missing" in reason


def test_boilerplate_only_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / RUNTIME_OUTPUT_QA).write_text(
        "---\n**Attempt: 2026-04-24 14:00 UTC**\n## Decision: Escalate\n---\n\n\n"
    )
    reason = _orch()._build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()

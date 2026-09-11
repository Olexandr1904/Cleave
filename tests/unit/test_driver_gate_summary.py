"""Tests for orchestrator/pipeline/driver.py: build_gate_summary.

PLAN_REVIEW is the only stage that parks at the approval gate under this
name now (Task 7 moved the gate from ANALYSIS to PLAN_REVIEW) — the
operator-facing message must say something about the plan the machine
already vetted, not fall through to the generic "Awaiting approval at"
placeholder.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.agent_runtime import _write_agent_output
from orchestrator.constants import REPORT_BA, RUNTIME_OUTPUT_PLAN_REVIEW
from orchestrator.pipeline.driver import build_gate_summary


def _ws(reports_dir: Path, meta_dir: Path):
    ws = MagicMock()
    ws.state = SimpleNamespace(ticket_id="T-1", company_id="acme")
    ws.reports_dir = reports_dir
    ws.meta_dir = meta_dir
    return ws


def test_plan_review_gate_shows_both_verdicts(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    (reports_dir / REPORT_BA).write_text(
        "# Plan\n\n## Verdict\nShip the widget.\n", encoding="utf-8",
    )
    # Written the way the pipeline writes it: `_write_agent_output` appends
    # every attempt under a `---` / `**Attempt:**` separator, so the file's
    # first line is `---`, never the verdict. Writing the report by hand here
    # is what let "Plan reviewer:\n---" ship green.
    _write_agent_output(
        reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW, "Status: PASS\n",
    )
    ws = _ws(reports_dir, meta_dir)

    text, buttons = build_gate_summary(ws, "PLAN_REVIEW")

    assert "Awaiting approval at" not in text
    assert "Ship the widget." in text
    assert "PASS" in text
    assert "start coding" in text
    assert "back to analysis" in text
    assert {b.action for b in buttons} == {"approve:T-1", "reject:T-1"}


def test_plan_review_gate_never_shows_the_attempt_separator(tmp_path: Path) -> None:
    """The verdict, not the `---` the appender opens each block with."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    (reports_dir / REPORT_BA).write_text(
        "# Plan\n\n## Verdict\nShip the widget.\n", encoding="utf-8",
    )
    _write_agent_output(
        reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW, "Status: PASS\n\nSound plan.",
    )

    text, _ = build_gate_summary(_ws(reports_dir, meta_dir), "PLAN_REVIEW")

    assert "Plan reviewer:\nStatus: PASS" in text
    assert "---" not in text


def test_plan_review_gate_shows_the_latest_attempts_verdict(tmp_path: Path) -> None:
    """After a bounce the operator must see the re-review, not the first one."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    (reports_dir / REPORT_BA).write_text(
        "# Plan\n\n## Verdict\nShip the widget.\n", encoding="utf-8",
    )
    report = reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(report, "Status: FAIL\n\n### Files to Modify\nNo list.")
    _write_agent_output(report, "Status: PASS\n\nThe file list is now concrete.")

    text, _ = build_gate_summary(_ws(reports_dir, meta_dir), "PLAN_REVIEW")

    assert "Plan reviewer:\nStatus: PASS" in text
    assert "FAIL" not in text

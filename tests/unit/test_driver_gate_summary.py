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
    (reports_dir / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: PASS\n", encoding="utf-8",
    )
    ws = _ws(reports_dir, meta_dir)

    text, buttons = build_gate_summary(ws, "PLAN_REVIEW")

    assert "Awaiting approval at" not in text
    assert "Ship the widget." in text
    assert "PASS" in text
    assert "start coding" in text
    assert "back to analysis" in text
    assert {b.action for b in buttons} == {"approve:T-1", "reject:T-1"}

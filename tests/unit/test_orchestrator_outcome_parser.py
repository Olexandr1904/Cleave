"""Characterization test for parse_agent_outcome.

Maps agent stdout to one of {'default', 'unclear', 'pass', 'fail'}. The
qa/scope_check branch reads from workspace.reports_dir; the analysis
branch checks for the BA plan file.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.pipeline.agent_stage import parse_agent_outcome
from orchestrator.constants import REPORT_BA


def _ws(reports_dir: Path):
    ws = MagicMock()
    ws.state = SimpleNamespace(ticket_id="T-1")
    ws.reports_dir = reports_dir
    return ws


def test_parse_outcome_analysis_with_ba_returns_default(tmp_path: Path) -> None:
    (tmp_path / REPORT_BA).write_text("# BA plan", encoding="utf-8")
    assert parse_agent_outcome("analysis", "anything", _ws(tmp_path)) == "default"


def test_parse_outcome_analysis_no_ba_is_unclear(tmp_path: Path) -> None:
    # tmp_path is empty — no BA file
    assert parse_agent_outcome("analysis", "I have questions", _ws(tmp_path)) == "unclear"
    # Even without explicit "unclear"/"questions", missing BA file → unclear (fallback)
    assert parse_agent_outcome("analysis", "all good", _ws(tmp_path)) == "unclear"


def test_parse_outcome_qa_pass_from_output(tmp_path: Path) -> None:
    """When report file is absent and output matches a pass keyword, returns 'pass'."""
    # "qa pass" is a literal substring checked by _looks_like_pass.
    output = "qa pass — all gates green"
    assert parse_agent_outcome("qa", output, _ws(tmp_path)) == "pass"


def test_parse_outcome_qa_fail_from_output(tmp_path: Path) -> None:
    """When report is absent and output does NOT match pass keywords, returns 'fail'."""
    output = "some random text without verdict"
    assert parse_agent_outcome("qa", output, _ws(tmp_path)) == "fail"


def test_parse_outcome_unknown_stage_returns_default(tmp_path: Path) -> None:
    assert parse_agent_outcome("dev", "anything", _ws(tmp_path)) == "default"
    assert parse_agent_outcome("push", "", _ws(tmp_path)) == "default"


def test_plan_review_pass_requires_exact_verdict_line(tmp_path: Path) -> None:
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: PASS\n\nThe plan is sound.", encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "pass"


def test_plan_review_fail_verdict(tmp_path: Path) -> None:
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: FAIL\n\n### Files to Modify\nNo file list.", encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"


def test_plan_review_does_not_inherit_loose_pass_matcher(tmp_path: Path) -> None:
    """'advances to' is a pass keyword for the other stages. Not for this one."""
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: FAIL\n\nThe plan advances to dev only if the file list is fixed.",
        encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"


def test_plan_review_fails_closed_on_missing_report(tmp_path: Path) -> None:
    assert parse_agent_outcome("plan_review", "looks good to me", _ws(tmp_path)) == "fail"


def test_plan_review_fails_closed_on_malformed_verdict(tmp_path: Path) -> None:
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    (tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW).write_text(
        "Status: mostly pass\n\nGood enough.", encoding="utf-8",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"

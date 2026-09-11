"""Characterization test for parse_agent_outcome.

Maps agent stdout to one of {'default', 'unclear', 'pass', 'fail'}. The
qa/scope_check branch reads from workspace.reports_dir; the analysis
branch checks for the BA plan file.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


# --- plan_review reads the newest attempt, not the oldest -----------------
# `_write_agent_output` appends every run to the same file under a `---` /
# `**Attempt:**` separator and never truncates. These tests go through that
# writer on purpose: writing "Status: PASS" straight to disk produces a file
# shape the pipeline never creates, and hid this defect the first time.

def test_plan_review_reads_the_latest_attempt_not_the_first(tmp_path: Path) -> None:
    """A rejected plan must be able to pass on re-review."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    report = tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(report, "Status: FAIL\n\n### Files to Modify\nNo file list.")
    _write_agent_output(report, "Status: PASS\n\nThe file list is now concrete.")
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "pass"


def test_plan_review_latest_attempt_can_fail_after_a_pass(tmp_path: Path) -> None:
    """The newest verdict wins in both directions."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    report = tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(report, "Status: PASS\n\nLooks sound.")
    _write_agent_output(report, "Status: FAIL\n\n### Tasks\nNo acceptance criteria.")
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"


def test_plan_review_single_attempt_through_the_real_writer(tmp_path: Path) -> None:
    """Even the first write opens the file with a `---` separator line."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    report = tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(report, "Status: PASS\n\nThe plan is sound.")
    assert report.read_text(encoding="utf-8").splitlines()[0] == "---"
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "pass"


@pytest.mark.parametrize("verdict_line", [
    "Status: PASS",
    "**Status: PASS**",
    "**Status**: PASS",
    "## Status: PASS",
    "Status: PASS ✅",
])
def test_plan_review_accepts_decorated_verdict_lines(
    tmp_path: Path, verdict_line: str,
) -> None:
    """Agents emit bolded and decorated verdicts — `verdict: **fail` is already
    matched elsewhere in this module because it was seen in the wild. A slip in
    formatting must not cost a full BA re-run."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    report = tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(report, f"{verdict_line}\n\nThe plan is sound.")
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "pass"


@pytest.mark.parametrize("verdict_line", [
    "Status: FAIL",
    "**Status: FAIL**",
    "## Status: FAIL",
])
def test_plan_review_accepts_decorated_fail_lines(
    tmp_path: Path, verdict_line: str,
) -> None:
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    report = tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(report, f"{verdict_line}\n\n### Files\nNo list.")
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"


def test_plan_review_prose_containing_the_words_is_not_a_pass(tmp_path: Path) -> None:
    """Loosening the decoration must not loosen the verdict token itself."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_PLAN_REVIEW
    report = tmp_path / RUNTIME_OUTPUT_PLAN_REVIEW
    _write_agent_output(
        report,
        "The plan's status: passing it now would be premature.\n"
        "Status: probably pass\n",
    )
    assert parse_agent_outcome("plan_review", "", _ws(tmp_path)) == "fail"

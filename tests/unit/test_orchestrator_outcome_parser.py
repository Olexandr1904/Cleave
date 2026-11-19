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
    """With no report, the verdict line of the reply decides."""
    output = "## Status: PASS\n\nAll gates green."
    assert parse_agent_outcome("qa", output, _ws(tmp_path)) == "pass"


def test_parse_outcome_qa_without_a_verdict_is_not_a_fail(tmp_path: Path) -> None:
    """No verdict line anywhere is its own outcome: there is nothing for dev to fix."""
    output = "some random text without verdict"
    assert parse_agent_outcome("qa", output, _ws(tmp_path)) == "no_verdict"


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


# --- scope_check / code_review / qa: the verdict is the report's Status line ---
# The agents' contract is a report file with `## Status: PASS|FAIL`
# (scope-guard.md, code-review.md, qa.md). The reply is free prose and its
# wording changes with the model: on three ACME tickets in one evening every
# scope check wrote `## Status: PASS` to the report, answered "The scope check
# passes", and was routed back to dev until the ticket blocked.

_JUDGE_REPORTS = [
    ("scope_check", "scope-guard.md"),
    ("code_review", "code-review.md"),
    ("qa", "qa.md"),
]


@pytest.mark.parametrize("stage_id, report_name", _JUDGE_REPORTS)
def test_pass_is_read_from_the_report_when_the_reply_is_prose(
    tmp_path: Path, stage_id: str, report_name: str,
) -> None:
    (tmp_path / report_name).write_text(
        "# Scope Certificate — T-1\n\n## Status: PASS\n\n## Summary\n- All files in plan: YES\n",
        encoding="utf-8",
    )
    reply = (
        "The scope check **passes**. I wrote the certificate to "
        "`ai_pipeline/T-1/scope-guard.md`, so the ticket can go to QA."
    )
    assert parse_agent_outcome(stage_id, reply, _ws(tmp_path)) == "pass"


@pytest.mark.parametrize("stage_id, report_name", _JUDGE_REPORTS)
def test_fail_is_read_from_the_report_when_the_reply_is_prose(
    tmp_path: Path, stage_id: str, report_name: str,
) -> None:
    (tmp_path / report_name).write_text(
        "# Scope Report — T-1\n\n## Status: FAIL\n\n## Violations\n\n1. Unauthorized file\n",
        encoding="utf-8",
    )
    reply = "One file is outside the plan. Once it is reverted the branch advances to QA."
    assert parse_agent_outcome(stage_id, reply, _ws(tmp_path)) == "fail"


def test_a_fail_is_not_overruled_by_an_older_attempts_pass(tmp_path: Path) -> None:
    """The reply file keeps every attempt; an old `Status: PASS` in it is not this run's verdict."""
    from orchestrator.agent_runtime import _write_agent_output
    from orchestrator.constants import RUNTIME_OUTPUT_SCOPE_GUARD
    replies = tmp_path / RUNTIME_OUTPUT_SCOPE_GUARD
    _write_agent_output(replies, "Scope check complete. **Status: PASS.**")
    newest = "Scope guard verdict written to the report.\n\n**Status: FAIL** — one violation."
    _write_agent_output(replies, newest)
    assert parse_agent_outcome("scope_check", newest, _ws(tmp_path)) == "fail"


def test_a_report_left_from_an_earlier_attempt_is_not_this_attempts_verdict(tmp_path: Path) -> None:
    import os
    import time
    report = tmp_path / "scope-guard.md"
    report.write_text("## Status: PASS\n", encoding="utf-8")
    started_at = time.time()
    os.utime(report, (started_at - 600, started_at - 600))
    reply = "Nothing changed since my last check."
    assert parse_agent_outcome("scope_check", reply, _ws(tmp_path), started_at=started_at) == "no_verdict"


def test_the_reply_decides_when_this_attempt_wrote_no_report(tmp_path: Path) -> None:
    assert parse_agent_outcome("scope_check", "Status: FAIL\n\nOne violation.", _ws(tmp_path)) == "fail"
    assert parse_agent_outcome("code_review", "**Status: PASS**\n\nNo findings.", _ws(tmp_path)) == "pass"


def test_prose_about_passing_is_not_a_verdict(tmp_path: Path) -> None:
    """The old substring match read "advances to" as PASS, also in a report of violations."""
    reply = "The check found two violations. After the fix the ticket advances to QA."
    assert parse_agent_outcome("scope_check", reply, _ws(tmp_path)) == "no_verdict"


@pytest.mark.parametrize("placeholder", ["## Status: PASS | FAIL", "## Status: PASS / FAIL", "Status: PASS or FAIL"])
def test_the_report_templates_placeholder_line_is_not_a_verdict(tmp_path: Path, placeholder: str) -> None:
    """code-review-agent.md shows the line as `## Status: PASS | FAIL`; copied as is, it decides nothing."""
    (tmp_path / "code-review.md").write_text(f"# Code Review — T-1\n\n{placeholder}\n", encoding="utf-8")
    assert parse_agent_outcome("code_review", "Review written.", _ws(tmp_path)) == "no_verdict"

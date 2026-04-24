from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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
    (ws.reports_dir / "ba-questions.md").write_text(
        "## Questions for Human Review\n\n"
        "1. [AC2] What error types must be handled?\n"
        "2. [Scope] Is this a standalone view?\n"
    )
    # Also drop a later output file — should be ignored for analysis.
    (ws.reports_dir / "ba-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate — waiting_for_human\n"
    )

    reason = _orch()._build_blocked_reason(ws, "analysis")

    assert "Questions for Human Review" in reason
    assert "[AC2]" in reason
    assert "Attempt:" not in reason


def test_non_analysis_strips_boilerplate(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "qa-agent-output.md").write_text(
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


def test_non_analysis_uses_latest_output_by_mtime(tmp_path):
    import os
    import time
    ws = _make_ws(tmp_path)
    old = ws.reports_dir / "scope-guard-agent-output.md"
    old.write_text("Old content.\n")
    # Force older mtime.
    old_mtime = time.time() - 60
    os.utime(old, (old_mtime, old_mtime))

    new = ws.reports_dir / "qa-agent-output.md"
    new.write_text("New content that should win.\n")

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert "New content" in reason
    assert "Old content" not in reason


def test_truncates_long_reason(tmp_path):
    ws = _make_ws(tmp_path)
    body = "x" * 2000
    (ws.reports_dir / "qa-agent-output.md").write_text(body)

    reason = _orch()._build_blocked_reason(ws, "qa")

    assert len(reason) <= 801  # 800 + the ellipsis char
    assert reason.endswith("…")


def test_empty_reports_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    reason = _orch()._build_blocked_reason(ws, "qa")
    assert "qa" in reason.lower()
    assert "reports" in reason.lower()


def test_missing_reports_dir_returns_fallback(tmp_path):
    ws = SimpleNamespace(
        reports_dir=tmp_path / "does-not-exist",
        state=SimpleNamespace(ticket_id="T-1"),
    )
    reason = _orch()._build_blocked_reason(ws, "analysis")
    assert "analysis" in reason.lower()


def test_analysis_falls_back_to_output_when_no_questions_file(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "ba-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 13:57 UTC**\n## Decision: Escalate\n\nRepo label missing from ticket.\n"
    )
    reason = _orch()._build_blocked_reason(ws, "analysis")
    assert "Repo label missing" in reason


def test_boilerplate_only_returns_fallback(tmp_path):
    ws = _make_ws(tmp_path)
    (ws.reports_dir / "qa-agent-output.md").write_text(
        "---\n**Attempt: 2026-04-24 14:00 UTC**\n## Decision: Escalate\n---\n\n\n"
    )
    reason = _orch()._build_blocked_reason(ws, "qa")
    # Everything was boilerplate → fallback.
    assert "qa" in reason.lower()
    assert "reports" in reason.lower()

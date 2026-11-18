# tests/unit/test_skillflow_compare_messages.py
from __future__ import annotations

from dataclasses import replace

import pytest

from orchestrator.skillflow.compare_messages import (
    BOTH_DRAFTS_LINE, CLOSED_LINE, READY_LINE, TRACKER_PROBLEM_LINE, comparison_message, duration,
    moved_on_message, retry_buttons, stuck_message, verdict_buttons, verdict_message,
)
from orchestrator.skillflow.compare_models import Comparison
from orchestrator.skillflow.sides import SideView
from orchestrator.tg_i18n import telegram_language
from orchestrator.tg_uk import TEXTS

NOW = "2026-11-17T12:00:00+00:00"
GONE = SideView("gone", True, 0, "", "", 0, 0, 0, "gone")
WITH_PR = SideView("pr_open", True, 13, "https://github.com/acme/app/pull/13", "", 5, 0, 0, "h")


@pytest.fixture
def comparison():
    return Comparison("cmp:abc", "acme/app", "ACME-1", "r1", "open", NOW, NOW, summary="Fix login",
                      skill="fix-this")


@pytest.fixture
def catalogue_uk():
    # The real catalogue: the test checks that the comparison's texts are in it.
    return TEXTS


def test_comparison_message_shows_both_sides(comparison):
    staged = SideView("PR_REVIEW", True, 12, "u12", "", 130, 1_200_000, 40_000, "f")
    skill = SideView("stopped", True, 0, "", "stopped: low confidence", 65, 800_000, 30_000, "g")
    text = comparison_message(comparison, skill, staged)
    assert text.splitlines() == [
        "ACME-1 · comparison: Fix login",
        "Staged: PR #12 u12 · PR_REVIEW · 2 h 10 min · 1200000 in / 40000 out tokens",
        "Skill (fix-this): no PR · stopped: stopped: low confidence · 1 h 05 min · 800000 in / 30000 out tokens",
        "Tap the better side. A reply to this message is kept as the verdict's note.",
    ]


def test_buttons_offer_only_sides_with_a_pr(comparison):
    staged = SideView("PR_REVIEW", True, 12, "u12", "", 1, 0, 0, "f")
    skill = SideView("no_change", True, 0, "", "no app fix", 1, 0, 0, "g")
    assert verdict_buttons("cmp:abc", skill, staged) == [["Staged", "sk_win_staged:cmp:abc"],
                                                         ["Neither", "sk_win_none:cmp:abc"]]


def test_texts_follow_the_language(comparison, catalogue_uk):
    with telegram_language("uk"):
        assert comparison_message(comparison, GONE, GONE).splitlines()[-1] != \
               "Tap the better side. A reply to this message is kept as the verdict's note."


def test_callback_data_fits_telegram():
    assert all(len(action.encode()) <= 64 for _, action in verdict_buttons("cmp:" + "f" * 12, WITH_PR, WITH_PR))


def test_both_sides_with_a_pr_offer_all_three():
    assert [action for _, action in verdict_buttons("cmp:abc", WITH_PR, WITH_PR)] == [
        "sk_win_staged:cmp:abc", "sk_win_skill:cmp:abc", "sk_win_none:cmp:abc"]
    assert retry_buttons("cmp:abc") == [["Retry", "sk_retry:cmp:abc"]]


@pytest.mark.parametrize("minutes, text", [(0, "0 min"), (45, "45 min"), (60, "1 h 00 min"), (125, "2 h 05 min")])
def test_duration(minutes, text):
    assert duration(minutes) == text


def test_moved_on_and_stuck(comparison):
    assert moved_on_message(comparison, "skill") == (
        "ACME-1 · comparison: the skill side moved on after your verdict; nothing was done."
        " A new message comes when both sides are at rest.")
    c = replace(comparison, verdict="staged")
    assert stuck_message(c, "close", 13, "HTTP 502").splitlines() == [
        "ACME-1 · verdict staged not finished: closing PR #13 failed: HTTP 502",
        "Retry continues from that step.",
    ]


def test_verdict_message_renders_the_report_in_order(comparison):
    c = replace(comparison, verdict="skill", report=[
        [READY_LINE, {"number": 13, "url": "u13"}], [CLOSED_LINE, {"number": 12}],
        [TRACKER_PROBLEM_LINE, {"problem": "HTTP 401"}]])
    assert verdict_message(c).splitlines() == [
        "ACME-1 · verdict: skill", "Ready for review: PR #13 u13", "Closed: PR #12", "Tracker: HTTP 401"]
    neither = replace(comparison, verdict="neither", report=[[BOTH_DRAFTS_LINE, {}]])
    with telegram_language("uk"):
        assert verdict_message(neither).splitlines() == [
            TEXTS["{ticket} · verdict: {verdict}"].format(ticket="ACME-1", verdict=TEXTS["neither"]),
            TEXTS["Both PRs stay drafts."]]

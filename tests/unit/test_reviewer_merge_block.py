from __future__ import annotations

import dataclasses

import pytest

from orchestrator.reviewer.merge_block import GENERIC, no_checks, refusal_text, rerun_message
from orchestrator.reviewer.models import Check, MergeState

A = "a" * 40
STATE = MergeState(status="BLOCKED", base="develop", head_ref="feature/x", head_sha=A, cross_repo=False,
                   review="APPROVED", checks=(Check("lint", "passed", "success"),))


def state(**changes):
    return dataclasses.replace(STATE, **changes)


@pytest.mark.parametrize("changes, text", [
    ({"status": "DIRTY"}, "GitHub refused the merge: #7 has conflicts with develop."),
    ({"status": "BEHIND"}, "GitHub refused the merge: #7 is behind develop: update its branch."),
    ({"status": "DRAFT"}, "GitHub refused the merge: #7 is a draft."),
    ({"checks": None}, "GitHub refused the merge: no checks ran on aaaaaaaaa, and develop waits for them (a base "
                       "that changed starts none): Re-run CI on its card starts them."),
    ({"checks": ()}, "GitHub refused the merge: no checks ran on aaaaaaaaa, and develop waits for them (a base "
                     "that changed starts none): Re-run CI on its card starts them."),
    ({"checks": (Check("build", "pending", "queued"), Check("scan", "pending", "in progress"),
                 Check("lint", "passed", "success"))},
     "GitHub refused the merge: checks still run on aaaaaaaaa: build (queued), scan (in progress). "
     "Merge once they pass."),
    # A failed check outweighs a running one: the merge will not pass when they end.
    ({"checks": (Check("build", "pending", "queued"), Check("tests", "failed", "failure"),
                 Check("e2e", "failed", "cancelled"))},
     "GitHub refused the merge: checks failed on aaaaaaaaa: tests (failure), e2e (cancelled)."),
    ({"review": "CHANGES_REQUESTED"}, "GitHub refused the merge: changes were requested on #7."),
    ({"review": "REVIEW_REQUIRED"}, "GitHub refused the merge: #7 needs an approving review."),
    ({"status": "UNKNOWN"}, "GitHub refused the merge: GitHub is still working out whether #7 can merge: "
                            "try again in a minute."),
    ({}, "GitHub refused the merge: a rule of develop blocks it, though no check on aaaaaaaaa runs or failed: "
         "see #7 on GitHub."),
])
def test_refusal_text_says_what_blocks_the_merge(changes, text):
    assert refusal_text(state(**changes), 7) == text


def test_refusal_text_without_a_state_is_the_generic_text():
    assert refusal_text(None, 7) == GENERIC == (
        "GitHub refused the merge (GitHub 405): checks, reviews or conflicts block it.")


def test_check_names_are_cleaned_cut_and_counted():
    checks = (Check("bad\x1b[31m\nname‮", "pending", "queued"), Check("x" * 300, "pending", "queued"),
              *(Check(f"c{i}", "pending", "queued") for i in range(5)))
    text = refusal_text(state(checks=checks), 7)
    assert text == ("GitHub refused the merge: checks still run on aaaaaaaaa: bad[31m name (queued), "
                    f"{'x' * 39}… (queued), c0 (queued), c1 (queued), c2 (queued) and 2 more. Merge once they pass.")


def test_a_base_name_is_cleaned_too():
    assert refusal_text(state(status="DIRTY", base="dev\x07elop"), 7) == (
        "GitHub refused the merge: #7 has conflicts with develop.")


@pytest.mark.parametrize("changes, expected", [
    ({"checks": None}, True),
    ({"checks": ()}, True),
    ({}, False),  # a check ran
    ({"checks": None, "status": "CLEAN"}, False),  # nothing blocks it: a PR into a branch no workflow runs for
    ({"checks": None, "status": "UNKNOWN"}, False),  # GitHub still computes: the next poll tells
    ({"checks": None, "head_sha": "b" * 40}, False),  # another head than the board's
    ({"checks": None, "cross_repo": True}, False),  # a fork's branch: Cleave cannot push to it
])
def test_no_checks(changes, expected):
    assert no_checks(state(**changes), A) is expected


def test_rerun_message():
    assert rerun_message("ABC-12", A) == "ABC-12: re-run CI (no checks ran on aaaaaaaaa)"
    assert rerun_message("", A) == "Re-run CI (no checks ran on aaaaaaaaa)"

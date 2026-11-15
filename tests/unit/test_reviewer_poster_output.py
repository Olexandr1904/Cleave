# tests/unit/test_reviewer_poster_output.py
from __future__ import annotations

import json
import time

import pytest

from orchestrator.reviewer.poster_output import (
    DryItem,
    PosterOutputError,
    parse_replies_results,
    parse_replies_steps,
    parse_review_confirm,
    parse_review_dry_run,
)
from tests.unit.test_reviewer_report import HEAD

SAMPLE = """Event  : APPROVE
Summary: yes
Inline : 3 comment(s)
  #01 app/src/main/CartRepository.kt:88  - NPE on an empty list
  #02 app/src/main/SyncWorker.kt:41  - Retry without backoff
  #05 app/src/main/res/values/strings.xml:212  - Untranslated key?
HEAD   : <HEAD>
Anchors: 2 inline, 1 degraded to body
Posting 2 comment(s) in 1 review(s)
--- DRY RUN batch 1/1 ---
{"commit_id": "<HEAD>", "event": "APPROVE",
 "comments": [{"path": "app/src/main/CartRepository.kt", "line": 88, "side": "RIGHT",
               "body": "**NPE on an empty list**\\n\\nThe list is read before the null check."},
              {"path": "app/src/main/SyncWorker.kt", "line": 43, "side": "RIGHT",
               "body": "**Retry without backoff**\\n\\nNetwork retry rule.\\n\\n_Anchor moved from line 41._"}],
 "body": "Two real problems in the sync path.\\n\\n### Findings without a diff anchor\\n\\n**Untranslated key?** `app/src/main/res/values/strings.xml:212`"}
"""
DRY = SAMPLE.replace("<HEAD>", HEAD)
STEPS = ('[{"step": "reply", "n": 1, "target": "app/src/main/CartRepository.kt:88 @octo-author", "body": "Fixed."},'
         ' {"step": "resolve", "n": 1, "target": "app/src/main/CartRepository.kt:88 @octo-author"},'
         ' {"step": "comment", "n": 3, "target": "review 456 @octo-author", "body": "Agreed."}]')
RESULTS = ('[{"n": 1, "replied": "https://github.com/acme-org/acme-app/pull/1712#discussion_r1"}, {"n": 1, "resolved": true},'
           ' {"n": 3, "commented": "https://github.com/acme-org/acme-app/pull/1712#issuecomment-2"}]')

ANCHORS = "Anchors: 2 inline, 1 degraded to body\n"
SKIPPING = "Skipping 1 finding(s) already posted by an earlier run\n"
POSTING = "Posting 2 comment(s) in 1 review(s)\n"
ITEM_5 = "  #05 app/src/main/res/values/strings.xml:212  - Untranslated key?\n"
C88 = '{"path": "app/src/main/CartRepository.kt", "line": 88, "side": "RIGHT",'
BATCH = "--- DRY RUN batch 1/1 ---\n"


def _with_item(line: str) -> str:
    return DRY.replace(ITEM_5, line + "\n" + ITEM_5)


def test_parse_review_dry_run():
    dry = parse_review_dry_run(DRY)
    assert dry.event == "APPROVE"
    assert dry.summary is True
    assert dry.items == (
        DryItem(1, "app/src/main/CartRepository.kt", 88, "NPE on an empty list"),
        DryItem(2, "app/src/main/SyncWorker.kt", 41, "Retry without backoff"),
        DryItem(5, "app/src/main/res/values/strings.xml", 212, "Untranslated key?"),
    )
    assert dry.head == HEAD
    assert (dry.skipped, dry.comments, dry.reviews) == (0, 2, 1)
    assert len(dry.payloads) == 1
    assert len(dry.payloads[0]["comments"]) == 2


def test_skipping_line():
    text = DRY.replace(ANCHORS, ANCHORS + SKIPPING)
    assert parse_review_dry_run(text).skipped == 1


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (DRY.replace(POSTING, ""), "no Posting line"),
        (DRY.replace("Event  : APPROVE", "Event  : MAYBE"), "Event is not understood: MAYBE"),
        (DRY.replace("batch 1/1", "batch 2/1"), "batch 2/1 out of order"),
        (DRY.replace('{"commit_id"', '{commit_id'), "batch 1/1 is not one JSON object"),
        (DRY.replace('"line": 88, ', ""), "a comment needs"),
        (DRY.replace(f"HEAD   : {HEAD}\n", ""), "no HEAD line"),
        (DRY + " " * (2_000_001 - len(DRY)), "too large"),
        (DRY.replace(ANCHORS, ANCHORS + "Note: anchors cached\n"), "unknown line: Note: anchors cached"),
        (DRY.replace(ANCHORS, ANCHORS + "HEAD   : 8f01a2b3c\n"), "HEAD line twice"),
        (DRY.replace("batch 1/1", "batch 1/2"), "batch 2/2 is missing"),
        # Posting and Skipping are whole lines: text after them is not known.
        (DRY.replace(POSTING, "Posting 2 comment(s) in 1 review(s) plus 4 file comment(s) in 1 review(s)\n"),
         r"unknown line: Posting 2 comment\(s\) in 1 review\(s\) plus"),
        (DRY.replace(ANCHORS, ANCHORS + "Skipping 1 finding(s) already posted; 3 more were dropped\n"),
         r"unknown line: Skipping 1 finding\(s\) already posted; 3"),
        # The header is in the spec's order.
        (_with_item(SKIPPING.rstrip("\n")), r"out of order:   #05"),
        (DRY.replace("Event  : APPROVE\nSummary: yes\n", "Summary: yes\nEvent  : APPROVE\n"),
         "out of order: Event"),
        (DRY.replace(ANCHORS, "").replace(POSTING, POSTING + ANCHORS), "out of order: Anchors"),
        # Every required label is there, once, with a value it can have.
        (DRY.replace("Event  : APPROVE\n", ""), "no Event line"),
        (DRY.replace("Summary: yes\n", ""), "no Summary line"),
        (DRY.replace("Inline : 3 comment(s)\n", ""), "no Inline line"),
        (DRY.replace("Summary: yes", "Summary: maybe"), "Summary is not understood: maybe"),
        (DRY.replace(f"HEAD   : {HEAD}", "HEAD   : not-a-sha"), "HEAD is not understood: not-a-sha"),
        (DRY.replace(f"HEAD   : {HEAD}", "HEAD   : abcdef"), "HEAD is not understood: abcdef"),
        (DRY.replace("Inline : 3 comment(s)", "Inline : 3 comments"), "Inline is not understood: 3 comments"),
        (DRY.replace(ANCHORS, "Anchors: 2 inline\n"), "Anchors is not understood: 2 inline"),
        (DRY.replace(POSTING, POSTING + POSTING), "Posting line twice"),
        (DRY.replace(ANCHORS, ANCHORS + SKIPPING + SKIPPING), "Skipping line twice"),
        # A number has at most 9 digits.
        (_with_item("  #1234567890 app/x.kt:1  - t"), "number too long"),
        (_with_item("  #3 app/x.kt:1234567890  - t"), "number too long"),
        (DRY.replace("Posting 2 comment(s)", "Posting 1234567890 comment(s)"), "number too long"),
    ],
    ids=["no-posting", "event-maybe", "batch-2-of-1", "payload-not-json", "comment-without-line",
         "no-head", "too-large", "unknown-line", "head-twice", "batch-1-of-2",
         "posting-trailing-text", "skipping-trailing-text",
         "skipping-among-items", "summary-before-event", "anchors-after-posting",
         "no-event", "no-summary", "no-inline", "summary-maybe", "head-not-hex", "head-6-hex",
         "inline-shape", "anchors-shape", "posting-twice", "skipping-twice",
         "item-number-10-digits", "item-line-10-digits", "posting-10-digits"],
)
def test_dry_run_errors(text, why):
    with pytest.raises(PosterOutputError, match=why):
        parse_review_dry_run(text)


def test_dry_run_blank_line_is_known():
    assert parse_review_dry_run(DRY.replace(ANCHORS, ANCHORS + "\n")) == parse_review_dry_run(DRY)


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        (C88, C88 + ' "start_line": 10, "start_side": "RIGHT",', "a comment needs"),
        (C88, C88 + ' "position": 3,', "a comment needs"),
        (C88, C88.replace('"RIGHT"', '"LEFT"'), "a comment needs"),
        (C88, C88.replace('"RIGHT"', '"SIDEWAYS"'), "a comment needs"),
        (C88, C88.replace('"line": 88', '"line": -5'), "a comment needs"),
        (C88, C88.replace('"line": 88', '"line": 0'), "a comment needs"),
        (C88, C88.replace('"line": 88', '"line": true'), "a comment needs"),
        ('{"commit_id"', '{"draft": true, "commit_id"', "batch 1/1 has the unknown key draft"),
        ('"body": "Two real', '"comments": [], "body": "Two real', "batch 1/1 has the key comments twice"),
        (C88, C88 + ' "line": 3,', "batch 1/1 has the key line twice"),
        ('"event": "APPROVE",', '"event": "APPROVE", "event": "COMMENT",', "batch 1/1 has the key event twice"),
        (C88, C88 + ' "extra": NaN,', "batch 1/1 has NaN"),
    ],
    ids=["start-line", "position", "side-left", "side-sideways", "line-negative", "line-zero", "line-true",
         "top-level-draft", "comments-twice", "line-twice", "event-twice", "nan"],
)
def test_payload_not_understood(old, new, why):
    with pytest.raises(PosterOutputError, match=why):
        parse_review_dry_run(DRY.replace(old, new, 1))


def test_payload_body_null():
    top_body = DRY.split("\n")[-2]
    assert top_body.startswith(' "body": "Two real problems')
    with pytest.raises(PosterOutputError, match="batch 1/1 has a body that is not a str"):
        parse_review_dry_run(DRY.replace(top_body, ' "body": null}'))


def test_item_without_a_line():
    items = parse_review_dry_run(_with_item("  #03 app/x.kt  - Wrong file")).items
    assert DryItem(3, "app/x.kt", None, "Wrong file") in items


def test_nine_digits_are_a_number():
    items = parse_review_dry_run(_with_item("  #123456789 app/x.kt:999999999  - t")).items
    assert DryItem(123456789, "app/x.kt", 999999999, "t") in items


def test_long_run_of_zeros_is_linear():
    start = time.perf_counter()
    with pytest.raises(PosterOutputError, match="unknown line"):
        parse_review_dry_run(_with_item("  #" + "0" * 200_000 + "x"))
    items = parse_review_dry_run(_with_item("  #" + "0" * 200_000 + "7 app/x.kt:1  - t")).items
    assert time.perf_counter() - start < 1
    assert DryItem(7, "app/x.kt", 1, "t") in items


def test_cap_boundary():
    exact = DRY + " " * (2_000_000 - len(DRY))
    assert len(exact) == 2_000_000
    assert parse_review_dry_run(exact) == parse_review_dry_run(DRY)


def test_deep_nesting_is_not_understood():
    deep = '{"a": ' * 100_000 + "1" + "}" * 100_000
    head = DRY.split(BATCH, 1)[0]
    with pytest.raises(PosterOutputError, match="batch 1/1 is not one JSON object"):
        parse_review_dry_run(head + BATCH + deep + "\n")
    with pytest.raises(PosterOutputError, match="not one JSON list"):
        parse_replies_steps("[" * 100_000 + "]" * 100_000)
    with pytest.raises(PosterOutputError, match="not one JSON list"):
        parse_replies_results("[" * 100_000 + "]" * 100_000)


def test_parse_review_confirm():
    text = "Posted batch 1/2: https://x/1\n  app/a.kt:3\nPosted batch 2/2: https://x/2\n"
    assert parse_review_confirm(text) == [(1, 2, "https://x/1"), (2, 2, "https://x/2")]
    with pytest.raises(PosterOutputError, match="unknown line: WARNING: slow"):
        parse_review_confirm("Posted batch 1/1: https://x/1\nWARNING: slow\n")


def test_confirm_stops_early_after_a_failed_batch():
    text = "Posted batch 1/3: https://x/1\n  app/a.kt:3\nPosted batch 2/3: https://x/2\n"
    assert parse_review_confirm(text) == [(1, 3, "https://x/1"), (2, 3, "https://x/2")]
    assert parse_review_confirm("") == []


@pytest.mark.parametrize(
    "text",
    [
        "Posted batch 2/1: https://x/1\n",
        "Posted batch 0/0: https://x/1\n",
        "Posted batch 1/2: https://x/1\nPosted batch 1/2: https://x/1\n",
        "Posted batch 2/2: https://x/2\nPosted batch 1/2: https://x/1\n",
        "Posted batch 1/2: https://x/1\nPosted batch 2/3: https://x/2\n",
    ],
    ids=["2-of-1", "0-of-0", "1-of-2-twice", "2-before-1", "total-changes"],
)
def test_confirm_sequence(text):
    with pytest.raises(PosterOutputError, match="out of order"):
        parse_review_confirm(text)


def test_confirm_location_before_any_batch():
    with pytest.raises(PosterOutputError, match="before any batch:   app/a.kt:3"):
        parse_review_confirm("  app/a.kt:3\nPosted batch 1/1: https://x/1\n")


def test_parse_replies():
    assert parse_replies_steps(STEPS) == json.loads(STEPS)
    assert len(parse_replies_steps(STEPS)) == 3
    assert parse_replies_results(RESULTS) == json.loads(RESULTS)
    assert len(parse_replies_results(RESULTS)) == 3
    for text in ("Plan OK\n" + STEPS, STEPS + "\ndone"):
        with pytest.raises(PosterOutputError):
            parse_replies_steps(text)
    with pytest.raises(PosterOutputError):
        parse_replies_results(RESULTS + "\ndone")
    with pytest.raises(PosterOutputError):
        parse_replies_steps(STEPS.replace('"step": "resolve"', '"step": "close"'))
    with pytest.raises(PosterOutputError):
        parse_replies_results(RESULTS.replace('"resolved": true', '"replied": "https://x/1", "resolved": true'))


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (STEPS.replace('"body": "Fixed."', '"body": "Fixed.", "resolve": true'), "step 1 .* not understood"),
        (STEPS.replace('"step": "reply", ', '"step": "reply", "step": "comment", '), "has the key step twice"),
        (STEPS.replace('"n": 3', '"n": NaN'), "has NaN"),
    ],
    ids=["unknown-key", "step-twice", "nan"],
)
def test_replies_steps_not_understood(text, why):
    with pytest.raises(PosterOutputError, match=why):
        parse_replies_steps(text)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (RESULTS.replace('{"n": 1, "resolved": true}', '{"n": 1, "resolved": true, "thread": "t"}'),
         "result 2 .* not understood"),
        (RESULTS.replace('{"n": 1, "resolved": true}', '{"n": 1, "n": 2, "resolved": true}'), "has the key n twice"),
        (RESULTS.replace('"n": 3', '"n": -Infinity'), "has -Infinity"),
    ],
    ids=["unknown-key", "n-twice", "infinity"],
)
def test_replies_results_not_understood(text, why):
    with pytest.raises(PosterOutputError, match=why):
        parse_replies_results(text)

# tests/unit/test_reviewer_report.py
from __future__ import annotations

import json
import re
import time

import pytest

from orchestrator.reviewer.markdown import fenced, split_lines
from orchestrator.reviewer.report import (
    Report,
    ReportError,
    parse_replies_plan,
    parse_report,
    parse_tg_lines,
)

HEAD = "26c4929c5" + "a" * 31

REPORT = f"""# Review: PR #1712 Fix cart sync

**Head SHA:** `{HEAD}`
**Candidates:** 9 (xhigh 6, rules 4, both 1) -> 3 confirmed · 2 questions · 1 pre-existing · 3 dropped
**Suggested event:** REQUEST_CHANGES - crash on an empty server response

## 🔴 Critical

### [ ] 1. NPE on an empty list
**File:** `app/src/main/CartRepository.kt:88`
The list is read before the null check.

---

## 🟠 Repo rules

### [ ] 2. Retry without backoff
**File:** `app/src/main/SyncWorker.kt:41` (anchor moves to line 43)
Network retry rule.

## 🟡 Minor

### [ ] 3. Date parsed in the local zone
**File:** `app/src/main/CartMapper.kt:17`

## ❓ Questions

### [ ] 4. Double refresh on rotation?
**File:** `app/src/main/CartViewModel.kt:60`

### [ ] 5. Untranslated key?
**File:** `app/src/main/res/values/strings.xml:212` (outside the diff - goes to the review body)

## 🟣 Pre-existing

### [ ] 6. Cursor leak
**File:** `app/src/main/LegacySync.kt:300`
**Ticket:** ACME-99

## Dropped (never posted)
- 7. Null check in the adapter - unreachable (xhigh)
- 8. Same as 1 - duplicate (rules)
- 9. Old warning - pre-dates the PR (xhigh)

## ✅ Looks Good
- Sync is idempotent.

## Summary
Two real problems in the sync path.
"""

def test_parse_full_report():
    r = parse_report(REPORT, HEAD)
    assert (r.title, r.event, r.event_reason, r.candidates_total) == (
        "Review: PR #1712 Fix cart sync", "REQUEST_CHANGES", "crash on an empty server response", 9)
    assert [(i.number, i.kind) for i in r.items] == [
        (1, "red"), (2, "orange"), (3, "yellow"), (4, "question"), (5, "question"), (6, "preexisting")]
    assert (r.items[0].path, r.items[0].line, r.items[0].note) == ("app/src/main/CartRepository.kt", 88, "")
    assert r.items[0].body.endswith("The list is read before the null check.")
    assert r.items[1].note == "moves to 43" and r.items[4].note == "outside the diff"
    assert [(d.number, d.why) for d in r.dropped] == [(7, "unreachable"), (8, "duplicate"), (9, "pre-dates the PR")]
    assert r.summary == "Two real problems in the sync path." and "idempotent" in r.looks_good
    assert Report.from_dict(json.loads(json.dumps(r.to_dict()))) == r

@pytest.mark.parametrize("text, message", [
    (REPORT.replace(HEAD, "b" * 40), "report is for head"),
    (REPORT.replace("**Head SHA:**", "**Head:**"), "report is for head"),
    (REPORT.replace("REQUEST_CHANGES - crash", "MAYBE - crash"), "no suggested event"),
    (REPORT.replace("## Summary\n", "## Closing\n"), "no ## Summary"),
    (REPORT.replace("- 9. Old warning", "- 3. Old warning"), "number 3 is used twice"),
    (REPORT.replace("## 🟡 Minor", "## Notes"), "item 3 is outside a findings section"),
])
def test_report_errors(text, message):
    with pytest.raises(ReportError, match=message):
        parse_report(text, HEAD)

def test_short_head_prefix_accepted():
    assert parse_report(REPORT.replace(HEAD, HEAD[:9]), HEAD).head_sha == HEAD[:9]

def test_replies_plan():
    plan = [{"n": 1, "thread_id": "T1", "body": "Fixed in abc.", "resolve": True}, {"n": 3, "review_id": 456, "body": "Agreed."}]
    assert parse_replies_plan(json.dumps(plan)) == plan
    for bad in ("{}", "[1]", '[{"n": "1", "body": "x"}]', '[{"n": 1}]', "not json"):
        with pytest.raises(ReportError, match="replies plan unreadable"):
            parse_replies_plan(bad)

def test_tg_lines():
    text = json.dumps({"1": "NPE when\nthe list is empty", "x": "dropped", "7": "u" * 300})
    assert parse_tg_lines(text) == {1: "NPE when the list is empty", 7: "u" * 200}
    assert parse_tg_lines("[") == {} and parse_tg_lines("[1]") == {}


# The model's text is untrusted: hostile input is bounded and only ever raises ReportError.

DROPPED_9 = "- 9. Old warning - pre-dates the PR (xhigh)"
BRIEF_DROPPED = re.compile(r"^- (\d+)\. (.+?) - (.+?)(?: \([^)]*\))?$")


def test_hostile_dropped_line_parses_in_linear_time():
    hostile = "- 9. a - b" + " (" * 100_000  # 200 010 characters, never closed
    start = time.monotonic()
    r = parse_report(REPORT.replace(DROPPED_9, hostile), HEAD)
    assert time.monotonic() - start < 1
    assert (r.dropped[2].number, r.dropped[2].text, r.dropped[2].why) == (9, "a", "b" + " (" * 100_000)


@pytest.mark.parametrize("line", [
    DROPPED_9,
    "- 9. a - b - c (xhigh)",
    "- 9. a - see (foo) here (xhigh)",
    "- 9. a - why (a) (b)",
    "- 9. a - why (a (b)",
    "- 9. a - why ()",
    "- 9. a - why",
    "- 9. a - (xhigh)",
    "- 9. a - why (xhigh) ",
    "- 9. a -  (xhigh)",
])
def test_dropped_split_matches_the_brief_regex(line):
    m = BRIEF_DROPPED.match(line)
    d = parse_report(REPORT.replace(DROPPED_9, line), HEAD).dropped[2]
    assert (d.number, d.text, d.why) == (int(m[1]), m[2], m[3])


def test_dropped_em_dash_separator():
    d = parse_report(REPORT.replace("Old warning - pre-dates", "Old warning — pre-dates"), HEAD).dropped[2]
    assert (d.text, d.why) == ("Old warning", "pre-dates the PR")


def test_report_size_cap():
    at_cap = REPORT + "x" * (1_000_000 - len(REPORT))
    assert parse_report(at_cap, HEAD).summary.endswith("x")
    with pytest.raises(ReportError, match="report too large"):
        parse_report(at_cap + "x", HEAD)


@pytest.mark.parametrize("old, new, message", [
    ("### [ ] 3. Date", "### [ ]3. Date",
     "item heading not understood: ### [ ]3. Date parsed in the local zone"),
    ("### [ ] 3. Date", "### [v] 3. Date",
     "item heading not understood: ### [v] 3. Date parsed in the local zone"),
    ("### [ ] 3. Date parsed in the local zone", "### [ ] 3 " + "x" * 100,
     "item heading not understood: ### [ ] 3 " + "x" * 50),
    ("## 🟡 Minor", "### 🟡 Minor", "kind heading at level 3: ### 🟡 Minor"),
    (DROPPED_9, "- 9. Old warning: pre-dates the PR (xhigh)",
     "dropped line not understood: - 9. Old warning: pre-dates the PR (xhigh)"),
    (DROPPED_9, "- 9.Old warning - pre-dates the PR (xhigh)",
     "dropped line not understood: - 9.Old warning - pre-dates the PR (xhigh)"),
])
def test_off_format_lines_fail_loudly(old, new, message):
    with pytest.raises(ReportError) as e:
        parse_report(REPORT.replace(old, new), HEAD)
    assert str(e.value) == message


def test_report_numbers_are_ascii_digits():
    text = REPORT.replace("**Candidates:** 9", "**Candidates:** ٩").replace("CartMapper.kt:17", "CartMapper.kt:١٧")
    r = parse_report(text, HEAD)
    assert r.candidates_total is None
    assert (r.items[2].path, r.items[2].line) == ("app/src/main/CartMapper.kt:١٧", None)
    with pytest.raises(ReportError, match=re.escape("item heading not understood: ### [ ] ٣. Date")):
        parse_report(REPORT.replace("### [ ] 3.", "### [ ] ٣."), HEAD)


def test_tg_lines_keys_are_ascii_digits():
    text = json.dumps({" 7 ": "a", "1_0": "b", "-1": "c", "٧": "d", "9" * 5000: "e", "8": "f"})
    assert parse_tg_lines(text) == {8: "f"}


def test_to_dict_is_json_shaped():
    r = parse_report(REPORT, HEAD)
    d = r.to_dict()
    assert d == json.loads(json.dumps(d))
    assert Report.from_dict(d) == r


def test_head_error_echoes_at_most_40_characters():
    with pytest.raises(ReportError) as e:
        parse_report(REPORT.replace(HEAD, "c" * 5000), HEAD)
    assert str(e.value) == f"report is for head {'c' * 40}, not {HEAD[:9]}"


@pytest.mark.parametrize("digits", ["1234567890", "9" * 5000], ids=["10-digits", "5000-digits"])
@pytest.mark.parametrize("old, new", [
    ("### [ ] 1.", "### [ ] {}."),
    ("CartRepository.kt:88", "CartRepository.kt:{}"),
    ("**Candidates:** 9", "**Candidates:** {}"),
    ("- 7.", "- {}."),
])
def test_number_longer_than_nine_digits(old, new, digits):
    with pytest.raises(ReportError, match="^number too long$"):
        parse_report(REPORT.replace(old, new.format(digits)), HEAD)


def test_nine_digit_numbers_parse():
    text = (REPORT.replace("### [ ] 1.", "### [ ] 123456789.")
            .replace("CartRepository.kt:88", "CartRepository.kt:987654321")
            .replace("**Candidates:** 9", "**Candidates:** 999999999").replace("- 7.", "- 111111111."))
    r = parse_report(text, HEAD)
    assert (r.items[0].number, r.items[0].line, r.candidates_total, r.dropped[0].number) == (
        123456789, 987654321, 999999999, 111111111)


def test_deeply_nested_json_is_unreadable():
    deep = "[" * 100_000
    with pytest.raises(ReportError, match="replies plan unreadable"):
        parse_replies_plan(deep)
    assert parse_tg_lines(deep) == {}


def test_replies_plan_rejects_bool_n():
    with pytest.raises(ReportError, match="replies plan unreadable"):
        parse_replies_plan('[{"n": true, "body": "x"}]')


def test_replies_plan_rejects_a_duplicate_n():
    # "post replies 1 3" names entries by n: two entries under one n would make a reply ambiguous.
    with pytest.raises(ReportError, match="^replies plan unreadable$"):
        parse_replies_plan('[{"n": 1, "body": "x"}, {"n": 2, "body": "y"}, {"n": 1, "body": "z"}]')


def test_em_dash_event():
    r = parse_report(REPORT.replace("REQUEST_CHANGES - crash", "REQUEST_CHANGES — crash"), HEAD)
    assert (r.event, r.event_reason) == ("REQUEST_CHANGES", "crash on an empty server response")


def test_checked_item_headings():
    text = REPORT.replace("### [ ] 1.", "### [x] 1.").replace("### [ ] 2.", "### [X] 2.")
    assert [i.number for i in parse_report(text, HEAD).items] == [1, 2, 3, 4, 5, 6]


def test_path_without_line():
    item = parse_report(REPORT.replace("CartRepository.kt:88`", "CartRepository.kt`"), HEAD).items[0]
    assert (item.path, item.line) == ("app/src/main/CartRepository.kt", None)


def test_item_in_header_is_outside_findings():
    text = REPORT.replace("**Suggested event:**", "### [ ] 10. Stray\n**Suggested event:**")
    with pytest.raises(ReportError, match="item 10 is outside a findings section"):
        parse_report(text, HEAD)


# --- Code-review fix wave 1 ---

ITEMS = [(1, "red"), (2, "orange"), (3, "yellow"), (4, "question"), (5, "question"), (6, "preexisting")]

@pytest.mark.parametrize("block", [
    "```md\n## Setup\n### [ ] 9. x\n```",
    "~~~\n## Setup\n---\n### 🔴 not a kind heading\n~~~",
    "````md\n```\n## Setup\n### [ ] 9. x\n````",   # a shorter fence line does not close it
])
def test_a_fenced_block_in_a_finding_never_starts_a_section_or_an_item(block):
    """A finding may quote Markdown or a `## ` comment: inside a fence it is the finding's text."""
    body = "The list is read before the null check.\n"
    r = parse_report(REPORT.replace(body, f"{body}{block}\n"), HEAD)
    assert [(i.number, i.kind) for i in r.items] == ITEMS
    assert r.items[0].body.endswith(f"{body}{block}")
    assert (r.items[2].number, r.items[2].title) == (3, "Date parsed in the local zone")

def test_fenced_lines_outside_the_findings_are_text():
    text = (REPORT.replace("- 9. Old warning - pre-dates the PR (xhigh)\n",
                           "- 9. Old warning - pre-dates the PR (xhigh)\n```\n- 10. quoted\n```\n")
                  .replace("Two real problems in the sync path.\n",
                           "Two real problems in the sync path.\n~~~\n### [ ] 11. quoted\n~~~\n"))
    r = parse_report(text, HEAD)
    assert [d.number for d in r.dropped] == [7, 8, 9]
    assert r.summary.endswith("~~~\n### [ ] 11. quoted\n~~~")


# --- Plan 5: a drafted number ---

DRAFT_7 = "### [ ] 7. Null check in the adapter\n**File:** `app/src/main/Adapter.kt:12`\nIs this reachable?\n\n"
DRAFTED = REPORT.replace("## 🟣 Pre-existing", DRAFT_7 + "## 🟣 Pre-existing")

def test_drafted_number_is_a_question_item_and_a_dropped_bullet():
    r = parse_report(DRAFTED, HEAD, frozenset({7}))
    assert [(i.number, i.kind) for i in r.items] == [*ITEMS[:5], (7, "question"), ITEMS[5]]
    assert [d.number for d in r.dropped] == [7, 8, 9]

@pytest.mark.parametrize("text, drafted", [
    (DRAFTED, frozenset()),
    (DRAFTED, frozenset({8})),
    (DRAFTED.replace("- 8. Same as 1", "- 7. Same as 1"), frozenset({7})),
    (DRAFTED.replace("## 🟣 Pre-existing", DRAFT_7 + "## 🟣 Pre-existing"), frozenset({7})),
    (REPORT.replace("## 🟡 Minor\n", "## 🟡 Minor\n\n" + DRAFT_7), frozenset({7})),
], ids=["not-drafted", "another-drafted", "dropped-twice", "two-items", "not-a-question-item"])
def test_drafted_number_repeats_only_as_one_question_and_one_dropped(text, drafted):
    with pytest.raises(ReportError, match="^number 7 is used twice$"):
        parse_report(text, HEAD, drafted)


# --- Plan 5, fix round 1: a backtick fence's info string holds no backtick (CommonMark 4.5) ---

NPE_BODY = "The list is read before the null check.\n"

def test_a_backtick_info_string_with_a_backtick_opens_no_fence():
    assert fenced(["```x`", "@octo", "~~~ x`", "a", "~~~"]) == [False, False, True, True, True]
    r = parse_report(REPORT.replace(NPE_BODY, NPE_BODY + "```x`\n"), HEAD)
    assert [(i.number, i.kind) for i in r.items] == ITEMS
    assert r.items[0].body.endswith("```x`")

def test_items_behind_a_backtick_info_line_fail_closed():
    # It used to open a fence that a later ``` closed, hiding items 2 and 3 (a ticked [x] 2 among them).
    text = (REPORT.replace(NPE_BODY, NPE_BODY + "```x`\n").replace("### [ ] 2. Retry", "### [x] 2. Retry")
                  .replace("CartMapper.kt:17`\n", "CartMapper.kt:17`\n```\n"))
    with pytest.raises(ReportError, match="^no ## Summary$"):
        parse_report(text, HEAD)


# --- Plan 5, fix round 2: fences and lines as cmark reads them ---

NBSP, LS, PS = chr(0xA0), chr(0x2028), chr(0x2029)  # no-break space, line and paragraph separators

def test_a_closer_takes_only_spaces_and_tabs_after_it():
    assert fenced(["```", "x", "```" + NBSP, "y", "``` \t", "z"]) == [True, True, True, True, True, False]

def test_lines_split_as_commonmark_splits_them():
    text =f"a\r\nb\rc\nd{LS}e\x0cf\x85g\x0bh{PS}i\n\n"
    assert split_lines(text) == ["a", "b", "c", f"d{LS}e\x0cf\x85g\x0bh{PS}i", ""]
    assert split_lines(text, keepends=True) == ["a\r\n", "b\r", "c\n", f"d{LS}e\x0cf\x85g\x0bh{PS}i\n", "\n"]
    assert split_lines("") == [] and split_lines("x") == ["x"]
    r = parse_report(REPORT, HEAD)
    assert parse_report(REPORT.replace("\n", "\r\n"), HEAD) == r == parse_report(REPORT.replace("\n", "\r"), HEAD)
    # A U+2028 or a form feed breaks no line: what follows it on the line is the body's text, not a section.
    odd = parse_report(REPORT.replace(NPE_BODY, f"Read{LS}## Summary\x0cfirst.\n"), HEAD)
    assert odd.items[0].body.endswith(f"\nRead{LS}## Summary\x0cfirst.")
    assert odd.summary == r.summary and [i.number for i in odd.items] == [1, 2, 3, 4, 5, 6]

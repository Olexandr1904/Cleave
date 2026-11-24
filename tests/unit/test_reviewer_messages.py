# tests/unit/test_reviewer_messages.py
from __future__ import annotations
import dataclasses
from datetime import datetime, timezone
from orchestrator.reviewer.lines import (
    drift_line, job_deferred_line, job_failed_line, login_line, outdated_line)
from integrations.base.notifier import Button
from orchestrator.reviewer.messages import (
    address_message, findings_message, fix_buttons, message_parts, recheck_message, review_message, split_message)
from orchestrator.reviewer.report import parse_report
from tests.unit.test_reviewer_report import HEAD, REPORT

URL = "https://github.com/acme-org/acme-app/pull/1712"
TG = {1: "NPE when the server returns an empty list", 2: "retry without backoff (network retry rule)",
      3: "date parsed in the local zone instead of UTC", 4: "refresh called twice on rotation?",
      5: "key not translated?", 6: "old cursor leak (not from this PR)", 7: "unreachable", 8: "duplicate",
      9: "pre-dates the PR"}
BARE = f"""# Review: PR #7 Bump deps

**Head SHA:** `{HEAD}`
**Suggested event:** APPROVE - nothing to fix

## Summary
Clean.
"""
REPLIES = [{"n": 1, "thread_id": "T1", "body": "Fixed in abc.", "resolve": True},
           {"n": 3, "review_id": 456, "body": "Agreed, renamed.\nSee the second commit."}]

def review(report=None, tg_lines=TG, files=14, ticket_key="ACME-1234"):
    return review_message("acme/app", 1712, "Fix cart sync", ticket_key, URL, files,
                          report or parse_report(REPORT, HEAD), tg_lines)

def test_review_message():
    assert review() == """[acme/app] Review of PR #1712 "Fix cart sync" · ACME-1234
head 26c4929c5 · 14 files · 9 candidates → 3 confirmed · 2 questions · 1 pre-existing · 3 dropped
Suggested: REQUEST_CHANGES — crash on an empty server response

🔴 1. CartRepository.kt:88 — NPE when the server returns an empty list
🟠 2. SyncWorker.kt:41 — retry without backoff (network retry rule)
🟡 3. CartMapper.kt:17 — date parsed in the local zone instead of UTC
❓ 4. CartViewModel.kt:60 — refresh called twice on rotation?
❓ 5. strings.xml:212 — key not translated?
🟣 6. LegacySync.kt:300 — old cursor leak (not from this PR)
Dropped: 7 — unreachable; 8 — duplicate; 9 — pre-dates the PR

Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"
https://github.com/acme-org/acme-app/pull/1712"""

def test_review_message_already_posted_line():
    report = parse_report(REPORT, HEAD)
    def lines(**posted):
        return review_message("acme/app", 1712, "Fix cart sync", "ACME-1234", URL, 14, report, TG,
                              **posted).splitlines()
    plan4 = review().splitlines()
    got = lines(posted_at_head=3, summary_posted=True)
    assert got[3] == "Already posted at this head: 3 comments and the Summary"
    assert got[:3] + got[4:] == plan4   # one line more, after the Suggested: line
    assert lines(posted_at_head=1)[3] == "Already posted at this head: 1 comment"
    assert lines(summary_posted=True)[3] == "Already posted at this head: 0 comments and the Summary"
    assert lines() == plan4 and plan4[3] == ""   # the defaults: the plan-4 text unchanged

def test_review_message_falls_back_to_titles():
    lines = review(tg_lines={}).splitlines()
    assert "🔴 1. CartRepository.kt:88 — NPE on an empty list" in lines
    assert "Dropped: 7 — unreachable; 8 — duplicate; 9 — pre-dates the PR" in lines

def test_review_message_item_without_line_or_path():
    r = parse_report(REPORT, HEAD)
    items = (dataclasses.replace(r.items[0], line=None), dataclasses.replace(r.items[1], path=""))
    lines = review(dataclasses.replace(r, items=items), tg_lines={}).splitlines()
    assert "🔴 1. CartRepository.kt — NPE on an empty list" in lines
    assert "🟠 2. Retry without backoff" in lines

def test_review_message_without_findings():
    text = review(parse_report(BARE, HEAD), tg_lines={}, files=1, ticket_key="")
    lines = text.splitlines()
    assert lines[0] == '[acme/app] Review of PR #1712 "Fix cart sync"'
    assert lines[1] == "head 26c4929c5 · 1 file · 0 candidates → 0 confirmed · 0 questions · 0 pre-existing · 0 dropped"
    assert "No findings." in lines and "Dropped:" not in text

def test_recheck_message():
    text = recheck_message("acme/app", 1712, "Fix cart sync", URL, HEAD, REPLIES,
                           {1: "fixed · CartRepository.kt:88 · null check added"})
    assert text == "\n".join([
        '[acme/app] Re-check of PR #1712 "Fix cart sync"',
        "head 26c4929c5 · 2 drafted replies",
        "",
        "1. fixed · CartRepository.kt:88 · null check added",
        "3. Agreed, renamed.",
        "",
        'Reply to this message: "post replies 1 3" · "text 1"',  # /code-review R3
        URL])
    one = recheck_message("acme/app", 1712, "Fix cart sync", URL, HEAD, REPLIES[:1], {})
    assert "head 26c4929c5 · 1 drafted reply" in one.splitlines()
    # The text example names a drafted reply: the first one.
    later = recheck_message("acme/app", 1712, "Fix cart sync", URL, HEAD, REPLIES[1:], {})
    assert 'Reply to this message: "post replies 3" · "text 3"' in later.splitlines()

def test_recheck_message_empty():
    text = recheck_message("acme/app", 1712, "Fix cart sync", URL, HEAD, [], {})
    assert "head 26c4929c5 · 0 drafted replies" in text.splitlines()
    assert "No replies drafted." in text.splitlines() and "Reply to this message" not in text

def test_long_texts_are_cut():
    # A hostile report or plan must not turn one line into many Telegram parts.
    r = parse_report(REPORT, HEAD)
    items = (dataclasses.replace(r.items[0], title="T" * 5000, path="app/" + "p" * 5000),) + r.items[1:]
    dropped = (dataclasses.replace(r.dropped[0], why="W" * 5000),) + r.dropped[1:]
    hostile = dataclasses.replace(r, items=items, dropped=dropped, event_reason="R" * 5000)
    lines = review_message("acme/app", 1712, "P" * 5000, "ACME-1234", URL, 14, hostile,
                           {2: "G" * 5000}).splitlines()
    # A cut text ends with "…", so a line never reads as whole when it is not.
    assert lines[0] == f'[acme/app] Review of PR #1712 "{"P" * 200}…" · ACME-1234'
    assert lines[2] == f"Suggested: REQUEST_CHANGES — {'R' * 200}…"
    assert f"🔴 1. {'p' * 200}…:88 — {'T' * 200}…" in lines
    assert f"🟠 2. SyncWorker.kt:41 — {'G' * 200}…" in lines
    assert f"Dropped: 7 — {'W' * 200}…; 8 — duplicate; 9 — pre-dates the PR" in lines
    assert review_message("acme/app", 1712, "Fix cart sync", "", URL, 14, r, {8: "D" * 5000}).splitlines()[-4] == (
        f"Dropped: 7 — unreachable; 8 — {'D' * 200}…; 9 — pre-dates the PR")
    replies = [{"n": 1, "body": "B" * 5000 + "\nsecond line"}, {"n": 3, "body": "short"}]
    lines = recheck_message("acme/app", 1712, "P" * 5000, URL, HEAD, replies, {3: "G" * 5000}).splitlines()
    assert lines[0] == f'[acme/app] Re-check of PR #1712 "{"P" * 200}…"'
    assert f"1. {'B' * 200}…" in lines and f"3. {'G' * 200}…" in lines
    assert f"3. {'G' * 200}" in recheck_message("acme/app", 1712, "t", URL, HEAD, replies, {3: "G" * 200}).splitlines()

def test_split_message():
    text = "".join(f"{i:02d}{'x' * 97}\n" for i in range(90))
    assert len(text) == 9000
    parts = split_message(text)
    assert len(parts) == 3 and all(len(p) <= 4096 for p in parts)
    assert "\n".join(parts) == text
    line = "y" * 5000
    assert [len(p) for p in split_message(line)] == [4096, 904] and "".join(split_message(line)) == line
    assert split_message("y" * 8192) == ["y" * 4096] * 2

def test_split_message_counts_utf16_units():
    # Telegram counts UTF-16 code units: an emoji outside the BMP is 2.
    def units(s):
        return len(s.encode("utf-16-le")) // 2
    assert split_message("\n".join(["😀" * 1000] * 3)) == ["😀" * 1000 + "\n" + "😀" * 1000, "😀" * 1000]
    line = "a" + "😀" * 3000
    parts = split_message(line)
    assert parts[0] == "a" + "😀" * 2047 and "".join(parts) == line  # cut between code points
    assert [units(p) for p in parts] == [4095, 1906]
    assert split_message("\ud83d" * 5, limit=4) == ["\ud83d" * 4, "\ud83d"]  # a lone surrogate never raises

def more(k):
    return f"… {k} more line{'' if k == 1 else 's'} — the full report is on the dashboard"

def test_message_parts_up_to_ten_are_the_split():
    text = "\n".join(f"{i:03d} " + "x" * 96 for i in range(300))   # 8 parts of 3900
    assert message_parts(text, 3900) == split_message(text, 3900)
    assert len(message_parts(text, 3900)) == 8

def test_message_parts_cap_at_ten_with_a_closing_line():
    lines = [f"{i:03d} " + "x" * 96 for i in range(600)]   # 60,000 characters: 16 parts of 3900
    text = "\n".join(lines)
    assert len(split_message(text, 3900)) > 10
    parts = message_parts(text, 3900)
    assert len(parts) == 10 and max(len(p) for p in parts) <= 3900
    shown = "\n".join(parts).split("\n")
    closing = shown.pop()
    k = len(lines) - len(shown)
    assert shown == lines[:len(shown)] and parts[9].endswith("\n" + closing) and closing == more(k)
    # As many lines as fit: one more would need an 11th part.
    assert len(split_message("\n".join([*lines[:len(shown) + 1], more(k - 1)]), 3900)) > 10

def test_message_parts_cut_a_line_too_long_for_the_rest():
    text = "\n".join(["header", "D" * 100_000, "hint", "url"])   # the long line alone needs 26 parts
    parts = message_parts(text, 3900)
    # The long line fills the parts it gets, in the pieces split_message cuts it into; cut short, it counts
    # as left out.
    assert parts == ["header", *["D" * 3900] * 8, more(3)]

def test_job_lines():
    assert job_failed_line("acme/app", 1712, "review", "exit 1: boom") == (
        "[acme/app] #1712: review failed — exit 1: boom")
    assert job_deferred_line("acme/app", 1712, "recheck", datetime(2026, 11, 20, 11, 5, tzinfo=timezone.utc)) == (
        "[acme/app] #1712: recheck deferred — usage limit reached; next try at 11:05 UTC")
    assert login_line() == (
        "[reviewer] claude is not logged in on the host. Log in from a GUI Terminal (run claude, then /login);"
        " reviews wait until then.")
    assert drift_line("acme/app", "docs/review/code-review-process.md", "Prepare") == (
        '[acme/app] The "Prepare" section of docs/review/code-review-process.md changed on the base branch:'
        " check that Cleave's Prepare still matches it.")


def test_outdated_line_names_the_commands_that_still_run():
    assert outdated_line("deferred", "usage limit") == (
        'This job ended deferred: only "review again" and "skip" work here. Nothing was done.')
    assert outdated_line("superseded", "new head") == "Outdated: there is a review of a newer head. Nothing was done."


def test_address_message():
    replies = [{"n": 1, "thread_id": "T1", "body": "Fixed in {fix_sha}: x.", "resolve": True, "fixed": True},
               {"n": 2, "thread_id": "T2", "body": "Intended.", "resolve": False}]
    fix = {"files": [{"path": "a.kt", "added": 24, "deleted": 7}], "protected": [], "bytes": 900}
    text = address_message("acme/app", 1712, "Fix cart sync", URL, "8f01a2b3c" + "0" * 31, replies,
                           {1: "FIXED · CartRepo.kt:88 · null list handled"}, fix)
    assert text == f"""[acme/app] Threads on your PR #1712 "Fix cart sync"
head 8f01a2b3c · 2 drafted replies · fix: 1 file, +24 −7 — not built or tested here

1. FIXED · CartRepo.kt:88 · null list handled
2. Intended.

Reply to this message: "push" · "post replies 1 2" · "text 1" · "diff" · "again: …"
{URL}"""
    none = address_message("acme/app", 1712, "t", URL, "8f01a2b3c" + "0" * 31, replies, {}, {})
    assert "no code change" in none and '"push"' not in none and '"diff"' not in none
    assert 'Reply to this message: "post replies 1 2" · "text 1" · "again: …"' in none
    guarded = address_message("acme/app", 1712, "t", URL, "8" * 40, replies, {}, {**fix, "protected": [".github/ci.yml"]})
    assert guarded.splitlines()[2] == 'Protected files in the fix (.github/ci.yml): Cleave pushes it only on "push anyway".'
    assert 'Reply to this message: "push anyway" · "post replies 1 2" · "text 1" · "diff" · "again: …"' in guarded
    empty = address_message("acme/app", 1712, "t", URL, "8" * 40, [], {}, fix)
    assert "No replies drafted." in empty and 'Reply to this message: "push" · "diff" · "again: …"' in empty


# --- Findings jobs ---

VERDICTS = [{"n": 2, "verdict": "FIXED", "why": "null check added"},
            {"n": 3, "verdict": "WON'T FIX", "why": "intended"}]
FINDINGS_FIX = {"files": [{"path": "a.kt", "added": 24, "deleted": 7}], "protected": [], "bytes": 900}


def test_findings_message():
    text = findings_message("acme/app", 108, "Fix cart sync", URL, "8f01a2b3c" + "0" * 31, VERDICTS,
                            {2: "FIXED · CartRepo.kt:88 · null list handled"}, FINDINGS_FIX)
    assert text == f"""[acme/app] Self-review findings on your PR #108 "Fix cart sync"
head 8f01a2b3c · 2 findings · fix: 1 file, +24 −7 — not built or tested here

2. FIXED · CartRepo.kt:88 · null list handled
3. WON'T FIX · intended

Reply to this message: "push" · "diff" · "again: …"
{URL}"""


def test_findings_message_without_a_patch_offers_no_push():
    text = findings_message("acme/app", 108, "t", URL, "8" * 40, VERDICTS[:1], {}, {})
    assert text.splitlines()[1] == "head 888888888 · 1 finding · no code change"
    assert '"push"' not in text and '"diff"' not in text
    assert 'Reply to this message: "again: …"' in text


def test_findings_message_with_a_protected_file_offers_push_anyway():
    text = findings_message("acme/app", 108, "t", URL, "8" * 40, VERDICTS, {},
                            {**FINDINGS_FIX, "protected": [".github/ci.yml"]})
    assert text.splitlines()[2] == 'Protected files in the fix (.github/ci.yml): Cleave pushes it only on "push anyway".'
    assert 'Reply to this message: "push anyway" · "diff" · "again: …"' in text


FIX = {"files": [{"path": "a.kt", "added": 1, "deleted": 0}], "protected": [], "bytes": 9}
GUARDED = {**FIX, "protected": [".github/ci.yml"]}
PUSH_ALL, PUSH, DIFF = (Button("Push + reply all", "rv_pushpost:j1"), Button("Push", "rv_push:j1"),
                        Button("Diff", "rv_diff:j1"))
PUSH_ANYWAY_ALL, PUSH_ANYWAY = Button("Push anyway + reply all…", "rv_pushx:j1"), Button("Push anyway…", "rv_pushx:j1")
REPLY_ALL = Button("Reply all", "rv_post:j1")


def test_fix_buttons_offer_only_what_can_run():
    assert fix_buttons("address", "j1", REPLIES, FIX) == [PUSH_ALL, DIFF]  # pushing and replying is one move
    assert fix_buttons("address", "j1", REPLIES, GUARDED) == [PUSH_ANYWAY_ALL, DIFF]
    assert fix_buttons("address", "j1", [], FIX) == [PUSH, DIFF]
    assert fix_buttons("address", "j1", [], GUARDED) == [PUSH_ANYWAY, DIFF]
    assert fix_buttons("address", "j1", REPLIES, {}) == [REPLY_ALL]
    assert fix_buttons("address", "j1", [], {}) == []
    assert fix_buttons("findings", "j1", [], FIX) == [PUSH, DIFF]
    assert fix_buttons("findings", "j1", [], GUARDED) == [PUSH_ANYWAY, DIFF]
    assert fix_buttons("findings", "j1", [], {}) == []
    assert fix_buttons("recheck", "j1", REPLIES, {}) == [REPLY_ALL]
    assert fix_buttons("recheck", "j1", [], {}) == []
    assert fix_buttons("review", "j1", [], {}) == []


def test_review_message_offers_fix_only_when_fixable_with_a_confirmed_finding():
    report = parse_report(REPORT, HEAD)
    hint = 'Reply to this message: "post 1 2 4 approve" · "text 2" · "edit 2: …" · "review again"'
    fixable = review_message("acme/app", 1712, "t", "", URL, 14, report, TG, fixable=True)
    assert hint + ' · "fix"' in fixable.splitlines()
    assert hint in review(report).splitlines()           # not fixable: no "fix"
    questions = dataclasses.replace(report, items=tuple(i for i in report.items if i.kind == "question"))
    assert questions.items
    only_questions = review_message("acme/app", 1712, "t", "", URL, 14, questions, TG, fixable=True)
    assert hint in only_questions.splitlines() and '"fix"' not in only_questions


def test_after_the_push_only_reply_all_and_diff_remain():
    labels = lambda buttons: [b.label for b in buttons]
    assert labels(fix_buttons("address", "j", REPLIES, FIX, pushed=True)) == ["Reply all", "Diff"]
    assert labels(fix_buttons("address", "j", REPLIES, GUARDED, pushed=True)) == ["Reply all", "Diff"]
    assert labels(fix_buttons("address", "j", REPLIES, FIX, pushed=True, unposted_replies=False)) == ["Diff"]
    assert labels(fix_buttons("address", "j", REPLIES, FIX, unposted_replies=False)) == ["Push", "Diff"]
    assert labels(fix_buttons("address", "j", REPLIES, {}, unposted_replies=False)) == []
    assert labels(fix_buttons("findings", "j", [], FIX, pushed=True)) == ["Diff"]

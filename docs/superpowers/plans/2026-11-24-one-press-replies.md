# One press for a fix and its replies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One press pushes a threads fix and posts its replies; a resolved thread or a `<` in code no longer blocks a
post; a refused dashboard action logs why.

**Architecture:** Telegram's `fix_buttons` and the dashboard's mirror (`review-actions.js`, `reviews-board.js`) drop
the plain Push beside replies; `Posting._post_replies` retries its dry run once without replies to threads GitHub no
longer lists; `body_problems` ignores a `<` inside a sure code span; `reviews_api._logged` gets the text.

**Tech Stack:** Python 3.12 (pytest), plain ES modules (node --test).

**Spec:** docs/superpowers/specs/2026-11-24-one-press-replies-design.md

## Global Constraints

- New Telegram texts go through `tr()` with a Ukrainian entry in `orchestrator/tg_uk.py`.
- Nothing typed by the operator is logged; Cleave's own answer texts may be.
- Public repo: no real logins, repos, ticket keys or local paths in code, tests or commits.

## Review Focus

- A footnote label holding a backtick before a `<` (`[^`]: <div> `) must still turn HTML mode on.
- A dry run that fails for another reason, with every thread still open, must refuse exactly as before.
- Push + reply all on a fix whose replies are all posted: pushes, then says nothing is left to post.
- `push_post` from the card on protected files must still ask first, naming the files.
- A reply left out must not be reported as "already posted" nor recorded in the Post's numbers.

---

### Task 1: One push button beside replies (Telegram and dashboard)

**Files:** Modify `orchestrator/reviewer/messages.py` (`fix_buttons`), `dashboard/static/js/review-actions.js`
(`fixButtons`, `mainCommand`), `dashboard/static/js/reviews-board.js` (`cleaveMove`, `mainButtonHtml`),
`docs/telegram.md`, `docs/dashboard.md`. Tests: `tests/unit/test_reviewer_messages.py`,
`tests/js/review-actions.test.mjs`, `tests/js/reviews-board.test.mjs`.

- [ ] Tests first: `fix_buttons("address", "j1", REPLIES, FIX) == [PUSH_ALL, DIFF]`; prActions with replies gives
  `['Push + reply all']`; `mainCommand('push', <address fix with an open reply>)` gives
  `{ action: 'push_post' }` (protected: `{ action: 'push_post', anyway: true }` with the confirm); a findings fix
  or one without open replies gives `push`; `cleaveMove` of an address fix is
  `{ kind: 'push', text: 'fix ready: push and reply' }`, a findings fix keeps `fix ready: push`; the card's main
  label is `Push + reply all` / `Push anyway + reply all…` for an address fix. Watch them fail.
- [ ] Implement; docs rows updated. Run `node --test 'tests/js/*.test.mjs'` and
  `pytest tests/unit/test_reviewer_messages.py tests/unit/test_reviewer_replies.py`.
- [ ] Commit `feat(reviewer): push and reply all is one button beside a fix's replies`.

### Task 2: Leave out replies to threads resolved on GitHub

**Files:** Modify `orchestrator/reviewer/posting.py` (`_post_replies`), `orchestrator/tg_uk.py`. Test:
`tests/unit/test_reviewer_posting_replies.py`.

- [ ] Tests first: a dry run that fails, then a threads read without T1, then a dry run of the rest → the second dry
  run's plan has no #1, the outcome is posted with `#1 left out: its thread is resolved on GitHub` appended and the
  Post's numbers exclude 1; every named thread gone → `nothing`, no confirm run; a failed dry run with every thread
  still listed → the old refusal, one dry run. Watch them fail.
- [ ] Implement: on `_Refused` from `_replies_dry_run`, `_threads_before`; `gone` = entries with a `thread_id`
  not among its threads; none → re-raise; else drop them from entries and `cmd.numbers`, rewrite the plan, dry run
  again; append the note to the outcome's text.
- [ ] Run `pytest tests/unit/test_reviewer_posting_replies.py tests/unit/test_tg_i18n.py`; commit
  `fix(reviewer): a reply to a thread resolved on GitHub no longer blocks the others`.

### Task 3: A `<` inside a code span is not HTML

**Files:** Modify `orchestrator/reviewer/report_edit.py` (`body_problems`). Test:
`tests/unit/test_reviewer_report_edit.py`.

- [ ] Tests first: `` "`TEST-<FQN>.xml` … `@HiltAndroidTest`" `` → `[]`; `kotlin-generic-in-span` → `[]` (ruling:
  the round-4 pin is relaxed by the `[` guard); `` "[^`]: <div> `\n\n    `@octo`" ``-style footnote with a backtick
  in its label → still `[OCTO]`; `` "`x` <div>" `` and `"<div>`@octo`"` stay HTML. Watch the first two fail.
- [ ] Implement: `in_html` probes `clean` from `_code_spans(line)` when it is sure and the line has no `[` before
  its first backtick, else the whole line.
- [ ] Run `pytest tests/unit/test_reviewer_report_edit.py tests/unit/test_reviewer_posting*.py`; commit
  `fix(reviewer): a "<" inside inline code no longer hides the code's mentions check`.

### Task 4: A refused dashboard action logs why

**Files:** Modify `dashboard/reviews_api.py` (`post_action`, `post_confirm`, `post_pr_move`). Test:
`tests/unit/test_reviews_api*.py` (wherever `_logged` is tested).

- [ ] Test first: a refused button command logs `... #N: refused 'why'`; a posted one logs no text; a typed text
  command's refusal logs Cleave's answer, never the typed text.
- [ ] Implement in `_logged` callers: state plus `!r` of the first text, cut to 300, for refused / failed /
  unverified.
- [ ] Run the dashboard tests; commit `feat(dashboard): a refused action's log line says why`.

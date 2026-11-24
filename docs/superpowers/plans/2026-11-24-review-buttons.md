# Review message buttons — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A review message posts every confirmed finding with one tap on the event the operator picks.

**Architecture:** `messages.review_buttons` builds the buttons; `replies._TAPS` maps each to the typed `post` command
(`all_confirmed`); the runner puts them on the last part; `refresh_buttons` removes them once the job is not `done`.

**Spec:** docs/superpowers/specs/2026-11-24-review-buttons-design.md

## Global Constraints

- Every Telegram text has a Ukrainian entry (`test_every_text_has_ukrainian`); "Approve" as the pipeline's ("Схвалити").
- JobRun fields added later have defaults.

## Review Focus

1. A tap on a review of the operator's own PR: only Comment is offered.
2. A tap after a post landed (a redelivered or stale button): the posting's checks refuse it.
3. A dashboard post: the Telegram buttons go.

### Task 1: buttons, taps, refresh, delivery

**Files:** `orchestrator/reviewer/messages.py`, `replies.py`, `runner.py`, `models.py`, `orchestrator/tg_uk/reviewer.py`,
`docs/telegram.md`; tests in `test_reviewer_messages.py`, `test_reviewer_replies.py`, `test_reviewer_actions.py`,
`test_reviewer_runner.py`.

- `review_buttons(job_id, suggested, *, own_pr=False) -> list[Button]` (`rv_comment`, `rv_approve`, `rv_changes`).
- Tests: order by suggestion, own PR, Ukrainian labels, each tap's command, refresh while done / after a post, the
  runner's last part and `own_pr`.

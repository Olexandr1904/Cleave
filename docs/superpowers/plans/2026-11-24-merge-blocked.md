# Merge blocked: the reason and Re-run CI — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A refused merge says what blocks it; a blocked merge with no checks on its head offers Re-run CI.

**Architecture:** One GraphQL read of the PR's merge state in the forge; pure texts and the `no_checks` rule in a
new `orchestrator/reviewer/merge_block.py`; the poll flags rows, `pr_move` explains a 405 and runs `rerun_ci`; the
board shows the flag as the card's line and main button.

**Tech Stack:** Python 3.12, httpx + respx, Starlette, vanilla JS (node --test).

**Spec:** docs/superpowers/specs/2026-11-24-merge-blocked-design.md

## Global Constraints

- Never show GitHub's response text; check names are shown cut to 40 characters, control characters dropped, at most 5.
- One write per GitHub call, never retried (the forge's `_write` rule); the branch moves without force.
- BoardRow fields added later have defaults.
- No real logins, repos, ticket keys or local paths in the repo (it is public).

## Review Focus

1. A merge state read that fails at a poll: the poll goes on, the flag is false.
2. A second Re-run CI before the next poll: refused, never a second empty commit.
3. A branch that moved between the read and the ref update: GitHub's 422 is a refusal, nothing overwritten.
4. A PR from a fork: no flag, the move refused.
5. A check name with control characters or 300 characters long.

---

### Task 1: forge reads the merge state and pushes an empty commit

**Files:** `orchestrator/reviewer/models.py` (MergeState, Check), `orchestrator/reviewer/forge_github.py`,
`tests/unit/test_reviewer_forge_github.py`.

- `Check(name: str, state: str)` with state `pending|passed|failed` and `word: str` (GitHub's status or conclusion,
  lowercased, `_` as space); `MergeState(status, base, head_ref, head_sha, cross_repo, review, checks: tuple[Check,
  ...] | None)`.
- `async merge_state(number) -> MergeState`: GraphQL; ForgeError on errors or a missing PR.
- `async empty_commit(branch, head, message) -> str`: GET `/git/commits/{head}` (tree), POST `/git/commits`, PATCH
  `/git/refs/heads/{branch}` `{"sha", "force": false}`; each write once; returns the new sha.
- Tests: states mapped (check run queued/in progress/success/skipped/failure/cancelled, status context
  pending/expected/success/error), no rollup → None, GraphQL error → ForgeError, empty commit's three calls and
  bodies, a 422 on the ref → ForgeError("GitHub 422") not unavailable.

### Task 2: the texts and the rule

**Files:** `orchestrator/reviewer/merge_block.py`, `tests/unit/test_reviewer_merge_block.py`.

- `refusal_text(state: MergeState | None, number: int) -> str` (None: the generic text), `no_checks(state, head) ->
  bool`, `rerun_message(ticket_key, head) -> str`. Order and words per the spec.

### Task 3: service

**Files:** `orchestrator/reviewer/models.py` (BoardRow.no_checks), `orchestrator/reviewer/service.py`,
`tests/unit/test_reviewer_service.py`, other fakes as needed.

- Poll: rows whose my turns include `merge` get `no_checks` from `merge_state`; ForgeError → false.
- `pr_move`: a 405 on merge reads the merge state for the text; `rerun_ci` (turn `merge`, needs `no_checks`, head
  prefix, live re-read, fork refused) pushes via `empty_commit`; done text per the spec; `poll_soon`.

### Task 4: dashboard

**Files:** `dashboard/reviews_api.py`, `dashboard/static/js/reviews-board.js`, `dashboard/static/js/reviews.js`,
`tests/js/reviews-board.test.mjs`, `tests/unit/test_dashboard_reviews.py`, `docs/dashboard.md`.

- `_pr` gives `no_checks`; `turnMove` gives `{kind: 'rerun'}` for merge with `no_checks`; `moveText` says
  `merge: no checks ran, re-run CI`; label `Re-run CI`; `onPrMove` runs `{move: 'rerun_ci', head}` at once.
- Docs: the Merge… row's 405 sentence, a Re-run CI row.

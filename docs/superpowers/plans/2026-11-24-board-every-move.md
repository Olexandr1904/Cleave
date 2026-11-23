# Board: every move a button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every move a board card's line names gets a main button that makes it: GitHub moves (re-request, request
review, ready for review, merge) through new routes, Review now / Fix now marked main; every answer on the card; every
dashboard action logged.

**Architecture:** The forge reader gains the GitHub writes and reads the moves need; `ReviewerService` gains
`pr_move`, `review_candidates`, `merge_options` and `poll_soon`, which check the move against the stored board row;
`reviews_api` exposes them. The board JS derives the move from the row (`turnMove`), renders the button and the dialogs
from pure functions, and `reviews.js` wires the clicks.

**Tech Stack:** Python 3 (Starlette, httpx, respx, pytest), plain ES modules (node:test).

**Spec:** `docs/superpowers/specs/2026-11-24-board-every-move-design.md`

## Global Constraints

- Public repo: no real logins, org or repo names, ticket keys or local paths in code, tests or docs.
- GitHub writes: one request each, never retried; a ForgeError never carries GitHub's body.
- Moves are refused unless the stored row names them: `request_review` → `request_review`, `ready` → `finish_draft`,
  `merge` → `merge` among `row.my_actions`.
- Merge methods: `merge`, `squash`, `rebase`; default the first allowed in that order.
- Login: `^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$`; 1–10 per request; never the PR's author.
- Python tests: `timeout 600 ~/Cleave/.venv/bin/python -m pytest tests --ignore=tests/e2e -q -p no:cacheprovider`
  (9 known environment failures; it hangs after the summary). JS: `node --test 'tests/js/*.test.mjs'`.

## Review Focus

1. A card refreshed (Auto) while a dialog is open: the dialog's answer must still go to the right PR.
2. A move pressed twice before the board catches up (re-request twice, merge twice): the second must answer, not hang.
3. GitHub not answering a merge: the operator must be told to check the PR, not that it failed.
4. A `request_review` turn with a reviewer plus a plain one on the same PR: the button must not drop either.
5. A login typed with spaces, commas or an `@`: picked logins must be clean or refused.

---

### Task 1: The board keeps a turn's reviewer; rows carry `rerequest`

**Files:** Modify `orchestrator/reviewer/board.py` (`build_board`), `dashboard/reviews_api.py` (`_pr`).
Test: `tests/unit/test_reviewer_board.py`, `tests/unit/test_dashboard_reviews.py`.

- [ ] Test `test_my_turn_keeps_its_reviewer`: a snapshot turn `Turn(me, "request_review", (), "rev1")` gives
  `{"action": "request_review", "threads": [], "reviewer": "rev1"}`; a turn without reviewer has no `reviewer` key.
- [ ] Test `test_rows_carry_the_reviewers_to_rerequest`: a row with my turns `request_review` (reviewer rev1),
  `request_review` (rev1 again), `address_review` (rev2) and `request_review` (none) → `"rerequest": ["rev1"]`.
- [ ] Run, watch both fail; implement: the dict gets `"reviewer"` only when `t.reviewer`; `_pr` adds
  `"rerequest": list(dict.fromkeys(t["reviewer"] for t in row.my_turns if t["action"] == "request_review" and t.get("reviewer")))`.
- [ ] Run the two files green; commit.

### Task 2: Forge writes and reads for the moves

**Files:** Modify `orchestrator/reviewer/forge_github.py` (`GitHubForgeReader`). Test:
`tests/unit/test_reviewer_forge_github.py` (respx).

**Interfaces — Produces:**
- `async request_reviewers(number: int, logins: list[str]) -> None` — one `POST pulls/{n}/requested_reviewers
  {"reviewers": logins}`.
- `async mark_ready(number: int) -> None` — the adapter's `mark_ready`; any error → `ForgeError`.
- `async merge(number: int, head: str, method: str) -> None` — one `PUT pulls/{n}/merge {"sha", "merge_method"}`.
- `async merge_methods() -> list[str]` — `GET /repos/{o}/{r}`; `allow_merge_commit`, `allow_squash_merge`,
  `allow_rebase_merge` (a missing field counts as allowed), in the order merge, squash, rebase.
- `async review_candidates(number: int) -> list[dict]` — one GraphQL query (the PR's author, `suggestedReviewers`,
  `reviewRequests`; a search of the author's 20 newest PRs with their reviews' authors): `[{login, reason}]`, GitHub's
  suggestions first ("suggested by GitHub"), then recent reviewers by count ("reviewed N of the author's recent
  PRs"), never the author, a bot or one already requested; at most 10.
- Errors: `ForgeError("GitHub <status>", unavailable=status >= 500)`; no answer → `ForgeError(<class>, unavailable=True)`;
  GraphQL `errors` → `ForgeError("GitHub GraphQL error")`.

- [ ] Tests: `test_request_reviewers_posts_once` (201; a 502 raises unavailable and the route is called once),
  `test_merge_sends_head_and_method` (body `{"sha": "a"*40, "merge_method": "squash"}`; 405 → `ForgeError("GitHub 405")`
  not unavailable), `test_merge_methods_in_order` (`allow_merge_commit: False` → `["squash", "rebase"]`; fields absent →
  all three), `test_review_candidates_ranked` (author, bot, already requested left out; suggested first; then by
  count), `test_review_candidates_graphql_error`, `test_mark_ready_error_is_forge_error`.
- [ ] Run, fail; implement; run green; commit.

### Task 3: The service's moves

**Files:** Modify `orchestrator/reviewer/service.py`. Test: `tests/unit/test_reviewer_service.py`.

**Interfaces — Produces:**
- `async pr_move(scope, number, move, *, reviewers=(), method="", head="") -> tuple[str, str]` — `(state, text)`,
  state `done` | `refused` | `failed`. `LookupError` (repo or row unknown), `ValueError` (unknown move).
  Refused: the row does not name the move; the forge credential is failing; no reviewer / a bad login / more than 10 /
  the author; a method not in merge, squash, rebase; `head` shorter than 7 or not a prefix of the row's head.
  Done texts: `Asked rev1, rev2 to review #N.`, `#N is ready for review.`, `Merged #N (<method>).`; then `poll_soon`.
  Failed: `GitHub refused it (GitHub 422).`; a merge's 405 → `GitHub refused the merge (GitHub 405): checks, reviews or
  conflicts block it.`; unavailable → `GitHub did not answer: check #N on GitHub before trying again.`
- `async review_candidates(scope, number) -> list[dict]`, `async merge_options(scope) -> dict` (`{"methods", "default"}`)
  — the forge's, `LookupError` for an unknown repo or row.
- `poll_soon(scope) -> None` — the repo is due now.

- [ ] Tests (FakeForge with recorded calls): `test_pr_move_requests_reviewers_and_polls_soon`,
  `test_pr_move_refuses_a_move_the_row_no_longer_names`, `test_pr_move_refuses_bad_logins_and_the_author`,
  `test_pr_move_merge_checks_the_head_and_method`, `test_pr_move_merge_unanswered_says_check_github`,
  `test_pr_move_refused_while_the_forge_credential_fails`, `test_merge_options_default_order`.
- [ ] Run, fail; implement; run green; commit.

### Task 4: Routes and the action log

**Files:** Modify `dashboard/reviews_api.py`. Test: `tests/unit/test_dashboard_reviews.py` (FakeService gains the
three methods).

- [ ] Tests: `test_pr_move_route` (200 with state/text; 400 for a missing move, a non-list reviewers, a non-string
  method/head; 404 from LookupError; 400 from ValueError), `test_candidates_and_merge_options_routes` (200; 404;
  ForgeError → 502 with its text), `test_dashboard_actions_are_logged` (caplog: review-now and pr-move log one INFO line
  naming the route, the PR and the outcome; a typed command's text is not logged).
- [ ] Run, fail; implement; run green; commit.

### Task 5: Board model — the turn's move, its button, the dialogs

**Files:** Modify `dashboard/static/js/reviews-board.js`, `dashboard/static/js/review-actions.js`,
`dashboard/static/style.css`. Test: `tests/js/reviews-board.test.mjs`, `tests/js/review-actions.test.mjs`.

**Interfaces — Produces:**
- `turnMove(pr, column)` → `{kind: 'review-now'|'fix'|'rerequest'|'request'|'ready'|'merge', logins?}` or null: null
  when not involved or `cleaveMove` names a move; else the first of my turns with a button (fix only on my PR with
  `can_fix`); `request_review` is `rerequest` with `pr.rerequest` when non-empty.
- `mainButtonHtml` covers the GitHub kinds (labels `Re-request <logins>`, `Request review…`, `Ready for review`,
  `Merge…`), every main button has class `is-main`; `buttonsHtml` adds `is-main` to its review or fix button when
  `turnMove` names that kind. CSS keys the always-visible rule on `.is-main`.
- `candidatesHtml(candidates)`, `pickedReviewers(checked, other)` (split on commas and spaces, strip a leading `@`,
  dedupe, keep order), `mergeHtml(number, options)` (radios `merge-method`, the default checked).

- [ ] Tests for each, then implement; run green; commit.

### Task 6: Wiring, answers on the card, docs

**Files:** Modify `dashboard/static/js/reviews.js`, `dashboard/static/js/api.js`, `docs/dashboard.md`.

- [ ] `api.js`: `prMove(scope, number, body)` (JSON POST like `reviewAction`), `reviewCandidates(scope, number)`,
  `mergeOptions(scope)` (GET, error text from the body).
- [ ] `reviews.js`: `onMainButton` sends the GitHub kinds to `onPrMove`: rerequest and ready post at once; request
  loads the candidates then a dialog (none picked → refused "Pick at least one reviewer."); merge loads the options then
  a dialog and sends `head: pr.head`. The target PR is fixed at the click. Answers set `result` with `card: true`.
  `runAction` (Review now, Fix now…) also sets `result` with `card: true`.
- [ ] Docs: the card buttons table, the panel header, the new routes.
- [ ] JS tests and the Python suite green; preview in headless Chrome with stubbed routes; commit.

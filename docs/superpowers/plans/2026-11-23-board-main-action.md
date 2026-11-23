# Board main action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the move a card's line names (Push / Push anyway… / Reply all / Post review…) on the card as its first
button, and the same button in the PR panel's header.

**Architecture:**
- The board row gains `fix.protected`.
- A pure `cleaveMove` in reviews-board.js decides the move. `moveText` and the new `mainButtonHtml` both use it.
- A pure `mainCommand` in review-actions.js turns a move and the PR's detail into the command for
  `POST /api/reviews/action`.
- reviews.js wires the click and draws the result line on the card.

**Tech Stack:** Python (Starlette, pytest), vanilla ES modules (`node --test`).

**Spec:** docs/superpowers/specs/2026-11-23-board-main-action-design.md

## Global Constraints

- Labels, verbatim: `Push`, `Push anyway…`, `Reply all`, `Post review…`.
- Push anyway asks with the panel's text: `The fix touches protected files: <files joined by ", ">. Push it anyway?`.
- Post review… asks with a Comment / Approve / Request changes choice, Comment preselected.
- The button sends only what the line names: the push move never posts replies.
- No new route. Commands go through `reviewAction(scope, number, jobId, {command})` (api.js).
- Public repo: test data uses `acme/app`, `octo`, `ACME-…`. No local paths.
- Tests:
  - Python: `timeout 600 ~/Cleave/.venv/bin/python -m pytest <paths> -q -p no:cacheprovider`. The 9 known
    environment failures are listed in the cleave-test-suite-quirks memory.
  - JS: `node --test 'tests/js/*.test.mjs'`.

## Review Focus

- A fix whose post is under way (`fix.posting`) shows no main button. The disabled "Posting" stays the only button.
- A findings fix that was already pushed, or has no patch, names no move, so there is no button.
- A tap while another command runs does nothing (the `acting` guard).
- The detail fetch fails: the card shows `failed` with the error, and nothing is sent.
- The PR's detail has no job for the move (no fix job, no replies job, no report): the card shows a refusal, and
  nothing is sent.

---

### Task 1: `protected` in the board's fix info

**Files:** Modify `orchestrator/reviewer/service.py` (`_fix_info`). Test `tests/unit/test_reviewer_service.py`
(`test_overview_carries_the_newest_address_job_whatever_job_is_newer`).

- [ ] Extend the test's expected dict with `"protected": False`. Add a second fix run with
  `fix={"files": [], "protected": ["build.gradle"], "bytes": 9}`, and assert that
  `overview()[0]["fixes"][n]["protected"] is True`.
- [ ] Run it and see it fail.
- [ ] `_fix_info` adds `"protected": bool((run.fix or {}).get("protected"))`.
- [ ] Run it and see it pass. Commit.

### Task 2: the move and its command, pure

**Files:**
- Modify `dashboard/static/js/reviews-board.js`: add `cleaveMove` and `mainButtonHtml`, and make `moveText` use
  `cleaveMove`.
- Modify `dashboard/static/js/review-actions.js`: add `mainCommand` and `protectedConfirm`, and make `fixButtons`
  use `protectedConfirm`.
- Tests in `tests/js/reviews-board.test.mjs` and `tests/js/review-actions.test.mjs`.

**Interfaces (produced):**
- `cleaveMove(pr, column) -> {kind: 'push'|'replies'|'review', text} | null`. It takes the branches `moveText` has
  today for "post the replies", "post the review", "fix ready: push", "fix pushed: post the replies" and
  "replies ready: post". kind is `replies` for the first, fourth and fifth.
- `mainButtonHtml(scope, pr, column, where) -> string`.
  - It is `''` when `cleaveMove` is null, or when `pr.fix && pr.fix.posting`.
  - Otherwise it is a `<button class="reviews-btn primary" data-main-move="<kind>" data-scope data-number
    data-fk="<where>:main:<scope>#<n>">`.
  - The label is the Global Constraints label. A push with `pr.fix.protected` is labelled `Push anyway…`.
- `cardHtml` puts `mainButtonHtml(…, 'card')` before `buttonsHtml`.
- `cardHtml` draws `resultLineHtml(view.result)` when `view.result.key === '<scope>#<n>'`.
- `resultLineHtml(result) -> string` is the panel's result markup, moved here and exported. reviews.js `resultHtml`
  uses it.
- `protectedConfirm(files) -> string`.
- `mainCommand(kind, detail) -> {jobId, command, confirm: string|null, pickEvent: boolean} | string`. It returns a
  string (a refusal) when the move's job is missing.

  | kind | jobId | command | refusal without the job |
  |---|---|---|---|
  | push | `detail.fix_job` | `{action:'push'}`, plus `anyway:true` and `confirm` when `detail.fix.protected` is non-empty | `No fix to push.` |
  | replies | `detail.replies_job` | `{action:'post_replies', all_replies:true}` | `No replies drafted.` |
  | review | `detail.report_job` | `{action:'post', all_confirmed:true}`, `pickEvent:true` | `No report to post.` |

  Anything else, already pushed or already posted included, is the layer's to refuse.

- [ ] **Tests:**
  - `moveText` keeps every string its tests pin.
  - `cleaveMove` gives the right kind for each of the five lines, and null for a pushed findings fix.
  - `mainButtonHtml`:
    - Push on a ready fix; `Push anyway…` with `protected: true`;
    - `''` while posting;
    - Reply all on "fix pushed: post the replies";
    - `Post review…` on "post the review";
    - `''` on a card with only GitHub turns.
  - `cardHtml` has the main button before Fix again….
  - `mainCommand`:
    - for each kind and its refusal;
    - push anyway carries `anyway:true` and the exact confirm text;
    - push without protected files has `confirm: null` and no `anyway`.
- [ ] Run them and see them fail. Implement. Run them and see them pass. Commit.

### Task 3: wiring, panel header, docs

**Files:**
- Modify `dashboard/static/js/reviews.js`, `dashboard/static/css/style.css` (only if the card's result line needs a
  margin) and `docs/dashboard.md`.

- [ ] **The panel header.** `panelHeadHtml` puts `mainButtonHtml(scope, pr, columnOf(pr), 'panel')` before
  `buttonsHtml`.
- [ ] **The click.** `onClick` sends `[data-main-move]` to `onMainButton(view, btn)`:
  - It returns at once while `acting`.
  - It always fetches `loadReviewDetail`. A failure sets `result` to failed.
  - It calls `mainCommand`. A string sets `result` to refused.
  - A `pickEvent` move gets a confirm dialog with the three EVENTS radios (`COMMENT` checked) and the report's
    suggested event and reason. The dialog's element is read on confirm.
  - A `confirm` move gets a confirm dialog with that text.
  - Then it calls `runCommand`.
- [ ] **`runCommand(view, target, jobId, body)`.** It is `runPanelCommand` generalised to any target:
  - It locks the panel when one is open, and disables every `[data-main-move]`.
  - It resets `ticked` and `cmdText` only when `target` is the open PR.
  - `runPanelCommand(view, jobId, body)` calls it with `openPr`.
- [ ] **The result.** The card view passes `result`. `openRow` keeps `result` when it is the opened PR's.
- [ ] **Docs.** In docs/dashboard.md, add a line on the card's main button and the panel header's.
- [ ] **Check.**
  - Run the JS and Python suites.
  - Preview the board in headless Chrome on a DB copy (dashboard-preview-headless-chrome memory): a card with the
    button, and the panel header with it.
  - Commit.

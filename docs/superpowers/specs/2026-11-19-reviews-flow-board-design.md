# Reviews tab: flow board — Design

Date: 2026-11-19. Replaces the two tables of the Reviews tab ("My turn / Waiting on others" in
`docs/dashboard.md`) with a board of four columns. The PR side panel stays, with the button changes below.

Reference mockup (local, git-ignored): `.superpowers/brainstorm/90853-1795037528/content/board-v4.html`. It
is drawn on the live board of 2026-11-18 (7 PRs of the operator's main repo).

## Why

The operator's verdict on the current tab: ugly and not informative. The UX review found:

1. **No answer to "what do I do next".** The Turn column shows machine codes (`resolve_conflicts`,
   `answer_threads`); the row buttons are hidden until hover.
2. **The green dot misleads.** `● Report ready` is green on #101, whose report has 15 critical findings:
   the color is the job's state, not the PR's health.
3. **The finding squares do not rank.** One square per finding: 25 squares read as "long", not as "15
   critical"; no labels, meaning by color alone.
4. **Who we are waiting on is invisible.** All four "waiting" PRs wait on one person; nothing says so.
5. **Noise and duplicates.** `need you` = `my turn`; `0 in the job queue` looks like a filter and is not;
   `sent to Telegram` on every row; the ticket key twice; two tables whose columns do not line up; rows
   sorted by PR number, not by urgency.

## Goals

- At a glance: what is mine to do, grouped by kind of move, and what waits on whom and for how long.
- Findings shown by severity with words, never by color alone.
- No accidental re-review: reviewing the operator's own PR is explicit, and re-running a review of a
  commit that already has a report asks first.
- One click to have Claude work through reviewers' threads on the operator's PR (**Fix now**), instead of
  waiting for the address job's quiet window (operator, 2026-11-19).

## Non-goals

- The dashboard-wide toolbar (Manual / Refresh / Auto): shared by every tab, unchanged.
- The switch panel (`Reviews: running ▶` and the repo pills): shared with the Board tab, unchanged.
- The side panel's body (report, replies, fix, posts, jobs, raw report): unchanged.
- A per-project Reviews page. A project in the sidebar still opens the tickets Board. Possible later.
- Posting from the dashboard. Telegram stays the only posting path.
- The Telegram `review again` reply: unchanged, no confirmation (the reply is explicit already).
- **Fixing the findings of a self-review** (#101's 15 critical): a new job kind, the **next project**
  (operator, 2026-11-19). The address job works from GitHub threads, not from Cleave's report. The board
  leaves room for its button on To fix cards; nothing of it is built here.
- Resolving conflicts, fixing checks, finishing a draft or requesting a review from the dashboard: later,
  if ever. Pushing and posting stay in Telegram (`push`, `post replies …`).

## Anti-goals

- A board where the operator has to hunt for their own moves.
- Parsing project-specific title conventions (an `Area > Sub-area >` path some repos put in their titles). Other
  projects title PRs differently.

## Approaches

Three directions were mocked on live data:

- **A · Inbox**: one action queue, a verb and one button per row, waits grouped by person. Closest to the
  current code.
- **B · Flow board**: columns plus four headline numbers. Shows where PRs pile up.
- **C · Cockpit**: a dense list with the selected PR's report always open beside it.

**The operator picked B.** Its columns could mean two things:

1. **Whose move**: To review · To fix · With others · To merge (all but With others are the operator's moves).
2. **PR lifecycle stage**: Prepare · Review · Author · Merge, with whose move shown by avatar and a
   "My moves" filter.

**The operator picked 1.** Their reason: no hunting for what is mine, everything is visible at once. Stage
columns scatter the operator's moves across all four columns.

A button for Claude to fix a To fix PR could mean three things:

- **A**: reviewers' threads on my PR. The address job exists; the button only starts it now.
- **B**: a self-review's findings. This needs a new job kind.
- **C**: conflicts. Cleave would have to merge the base into the branch.

**The operator picked A now and B as the next project.** C is not planned.

## The board

### Columns

One rule: **does the operator have a move on this PR now** (`my_turns` non-empty)?

| Column | Rule | Live example (2026-11-18) |
|---|---|---|
| **To review** | a `review` or `recheck` among my turns | — (empty) |
| **To fix** | else any of my turns other than `merge` (`resolve_conflicts`, `request_review`, `answer_threads`, `resolve_threads`, `address_review`, `finish_draft`, `fix_checks`, any unknown action) | #101 (request review), #102 (resolve conflicts, request review) |
| **With others** | no turn of mine | #103 (octo-reviewer: re-check), #104 and #105 (octo-reviewer: answer threads), #106 (octo-reviewer: merge) |
| **To merge** | else (`merge` is my only turn) | #107 |

Columns in this order, left to right (operator, 2026-11-19: With others before To merge). For my turns the
checks run To review, then To fix, then To merge: that order is the precedence when my turns span columns,
which the reference readiness script (`.github/pr_comments/pr_threads.py`) does not produce in practice. A
PR with nobody's turn goes to **With others**. Within a column, the oldest `last_change` comes first. An empty
column keeps its place and shows a dashed placeholder: `Nothing to review`, `Nothing to fix`,
`Nothing to merge`, `Nobody else's move`. Each header shows the column name, its count and a one-line
description (`your review or re-check`, `your move on your PR`, `your merge`, `no move of yours`).

### Above the columns

1. The switch panel, as today.
2. **Status line**, one per repo: a dot colored by `status`, the scope, the status word when it is not
   `ok`, `synced <time ago>` (`last_poll_at`), and the number of queued and running jobs (`no jobs
   running`, `2 jobs running`). This line replaces the chip `0 in the job queue` and today's repo line.
   A repo's `reason` keeps its amber line under it, as today. The search box sits at the right of the
   first status line.
3. **Repo filter**, only when more than one repo has reviews: chips `All · <repo> <count> · …`. The choice
   is kept in the URL hash (`#/reviews/<scope>`), so a reload or a link keeps it. With one repo there are
   no chips, and the repo is not named on the cards. With several, each card names its repo.
4. **Four tiles.** They count the cards the repo filter shows; search does not change them.

| Tile | Value | Sub-line |
|---|---|---|
| Need you | cards in To review + To fix + To merge | the non-zero columns: `2 to fix · 1 to merge` |
| Critical in your PRs | sum of `critical` over my PRs (role `author`) whose `review` (see API) is at the current head in `done` or `posted` | `self-reviews: 15 in #101 · 4 in #102`, at most two PRs then `…`; value red when > 0; `no self-reviews` when there are none |
| Waiting on others | cards in With others | the logins holding them: `all on octo-reviewer`, or `octo-reviewer 3 · octo-dev 1` |
| Longest wait | the oldest `last_change` in With others, as an age | `#103 · re-check`; `—` when the column is empty |

### Card

Top to bottom:

1. **Header line**, small:
   - `YOUR PR` (role `author`) or `REVIEWING`;
   - `#<number>`, a link to the PR on GitHub;
   - the ticket key, a link to `<jira_url>/browse/<key>`, or plain text when the repo has no `jira_url`;
     no key, nothing;
   - the flags `large`, `draft`, `pipeline` as today;
   - the repo name when there is more than one repo.
2. **Title**: the PR title as is, with only a leading ticket key removed. When the title starts with the
   row's `ticket_key`, drop the key and a following `:` or `-` and spaces. No other parsing. At most two
   lines; the full title in the tooltip.
3. **Findings**, from the PR's newest review (`review`, see API), only when it is `done` or `posted` with
   `findings`. A newer re-check or address job does not hide them:
   - on my PR, a source line `self-review · <started_at, local>`;
   - a thin segmented bar (critical, major, minor, questions) on a scale shared by the whole board, so 15
     critical draws longer than 4;
   - the counts in words: `15 critical · 3 minor · 7 questions`, only the non-zero kinds; pre-existing and
     dropped stay in the panel;
   - a suffix: `· posted`, `· report in Telegram` (done, sent), `· not delivered yet` (done, not sent);
   - when the review's head is not the PR's current head, the whole block is muted and says `on an older
     commit`.

   Colors are the status ramp of the dataviz palette: critical `#d03b3b`, major `#ec835a`, minor
   `#fab219`, question `#3987e5`. The text stays in ink colors; each colored mark has its word next to
   it.
4. **Job line**, only while something is happening, in the job color (purple):
   - `Cleave review queued`;
   - `… running · 12 min`;
   - `… deferred · next try 14:20`;
   - `… failed: <reason>`.

   A `recheck` or `address` job in `done` shows `replies ready` or `fix ready` instead of findings, as the
   Job column does today. `skipped` (the head is skipped) shows `skipped` muted. Stale, superseded and
   cancelled jobs show nothing. The green `Report ready` dot is gone.
5. **Bottom line**:
   - the avatar of whoever holds the move: me in the three left columns, the other turns' logins in With
     others, at most two avatars then `+N`;
   - what to do, in words, for example `resolve conflicts, request review` or `octo-reviewer: answer
     threads`;
   - in To review with a `done` review at the current head: `post the review from Telegram`; with a
     `done` re-check: `post the replies from Telegram`;
   - the age since `last_change`, amber from 48 hours.

Action words: `review` → review, `recheck` → re-check, `merge` → merge, `resolve_conflicts` → resolve
conflicts, `request_review` → request review, `answer_threads` → answer threads, `resolve_threads` →
resolve threads, `address_review` → address review, `finish_draft` → finish draft, `fix_checks` → fix
checks; any other action → its name with `_` as spaces.

**Avatars**: `https://github.com/<login>.png?size=40` when the PR URL is on `https://github.com/`. When
the image fails to load, or the forge is not GitHub, show the login's first two letters, upper-case, on a
color derived from the login. My login is the API's new `me`.

### Card buttons

Shown while the pointer is on the card, the card has the keyboard focus or its panel is open, and always on
touch screens, as the row buttons are today.

- **My PR** (role `author`): no review button; Self-review lives in the panel only. **Fix now** or
  **Fix again…** when the PR qualifies (next section).
- **Other PRs**:
  - **Review now**, or **Review anyway** on a large PR;
  - **Review again…** when **a report of the current head exists**: `review.head` is the PR's head and
    `review.state` is `done`, `posted` or `stale`, the states `review_now()` supersedes. It opens the
    confirmation below before queuing. This looks at the newest review, not at the row's job: a re-check
    or address job at the same commit must not turn the button back into a plain Review now (builder
    check; my call);
  - disabled **Queued**, **Running** or **Deferred** while a review at the head is pending, as today;
  - **Skip** under today's rule.
- **GitHub ↗ is removed**: the card's `#<number>` is the GitHub link.

### Fix now (address jobs)

The address job (`2026-11-18-reviewer-address-design.md`) already does the work: it tries to refute
each reviewer comment, fixes with Opus what holds, and drafts a reply per thread. The result goes to
Telegram, where `push` and `post replies …` act on it. Today it starts on its own once the PR has been
quiet for `reviewer.quiet_minutes`. **Fix now** starts it at once. It changes nothing about what the job
does or how its result is pushed and posted.

- **Shown** on the card in To fix and in the panel header when the PR's new `can_fix` is true. The server
  applies `plan_jobs`'s address rule:
  - the repo has `reviewer.process.address_doc`;
  - the PR is mine (role `author`);
  - one of my turns is `answer_threads`, `resolve_threads` or `address_review`;
  - no ticket workspace holds the PR (`pipeline` is false);
  - the PR is not held by a skill-flow comparison (`held_prs`).

  On the live board no PR qualifies today (#101 and #102 have no thread turn).
- **States**:
  - **Fix now** while the PR has no address job at the current head;
  - disabled **Queued**, **Fixing** or **Deferred** while one is queued, running or deferred;
  - **Fix again…** when the PR's newest address job is `done`, `posted` or `stale`, the states a new one
    supersedes. Its confirmation, through `showConfirmDialog`: "Run the fix of #N again? The fix ready
    since <time> is replaced; push it from Telegram first if you want it." with [Cancel] [Run again].
- **What it calls**: a new `POST /api/reviews/fix-now` (`{scope, number}`, the same cross-site guard as
  the other two routes) and a new service method `address_now(scope, number)`. It works like
  `address_again` (the same key as `plan_jobs` gives the job, and it supersedes the PR's done, posted
  or stale address jobs) but with no instruction, and it refuses a PR that does not qualify:
  - 404 when the PR has left the board;
  - 409 `already queued`;
  - 409 `not fixable: <why>`.

  The status line at the bottom of the view reports the result, as for Review now: `Queued a fix of
  #108 at 1a2b3c4d5`.
- **On the card**:
  - the job line names the address job: `Claude fixing · 8 min`, `fix queued`;
  - in `done`, the bottom line says what is left: `fix ready: push from Telegram`, or `replies ready:
    post from Telegram` when the job made no change.

### Side panel

The body is unchanged. The header speaks the card's language (builder check; my call):

- **Header line**: `#<number>` (the GitHub link), the ticket key as the same Jira link as on the card, the
  number of changed files, the flags. Below it, the title as on the card, unclamped.
- **Moves and job**: the card's action words and its job line replace the turn chips (`resolve_conflicts`)
  and the state dot. There is no green `Report ready` in the panel either.
- **My PR**: **Self-review**, or **Self-review again…** when a report of the current head exists (the
  card's rule above). Beside it, **Fix now** / **Fix again…** under the rule above.
- **Other PRs**: the card's buttons above (Review now / anyway / again…, Skip).
- **GitHub ↗ is removed** here too: the header's `#<number>` is the link.
- **Confirmation** for both `…again` buttons, through the dashboard's `showConfirmDialog`
  (`dashboard/static/js/actions.js`), never `window.confirm`:

  > **Run a self-review of #101 again at the same commit?** (or "Review #109 again …")
  > The last one finished 18 Nov 10:34, took 1h 0m and cost $28.93 (API-equivalent). The new report
  > replaces it.
  > [Cancel] [Run self-review]

  The time, duration and cost come from `review.finished_at`, `review.duration_s` and `review.cost_usd`.
  When they are missing, the sentence names what is known and drops the rest. Confirming calls today's
  `POST /api/reviews/review-now`.

### Keyboard, search, narrow screens

- **Keys**: `j` / `k` move the card focus in reading order (down the first column, then the next).
  `Enter` opens the focused card, `Esc` closes the panel, `/` goes to search. No key queues a review.
- **Search** (`#`, title, ticket) hides the cards that do not match; the column counts follow, the tiles
  do not.
- **Panel open**: the board keeps the width the list keeps today (the panel takes at most 540 px and
  leaves the board 700 px). Cards narrow and titles clamp.
- **At most 760 px wide**: the columns stack vertically in the same order, and the panel is the modal
  dialog it is today.

## API additions (`/api/reviews`)

- **Per repo**:
  - `me`: the readiness snapshot's login, already stored in the repo state (`RepoState.me`);
  - `jira_url`: the base URL from the repo's project tracker config when the provider is Jira (as
    `dashboard/web.py` builds ticket links from `projects`), else `""`. `build_review_routes` has no
    access to the projects today, so it needs them passed in.
- **Per PR**, `review`: the PR's newest job of kind `review`, at any head and in any state, or `null`.
  Fields: `id`, `state`, `head` (9 characters, as `job.head`), `started_at`, `finished_at`, `duration_s`,
  `cost_usd`, `message_sent`, `findings`. These are the values `_job_info` and the detail's jobs already
  compute from the job's run. The PR's `job` (its newest review, re-check or address job) stays as it is
  and drives the job line.

- **Per PR**, `can_fix`: whether Fix now applies (the rule in "Fix now"), computed by the server so the
  browser does not repeat `plan_jobs`'s rule.
- **New route** `POST /api/reviews/fix-now` and service method `address_now`, as described in "Fix now".

Nothing else changes server-side. The board is computed in the browser from the rows.

## Removed

- The filter chips (`all PRs`, `need you`, `my turn`, `waiting on others`, `in the job queue`).
- The two tables and today's repo line (replaced by the status line).
- `sent to Telegram` on every row.
- The GitHub buttons.

## Docs and tests

- **`docs/dashboard.md`**, the Reviews view:
  - rewrite "My turn / Waiting on others" as the board (columns, tiles, card, card buttons, Fix now);
  - add `fix-now` beside Review now and Skip in the routes and the buttons table;
  - update "PR detail view" for the header (moves in words, job line, Jira link, buttons) and the
    confirmation;
  - the job-state tables stay.
- **`tests/unit/test_dashboard_reviews.py`**: cover the new API fields (`me`, `jira_url` with and without
  a Jira tracker, the PR's `review`: the newest review even when a re-check or address job is newer, a
  `stale` one, and `null` without any; `can_fix` for each condition of the rule). Cover `fix-now` and
  `address_now`: it queues, supersedes a done address job, and refuses an already queued job, a PR that
  does not qualify, a gone row and a cross-site request.
- **The JS has no unit tests in this repo.** Verify in the running dashboard against the live board:
  - the 7-PR example lands as in the table above;
  - a repo with a failing status still shows its reason;
  - the confirmation appears for a commit that already has a report;
  - the layout at 760 px and with the panel open.

## Left to the builder

- Exact spacing, sizes and the dark palette beyond the colors named here. Follow `style.css` and the
  mockup.
- How the column grid behaves between 760 px and the panel-open width.
- The internal structure of `reviews.js`, which can be split, for example a `reviews-board.js` beside
  the panel code.

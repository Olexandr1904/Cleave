# Dashboard Reference

Tour of the Cleave web dashboard: every view, every button, what each one does.

This doc is a user-facing reference. For the implementation/spec, see [docs/features/dashboard.md](features/dashboard.md).

---

## Starting and reaching it

The dashboard runs in-process with the daemon — when you start Cleave (`./run.sh` or `python main.py --config config-live`), the dashboard comes up at the same time.

- **URL:** [http://localhost:8080](http://localhost:8080) (defaults; the daemon binds to `dashboard.host` `0.0.0.0` and listens on `dashboard.port` `8080`)
- **Configured at:** `global.yaml` → `dashboard.host`, `dashboard.port`
- **Disable:** `dashboard.enabled: false` in `global.yaml`
- **Who can reach it:** `dashboard.allowed_networks`, a list of CIDR ranges. Default: `["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"]` (the host itself and the Tailscale ranges). A peer outside the list gets `403 Forbidden`, whatever `dashboard.host` binds to. Add a range to open the dashboard to another network
- **Which names it answers:** a request must name the dashboard (its `Host` header) by an IP address, `localhost`, a Tailscale name (`*.ts.net`), this machine's hostname in full or short (`box.local` or `box`), or a name in `dashboard.allowed_hosts` (a list, default empty); any other `Host`, or none, gets `403 Forbidden`. That stops a DNS-rebinding page, whose own name points at this host, from reaching the dashboard through the operator's browser. Add a name to reach the dashboard by another DNS name. Checked with `allowed_networks`
- **Cross-site writes:** on every route, a write (`POST`/`PUT`/`PATCH`/`DELETE`) whose `Origin` or `Sec-Fetch-Site` header names another site is refused with `403 cross-site request refused`. The reviews routes (`/api/reviews/…`) and `PUT /api/settings/telegram-language` also refuse a body not sent as `Content-Type: application/json`, which another site's page cannot send without a CORS preflight. curl, which sends neither header, passes
- **Database:** `dashboard.db_path` (default `data/events.db`) — SQLite event store
- **Auto-refresh:** every 30 s, toggle off via the **Auto (30s)** checkbox
- **URL hash routing:** the active view is saved in the URL hash, so refreshing keeps you on the same page

If `projects` is empty when the daemon starts, the dashboard still comes up so you can create your first project from the wizard.

---

## Sidebar

- **Board** / **Reviews** / **Event Log** / **Settings** — top-level navigation
- **Projects** — auto-derived from workspaces and configured projects; click one to filter the Board to that project
- **Daemon** — live status block: mode (auto/manual), uptime, current global model

---

## Toolbar

- **View title** — Board / Reviews / Ticket detail / Event Log / Settings
- **Stats** — count of active vs done workspaces (Board view)
- **Mode indicator** — `AUTO` / `MANUAL`, click-through to toggle
- **Hide done** — checkbox to remove DONE/ARCHIVED workspaces from the Board
- **+ New Project** — opens the project-creation wizard (Atlas)
- **Refresh** — manual reload
- **Auto (30s)** — toggle auto-refresh

In the Event Log view, an additional event-type filter dropdown appears.

---

## Board view

A grid of workspace cards, grouped by project. Cards are sorted by state — active states (BLOCKED, AWAITING_APPROVAL, DEV, ANALYSIS, SCOPE_CHECK, QA, PR_REVIEW, PUSHED) at the top, then NEW, DONE, FAILED, ARCHIVED.

Each card shows:

- Ticket id, title (read from `meta/ticket.md`), state badge
- Branch name, PR number (if pushed), iteration counts (e.g. `dev:2 qa:1`)
- Model pill (`opus` / `sonnet` / `haiku`) — see [docs/labels.md](labels.md)
- External links: Jira, Repo, PR
- Action buttons (contextual, see below)

### Card action buttons

Buttons only appear when relevant for the workspace's current state:

| Button | When it appears | What it does |
|---|---|---|
| **Approve** / **Reject** | AWAITING_APPROVAL | Resolve the gate (or stop the workspace) |
| **Retry** | FAILED, DEFERRED | Re-enter at `previous_state` |
| **Rerun** | DONE | Show reason dialog → re-clone source, refresh ticket data, restart from ANALYSIS |
| **🧹** | FAILED with AAPT2 corruption signature | Wipe `<gradle_home>/caches/*/transforms` and retry |
| **Take Control** | Any active state except already-MANUAL_CONTROL | Pause workspace, hand to a human Claude session |
| **Release** | MANUAL_CONTROL | Re-enter pipeline at ANALYSIS |

### Tickets switch panel

The Board has a **Tickets** switch panel: one global pill (`Tickets: running ▶` or `Tickets: paused ⏸`) plus one pill per repo, each `running` or `paused`. Click a pill to flip it. While a switch is paused, the ticket pipeline picks up no new tickets for that scope **and** stops advancing every existing workspace in that scope. An agent stage that is already running finishes, then the workspace waits. Approve and retry are still accepted, but nothing moves until tickets run again.

When the global switch is paused, every repo is paused with it. A repo pill whose own switch is still running is then drawn dimmed with a dashed border, and hovering it shows the tooltip `Paused globally — start Tickets first`. There is no banner. Those pills cannot be clicked until the global switch is running again.

The switches are stored in `~/CleaveData/reviewer/reviewer.db` and survive daemon restarts. **First start after upgrading:** the daemon creates that database with **tickets paused** and reviews running, so the ticket pipeline picks up nothing until you click the `Tickets: paused ⏸` pill to switch it to running, or send `tickets start` in Telegram (see [docs/telegram.md](telegram.md#switch-commands)).

### Credential strip

Above the cards, a strip shows one chip per credential that needs attention. The credentials checked are the forge and tracker tokens (per repo / project), the Telegram bot token, and the `claude` login on the host. Each chip shows one of these states:

- **invalid** (red, chip text `invalid or expired`) — the credential was rejected or has expired
- **no_access** (red, chip text `no access`) — the credential is valid but cannot reach the repo or project
- **expiring** (yellow, chip text `expires <local time>`) — the `claude` login expires within 3 days
- **unreachable** (grey, chip text `unreachable`) — the service could not be reached (or `claude` is not on the daemon's PATH), so the credential could not be judged

Click a chip to see the env var name (never the value), `since <local time>` (when it entered this state), `checked <local time>` (when it was last checked) and the fix, e.g. `Create a new token, put it in .env as GITHUB_TOKEN, restart the daemon.` The strip is hidden when every credential is ok.

Credentials are re-checked in the background every 15 minutes, and immediately after any adapter gets a 401/403. Telegram gets a line when a credential changes state, and again 3 days and 1 day before the `claude` login expires (nothing is sent about the Telegram token itself):

- `⚠️ <label>: invalid or expired. <fix>`
- `⚠️ <label>: no access (HTTP 403). <fix>`
- `✅ <label>: working again.`
- `⏳ The claude login on the host expires 2026-11-24 12:00. Log in again on the host in a GUI terminal.`

---

## Reviews view

**Reviews** in the sidebar (`#/reviews`) shows the reviews board of every repo with `reviewer.enabled: true` in its repo YAML: the open PRs the repo's `readiness.status_command` lists (those that involve you, or every open PR when the command takes `--all`), and whose turn it is on each. With no such repo the view says `No repo has reviews enabled.` It refreshes with the toolbar's **Refresh** and **Auto** like the Board; the Board's **Hide done** and **+ New Project** are not shown on it.

Nothing is posted from the dashboard: Telegram is the only posting path. Queued reviews and re-checks run unattended, up to `reviewer.max_parallel_jobs` (default 6) at once and one per PR, and their result comes to Telegram (see [review and re-check messages](telegram.md#review-and-re-check-messages)); you post it by replying there (see [replying to reviewer messages](telegram.md#replying-to-reviewer-messages)). The dashboard shows what was posted.

### Reviews switch panel

The same pill panel as the Board's [Tickets switch panel](#tickets-switch-panel), for reviews: one global pill (`Reviews: running ▶` or `Reviews: paused ⏸`) plus one pill per repo with reviews enabled. While reviews are paused, the board keeps refreshing and the [reviewer lines](telegram.md#reviewer-lines) are still sent, but no job is queued automatically and none starts (a running one finishes). **Review now** still queues one; it starts once reviews run again.

### Repo status

Each repo has a status line above the board: `<project>/<repo>`, its status when it is not `ok`, `synced <time> ago` (the last poll) and its jobs (`no jobs running`, or `1 running · 2 queued`); for every status but `ok`, the line under it gives the reason:

| Status | Meaning |
|---|---|
| `new` | Not polled yet |
| `ok` | The last poll succeeded |
| `doc_missing` | The base branch has no process document (`reviewer.process.doc`). The board refreshes, but no job is queued. The line reads `reviews are off: <doc> not on <base>` |
| `token_failing` | Credential health reports the repo's forge token as failing (see the [credential strip](#credential-strip)). The repo is not read until the token passes again |
| `readiness_failed` | The readiness command failed, timed out or printed something that is not a board snapshot |
| `error` | Updating the review clone or a GitHub read failed |

Each repo is polled every `reviewer.poll_interval_seconds` (default 300, at least 60). A failed poll keeps the last board and backs off: the next try comes after twice the poll interval, the delay doubles after each further failure up to 1 hour (never less than the poll interval), and the first poll that succeeds resets it.

### Board

The PRs of every repo with reviews are on one board of four columns, by whose move it is:

| Column | A PR is in it when |
|---|---|
| **To review** | one of your turns is `review` or `recheck`, or you have no role on the PR (you are not its author, an assignee or a reviewer) |
| **To fix** | else you have any other turn but `merge` (`resolve_conflicts`, `request_review`, `answer_threads`, `resolve_threads`, `address_review`, `finish_draft`, `fix_checks`, or any new action), or the PR's newest fix (of its threads or of its findings) is `done`, made a patch and is not pushed: pushing it, here or in Telegram, is your move |
| **With others** | you have no turn (and no fix to push): someone else has, or nobody |
| **To merge** | else your only turn is `merge` (with no fix to push) |

A PR you have no role on (listed by a status command with `--all`) has a dashed card tagged **Not involved**, and a `not involved` flag in its panel. Its move line says whose move it really is (`<login>: finish draft`); **Review now** reviews it, and no review of it starts by itself, since none of its turns is yours.

Each column lists its PRs the longest unchanged first; an empty one shows a dashed `Nothing to review`, `Nothing to fix`, `Nobody else's move` or `Nothing to merge`. The ticket pipeline's own PRs are on the board too.

With more than one repo, chips above the board (`All`, then each repo with its number of PRs) show one repo's PRs only; the choice is in the address (`#/reviews/<project>/<repo>`), and an address naming a repo without reviews shows them all. Each card then names its repo.

Four tiles count the PRs the repo chips show (the search does not change them):

| Tile | Value | Under it |
|---|---|---|
| **Need you** | the PRs in To review, To fix and To merge where the move is yours: a PR you have no role on (**Not involved**) is not counted | how many in each: `2 to fix · 1 to merge` |
| **Critical in your PRs** | the critical findings of the reviews of your own PRs (self-reviews) at their current head, `done`, `posted` or `stale`; red above 0 | the PRs with the most: `self-reviews: 15 in #1712 · 4 in #1715`, or `no self-reviews` |
| **Waiting on others** | the PRs in With others | who holds them: `all on <login>`, or `<login> 3 · <login> 1` |
| **Longest wait** | how long the oldest PR in With others has been unchanged | its number and what it waits for: `#1708 · re-check` |

A card shows, top to bottom:

- `YOUR PR` or `REVIEWING`, `#<number>` (links to GitHub), the ticket key (links to `<jira url>/browse/<key>` when the repo's project tracks tickets in Jira, else plain text) and the flags `large` (at least `reviewer.max_files_auto` changed files), `draft` and `pipeline`
- the PR's title as it is, without a leading ticket key; at most two lines, the whole title in its tooltip
- for a PR whose newest review is `done`, `posted` or `stale`: on your own PR `self-review · <when>`; a bar of its critical, major, minor and question findings, on a scale shared by the whole board (15 critical draws longer than 4); the counts in words (`15 critical · 3 minor · 7 questions`; critical red, major orange, minor yellow, question blue, each beside its word) and `posted`, `report in Telegram` or `not delivered yet`. A review of an older commit is dimmed and says `on an older commit`. A review your turn left (`stale`: you reviewed the PR on GitHub, say, or your review request was removed) keeps its findings on the card, dimmed, and says `turn gone`, since Telegram no longer posts from its message. Pre-existing findings and the dropped candidates are in the [side panel](#pr-detail-view)
- while a job is queued, running, deferred or failed, a line saying so: `Cleave review running · 12 min`, `Claude fixing · 8 min`, `Cleave re-check deferred · next try 14:20`, `Cleave review failed: <reason>`; `skipped` when you skipped the current head
- whose move it is (your avatar, or the logins holding it; GitHub pictures, the login's initials when one does not load), what to do in words (`resolve conflicts, request review`, `octo-reviewer: answer threads`, `post the review` once its message was delivered, `fix ready: push`, `fix pushed: post the replies`; a fix of the findings says `fix ready: push` until it is pushed, and nothing once pushed or without a patch; the line names no channel: each is done here or in Telegram) and how long the PR has been unchanged (amber from 2 days)

Click a card (outside its links and buttons) to open the PR in the [side panel](#pr-detail-view); the open card is outlined. Keyboard: `j` / `k` move the card focus (down a column, then the next), `Enter` opens the focused card, `Esc` closes the panel and `/` goes to the search box, which hides the cards whose number, title or ticket key do not contain the text (the column counts follow). No key queues a job.

A review is queued automatically when your turn is a review, the head has been quiet for `reviewer.quiet_minutes` (default 30), the head is not skipped, reviews are running and the process document is on the base branch. A PR with at least `reviewer.max_files_auto` (default 40) changed files is not reviewed automatically: Telegram gets a line instead.

The job runner starts queued jobs as they become runnable, up to `reviewer.max_parallel_jobs` at once (one per PR; none while the work volume has less free space than `workspaces.min_free_disk_gb`), and writes these states:

| State | Meaning |
|---|---|
| `running` | Cleave is preparing the PR, or the model session is running |
| `done` | The session ended well, its report (a replies plan for a re-check) was read and the PR still has that head and your turn (a **Review now** review needs no turn); its message goes to Telegram |
| `failed` | Prepare or the session failed; the reason is in the detail view and Telegram gets a `failed` line (none for a job interrupted by a daemon restart). Queued again on the next poll, until two jobs have failed at that head |
| `deferred` | The session hit the Claude usage limit, or the `claude` CLI is not logged in. The detail view shows its next try; the job runs again then. Its third deferral fails it |
| `posted` | A post from a Telegram reply landed on GitHub, in full or in part. The numbers that did not land can still be posted from the same message. A newer review supersedes it like a `done` one. A posted re-check holds back a new re-check of the same threads until your own thread turns change after the post: another reviewer's turn, a role or the draft flag does not count |

A job whose PR moved on meanwhile ends `superseded` (a new head, or a newer job), `stale` (the PR or your turn is gone) or `skipped` (you skipped that head while it ran), and nothing is sent for it.

Card buttons, shown while the pointer is on the card, the card has the keyboard focus or its panel is open, and always on touch screens. The main button is always shown, except on the open card (its panel's header has it): the move the card's line names, Cleave's own (Push, Reply all, Post review…), else one on GitHub (Re-request, Request review…, Ready for review, Merge…), else **Review now** or **Fix now** when the line is your review or your threads. A re-check, `resolve conflicts` and `fix checks` have none. What any card button did shows on its card in one line (in full in the panel while that PR is open); a press while another push, post or move waits for its answer says `Another action is running: wait for its answer, then try again.` A card of your own PR has none but its main button and **Fix now**; its **Self-review** is in the [side panel](#pr-detail-view). `#<number>` opens the PR on GitHub.

| Button | When it appears | What it does |
|---|---|---|
| **Push**, **Push anyway…**, **Reply all**, **Post review…** (the main button, first) | The card's line names a move Cleave makes: `fix ready: push` (**Push anyway…** when the fix touches protected files), `fix pushed: post the replies`, `replies ready: post` or `post the replies`, `post the review`. None while a post of the fix is under way. The panel's header shows the same button | Reads the PR's detail, then sends the panel's command (`POST /api/reviews/action`): `push` alone (never the replies with it: the card then reads `fix pushed: post the replies`), `post_replies` of every reply, or `post` of every confirmed finding. **Push anyway…** asks `The fix touches protected files: <files>. Push it anyway?`; **Post review…** asks for Comment, Approve or Request changes (Comment checked) and shows the report's suggested event. What it did shows on the card, and in the panel while that PR is open, as the panel's result line; Telegram gets its `🖥 Dashboard: …` line |
| **Re-request &lt;login&gt;** (the main button) | Your own PR whose line is `request review` because a reviewer asked for changes on an older head (the status command names them), and nobody new is to be asked (else **Request review…**) | Asks them to review again on GitHub (`POST /api/reviews/pr-move`), at once. The repo is then polled within a minute, so the card's line follows |
| **Request review…** (the main button) | Your own PR whose line is `request review` and nobody asked yet | Opens a dialog listing GitHub's suggested reviewers and the people who reviewed the author's 20 newest PRs (how many of them), as checkboxes, with a field for other logins (commas or spaces; a leading `@` is dropped); asks the ticked and typed ones on GitHub. None picked: `Pick at least one reviewer.` |
| **Ready for review** (the main button) | Your own draft PR (`finish draft`) | Marks it ready for review on GitHub, at once |
| **Merge…** (the main button) | Your own PR whose line is `merge` | Opens a dialog with the merge methods the repo allows (merge commit checked when allowed, else squash, else rebase); merges at the head the card shows: if the PR moved since, nothing is merged (`The PR's head moved on GitHub: refresh the board.`). GitHub refuses a merge that checks, reviews or conflicts block (405). Until a poll shows the merge, a second **Merge…** answers `Already merged: the board shows it within a minute.` |
| **Review now** (**Review anyway** on a large PR) | A PR you do not author, with no report of its current head; while a review at the current head is queued, running or deferred, a disabled **Queued**, **Running** or **Deferred** in its place (**Deferred** names its next try: `Deferred; retries at HH:MM`, or `Deferred; retries automatically` without one) | Queues a review at the PR's current head, whatever the quiet window, the PR's size, a skip or the reviews switch. Refused with `already queued` when a review at that head is queued, running or deferred |
| **Review again…** | In place of Review now when the PR's newest review is of its current head and `done`, `posted` or `stale` | Asks first (`Review #1720 again at the same commit?`, with when the last one finished, how long it took and its cost), since the new review replaces that report; then as Review now |
| **Skip** | Your turn is a review and the current head is not skipped yet, or a review is queued or deferred at it (**Review now** after a skip) | Skips this head: its queued or deferred review is dropped and none is queued for it again. A new push makes a new head, reviewed as usual |
| **Fix now** | Your own PR in a repo with `reviewer.process.address_doc`, one of your turns is `answer_threads`, `resolve_threads` or `address_review`, no ticket workspace holds the PR and no skill-flow comparison holds it. Its state comes from the PR's newest address job, whatever job is newer (a self-review after a fix does not hide the fix): while that job is queued, running or deferred, a disabled **Queued**, **Fixing** or **Deferred**; while a `push` or a post of its replies is under way, a disabled **Posting** | Queues its address job (the threads message of your own PR, see [replying to reviewer messages](telegram.md#replying-to-reviewer-messages)) at once instead of after the quiet window (`POST /api/reviews/fix-now`). The result comes to Telegram as usual; `push` and `post replies …` there, or the panel's controls, act on it |
| **Fix again…** | In place of Fix now when the PR's newest fix job is an address job `done`, `posted` or `stale` | Asks first: the fix from then is replaced (push it first, here or in Telegram, to keep it), or, once pushed, its drafted replies are (post them first); then as Fix now |
| **Fix findings…** (panel only) | Your own PR in a repo with `reviewer.process.address_doc`, no ticket workspace or skill-flow comparison holding it, whose newest review at its current head (its self-review) is `done` or `posted` and has a confirmed finding (🔴 🟠 🟡). One fix of a PR at a time: while the PR's newest fix of either kind is queued, running or deferred, a disabled **Queued**, **Running** or **Deferred**, and while a `push` or a post of a fix is under way a disabled **Posting** (once, when Fix now shows it too) | Asks first: **Fix the findings of the self-review?** `Claude (Opus) weighs each of the 4 confirmed findings of the self-review at 8f01a2b3c, fixes those that hold and says why for the rest. Nothing is pushed until you push it, here or in Telegram.` Then queues a findings job (`POST /api/reviews/fix-findings`); its result comes to Telegram (see [fixing the findings of a self-review](telegram.md#fixing-the-findings-of-a-self-review)), where `push` (or the panel's **Push**) acts on it |
| **Fix findings again…** (panel only) | In place of Fix findings… when the PR's newest fix job is a findings job `done`, `posted` or `stale` | Asks the same, adding `The fix from <when> is replaced; it was not pushed.`; then as Fix findings… |

While a button runs, it reads `Queuing…` or `Skipping…`; then a line at the bottom of the screen says what happened (`Queued a review of #1712 at 26c4929c5`, `Queued a fix of #1712 at 26c4929c5`, `Queued a fix of the findings of #1712 at 26c4929c5`, `Skipped this head of #1712 (26c4929c5)`) or why it failed (`Review now of #1720 failed: no PR #1720 on the acme/app board` when the PR left the board after the last refresh, `Fix now of #1720 failed: not fixable: no thread turn`, `… failed: a post is under way`).

Polls, new turns and job changes are also recorded in the [Event Log](#event-log-view) as `reviewer_poll`, `reviewer_poll_failed`, `reviewer_turn` and `reviewer_job` events (not in its type filter yet).

### PR detail view

A PR opens in a side panel beside the board, which stays visible (at a width of 760 px or less the panel covers the screen, and Tab stays inside it); the keyboard focus moves to its **← Back**, and **← Back** or `Esc` closes it and returns the focus to the PR's card. The toolbar's **Refresh** and **Auto** redraw the board and the panel in place: the panel stays open, the findings you expanded stay expanded, and the search text and the card focus are kept. It changes nothing but through its buttons:

- **Header** — the PR number (links to GitHub), the ticket key (a Jira link, as on the card), the number of changed files and the flags; the title, whole; then whose move it is and what to do, in the card's words, the job line while a job is queued, running, deferred or failed, and the buttons, the card's main button first; `not on the board` once the PR has left it. The buttons are the card's (**Review now** / **Review anyway** / **Review again…**, **Skip**) on a PR you do not author; on your own PR, **Self-review** (a review of your PR at its current head, as Review now), **Fix now** / **Fix again…** under the card's rule and **Fix findings…** / **Fix findings again…**. When a report of the current head exists, **Self-review** reads **Self-review again…** and asks first: `Run a self-review of #1712 again at the same commit?`, with when the last one finished, how long it took and its cost (API-equivalent), since the new report replaces it. Nothing in the panel opens GitHub but `#<number>`
- **Verdict** — for a PR with a review report: the suggested event (`REQUEST_CHANGES` red, `APPROVE` green, `COMMENT` plain) and its reason, then the report's summary; a bar with one segment per finding colored by kind, the count of each kind and of the dropped candidates; and the Telegram line of that review's message: `sent in <n> parts`, `not delivered yet` (followed by `message not delivered after 5 attempts` once Cleave has stopped trying), or `not sent: the job ended <state>` for a review whose PR moved on before its message went out. The run's other notes are in the Jobs table
- **Findings** — grouped Confirmed (critical, major, minor), Questions and Pre-existing. Each shows its kind, number, title, post state, file and line and any note; click it for its text. Critical findings start expanded. The post state is a badge with its word: `posted` (landed on GitHub at the report's head), `ticked` (named in a post that did not land in full: one still under way, failed, partly posted, unverified or interrupted; see **Posts**) or `open` (not posted). After an edit or a draft, the findings show the edited report
- **Dropped (never posted)** — the candidates the review dropped, and why
- **Fix of the findings** — under the report, when the newest findings job that wrote verdicts fixes that report: `n. VERDICT — why` per finding of the self-review, then, when it is the PR's newest fix, its **Fix** block (its files, the protected ones, `Pushed <sha>` or `Not pushed: reply "push" in Telegram.`, and the patch). A fix is only ever shown beside the job that made it
- **Replies** — when the newest re-check (or address job) that wrote a plan is newer than the report and than a fix of that report's findings: its drafted replies (number, post state as for findings, target thread or review, text), in place of the verdict and the findings
- With neither: one line saying why there is no report — the job is queued or running, deferred until its next try, failed (with the reason), or the PR is too large for an automatic review
- **Posts** — every post Cleave made from a Telegram reply, newest first: its state (`posting`, `posted`, `partial`, `failed`, `unverified` or `interrupted`), its kind (`review`, `approve` for an approve without a body, `replies`), its event, the numbers it named, the comments it landed, the head and when; the links to what landed on GitHub (links to anything but `https://github.com/` show as text); and its error. With none: `Nothing posted yet. Posting is done by replying to the review in Telegram.`
- **Jobs** — every job of the PR, newest first, the edit and draft jobs too: kind (`review`, `recheck`, `edit`, `draft`), state (a `deferred` job also shows its next try), the reason and the job's notes (a worktree left behind, a message not delivered after 5 attempts), the head, when it started, how long it ran, its cost and the Telegram message ids of its message. The cost is the API-equivalent estimate the Claude Code CLI reports; the sessions run on the claude.ai login, so it is not what you pay
- **Raw report** — the newest review report as the session wrote it (as edited, after an edit or a draft), collapsed; a report Cleave could not read shows only this

#### Push, post and commands from the panel

The panel can do what a reply to the job's Telegram message does, through one shared layer, so a push or post from either place gives the same outcome and the same refusals. The controls follow the PR's newest job, as the buttons of its Telegram message do:

- **Push + reply all** / **Push**, and **Push anyway + reply all…** / **Push anyway…** for a fix that touches protected files (asks first, naming the files), under a fix that is not pushed yet. Once it is pushed, **Reply all** posts its drafted replies
- **Reply all** and **Reply to checked** under the drafted replies of a re-check or an address job (the checked rows; none checked does nothing)
- **Post all confirmed** and **Post checked** under a review report, with the review event chosen beside them (Comment, Approve, Request changes)
- A **command field** for free text to the newest job you could reply to (`push`, `post replies 1 2`, `again: …`), worded as in Telegram. A command Cleave only understood through the model is not run at once: the panel asks `Yes` / `No` first, and a `Yes` more than 15 minutes later is refused (`Expired …`)

A control acts on the job the panel showed. If a newer job has replaced it meanwhile, the action is refused with the outdated line and nothing runs. Pushing the same fix from the panel and from Telegram at the same moment pushes once: the second is refused with `Already pushed as …`. The line under the buttons says what the action did or why it was refused, in English whatever the project's Telegram language.

Each action is announced in Telegram as a reply to the job's message: `🖥 Dashboard: <the command> — <outcome>` (with the commit or review link when there is one), see [telegram.md](telegram.md#actions-from-the-dashboard). A refusal and a read-only command (`text`, `diff`) are not announced. If Telegram is down, the action still completes and the failure is logged.

The `/api/reviews/action`, `/api/reviews/confirm`, `/api/reviews/pr-move`, `/api/reviews/candidates` and `/api/reviews/merge-options` routes exist only when the daemon has reviews enabled. Every dashboard action logs one line to the daemon log (`Dashboard <what> on <repo> #<n>: <outcome>`); a typed command's text is never logged.

Each job's files are kept in `<reviewer data dir>/jobs/<job id>/`: what the session's run directory held (`meta.env`, the report, `tg_lines.json`, …), the session's settings and transcript, and `posting/`, Cleave's own copy of the report that edits, drafts and posts work on. The poster tool's ledgers are in `<reviewer data dir>/ledgers/<project_id>/<repo_id>/PR_<number>/`. Once a day, a PR closed 14 days ago or more loses its job directories, its ledgers and its `pr-review/<number>` branch in the review clone; its rows in `reviewer.db` stay.

### Review clone

Each reviewed repo has a Cleave-owned single-branch clone of its base branch at `<reviewer data dir>/repos/<project_id>/<repo_id>` (default `~/CleaveData/reviewer/repos/<project_id>/<repo_id>`), created on the first poll. A PR stacked on another PR's branch (its base is that branch, not the repo's base branch) gets that branch fetched beside it: the job's merge base (`MB`) and `BASE` in `meta.env` are that branch's, as GitHub diffs the PR, while the process document, the prepare steps and the session still run from the base branch's checkout; nothing from the unmerged branch runs. The same holds for a PR into any other branch (a release branch, main). A base name that is not a plain branch name, or one that differs from the base branch or `pr-review/…` only in case (the clone may sit on a case-insensitive disk), is refused (`'<name>' is not a branch Cleave fetches`), and a base branch deleted on GitHub fails the job until GitHub retargets the PR. Whatever the base, the job clone's checkout must be the base branch commit just fetched, or no step runs. The forge token reaches git only through the environment of that one git process: it is never written to `.git/config` or the keychain.

No review session runs in it. Each review or re-check gets a **job clone** of its own, `git clone --local` from it (objects hard-linked, no token, its origin the review clone's path) at `<reviewer data dir>/work/<project_id>/<repo_id>/<job_id>/clone`, with the PR's worktree beside it; the job's directory is removed when the job ends, and any left at start-up is removed then. The review clone is locked only for a job's git there (refresh, the PR head, the readiness threads, the job clone), for a poll's refresh and readiness, and for a post's poster runs.

Repos added by hot reload (the **+ New Project** wizard) are not on the Reviews view until the daemon restarts.

---

## Ticket Detail view

Click any card on the Board to open this view.

- **Pipeline progress bar** — shows the current stage on the workflow line. Off-pipeline states (BLOCKED, FAILED, MANUAL_CONTROL, AWAITING_APPROVAL) highlight the `previous_state` so you can see where the ticket was.
- **External links** — Jira ticket, repo root, PR (extensible to CI/CD links via project config)
- **Workspace info** — branch, started_at, last_updated_at, error message if any
- **Iteration counts** — per stage (`dev`, `qa`, `scope_check`, `pr_review`, `fix`)
- **Reports viewer** — every Markdown report under `<workspace>/source/reports/` is browsable inline (BA plan, scope-guard report, QA output, PR-comment resolution, …)
- **Meta files** — `<workspace>/meta/` contents (ticket.md, plan.md, etc.)
- **Logs** — per-stage logs under `<workspace>/logs/`

The same action buttons available on the Board card appear in the toolbar of this view. The panel also answers and steers a ticket, with the same effect as the Telegram reply or button:

- **Answer** — for a BLOCKED ticket that waits for your input (it shows the question): a text box and **Send answer**. Writes the answer and wakes the pipeline. If the question was already answered in Telegram, it is refused (`… is ANALYSIS now: nothing was done.`) and nothing is written twice
- **Retry** and **From stage…** — for a BLOCKED or FAILED ticket: **Retry** restarts at the stage it stopped at, as Telegram's **Retry** button does; **From stage…** picks `analysis`, `plan_review`, `dev`, `scope_check`, `code_review`, `qa` or `push`
- **Skip** — for a BLOCKED or FAILED ticket: moves it on without that stage
- **PR comments** — for a PR_REVIEW ticket: each escalated comment with its author, `file:line`, the suggestion, the agent's assessment and the decision so far. Per undecided comment: **Fix**, **Won't fix** (with an optional reason) and **Re-investigate** (with a hint, required). The decision is stored as Telegram's tap or reply stores it, and the last one wakes the loop
- **Reviewed** — for a PR_REVIEW ticket: what Telegram's **Review Complete** button does

While an action runs, the panel's buttons are disabled. A refusal is shown in an alert (`Retry: <why>`), then the panel reloads; it reloads after every action. Every action is announced in Telegram with 🖥 (see [telegram.md](telegram.md#actions-from-the-dashboard)): as a reply to the ticket's escalation message when it has one, to the comment's own message for a comment decision, else to a message with the ticket header.

---

## Event Log view

Every event the orchestrator, agent runtime, and Telegram adapter emit, persisted to SQLite (`data/events.db`).

- **Filter by type** dropdown: `agent_dispatched`, `agent_completed`, `agent_failed`, `stage_transition`, `workspace_created`, `pr_created`, `dashboard_approve`, `dashboard_reject`, `dashboard_retry`, `dashboard_rerun`, `manual_control_started`, `manual_control_released`, `tg_message_received`, `tg_message_sent`, `poll_cycle`, `daemon_started`
- **Project filter** — clicking a project in the sidebar narrows the log
- **Ticket filter** — opens automatically when you arrive from a Ticket Detail view

Useful when something looks wrong and you want to see exactly what the daemon did, in order.

---

## Settings view

- **Claude model picker** — `Haiku` / `Sonnet` / `Opus`. The choice is the global default model, applied to new workspaces unless a `model-*` Jira label overrides it. Changes are stored in dashboard SQLite (`settings` table) and hot-reloaded by adapters on the next agent dispatch — no daemon restart required. In-flight workspaces keep the model that was current when they were created.
- **Telegram language** — `English` / `Українська`, one row for `Global (no project)` and one per loaded project, each showing the language it uses now (a project without its own setting shows the global one). **Save** writes `telegram.language` into that project's `project.yaml` (`global.yaml` for the global row): only that line changes, added to the `telegram:` block (or a new block) when missing, and the file is read back to check that nothing else did. A `telegram:` section written inline (`telegram: {…}`) is refused with a message: edit that file by hand. The running daemon uses the new language from the next message; no restart. See [docs/telegram.md](telegram.md#language) for what is translated.

See [docs/labels.md](labels.md) for per-ticket overrides.

---

## Credential health (cross-cutting)

Credential checks run in the background every 15 minutes (and at once after an adapter 401/403); the results feed the [credential strip](#credential-strip) on the Board:

| Credential | What it verifies | Fix hint shown on failure |
|---|---|---|
| Forge token (GitHub / GitLab), per repo | Token is accepted and can reach the repo | New token in `.env`, or check access (SSO for classic GitHub tokens) |
| Tracker token (Jira / Trello), per project | Token is accepted and can reach the project | New token in `.env`, or check access |
| Telegram bot token | `getMe` succeeds | New token in `.env` |
| `claude` login on the host | Logged in, and not expiring within 3 days | Log in again on the host; if `claude` is not found, add its directory to the daemon's PATH |

Stage verifiers also run after each agent stage to catch silent failures (e.g. dev-agent ran but never committed) — these don't show up in the credential strip but transition the workspace to BLOCKED with a real reason instead of silently advancing.

---

## Take Control

A "let me drive this one" escape hatch.

1. From a Ticket Detail view (or its Board card), click **Take Control**.
2. The workspace transitions to MANUAL_CONTROL. The orchestrator skips it on subsequent poll cycles. Any agent currently running on it is cancelled.
3. The dashboard shows a `claude` CLI command. Paste it into a terminal — it opens an interactive Claude Code session against the workspace's source tree.
4. Work on the ticket directly. Commit, push, edit the workflow, whatever you need.
5. When done, click **Release**. The workspace re-enters at ANALYSIS so the pipeline can pick up your changes from a clean state.

Configurable terminal command via `global.yaml` → `dashboard.terminal_command` (default `gnome-terminal -- bash -c`).

---

## + New Project wizard

Click **+ New Project** in the toolbar. The wizard runs the [Atlas project-setup-agent](features/project-setup-agent.md) and walks you through five steps (defined in `dashboard/static/js/project-wizard.js`):

1. **Project** — id, display name
2. **Jira** — URL, email, project key, trigger labels (defaults to `[ai-pipeline]`), ignore labels, status name mapping; validates against the live Jira API before continuing
3. **Repository** — VCS provider, owner/repo, default branch, branch prefix, merge method, clone URL; validates against the GitHub/GitLab API
4. **Notifications** — per-project Telegram chat id (optional; falls back to the global default) and the Telegram language (`English` or `Українська`, preselected with the global one). Left as it is, the project follows the global language; another choice is written into `project.yaml` by Cleave once Atlas has written the file
5. **Review** — Atlas writes `config-live/projects/<id>/{project,repos/*}.yaml`

The orchestrator hot-reloads the new project: registers the GitHub adapter, attaches the Jira tracker if it wasn't already attached, extends the Telegram allowlist, and emits `project_loaded` events. No daemon restart.

If validation fails on any step, the wizard stops and tells you what to fix — bad credentials, missing scope, unreachable hosts.

---

## REST API

The dashboard frontend is a thin client over a JSON API. Useful if you want to script against it. Routes are registered in `dashboard/web.py` (read endpoints) and `dashboard/actions.py` (write endpoints) — that's the source of truth; the list below is the headline shape.

Read endpoints (`GET`):

- `/api/health` — health probe + event count
- `/api/events` — recent events with optional `project_id` / `ticket_id` filters
- `/api/projects` — projects with workspace counts
- `/api/projects/{id}/tickets` — ticket history per project
- `/api/health/credentials` — credential states (`?refresh=1` re-checks now)
- `/api/switches` — running/paused switches (also `PUT` with `{"mode": "tickets"|"reviews", "state": "running"|"paused", "scope": "*"|"<project>/<repo>"}`)
- `/api/reviews` — per repo with reviews enabled: status, reason, poll times, your login (`me`), the Jira base URL of its project (`jira_url`, `""` when the project does not track tickets in Jira) and the `my_turn` / `waiting` PRs. Each PR carries its row's job (`job`: its newest review, re-check or address job), its newest review whatever job is newer (`review`: state, head, findings, start and finish times, duration and API-equivalent cost), its newest fix job of either kind (`fix`: `kind` — `address` or `findings` —, state, head, start time, whether it made a fix, whether that fix was pushed, whether a post of it is under way and whether it touches protected files, `protected`), whether Fix now applies (`can_fix`), whether Fix findings… applies (`can_fix_findings`) and the reviewers Re-request asks (`rerequest`: who asked for changes on an older head, from the status command's `request_review` turns). The board is computed in the browser from these (the `/api/reviews` routes exist only when at least one repo has reviews enabled)
- `/api/reviews/pr?scope=<project>/<repo>&number=<PR number>` — one PR for the detail view: its board row, its jobs (newest first), the newest review report (parsed and raw), the newest re-check's drafted replies, the newest findings job's verdicts (`verdicts_job`, `verdicts`, and the review they fix, `verdicts_review`), the fix, patch and pushed commit of the newest fix job of either kind (`fix`, `fix_patch`, `pushed_sha`, and that job, `fix_job`), each number's post state (`number_states`, `reply_states`) and the PR's posts; 400 without a scope or with a number that is not an integer, 404 for a PR with neither a board row nor a job
- `/api/workspaces` — every workspace from disk (`<project>/<repo>/tickets/<id>/` and `<project>/<repo>/setup/`), with state.json + report/meta listings. `disk_bytes` is the size a background thread last measured (a request remeasures one older than 5 minutes for a later load), `null` before the first measurement after a restart
- `/api/workspaces/{ticket_id}/report/{filename}?folder=reports|meta|logs` — fetch one file
- `/api/settings/model` — global default model (also `PUT` to change)
- `/api/settings/telegram-language` — `{"options": ["en", "uk"], "global": "<lang>", "projects": [{"id", "name", "language", "effective"}]}` (`language` is the project's own setting, `""` when it follows the global one); `PUT` with `{"project_id": "<id>" | null, "language": "en" | "uk"}` writes it (null: `global.yaml`) → 200 `{"language", "effective"}`, 400 for another language, 404 for a project not loaded, 409 when the YAML's `telegram:` section cannot be edited line by line, 403 `cross-site request refused` under the same rule as the reviews routes
- `/api/daemon/status` — current mode + uptime

Write endpoints (`POST`):

- `/api/daemon/mode` — switch auto/manual
- `/api/workspaces/{id}/approve|reject|retry|skip` — resolve gates and re-run. `retry` takes an optional `{"from": "<stage>"}`
- `/api/workspaces/{id}/answer` — body `{"text": "<answer>"}`; answers a BLOCKED ticket's question
- `/api/workspaces/{id}/comments/{comment_id}/decision` — body `{"decision": "fix"|"wont_fix"|"reinvestigate", "text": "<reason or hint>"}`; `reinvestigate` needs a hint
- `/api/workspaces/{id}/reviewed` — a PR_REVIEW ticket's review is complete
- `/api/workspaces/{id}/take-control|release-control` — hand a workspace to a human and back
- `/api/workspaces/{id}/pause|unpause|resume|archive|delete|clean` — workspace lifecycle
- `/api/workspaces/{id}/rerun` — DONE-only: re-clone source, refresh ticket data from tracker, restart pipeline from ANALYSIS; body `{ "reason": "<non-empty string>" }`
- `/api/workspaces/{id}/clear-gradle-and-retry` — server-side validates the workspace is FAILED and the error matches the AAPT2 signature before wiping caches; it clears only an explicitly set `GRADLE_USER_HOME` (never the shared `~/.gradle`) and stops only that home's Gradle daemons
- `/api/projects/create` and `/api/projects/validate-step` — the wizard's two endpoints (validate one step against live APIs, then commit)
- `/api/reviews/action` — body `{"scope", "number", "job_id"}` and exactly one of `"command": {…}` (a button's command) or `"text": "<command>"` (typed): a push, post or reply command on a job, answered `{"state", "texts", "command"}` with its outcome, or `{"state": "confirm", "question", "confirmation"}` for a command the model understood, to answer at `/api/reviews/confirm` (body `{"confirmation": "<id>", "yes": true|false}`). A refusal (why the command cannot run, in the words Telegram answers with; at confirm also `Cancelled`, `Already handled` or `Expired`) is a `200` with `{"state": "refused", "texts": ["<why>"]}`; `400` for a bad body, `404` for an unknown job or a confirmation that is not the dashboard's. Present only with reviews enabled. The ticket routes above refuse with a `400` and `{"status": "error", "message": "<why>"}` instead
- `/api/reviews/pr-move` — body `{"scope", "number", "move"}` with `move` `request_review` (and `"reviewers": ["<login>", …]`, 1 to 10, never the author), `ready`, or `merge` (and `"method": "merge"|"squash"|"rebase"`, `"head": "<the head the board showed, 7+ characters>"`): a move on GitHub, answered `{"state": "done"|"refused"|"failed"|"unverified", "text"}`. It runs only while the PR's board row names it as your turn (`request_review`, `finish_draft`, `merge`); `failed` when GitHub refuses it, `unverified` when GitHub does not answer (it may have landed: `check #<n> on GitHub before trying again`). After a move the repo is polled within a minute, even when a poll was running at the time. `400` for a bad body or an unknown move, `404` for a PR not on the board. `GET /api/reviews/candidates?scope=&number=` (`{"candidates": [{"login", "reason"}]}`) and `GET /api/reviews/merge-options?scope=` (`{"methods", "default"}`) feed the dialogs; `502` with GitHub's status when GitHub fails
- `/api/reviews/review-now`, `/api/reviews/skip`, `/api/reviews/fix-now` and `/api/reviews/fix-findings` (fix-now only queues the PR's address job, fix-findings its findings job; pushing and posting go through `/api/reviews/action`) — body `{"scope": "<project>/<repo>", "number": <PR number>}`; 404 for a PR not on the board (or a repo no longer configured), 409 from review-now when a review at that head is already queued, running or deferred, 409 from fix-now with `already queued` (a fix of either kind is pending), `a post is under way` or `not fixable: <why>` (`no address_doc`, `not your PR`, `no thread turn`, `a ticket workspace holds it`, `held by a comparison`), 409 from fix-findings with `already queued`, `a post is under way` or `not fixable: <why>` (`no address_doc`, `not your PR`, `a ticket workspace holds it`, `held by a comparison`, `no finished self-review at this head`, `no confirmed findings`); 403 `cross-site request refused` under the cross-site rule in [Starting and reaching it](#starting-and-reaching-it)

---

## See also

- [docs/features/dashboard.md](features/dashboard.md) — implementation spec, change log, FRs
- [docs/labels.md](labels.md) — per-ticket model selection via Jira label
- [docs/telegram.md](telegram.md) — equivalent operations from Telegram (most dashboard buttons have a Telegram counterpart)
- [docs/troubleshooting.md](troubleshooting.md) — when the dashboard isn't reachable or shows red health

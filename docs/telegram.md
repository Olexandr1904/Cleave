# Telegram Bot Reference

Every command, switch command, free-text intent, inline button, and reply pattern Cleave's Telegram bot understands.

This doc is a user-facing reference. For the implementation/spec, see [docs/features/telegram-notifications.md](features/telegram-notifications.md).

---

## Setup

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

- **Bot token:** create a bot via [@BotFather](https://t.me/BotFather)
- **Chat id:** message [@userinfobot](https://t.me/userinfobot) for a personal chat id; for a group, add the bot to the group and use the negative group id

The bot polls (no webhooks) and starts automatically when the daemon starts. If `TELEGRAM_BOT_TOKEN` is empty, Telegram is disabled and Cleave escalates only via the dashboard.

## Allowlist

The bot ignores messages from chat ids that aren't on its allowlist. The allowlist is built at startup from:

- `global.yaml` → `telegram.default_chat_id`
- Each project's `project.yaml` → `telegram.default_chat_id`

If the allowlist ends up empty for any reason, the bot is locked down (fail-safe) — it accepts no messages until at least one chat id is configured. New projects added through the dashboard wizard extend the allowlist live.

---

## Switch commands

Fixed commands pause and resume the ticket pipeline and reviewer mode. They are not replies, and they run before the LLM intent parser (allowlisted chats only):

| Command | Effect |
|---|---|
| `tickets pause` / `tickets start` | Pause / resume the ticket pipeline globally |
| `reviews pause` / `reviews start` | Pause / resume reviews globally |
| any of the above + a repo id, e.g. `tickets pause app` or `reviews start acme/app` | Same, for one repo (`repo` or `project/repo`) |
| `status` | The switch block (tickets and reviews, global and per repo), then the pipeline summary |

Commands are case-insensitive. Russian/Ukrainian words are accepted as input synonyms: `тикеты` / `тікети` for `tickets`; `ревью` / `рев'ю` (ASCII or typographic apostrophe) for `reviews`; `пауза` for `pause`; `старт` for `start`; `статус` for `status`. Replies are in English whichever spelling you use.

The switch block looks like this (`(global)` marks a repo that is paused only because the global switch is):

```
Tickets: paused
· acme/app — paused (global)
· acme/web — paused
Reviews: running
· acme/app — running
```

With no repos it shows `· no repos` for tickets and `· no repos with reviews` for reviews. Errors for a repo id:

- `Unknown repo "nope". Known: acme/app, acme/web`
- `"app" exists in several projects: acme/app, beta/app — use project/repo`
- `Reviews are not enabled for acme/web (reviewer.enabled in YAML)` — `reviews pause <repo>` or `reviews start <repo>` for a repo whose `reviewer.enabled` is off

While tickets are paused, `analyze` refuses the ticket and names the command that resumes it: `ACME-1: tickets are paused globally — send "tickets start"`, or `ACME-1: tickets are paused for acme/app — send "tickets start acme/app"` when only that repo is paused.

The switches are stored in `~/CleaveData/reviewer/reviewer.db` and survive restarts. **First start after upgrading:** the database is created with **tickets paused** and reviews running, so the ticket pipeline picks up nothing until you send `tickets start` (or click the `Tickets: paused ⏸` pill on the Board to switch it to running).

---

## Free-text commands

The bot does not require slash prefixes. Free-text messages are classified by Claude using full pipeline context (mode, awaiting approvals, active workspaces, blocked workspaces, deferred workspaces) — so "yes" resolves the one pending approval if there's only one, "the bug is X" gets routed as `provide_input` if a workspace is BLOCKED, etc.

Recognized intents (from `integrations/telegram/intent_parser.py`):

| Intent | Trigger phrases | Effect |
|---|---|---|
| `status` | `/status`, `status`, `what's running`, `pipeline status` | The switch block first, then the pipeline summary: mode, uptime, active workspaces, recent completions |
| `status` (drill-down) | `/status TICKET-123`, `status of TICKET-123` | Per-workspace detail: stage, branch, Jira URL, PR URL, iteration counts |
| `analyze` | `analyze TICKET-1, TICKET-2`, `pick up TICKET-1` | Validate tickets against Jira and create workspaces (used in manual mode) |
| `approve` | `approve`, `approve TICKET-1`, `yes` (when one approval pending) | Resolve an AWAITING_APPROVAL gate to its happy-path next state |
| `reject` | `reject`, `reject TICKET-1`, `no` (when one approval pending) | Stop a workspace at the gate |
| `set_mode` | `/auto`, `/manual`, `auto mode`, `manual mode` | Switch pipeline mode |
| `retry` | `retry TICKET-1`, `resume TICKET-1`, `retry TICKET-1 from dev` | Re-run a BLOCKED, FAILED, or DEFERRED ticket from `previous_state` (or the named stage) |
| `provide_input` | Free text replying to a BLOCKED escalation, e.g. "the bug is X", "yes both screens" | Stored on `state.human_input_reply`, fed to the agent on resume |
| `reviewed` | `reviewed`, `review done`, `review complete` | Mark a PR_REVIEW workspace as reviewed and advance to DONE |
| `unanswered` | `/unanswered`, `/repeat`, `what's pending`, `which comments are open` | List PR review comments still waiting on your decision |

Unrecognized text gets a fallback reply listing what the bot can do. If intent classification itself errors out (network/timeout), the bot says so and asks you to retry.

---

## Inline buttons

For messages that expect discrete choices, Cleave attaches inline keyboard buttons. Pressing a button is equivalent to typing the corresponding free-text — the bot echoes the recorded decision back as a reply to the original message.

| Button label | Where it appears | Callback action |
|---|---|---|
| **Approve** / **Reject** | AWAITING_APPROVAL gates (manual mode) | `approve:<ticket>` / `reject:<ticket>` |
| **Review Complete** | PR_REVIEW notifications (you've finished reviewing the PR) | `reviewed:<ticket>` |
| **Fix** / **Won't Fix** | PR review comment escalations (per comment) | `pr_fix:<key>` / `pr_wontfix:<key>` |
| **Retry Now** / **Retry** | DEFERRED notifications (quota / transient error) and FAILED notifications | `retry:<ticket>` |
| **🧹 Clear cache & retry** | FAILED notifications whose error matches the AAPT2 corruption signature | `clear_gradle:<ticket>` — wipes `<gradle_home>/caches/*/transforms` then retries |
| **Show N unanswered** | PR_REVIEW summary when comments are pending your decision | `unanswered:<ticket>` |

Escalation messages that need a free-text answer (BLOCKED on requirements, ambiguity, etc.) deliberately don't carry buttons — the answer text is what's needed, not a yes/no.

---

## Replies and unblock flow

When Cleave escalates a BLOCKED workspace, the message you receive in Telegram is a *thread anchor*: replying to it (Telegram's "Reply to" feature, not a new message) feeds your reply back to the agent.

- The reply text is stored on `state.human_input_reply` and made available as additional context when the agent resumes.
- The workspace transitions out of BLOCKED back to its `previous_state` and the orchestrator wakes immediately.
- If you reply with `retry`, the workspace re-enters its `previous_state` without adding context — useful when the agent just needs another shot.
- If you reply with `skip`, the workspace is advanced to the next stage (e.g. ANALYSIS → DEV) without re-running the blocked stage — useful when you want to move past a stuck agent rather than give it another shot.

The PR_REVIEW notification message (the one with the Review Complete button) is also a reply anchor. Replying to it with any text signals that you have finished reviewing and the pipeline should fetch PR comments. This is equivalent to tapping the Review Complete button.

For PR review escalations specifically, replies are classified into three buckets by `_classify_reply` in `integrations/telegram/command_handler.py`:

- **Fix** (exact match required, no trailing reason) → recorded as FIX, dev-agent re-engages.
  Tokens: `fix it`, `fix`, `yes`, plus typo-tolerant variants `fxi`, `fifx`, `fixx`, `fx`, `fi`.
- **Won't-fix** (token at start, optional `:` or whitespace + reason) → recorded as WON'T FIX, posted as a GitHub reply.
  Tokens: `won't fix`, `wont fix`, `do not fix`, `don't fix`, `dont fix`, `not fix`, `no fix`.
  Examples: `won't fix`, `won't fix: out of scope for this ticket`, `dont fix already handled upstream`.
- **Anything else** → routed to a re-investigation flow (treated as a hint, not a decision).

The exact-match rule for fix tokens is deliberate — a bare `yes` is FIX, but `yes please fix the import` is re-investigation, since the operator is providing context rather than a clean decision.

Both decision paths echo the recognized decision back, including the matched token, e.g. `✓ Recognized as FIX (matched: 'yes'). Dev-agent will re-engage on Frag.kt:96.`

---

## Per-message project tagging

Every outgoing message is prefixed with `[PROJECT/REPO]` so you can tell which project a notification is for when multiple projects share a chat. Per-project chat routing is supported — set `telegram.default_chat_id` on `project.yaml` to send that project's notifications to a different chat than the global one.

---

## Notification types

Cleave sends Telegram notifications in these situations:

- **Daemon started / stopped** (informational)
- **Heartbeat** (daily, configurable via `global.yaml` → `heartbeat`)
- **Workspace created** (manual-mode `analyze` confirmation)
- **AWAITING_APPROVAL gate** (with Approve/Reject buttons)
- **BLOCKED — agent question** (free-text reply expected)
- **BLOCKED — verification failure** (e.g. dev-agent ran but never committed)
- **DEFERRED** — three flavors: real quota exhausted, agent hit max-turns, transient agent failure (with the actual error head)
- **PR opened** (informational, with PR link)
- **PR_REVIEW pending** (with Review Complete + Show N unanswered buttons)
- **PR review comments** (per-comment Fix / Won't Fix buttons)
- **FAILED** (with Retry button, plus 🧹 Clear cache & retry on AAPT2 errors)
- **Workspace done**

A typing indicator is shown while the intent parser thinks, so you can tell the bot is listening.

### Reviewer lines

For each repo with reviews enabled (see the [Reviews view](dashboard.md#reviews-view)), the reviewer sends one-line messages to `reviewer.telegram_chat_id` in `global.yaml`, or, when that is empty, to the operator chat (`telegram.default_chat_id` in `global.yaml`, else the first project's). They are sent while reviews are paused too.

| Line | When |
|---|---|
| `[acme/app] #1720: your turn — merge · https://github.com/acme-org/acme-app/pull/1720` | A new turn of yours that is not a review or a re-check (those queue jobs instead) |
| `[acme/app] #1718: replies in 3 thread(s) — handle them locally · <PR url>` | New replies to you in review threads |
| `[acme/app] #1730 is large (57 files) — no automatic review · <PR url>` | Once per head, after the quiet window, for a PR to review with at least `reviewer.max_files_auto` changed files. **Review now** on the dashboard still queues a review |
| `[acme/app] readiness command failed: <reason>. Keeping the last board; next try in 10 min.` | The first failed poll of a streak, when the repo's readiness command is what failed. The later failures of the streak send nothing, whatever fails, until a poll succeeds |
| `[acme/app] poll failed: <reason>. Keeping the last board; next try in 10 min.` | The first failed poll of a streak, when the review clone, a GitHub read or anything else is what failed. The later failures of the streak send nothing, a readiness failure included |
| `[acme/app] reviews are off: docs/review/code-review-process.md is not on develop.` | Once, when the base branch has no process document (again if it goes missing after coming back) |
| `[acme/app] #1712: review failed — <reason>` (or `recheck failed`) | A review or re-check job failed: Prepare could not get the PR ready (the process document or its `reviewer.process.prepare_section` missing, the PR's base is not `reviewer.base_branch`, a prepare step, git, a GitHub read or the readiness command failed), the session failed or ran past `reviewer.jobs.<kind>.timeout_seconds`, it wrote no report (no replies plan for a re-check) or one Cleave cannot read, or it changed the review clone's git config (`the session changed the review clone's git config`: Cleave writes the saved config back). A failed job is queued again on the next poll; after two failures at the same head it is not (**Review now** still queues a review) |
| `[acme/app] #1712: review deferred — usage limit reached; next try at 14:00 UTC` | The session hit the Claude usage limit. The job runs again at the limit's reset time (an hour later when the CLI gives none), and no other job starts before then. Sent once per streak: again only after a session has ended some other way. A job's third deferral (usage limit or login) fails it instead: `review failed — deferred 3 times (usage limit or login)` |
| `[reviewer] claude is not logged in on the host. Log in from a GUI Terminal (run claude, then /login); reviews wait until then.` | A session failed because the `claude` CLI says it is not logged in (its error starts with `Not logged in` or `Failed to authenticate`). The job is deferred 15 minutes and credential health re-checks the claude login (see the [credential strip](dashboard.md#credential-strip)); no job starts while that check fails, nor within 15 minutes of the last login failure. Sent once per streak: again only after a session has ended some other way |
| `[acme/app] The "Prepare" section of docs/review/code-review-process.md changed on the base branch: check that Cleave's Prepare still matches it.` | A job found the process document's preparation section (`reviewer.process.prepare_section`, default `Prepare`) changed since the repo's previous job. Cleave does that section's work itself, so check that it still matches. Sent whatever the job's outcome; the repo's first job records the section without a line |
| `[acme/app] #1712: worktree <path> was not removed — <reason>` | After a job, git refused to remove the PR's worktree (`reviewer.process.worktree_dir`): it has changes or untracked files. Cleave never forces it; remove it by hand, the PR's next job fails until then. The reason is also in the job's notes on the dashboard |

A job whose PR got a new head or was closed while it was being prepared ends `superseded` or `cancelled` without a line; with the forge token failing, it goes back to the queue. With GitHub unavailable (a 5xx, a timeout or no connection) it is queued again after 1, 5, 15 and then 60 minutes, other jobs running meanwhile; the sixth time in a row fails it (`review failed — GitHub unavailable (6 attempts)`). A re-check waits while a review of the same PR is queued, deferred or running, and until a poll has recorded your login (`waiting for the first poll`). A job interrupted by a daemon restart is marked failed (`interrupted (daemon restarted)`) without a line.

The first poll of a repo records the turns that already exist without sending a line for them. Once credential health has flagged the forge token as failing, the reviewer sends no line for it: credential health sends its own (see the [credential strip](dashboard.md#credential-strip)). Until then (say, at daemon start with a rejected token), the first poll can fail and send one `poll failed: git clone failed …` line (`git fetch failed …` once the review clone exists). A line that could not be sent is not sent again, except the large-PR and missing-document lines, which are retried on the next poll (with no Telegram bot or chat configured, on the first start that has both).

### Review and re-check messages

Reviews and re-checks run unattended, one job at a time: Cleave prepares the PR in its [review clone](dashboard.md#review-clone), runs the repo's review process in an isolated Claude Code session on the host's claude.ai login, and sends the result to the same chat as the [reviewer lines](#reviewer-lines). Nothing is posted to GitHub.

A review sends the review message:

```
[acme/app] Review of PR #1712 "ACME-1234 Fix cart sync" · ACME-1234
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
https://github.com/acme-org/acme-app/pull/1712
```

- **Header** — the repo, the PR, its title and, when the title holds one, the ticket key
- **Counts** — the head reviewed (its first 9 characters), the PR's changed files, then the report's counts: candidates (the number the report's `**Candidates:**` line starts with, else findings plus dropped), confirmed findings, questions, pre-existing issues and dropped candidates
- **Suggested** — the report's suggested event (`COMMENT`, `APPROVE` or `REQUEST_CHANGES`) and why
- **One line per finding**, numbered as in the report: 🔴 🟠 🟡 a confirmed finding (by the report's severity section), ❓ a question, 🟣 a pre-existing issue; then the file name and line, and the session's one-line summary of it (the finding's title when it wrote none)
- **Dropped** — the candidates the review dropped, each with why; `No findings.` when the report has neither findings nor dropped candidates
- **Reply hint** and the PR's URL

A re-check sends the re-check message: the header, the head and the number of drafted replies, then one line per drafted reply, with its number and the session's verdict line (fixed, not fixed, convinced or not convinced · path:line · gist), else the reply's first line; `No replies drafted.` when there is none.

```
[acme/app] Re-check of PR #1712 "ACME-1234 Fix cart sync"
head 26c4929c5 · 2 drafted replies

1. fixed · CartRepository.kt:88 · null check added
3. Agreed, renamed.

Reply to this message: "post replies 1 3"
https://github.com/acme-org/acme-app/pull/1712
```

Replies to these messages are not acted on yet: Cleave answers `Replies to review messages are handled from the next version on; nothing was done.` and does nothing else (such a reply never reaches the ticket pipeline's intent parser or its approvals); a later version reads them (post, text, edit, review again, post replies). Each text taken from the report, the replies plan or the PR's title is cut at 200 characters; the full report is on the dashboard's [PR detail view](dashboard.md#pr-detail-view).

The message is sent once the job is done: the session wrote a report (a replies plan for a re-check) that Cleave could read, and the PR still has that head (Cleave asks GitHub once, then checks the board) and your turn — a **Review now** review needs no review turn. Otherwise the job ends `superseded` (a new head, or a newer job), `stale` (the PR is closed or gone, or the turn is gone) or `skipped` (you skipped that head while the review ran) and nothing is sent.

A long message comes in parts: it is split at line ends into messages of at most 3,900 characters (an emoji such as 🔴 counts as two), sent in order, at most 10 of them: a longer message is cut, its last part ending with `… <k> more lines — the full report is on the dashboard`. A message that could not be delivered is tried again from its first part not delivered yet (a part already out is never sent again), after waits of 1, 5, 15 and then 60 minutes between attempts; a retry that is due goes out before the next job, and Cleave looks for one every 30 seconds while no job runs. Each retry first checks the job against the board, so a message that is no longer current is not sent. The fifth failed attempt is the last: the job's notes on the dashboard say the message was not delivered. With no Telegram bot or chat configured, it waits for the first start that has both.

---

## Manual vs auto mode

Mode is global (set via `global.yaml` → `pipeline.mode` or toggled live with `/auto` / `/manual`).

- **auto** — workspace flows through every stage without gates.
- **manual** — Cleave inserts AWAITING_APPROVAL gates at major transitions (after analysis, after QA, before pushing). Each gate sends a Telegram message with Approve/Reject buttons. Switching to `auto` mid-flight auto-resumes any AWAITING_APPROVAL workspaces to their happy-path next state.

The `/status` reply always shows the current mode in its header.

---

## See also

- [docs/features/telegram-notifications.md](features/telegram-notifications.md) — implementation spec, change log, FRs
- [docs/labels.md](labels.md) — Jira labels (trigger, ignore, model, repo-routing)
- [docs/troubleshooting.md](troubleshooting.md) — what to do when notifications don't arrive

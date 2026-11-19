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

Replies to [reviewer messages](#replying-to-reviewer-messages) and taps on its **Yes** / **No** buttons are read only in these chats too, and never in any chat when none is configured. `reviewer.telegram_chat_id` in `global.yaml` is not added to the allowlist: set it to one of the chats above. When it is none of them (or none is configured), replies to reviewer messages there are ignored: they reach neither the reviewer nor the ticket pipeline, and get no answer. The daemon says so at startup (and logs a warning), and leaves `, posting from Telegram` off its `Reviewer:` line:

```
  Reviewer: replies are ignored — reviewer.telegram_chat_id is not an allowed chat
```

## Language

The bot writes in English (the default) or Ukrainian, per project:

```yaml
# config-live/projects/<id>/project.yaml
telegram:
  language: uk        # en (default) or uk
```

A project without `telegram.language` follows `telegram.language` in `global.yaml`, and English when that has none too. Any other value stops the config from loading (`'telegram.language' must be one of ('en', 'uk')`). Set it in the dashboard (**Settings → Telegram language**, or the **+ New Project** wizard's Notifications step), or in the YAML. A change made in Settings applies to the next message; one made in the YAML by hand, after a restart.

**Which language a message takes:**

| Message | Language |
|---|---|
| A ticket's messages, buttons, and the answers to replies and taps on them | the ticket's project's |
| Reviewer messages and lines (the reviewer chat is shared by every repo) | the project of the PR's repo |
| Answers to replies on reviewer messages, and to taps on their **Yes** / **No** | the anchored job's project's (`global.yaml`'s when none is known, e.g. reviewer mode off) |
| Free-text and switch commands (`status`, `tickets start`, …) | the language of the projects whose `default_chat_id` is that chat when they agree, else `global.yaml`'s |
| Credential lines (no project) | `global.yaml`'s |

**What is translated:** everything the bot sends, including what models wrote. Cleave's own texts (notifications, hints, errors, button labels) come from a hand-written Ukrainian catalogue (`orchestrator/tg_uk/`). Text a model wrote — finding titles and Telegram lines, the suggested event's reason, why a candidate was dropped, drafted re-check replies, the `text N` text, edit and draft results, escalation reasons and agent output, the comment classifier's assessment, an error's detail — is translated when the message is sent: one `claude -p` turn on `haiku` per message (no tools, the reviewer sessions' scrubbed environment, on the claude.ai login), 60 s at most. Code spans and blocks, links, paths and `file:line` anchors, `#N`, ticket keys, SHAs and @-mentions are masked before the model sees the text and put back after; a text whose masks or numbers do not come back intact is shown as written, and so is everything in the message when the turn fails or times out: a translation never holds back or loses a message. An English project never makes the call. A review or re-check message is translated once, before its first part goes out, and kept with the job's run: a resend sends exactly the same parts, even after the project's language changed.

**What stays as it is:**

- **Commands.** Hints quote them in the grammar's words (`"post 1 2 4 approve"`, `"edit 2: …"`, `"review again"`, `"skip"`, `"retry ACME-12 from dev"`, `fix`, `won't fix: <reason>`), and every reply works as before, in English or with the Russian/Ukrainian synonyms.
- **GitHub.** Reviews and replies are posted in English, from the job copy's report and replies plan, which never hold a translation; edit and draft sessions still write English whatever language your instruction is in. In Ukrainian, `text N` and edit/draft results end with `(На GitHub піде англійський текст.)`.
- **What people wrote:** PR and ticket titles, quoted review comments, branch names, your own words (an edit instruction, a won't-fix reason).
- **Names Cleave and GitHub use:** stage and state ids (`DEV`, `PR_REVIEW`, `superseded`), events (`REQUEST_CHANGES`), `Summary`, repo and project ids.
- **Everything outside Telegram:** the dashboard, stored job reasons and workspace errors, events and logs.

The cost: haiku turns of a few hundred to a few thousand tokens, only for messages that hold model text (a review message, an escalation, a `text N`), on the claude.ai subscription's usage like the reviewer's sessions.

---

## Switch commands

Fixed commands pause and resume the ticket pipeline and reviewer mode. They are not replies, and they run before the LLM intent parser (allowlisted chats only):

| Command | Effect |
|---|---|
| `tickets pause` / `tickets start` | Pause / resume the ticket pipeline globally |
| `reviews pause` / `reviews start` | Pause / resume reviews globally |
| any of the above + a repo id, e.g. `tickets pause app` or `reviews start acme/app` | Same, for one repo (`repo` or `project/repo`) |
| `status` | The switch block (tickets and reviews, global and per repo), then the pipeline summary |

Commands are case-insensitive. Russian/Ukrainian words are accepted as input synonyms: `тикеты` / `тікети` for `tickets`; `ревью` / `рев'ю` (ASCII or typographic apostrophe) for `reviews`; `пауза` for `pause`; `старт` for `start`; `статус` for `status`. Replies are in the chat's [language](#language) whichever spelling you use.

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
| **Review Complete** | PR_REVIEW notifications, in a repo where reviewer mode is off (you've finished reviewing the PR) | `reviewed:<ticket>` |
| **Fix** / **Won't Fix** | PR review comment escalations (per comment) | `pr_fix:<key>` / `pr_wontfix:<key>` |
| **Retry Now** / **Retry** | DEFERRED notifications (quota / transient error) and FAILED notifications | `retry:<ticket>` |
| **🧹 Clear cache & retry** | FAILED notifications whose error matches the AAPT2 corruption signature | `clear_gradle:<ticket>` — stops that gradle home's own daemons, wipes `<gradle_home>/caches/*/transforms` then retries. Needs `GRADLE_USER_HOME` set (Cleave's own, in `.env`): the shared `~/.gradle` is never cleared |
| **Show N unanswered** | PR_REVIEW summary when comments are pending your decision | `unanswered:<ticket>` |
| **Yes** / **No** | The reviewer's echo of a reply it understood through the model (see [replies the grammar does not know](#replies-the-grammar-does-not-know)) | `rv_ok:<id>` / `rv_no:<id>` |

Escalation messages that need a free-text answer (BLOCKED on requirements, ambiguity, etc.) deliberately don't carry buttons — the answer text is what's needed, not a yes/no.

---

## Replies and unblock flow

A reply to a reviewer message goes to the reviewer before any of the below (see [replying to reviewer messages](#replying-to-reviewer-messages)).

When Cleave escalates a BLOCKED workspace, the message you receive in Telegram is a *thread anchor*: replying to it (Telegram's "Reply to" feature, not a new message) feeds your reply back to the agent.

- The reply text is stored on `state.human_input_reply` and appended to `meta/human-input.md`, together with the message it answers (`In reply to:`), so a short answer such as `yes` keeps its meaning. Every agent of the ticket then gets your replies as the **Operator Replies** section of its instructions, apart from the ticket text.
- The workspace transitions out of BLOCKED back to its `previous_state` and the orchestrator wakes immediately.
- The stage it goes back to gets a fresh attempt budget: a stage that was blocked by its attempt cap (`stuck after 2/2 attempts`) runs again. The other stages keep their counts.
- If you reply with `retry`, the workspace re-enters its `previous_state` without adding context — useful when the agent just needs another shot.
- If you reply with `skip`, the workspace is advanced to the next stage (e.g. ANALYSIS → DEV) without re-running the blocked stage — useful when you want to move past a stuck agent rather than give it another shot.

**Allowing a lint or CI configuration change.** Agents never change lint or CI/CD configuration files on their own, and nothing in a ticket can allow it. When a ticket cannot be done without such a file, the BA asks you, naming the files. A reply that allows it — naming the file (`yes, you may edit .github/workflows/release.yml`), or a plain `yes` to a question that names it — opens that file for this ticket: the BA puts it in the plan marked `(operator-authorized)`, dev changes it as the plan says, and scope-guard accepts it and names it in its certificate. The permission covers the files you named and nothing else, and `arch-rules.md` stays closed. If you decline, the plan carries the edits for a human to apply.

The PR_REVIEW notification message (the one with the Review Complete button; sent only in a repo where reviewer mode is off) is also a reply anchor. Replying to it with any text signals that you have finished reviewing and the pipeline should fetch PR comments. This is equivalent to tapping the Review Complete button.

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
- **PR_REVIEW pending** (only in a repo where reviewer mode is off; with Review Complete + Show N unanswered buttons)
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
| `[acme/app] #1712: review failed — <reason>` (or `recheck failed`) | A review or re-check job failed: Prepare could not get the PR ready (the process document or its `reviewer.process.prepare_section` missing, the PR's base is not `reviewer.base_branch`, a prepare step, git, a GitHub read or the readiness command failed), the session failed or ran past `reviewer.jobs.<kind>.timeout_seconds`, it wrote no report (no replies plan for a re-check) or one Cleave cannot read, or it changed its job clone's git config (`the session changed its job clone's git config`: the job clone is removed with the job, so nothing of it is used again). A failed job is queued again on the next poll; after two failures at the same head it is not (**Review now** still queues a review) |
| `[acme/app] #1712: review deferred — usage limit reached; next try at 14:00 UTC` | The session hit the Claude usage limit. The job runs again at the limit's reset time (an hour later when the CLI gives none), and no other job starts before then. Sent once per streak: again only after a session has ended some other way. A job's third deferral (usage limit or login) fails it instead: `review failed — deferred 3 times (usage limit or login)` |
| `[reviewer] claude is not logged in on the host. Log in from a GUI Terminal (run claude, then /login); reviews wait until then.` | A session failed because the `claude` CLI says it is not logged in (its error starts with `Not logged in` or `Failed to authenticate`). The job is deferred 15 minutes and credential health re-checks the claude login (see the [credential strip](dashboard.md#credential-strip)); no job starts while that check fails, nor within 15 minutes of the last login failure. Sent once per streak: again only after a session has ended some other way |
| `[acme/app] The "Prepare" section of docs/review/code-review-process.md changed on the base branch: check that Cleave's Prepare still matches it.` | A job found the process document's preparation section (`reviewer.process.prepare_section`, default `Prepare`) changed since the repo's previous job. Cleave does that section's work itself, so check that it still matches. Sent whatever the job's outcome; the repo's first job records the section without a line |

A job whose PR got a new head or was closed while it was being prepared ends `superseded` or `cancelled` without a line; with the forge token failing, it goes back to the queue. With GitHub unavailable (a 5xx, a timeout or no connection) it is queued again after 1, 5, 15 and then 60 minutes, other jobs running meanwhile; the sixth time in a row fails it (`review failed — GitHub unavailable (6 attempts)`). A re-check waits while a review of the same PR is queued, deferred or running, and until a poll has recorded your login (`waiting for the first poll`). A job interrupted by a daemon restart is marked failed (`interrupted (daemon restarted)`) without a line.

The first poll of a repo records the turns that already exist without sending a line for them. Once credential health has flagged the forge token as failing, the reviewer sends no line for it: credential health sends its own (see the [credential strip](dashboard.md#credential-strip)). Until then (say, at daemon start with a rejected token), the first poll can fail and send one `poll failed: git clone failed …` line (`git fetch failed …` once the review clone exists). A line that could not be sent is not sent again, except the large-PR and missing-document lines, which are retried on the next poll (with no Telegram bot or chat configured, on the first start that has both).

### Review and re-check messages

Reviews and re-checks run unattended, up to `reviewer.max_parallel_jobs` (default 6) at once and one per PR: Cleave prepares each PR in a job clone of its own, made from its [review clone](dashboard.md#review-clone), runs the repo's review process in an isolated Claude Code session on the host's claude.ai login, and sends the result to the same chat as the [reviewer lines](#reviewer-lines). Nothing is posted to GitHub until you reply to the message (see [replying to reviewer messages](#replying-to-reviewer-messages)).

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

Reply to this message: "post replies 1 3" · "text 1"
https://github.com/acme-org/acme-app/pull/1712
```

Reply to the message to act on it: see [replying to reviewer messages](#replying-to-reviewer-messages). A later review of a head that already has posts says so under the Suggested line: `Already posted at this head: 3 comments and the Summary`. Each text taken from the report, the replies plan or the PR's title is cut at 200 characters; the full report is on the dashboard's [PR detail view](dashboard.md#pr-detail-view), and `text 2` sends a finding's full text.

The message is sent once the job is done: the session wrote a report (a replies plan for a re-check) that Cleave could read, and the PR still has that head (Cleave asks GitHub once, then checks the board) and your turn — a **Review now** review needs no review turn. Otherwise the job ends `superseded` (a new head, or a newer job), `stale` (the PR is closed or gone, or the turn is gone) or `skipped` (you skipped that head while the review ran) and nothing is sent.

A long message comes in parts: it is split at line ends into messages of at most 3,900 characters (an emoji such as 🔴 counts as two), sent in order, at most 10 of them: a longer message is cut, its last part ending with `… <k> more lines — the full report is on the dashboard`. A message that could not be delivered is tried again from its first part not delivered yet (a part already out is never sent again), after waits of 1, 5, 15 and then 60 minutes between attempts; a retry that is due goes out before the next job, and Cleave looks for one every 30 seconds while no job runs. Each retry first checks the job against the board, so a message that is no longer current is not sent. The fifth failed attempt is the last: the job's notes on the dashboard say the message was not delivered. With no Telegram bot or chat configured, it waits for the first start that has both.

### Replying to reviewer messages

Every message the reviewer sends — its lines, the review and re-check messages, and its answers — is a reply anchor. Reply to one (Telegram's **Reply**, not a new message) and Cleave acts on the reply, or answers why it does nothing. Each answer is a reply to the message you replied to and is anchored like it, so you can reply to an answer too; a long one (a posting result with many replies, say) comes in up to 3 parts, each anchored the same way. A reply to a reviewer message is handled first: it never reaches the ticket pipeline's intent parser, its approvals or the escalation replies (there, the review message's own hint `post 1 2 4 approve` would read as an approval). Replies are read in [allowlisted](#allowlist) chats only. When `reviewer.db` cannot be read, a reply that matches no ticket escalation is answered `Reply not handled: the reviewer's store could not be read; nothing was done.` Cleave records each message as an anchor right after it is sent, trying 3 times 0.2 seconds apart. When that still fails, the message is out but a reply to it is not recognised: the daemon log has an error, and the job's notes on the dashboard say `message <id> is not registered; replies to it are not recognised`.

While a repo is in reviewer mode, a reviewer command sent as a new message rather than a reply (or as a reply to a message that is not the reviewer's), in any chat the ticket pipeline reads, runs nothing and never reaches the ticket pipeline either: one that names numbers (`post 1 2 4 approve`, `text 2`, `edit 2: …`), says `all confirmed`, or is any other command of the table below (`post replies 1 3`, `draft 7`, `review again`, `skip`) is answered `Reply to the reviewer's message to act on it; nothing was done.` A bare event without numbers (`approve`, `апрув`, `request changes`) and every ticket command stay the ticket pipeline's, as before. With no repo in reviewer mode, every message goes to the ticket pipeline as before.

A reply runs at once only when every word in it is in the table below. Case does not matter, commas separate words, `#2` is `2`, and one final `.` or `!` is dropped. Between the words of a post you may write `comments`, `comment`, `questions`, `question` (accepted synonyms: `коментарии`, `комментарии`, `коменты`, `комменты`, `коментарі`, `коменти`, `вопросы`, `вопрос`, `питання`) and `and`, `also`, `+`, `&` (`и`, `і`, `й`, `та`). Any other reply goes to the [model fallback](#replies-the-grammar-does-not-know), which never runs anything without your **Yes**.

| Reply | Accepted synonyms | Does |
|---|---|---|
| `post 1 2 4` | `пости`, `постни`, `запости`, `публикуй`, `опубликуй`, `публікуй`, `опублікуй` | Posts exactly those numbers in a `COMMENT` review, the report's Summary as its body |
| `post 1 2 4 approve` | `апрув`, `аппрув`, `ставь апрув`, `поставь апрув`, `став апрув`, `постав апрув`, `ставь approve` | The same with the `APPROVE` event |
| `post 1 2 4 request changes` | `request-changes`, `реквест ченджес`, `реквест ченджиз` | The same with the `REQUEST_CHANGES` event |
| `post all confirmed` | `все подтверждённые`, `всі підтверджені`, `усі підтверджені` | Adds every confirmed finding (🔴 🟠 🟡) not posted at this head yet to the numbers |
| `post … no summary` | `without summary`, `без summary`, `без саммари`, `без самари` | Leaves the Summary out of the review body |
| `post … anyway` | `всё равно`, `все одно`, `однаково` | Posts although you submitted a review yourself since the review started, or after an approve of Cleave's that may have landed (see [posting a review](#posting-a-review)) |
| `approve`, `request changes` | as above | That event with the Summary as the body and no inline comment. `approve no summary` approves without a body; `request changes no summary` is refused |
| `text 2` (`text 2 3`) | `текст` | Sends each finding's full text: its place, title and body. On a re-check message, each drafted reply in full: where it goes (`#1 · reply in thread <id>, then resolve it`, `left unresolved`, or `#3 · answer to review <id>`), then its text, so you see what you post rather than the session's one-line gist |
| `edit 2: <what to change>` | `измени`, `исправь`, `поменяй`, `зміни`, `виправ` | An [edit job](#edit-and-draft-jobs): rewrites finding 2's text as you ask. Nothing is posted |
| `draft 7` | `черновик`, `чернетка` | A [draft job](#edit-and-draft-jobs): writes dropped candidate 7 as a question you can post. Nothing is posted |
| `post replies 1 3` (re-check message) | `reply`, `answers`, `ответы`, `ответ`, `відповіді`, `відповідь` for `replies` | Posts those drafted replies (see [posting re-check replies](#posting-re-check-replies)) |
| `review again` | `re-review`, `rereview`, `ревью заново`, `заново ревью`, `ревью ще раз`, `рев'ю заново` | Queues a review of the PR's current head, as **Review now** does: `Queued a review of #1712 at 26c4929c5.` |
| `skip` | `пропусти`, `пропусть` | Skips the PR's current head, as **Skip** does: `Skipped #1712 at 26c4929c5: no automatic review of this head.` |
| `push` (threads or findings message of your own PR) | `commit and push`, `пуш`, `пушь`, `запушь`, `запуш`, `закоммить и запушь` | Commits the job's fix on the PR head the job ran on and pushes it to the PR branch, never forced and only while the branch is still at that head: `Pushed 9c2e41d07 to #1712: <commit url>`. Refused when the head moved, the branch is in a fork or is the base branch, the fix touches `reviewer.process.address_protected` files, it was pushed already, or a ticket of the pipeline still holds the PR |
| `diff` (threads or findings message) | `patch`, `дифф`, `диф`, `патч` | Sends the fix's patch, in up to 3 parts (cut with a note; the dashboard has all of it) |
| `again` / `again: <instruction>` (threads or findings message) | `redo`, `заново`, `переделай`, `наново` | A new run on the PR's current head, your instruction passed to the session as your own words; the old job is superseded. Works on a failed run's line too. On a findings message it fixes the findings of the PR's self-review at its current head |
| `fix` (review message of a self-review of your own PR) | `fix findings`, `пофикси`, `почини`, `фикс`, `пофікси`, `полагодь` | Claude fixes the self-review's confirmed findings (see [fixing the findings of a self-review](#fixing-the-findings-of-a-self-review)): `Fixing the findings of #1712 at 8f01a2b3c: the result comes here; nothing is pushed until you reply "push".` Only the whole reply: `fix 2` goes to the model fallback, which never runs it |

Numbers need the post word: `approve 1 2`, or `post` with no number and no event, goes to the model fallback. An event alone takes only the joining words: `approve comments` goes to the fallback too, because a bare approve is never guessed. All of this works while reviews are paused; a review queued by `review again` starts once reviews run.

A reply that cannot run is answered, and nothing from it runs:

| Answer | When |
|---|---|
| `Unknown number: 12. Nothing was done.` | A number that is not in the report (nor among the drafted replies of a re-check) |
| `#7 was dropped: reply "draft 7" first. Nothing was done.` | `post`, `text` or `edit` of a dropped candidate that is not drafted |
| `#1 is already posted at this head. Nothing was done.` | A number that landed at this head (see [never twice](#never-twice)) |
| `Nothing to post: name numbers or keep the Summary. Nothing was done.` | `request changes no summary`, or `post all confirmed` with no confirmed finding |
| `Nothing left to post: every confirmed finding is already posted at this head. Nothing was done.` | `post all confirmed` (with no other number) once every confirmed finding landed at this head |
| `Outdated: there is a review of a newer head. Nothing was done.` (or `there is a newer review`, or `this report no longer matches the PR (<reason>)`) | The job was superseded by a newer review or is stale (`PR gone`: it left the board, merged or closed, or without `--all` no longer yours; `turn gone`: your turn left a PR still on it): nothing runs from its messages, `review again` and `skip` included. Use the newer review's message, or **Review now** |
| `This job ended failed: only "review again" and "skip" work here. Nothing was done.` | A command about the report on a job that is not done or posted (failed, deferred, …) |
| `#7 is not a dropped number. Nothing was done.` | `draft` of a number that is not a dropped candidate |
| `#7 is drafted already: reply "text 7". Nothing was done.` | `draft` of a candidate drafted before |
| `A review of #1712 at this head is already queued.` | `review again` while a review of the current head is queued, running or deferred |
| `#1712 is not on the board any more; nothing was done.` | `review again` or `skip` after the PR left the board |
| `This is a re-check message: reply "post replies 1 3". Nothing was done.` | `post`, `edit` or `draft` on a re-check message (and the reverse for `post replies` on a review message) |
| `"push" works on a fix message of your own PR (threads or self-review findings). Nothing was done.` | `push`, `diff` or `again` on a review or re-check message |
| `"fix" works on the message of a self-review of your own PR. Nothing was done.` | `fix` on any message but a review's |
| `The forge token is failing (see the dashboard): nothing posted.` | A post while credential health flags the repo's forge token |
| `This reply is from 14:05 UTC; send it again to act on it. Nothing was done.` | The reply is older than 15 minutes (`20 Nov 14:05 UTC` past a day): say, sent while the daemon was down. Telegram delivers it at the next start, where it runs nothing |
| `Reviewer mode is off on this daemon: nothing was done.` | No repo is reviewed by this daemon (no repo with `reviewer.enabled` and a forge token, or a dry run) |
| `This repo is no longer reviewed; nothing was done.` | The message's repo has reviews turned off now |
| `Not understood; nothing was done. Reply like "post 1 2 4 approve", "text 2", "edit 2: …", "draft 7" or "review again".` | Neither the grammar nor the model fallback understood it (`"post replies 1 3", "text 1" or "review again"` on a re-check) |
| `Internal error (<class>); nothing was done.` | Something failed in Cleave outside a post; details are in the daemon log (a post that stops on an error answers `Posting stopped on an internal error (<class>); …` before its confirm run, or `Posting on #1712 ran, but stopped on an internal error (<class>): …` after it, see [posting a review](#posting-a-review)) |

#### Replies the grammar does not know

A reply to a review or re-check message whose job is done or posted, made of other words (`post everything but 3`, a typo, a question), goes to a one-turn `claude -p` on the host's claude.ai login with no tools and no MCP server (`reviewer.jobs.command` in `global.yaml`, default `sonnet`, 60 seconds). It sees only your reply and the message's numbers with their kinds: never a title, a finding's text or the PR's. Its command is checked like a grammar reply, then echoed with two buttons:

```
Understood: post 1 2 4 + APPROVE at 26c4929c5. Correct?      [Yes] [No]
```

Nothing runs until you tap **Yes**, which runs the echoed command against the job as it is then, checked again. **No** answers `Cancelled: nothing was done.` A tap counts once (`Already handled.` after that), only in the chat of the echo, and only within 15 minutes of it (`Expired: reply again; nothing was done.`). With the claude login failing, the fallback does not run: `claude is not logged in on the host: only exact replies work now; nothing was done.`

#### Posting a review

`post …` answers `Checking before posting: post 1 2 4 + APPROVE at 26c4929c5.`, then posts through the repo's own poster tool (`reviewer.poster.review_command`, run from the [review clone](dashboard.md#review-clone) with the forge token), never through the model. Posts run one at a time, across all repos. Cleave checks, in this order, and the first check that fails stops the post before anything is posted:

1. **The job as it is now** — still done or posted; your GitHub login known from a poll (`Waiting for the first poll: nothing posted.`); the numbers checked again, since an identical post may have landed after your reply was checked.
2. **GitHub's head** — the PR is open (`PR #1712 is closed: nothing posted.`) and still at the reviewed head (`Head moved (26c4929c5 → 8f01a2b3c): nothing posted. Reply "review again" for a review of the new head.`). Every GitHub read before the confirm run (or the approve) that fails stops the post: `GitHub could not be read for #1712 (<error>): nothing posted.`
3. **Your reviews on GitHub** — a second approve at the same head is refused, with `anyway` too: `Already approved #1712 at 26c4929c5. Nothing was done.` (a review of yours that approved that head, Cleave's own included, or an approve Cleave recorded there). An approve where an approving post of Cleave's ended unknown at this head (unverified, or interrupted by a restart) needs `anyway`: `An approve of #1712 at 26c4929c5 may have landed: check the PR, then add "anyway" to your reply to approve again. Nothing was done.` A review you submitted yourself after the review job started needs `anyway` too: `You already submitted a review on this PR after the report (<time>): nothing posted. Add "anyway" to your reply to post regardless.`
4. **The named findings' text** — the [body check](#the-body-check).
5. **The ticked copy** — `approve no summary` uses no tool: right before the approve, GitHub's head is checked again, your reviews are read again for step 3's approve rule (not for a review of your own since the job started, already checked), and the job must still be done or posted; then one approve call goes to GitHub, never retried, then step 9. Otherwise the job's copy of the report is ticked on exactly the numbers you named (with `no summary`, without its Summary). Cleave keeps its own copy of the report in the job's directory; the review session cannot change it.
6. **The dry run** — the tool with `reviewer.poster.dry_run_flag` (default `--dry-run`), from the review clone (while a job's Prepare or another post holds the clone, Cleave waits for it up to 10 minutes, then refuses: `the review clone is in use; try again in a minute`). It must exit 0, print nothing Cleave does not understand, and plan exactly what you named: it lists your numbers, at the job's head, with your event; each inline comment is one of your numbers; each of your numbers is inline, in the review body, or skipped by the tool's ledger as already posted; no number you did not name is in a comment or the body; its `Summary:` line agrees with the payloads (the report's Summary is in a review body exactly when it says `yes`); and each comment and the review body (the Summary too) pass the body check. Else: `Not posted on #1712: <why>.` (a dry run that runs out of time: `the dry run failed: timed out after <n> s`). A dry run that plans nothing answers `Nothing left to post on #1712 at 26c4929c5: what you named is already posted.` (after a bare `approve`, it adds `Reply "approve no summary" to approve without a body.`).
7. **Right before the confirm run** — GitHub's head again, your reviews again (the approve rule of step 3), the job still done or posted, and the named findings unchanged: an edit that landed during the dry run answers `The report changed while posting (an edit landed): look at it again and reply. Nothing was done.`
8. **The confirm run** — the dry run's command with `reviewer.poster.confirm_flag` (default `--yes`) in place of the dry-run flag, nothing else changed.
9. **The post-condition, on GitHub only** — read up to 3 times, 2 seconds apart: exactly the planned number of new reviews by you since right before the confirm run, each with your event at the head, the planned number of comments, and the planned body in the first. GitHub is the truth whatever the tool's exit or output says: a confirm run that failed, timed out or printed something Cleave does not understand is checked the same way, and only what GitHub shows is recorded as landed.

The result:

| Answer | Means |
|---|---|
| `Posted on #1712 at 26c4929c5 — APPROVE, 1 review: <url>`, then `inline: 1 2 · moved anchor: 2 · in the review body: 5` (`already posted, skipped: …` for numbers the tool's ledger skipped; `Summary only` with no number) | GitHub shows all of it as planned. A tool that exited non-zero adds `The tool exited 1, but GitHub shows everything posted.`, one whose output Cleave did not understand `The tool's output was not understood (<error>), but GitHub shows everything posted.` |
| `Approved #1712 at 26c4929c5 without a body: <url>` | `approve no summary` landed |
| `Partly posted on #1712 at 26c4929c5: batch 1 of 2 landed (<url>); landed 1 2; not landed 4 — <why>.`, then `Reply "post 4" to post the rest.` | The tool posts in batches and only some are on GitHub. The last line is the exact command for the rest, with your flags, but without `approve` when batch 1 approved already: the rest goes as a `COMMENT` review, since a second approve is refused; and with `no summary` when batch 1 carried the Summary, which landed with it. What landed is refused as already posted. When the tool reports a batch that GitHub does not show, the line says so: check the PR on GitHub |
| `Not posted on #1712: <why>.` | GitHub shows nothing new, and the tool failed without reporting a batch posted, with output Cleave understood (or GitHub refused the approve) |
| `Posting on #1712 ran, but GitHub does not show it as planned (<why>). Check the PR on GitHub before posting again: <url>` | Unverified: the tool reports a post, says something Cleave does not understand (`the tool's output was not understood (<error>)` in `<why>`), or GitHub shows something else new, and GitHub does not show it as planned. It may have landed, so it is never called "Not posted". If it landed, its review is not known as Cleave's, so the next post at that head needs `anyway` |
| `Posting on #1712 ran, but GitHub could not be read to check it (<error>). Check the PR before posting again: <url>` | Unverified: GitHub could not be read after the confirm run |
| `Posting on #1712 ran, but stopped on an internal error (<class>): it may have landed. Check the PR before posting again.` | Unverified: Cleave itself failed once the confirm run (or the approve) had run (details in the daemon log). An approve at that head then needs `anyway` |
| `Posting stopped on an internal error (<class>); check the PR before posting again.` | Cleave itself failed before the confirm run (details in the daemon log): nothing was posted |

A post that landed, in full or in part, sets the job `posted`; you can still post the numbers that did not land from the same message. A job superseded since the post started keeps that state, and so does one stale for another reason than its turn going. A poll that sees your turn gone while a post runs (the post itself takes it away) leaves the job to the post.

#### Never twice

Cleave records in `reviewer.db` what landed at each head (each finding and whether the Summary went with it) and refuses those numbers, once when your reply is checked and again under the posting lock: the same reply delivered twice, or sent again, runs the tool once and then answers `#1 is already posted at this head. Nothing was done.` The tool's own ledger (`{ledger}`, `<reviewer data dir>/ledgers/<project>/<repo>/PR_<n>/posted_<head9>.json`) is passed on every review post as a second guard. The Summary has its own: a dry run that carries the Summary at a head where a post of Cleave's landed it is refused before the confirm run, `The Summary already landed at #1712 at 26c4929c5: add "no summary". Nothing was done.` An event-only post (`request changes`, `approve`: no number) has nothing else to post then, so it answers `Nothing left to post on #1712 at 26c4929c5: what you named is already posted.` instead (after `approve`, with the `approve no summary` hint). A later review of the same head (**Review now** or `review again` after a post) shows `Already posted at this head: <n> comments` (`and the Summary`) under its Suggested line.

#### Posting re-check replies

`post replies 1 3` on a re-check message runs the replies tool (`reviewer.poster.replies_command`) by the same rules, one post at a time and while reviews are paused too:

1. **The job as it is now** — done or posted; your login; the numbers checked again.
2. **GitHub's head** — the PR open and at the re-check's head.
3. **The plan** — checked against Prepare's snapshot of the PR's threads, taken before the session (which cannot change it): each named reply must still answer an unresolved thread where it is your turn, or a review body not answered yet (`#3 cannot be posted: <why>. Nothing was done.`). Then each reply's text: the [body check](#the-body-check) (`#1: its text <problem>: nothing posted. A re-check reply cannot be edited here: answer that thread on GitHub yourself.`).
4. **A plan file** of exactly the named replies.
5. **The dry run** — each step must be the planned one (a reply, and a resolve when the plan resolves the thread, for a thread; a comment for a review body), with its planned text, aimed at its thread or review, passing the body check.
6. **Right before the confirm run** — GitHub's head again; the threads command's snapshot (when it fails, nothing is posted); each step still possible on GitHub. A reply the dry run leaves out counts as already posted only when GitHub shows it. When the tool plans nothing, the answer is `Nothing left to post on #1712 at 26c4929c5: what you named is already posted.`, or, when GitHub does not show it all, `Nothing posted on #1712: the replies tool has nothing to post, but GitHub does not show everything posted (<why>). Check the PR on GitHub: <url>`.
7. **The confirm run**, then **the post-condition** with the threads command, read up to 3 times: each reply new in its thread, by you, with its text; each resolved thread no longer listed as unresolved; each review-body answer answered. It runs whatever the tool's exit or output, after a timeout too, and records as landed only what the threads command shows; with the tool's report unknown, the answer is unverified and says to check the PR.

The result is `Posted replies on #1712:` with one line per reply (`1: replied <url>`, `3: answered <url>`, `2: already posted`); `Partly posted replies on #1712 — <why>.` with what landed, what only you can finish on GitHub, and `Reply "post replies 3" to post the rest.`; `Not posted on #1712: <why>.`; or one of the unverified answers above.

#### The body check

Text Cleave posts — each named finding before the dry run, then every comment and the review body (the Summary too) after it, and each re-check reply — is refused when it:

- has an image (`has an image`);
- links anywhere but the repo owner's `https://github.com/<owner>/` and the project's tracker (the Jira URL's host, or `https://trello.com/`): relative links, `#anchors`, other schemes and links that leave those prefixes with `..` included (`links to <url>`);
- holds an e-mail address, bare (`user@host.tld`) or after `mailto:` or `xmpp:`: GitHub links each (`links to <address>`);
- refers to another owner's repo (`owner/repo#12`, `owner/repo@<sha>`);
- mentions someone (`mentions @name`).

The answer is `#2: its text <problem>: nothing posted. Fix it with "edit 2: …".` Code is skipped only where GitHub surely shows it as code: a fenced block that opens at column 0 (not a math block) and inline code spans; and when the text holds a `<` that may open an HTML tag anywhere outside those blocks, no code is skipped at all. The check fails closed, so it has known false refusals: a Kotlin or Java finding that mentions generics (`List<String>`) and has an annotation (`@Composable`) in its code block, indented code with an `@`, relative links into the repo, footnotes, an SSH remote (`git@github.com:…`) outside code. Fix each with `edit N: …`, asking to drop the link, the address, the annotation or the `<…>`.

#### Edit and draft jobs

`edit 2: …` answers `Editing 2: the new text comes in a few minutes; nothing is posted.` and `draft 7` answers `Drafting 7: the comment comes in a few minutes; nothing is posted.` Each runs a small Claude Code session on the host's claude.ai login (`reviewer.jobs.edit` and `reviewer.jobs.draft`, default `sonnet`, 5 minutes) that can only read the review job's copy, with no tool but Read, Grep and Glob; Cleave, not the model, writes the answer into its copy of the report and reads the report again. They run one at a time, next to the review jobs, while reviews are paused too, and wait until the review message is delivered. The result comes as a new message that belongs to the review: a reply to it works as a reply to the review message. An edit's:

```
[acme/app] #1712: new text (nothing posted)

#2 · app/src/main/SyncWorker.kt:41 (moves to 43)
**Retry without backoff**

<the new text>

Reply "post 2" to post it, or "edit 2: …" again.
```

A draft becomes finding 7 under Questions, keeping its number (the Dropped line stays): `[acme/app] #1712: drafted as a question (nothing posted)`, its text, then `Reply "post 7" to post it.` On the dashboard, the findings and the raw report show the report as edited. A job that fails answers `[acme/app] #1712: edit of 2 failed — <reason>` (`draft of 7`), for instance `usage limit reached`, `claude login failing`, `the review is outdated`, `draft unreadable` or `interrupted (daemon restarted)`; ask again. With the claude login failing, nothing starts: `claude is not logged in on the host: nothing was started.`

#### Fixing the findings of a self-review

A review of your own PR (**Self-review** or **Review now** on it, see the [Reviews board](dashboard.md#board)) in a repo with `reviewer.process.address_doc` ends its reply hint with `· "fix"` when its report has a confirmed finding (🔴 🟠 🟡). Reply `fix` to that message, or use **Fix findings…** in the PR's panel: Claude (`reviewer.jobs.findings`, default `opus` at `xhigh`, 90 minutes) follows the address document for each confirmed finding of the self-review at the PR's head, the way it answers a reviewer's thread: it weighs the finding, fixes it in the PR's worktree when it holds and says why when it does not. ❓ questions and 🟣 pre-existing issues are not given to it. Nothing is built or tested on the host, and nothing is pushed until you reply `push`.

`fix` is refused, and nothing is queued, when:

| Answer | When |
|---|---|
| `Cannot fix the findings of #1712: <why>. Nothing was done.` | `no address_doc`, `not your PR`, `a ticket workspace holds it`, `held by a comparison`, `no finished self-review at this head` (the newest review at the PR's head is not done or posted) or `no confirmed findings` |
| `This is not the newest self-review of #1712 at its head. Nothing was done.` | A newer review of the same head exists: reply to its message |
| `A run on #1712 is already queued.` | A fix of the PR (of its threads or of its findings) is queued, running or deferred: one fix of a PR at a time. Fix now and `again` on a threads message refuse a findings fix the same way |
| `A push or a post of a fix of #1712 is under way; try again when it ends. Nothing was done.` | A `push` (or a post of replies) of a fix of the PR is under way |
| `#1712 is not on the board any more; nothing was done.` | The PR left the board |
| `claude is not logged in on the host: nothing was started.` | The claude login is failing |

The result is the findings message, numbered as in the self-review's message:

```
[acme/app] Self-review findings on your PR #108 "Fix cart sync"
head 8f01a2b3c · 4 findings · fix: 3 files, +24 −7 — not built or tested here

2. FIXED · CartRepo.kt:88 · null list from the server now handled
3. WON'T FIX · CartView.kt:40 · the reload is intended
5. FOLLOW-UP · Sync.kt:12 · retry policy belongs to ACME-120
7. FIXED · CartRepo.kt:120 · renamed for clarity

Reply to this message: "push" · "diff" · "again: …"
https://github.com/acme/app/pull/108
```

Each line is the session's verdict line, else `<verdict> · <why>`. Without a patch the head line says `no code change` and only `"again: …"` is offered; a fix that touches protected files is named and not offered for `push`. Replies: `push` commits the fix as `fix(<ticket>): self-review findings on #108` (`fix: self-review findings on #108` without a ticket key) with the same checks as a threads fix, and answers without a `post replies` hint; `diff` sends the patch; `again: <instruction>` re-runs with your words on the PR's self-review at its current head (`again: leave 3 as it is`). Any other command answers `This is a findings message: reply "push", "diff" or "again: …". Nothing was done.`; text the grammar does not know answers `Not understood; nothing was done. Reply like "push", "diff" or "again: …".` (no model fallback). The session fails the job when it writes no `verdicts.json` (`no verdicts written`), or one without exactly one verdict per finding (`report unreadable: <why>`); Prepare fails it when the self-review's report is no longer there or is of another head (`the self-review's report is gone`). After a push the PR's head moved: nothing runs by itself, and **Self-review** is offered again.

#### Replies to the other lines

- The large-PR line (`… is large (57 files) — no automatic review …`) takes `review again` and `skip`; anything else answers `Only "review again" and "skip" work on this line; nothing was done.`
- A line about a job (`review failed`, `review deferred`, an edit, draft or posting answer) is that job's: a reply to it works as a reply to the job's message. On a failed or deferred job, only `review again` and `skip` run.
- Every other line (a turn, replies in threads, a failed poll or readiness command, the missing process document, the changed Prepare section, the login line) answers `This line takes no replies; nothing was done.`

#### After a restart

A post cut short by a shutdown cannot be known to have landed or not. At the next start, before any reply is read, Cleave marks it interrupted and sends, as a reply anchor to its job:

```
[acme/app] #1712: a post was interrupted by a restart. Check the PR, then reply "post …" again with "anyway": what landed is skipped.
```

An approve in it may have landed, so a new approve at that head needs `anyway`. An interrupted post of re-check replies takes no `anyway`: name again only the replies GitHub does not show.

```
[acme/app] #1712: a post of replies 1 3 was interrupted by a restart. Check the PR, then reply "post replies …" with the ones GitHub does not show.
```

Replies sent while the daemon was down reach it at the start; one older than 15 minutes by then runs nothing (`This reply is from …`): send it again.

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

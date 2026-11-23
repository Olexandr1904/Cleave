# Control Cleave from the dashboard as well as from Telegram — design

Date: 2026-11-23. Status: draft for the operator's approval.

## Intent

The operator watches Cleave in the dashboard, but every "send" still happens in Telegram: pushing a reviewer fix,
posting replies or findings, answering an agent's question. Switching to Telegram only to type `push` or
`post replies 1 2` breaks the flow. Both channels should be equal ways to drive Cleave: anything the operator can send
from Telegram can be done from the dashboard, and the two never drift apart in what they allow or how they behave.

Evidence (the operator's Telegram traffic since full logging began, 46 messages): about half are replies to reviewer
messages (`push`, `post replies 1 2 3`, `post all`, `post 1`), then `/status`, `retry <ticket> [from <stage>]`,
Approve / Retry taps, and one free-text answer to an agent's question.

## Goals

1. From the dashboard: push a reviewer fix (also "push anyway" and "push and post"), post a review's findings (all or
   chosen ones, as a comment, Approve or Request changes), post drafted replies (all or chosen ones), and send any
   reviewer reply command as text, in the same words Telegram takes.
2. From the dashboard: answer an agent's question (a blocked ticket), retry a ticket (also from a named stage), skip
   it, and, for a ticket whose PR is in review (PR_REVIEW): decide each escalated PR comment (Fix, Won't fix with an
   optional reason, or re-investigate with a hint) and signal "reviewed". With these, every action the operator can
   send from Telegram is in the dashboard (the operator asked for all of them, 2026-11-23).
3. One implementation per action: Telegram and the dashboard call the same code, get the same checks and the same
   refusals. Where the two differ today (ticket retry, below), they converge on Telegram's behaviour.
4. Telegram stays complete: an action taken from the dashboard shows in the Telegram chat, and a reviewer message's
   buttons stop offering what can no longer run. (Ticket messages' buttons are not refreshed: a stale Approve or Retry
   tap is refused by the state checks, as today. Builder's call: their message ids are not stored today.)
5. The dashboard answers only the host itself and the operator's Tailscale network, and its write endpoints refuse
   cross-site requests.

## Non-goals

- No login, password or user accounts in the dashboard: Tailscale already says who is asking.
- Dashboard-only actions (pause, take control, archive, clean, delete, rerun, clear Gradle and retry, project
  creation, settings, switches) are not changed beyond the cross-site check.
- No new Telegram capabilities except buttons that follow state (goal 4).
- No dashboard chat view of the Telegram conversation.

## Anti-goals (failure even if it works)

- The dashboard bypasses a check Telegram makes: protected files without "push anyway", a posting lock, a credential
  that is failing, a job no longer postable, a confirmation that has expired.
- A second copy of an action's logic, in the dashboard or anywhere else. Today's dashboard ticket retry is exactly this
  and has already drifted.
- Telegram loses a capability or a reply it gives today.
- A device on the home LAN (not on Tailscale) can still drive Cleave.

## Current state

- Reviewer (`orchestrator/reviewer/`): `ReviewerReplies` (replies.py) parses a reply to an anchored message
  (`parse_reply`, grammar.py), checks it against the job (`_check` → `check_command`), runs it (`_execute`) and answers
  in Telegram through one method, `_answer`. Pushing and posting already return a result:
  `Posting.push_fix / post_review / post_replies` → `PostOutcome(state, text)`. "Review again", "skip", "fix" and
  "again" already go through `ReviewerService` (service.py), which the dashboard also calls
  (`/api/reviews/review-now`, `/skip`, `/fix-now`, `/fix-findings`). Free text the grammar does not parse goes to an
  LLM parser, and the understood command is echoed with Yes / No before it runs.
- The dashboard's PR panel (`dashboard/static/js/reviews.js`, data from `ReviewerService.detail`) already shows the
  report, the drafted replies with their per-number state, the fix, its patch and the pushed commit, and names the jobs:
  `report_job`, `replies_job`, `fix_job`. Its texts say "Not pushed: reply \"push\" in Telegram" and "Posting is done by
  replying to the review in Telegram".
- Tickets: Telegram's `CommandHandler` (integrations/telegram/command_handler.py) has approve, reject, retry (with
  `from <stage>`), skip and the free-text answer to an escalation (`_handle_provide_input`). The dashboard
  (dashboard/actions.py) has its own approve, reject and retry. Its retry differs from Telegram's: a ticket whose QA
  report exists is parked at the approval gate after QA, which no longer exists (Telegram sends it to PUSHED), it does
  not wake the loop, and it does not clear `rejected_at` when a stage is named (it cannot name one).
- Access: the dashboard listens on `0.0.0.0` (`dashboard.host`) with no authentication. Only the review endpoints check
  for cross-site requests (`_cross_site` in reviews_api.py); the ticket action endpoints do not, so a page open in the
  operator's browser could POST an approve.
- Buttons: reviewer messages carry the buttons valid when sent (`fix_buttons`, messages.py); nothing updates them
  afterwards. A tap on a stale one is refused by the state checks.

## Approaches

- **A. The dashboard sends through Telegram's entry point.** Each dashboard action becomes the same "reply to the bot's
  message" Telegram receives; the bot's answers go to Telegram and the dashboard shows them from there. Least code and
  exact parity, but the dashboard only gets Telegram-formatted text after the fact.
- **B. A shared action layer (chosen by the operator).** The actions move into functions that check and run them and
  return a structured result. Telegram formats that result as it does today; the dashboard renders it. Clean results in
  the dashboard and no dependency on Telegram being up, at the cost of reshaping replies.py and the ticket handlers.
- Dropped: embedding Telegram Web (Telegram forbids framing), sending as the operator's Telegram account (needs the
  operator's Telegram session on the host).

## Design

### 1. Access

- A middleware in `dashboard/web.py` refuses (403) any request whose peer address is outside an allow list. Default:
  `127.0.0.0/8`, `::1/128`, `100.64.0.0/10` and `fd7a:115c:a1e0::/48` (Tailscale's IPv4 and IPv6 ranges). A refusal
  is logged with the peer address. The list is `dashboard.allowed_networks` in global.yaml, so a deployment without
  Tailscale can widen it. The address and port the dashboard listens on do not change.
- A range check trusts every device on the tailnet. On the operator's tailnet every device is the operator's (checked
  2026-11-23), so that is enough. If another person's device ever joins or is shared in, this check has to become an
  identity check (`tailscale whois` of the peer), not a range check.
- Every write endpoint (POST, PUT, DELETE) goes through the cross-site check that the review endpoints use today
  (moved to a shared place). The dashboard's own JS sends JSON with the same-origin headers, so it passes.

### 2. The shared reviewer actions

- A reviewer action layer (a new module or `ReviewerService` methods; the builder decides) takes a job id and a
  `ReplyCommand` and does what `_execute` does today: check it against the job as the store has it now, then run it.
  It returns a result: an outcome state (`posted`, `partial`, `refused`, `failed`, `queued`, `nothing`, …), the text to
  show, and the job id the result belongs to. Refusals are results, not exceptions.
- Telegram's `ReviewerReplies` keeps everything Telegram-specific: anchors, reply age, the progress line ("Checking
  before pushing."), Yes / No confirmations, splitting into parts, translation, running long work in tracked tasks. It
  calls the layer and answers with the result's text. Telegram's answers do not change.
- Texts are produced in the caller's language: the project's Telegram language for Telegram, English for the dashboard
  (the dashboard UI is English). `tr()` translates when a text is built, so a dashboard call runs the layer under
  English; `PostOutcome.text` and the texts in posting.py and lines.py are not reshaped.
- "Push anyway" from the dashboard needs a confirm dialog in the browser, as the Telegram button asks Yes / No.
- Free text from the dashboard: the grammar first. Text the grammar does not parse goes to the same LLM parser, and the
  dashboard shows the understood command with Yes / No, as Telegram does, using the same confirmation store and expiry.

### 3. The shared ticket actions

- One module for approve, reject, retry, skip and answer-the-question. Telegram's `CommandHandler` and
  `dashboard/actions.py` both call it. It returns the new state or why nothing was done.
- Retry has the modes Telegram has today, and the module keeps all three:
  - **Where it stopped:** the Retry button and the "retry" reply to an escalation restart the stage the ticket
    stopped at.
  - **Furthest stage:** a typed `retry X` goes to the furthest stage its reports show (`_retry_target`).
  - **Named stage:** `retry X from <stage>`.
  Every mode moves the ticket through `_restart`: fresh attempts, the loop woken, and `rejected_at` cleared when a
  stage is named.
- The dashboard's Retry is Telegram's button, "where it stopped", plus a stage choice, as Telegram's
  Retry · From stage… (`{"from": "qa"}`, checked against Telegram's stage list). My call: the dashboard mirrors the
  buttons, not the typed command.
- Reject: Telegram has two today. The typed `reject` always sends a ticket to FAILED. The Reject button sends a
  plan-review rejection back to ANALYSIS and fails any other gate. The dashboard does what the button does at plan
  review, and sends an old ANALYSIS or QA gate back to the previous state.
  - The module implements the button's behaviour. The typed `reject` and the dashboard converge on it, so a typed
    `reject` at plan review now sends the plan back to analysis instead of failing the ticket.
  - My call: the button is the one in use, and its code explains why FAILED would be wrong there.
  - This is the one Telegram reply that changes.
- Skip gets a button in the dashboard's ticket panel, as in Telegram.
- Answering a question is `_handle_provide_input`'s core: it stores the input and resumes from the previous state.
- PR comment decisions, for a ticket in PR_REVIEW with `pending_review_comments`:
  - One function records a comment's decision, used by the Fix / Won't Fix taps, the classified reply and the
    dashboard. Today the tap and the reply each set it themselves. The decisions are:
    - `fix`;
    - `won't fix: <reason>` (the default reason "operator decision");
    - re-investigate with a hint (`_stage_reinvestigation`).
  - After recording, it does what Telegram does today: emits `pr_comment_decision_recorded` with `via` set to
    `button`, `reply` or `dashboard`, and wakes the loop when the last undecided comment is decided.
  - Telegram's confirmation texts and the "Show N unanswered" button stay in Telegram's handler.
- "Reviewed" is `_handle_reviewed`'s core: a PR_REVIEW ticket is told its review is complete, and the loop is woken
  to fetch the PR's comments now.

### 4. Dashboard API and UI

- `POST /api/reviews/action` with the PR (scope, number), the job id the panel shows, and either a structured command
  (from a button) or text (from the command field). The response is the result above; for text the LLM parser
  understood, it is the understood command and a confirmation id, and `POST /api/reviews/confirm` runs or drops it.
  The request waits for the push or post to finish (seconds to a minute); the posting lock already stops a second one.
- `POST /api/workspaces/{ticket}/answer` with the text, for a ticket blocked on a question.
- PR panel:
  - The buttons Telegram offers for the job's state: Push + reply all, Push, Push anyway (confirm), Reply all,
    Reply to checked.
  - For a review report: Post all confirmed, Post checked, each as a comment, Approve or Request changes. "Post all
    confirmed" is Telegram's `post all confirmed`: the 🔴🟠🟡 findings not posted at this head. My call: Telegram has
    no command that posts every item, and a post cannot be taken back. Questions and 🟣 items go by checkbox.
  - Checkboxes on the finding and reply rows.
  - A command field.
  - The result line under the buttons, coloured by outcome.
  - The "reply … in Telegram" texts go.
  - The patch is already shown, so no Diff button.
- Ticket panel:
  - An answer box in the "Error / Escalation" panel of a blocked ticket waiting for input.
  - Retry with a stage choice, and Skip.
  - For a PR_REVIEW ticket:
    - a "PR comments" block that lists the escalated comments: author, file:line, the suggestion, the agent's
      assessment and the decision so far;
    - per comment: Fix, Won't fix (with an optional reason) and Re-investigate (with a hint);
    - a Reviewed button.
  - Endpoints: `POST /api/workspaces/{ticket}/comments/{comment_id}/decision` and
    `POST /api/workspaces/{ticket}/reviewed`.
- While an action runs, the panel's buttons are disabled and show progress; when it ends, the panel reloads.

### 5. Telegram stays complete

- An action taken from the dashboard is announced in Telegram as a reply to the message it acted on. For a reviewer
  job, that is the job's message; for a PR comment decision, that comment's escalation message (its ids are stored on
  the comment); for any other ticket action, the escalation message if there is one, else a message with the ticket
  header.
  - The announcement is one short line in the project's Telegram language: "🖥 Dashboard: <the command, as
    `describe` words it> — <the outcome in a word>", plus the commit or review link when there is one. The full
    outcome text stays in the dashboard.
  - My call: this needs no second rendering of the outcome text.
  - A refusal (nothing was done) and a read-only command (`text`, `diff`) are not announced: nothing changed.
- Buttons follow state: after any action on a reviewer job, from either channel, the keyboard of the job's message that
  carries buttons is recomputed with `fix_buttons` from the job as it is now; nothing valid leaves no keyboard. This
  needs the notifier to replace a message's keyboard without its text, and the store to know which message of a job
  carries the buttons.
- The dashboard already polls; nothing new is needed for Telegram actions to show there.

## Testing

- The layer: every reviewer command and ticket action, including the refusals (protected files, outdated job, failing
  credential, expired confirmation), tested once at the layer.
- Parity: the same command through Telegram and through the dashboard API gives the same outcome state and does the
  same thing, for push, post, post replies and retry.
- Retry convergence: a ticket with a QA report retried by Telegram's typed `retry X` goes to PUSHED. The dashboard's
  Retry restarts where the ticket stopped, as Telegram's button does.
- PR comments: a decision from the dashboard is recorded exactly as the Telegram tap or reply records it (the same
  stored decision and event, `via: dashboard`). The last decision wakes the loop. "Reviewed" from the dashboard sets
  what Telegram's `reviewed` sets.
- Access: a peer outside the allow list gets 403; a cross-site POST to every write endpoint is refused.
- Telegram: the announcement and the keyboard refresh after a dashboard action; the Ukrainian catalogue covers every
  new Telegram text (the i18n tests enforce it).
- The existing Telegram reviewer tests pass unchanged, since Telegram's reviewer answers do not change. The ticket
  tests change only for the typed `reject` at plan review.

## Left to the builder

- Module names and where the layer lives; how the result type looks beyond the fields above.
- How the store records which message carries a job's buttons.
- Layout and styling of the new controls, within the dashboard's existing look.
- Whether long dashboard actions stream progress or just wait.

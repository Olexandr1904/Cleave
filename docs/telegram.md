# Telegram Bot Reference

Every command, free-text intent, inline button, and reply pattern Sickle's Telegram bot understands.

This doc is a user-facing reference. For the implementation/spec, see [docs/features/telegram-notifications.md](features/telegram-notifications.md).

---

## Setup

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

- **Bot token:** create a bot via [@BotFather](https://t.me/BotFather)
- **Chat id:** message [@userinfobot](https://t.me/userinfobot) for a personal chat id; for a group, add the bot to the group and use the negative group id

The bot polls (no webhooks) and starts automatically when the daemon starts. If `TELEGRAM_BOT_TOKEN` is empty, Telegram is disabled and Sickle escalates only via the dashboard.

## Allowlist

The bot ignores messages from chat ids that aren't on its allowlist. The allowlist is built at startup from:

- `global.yaml` → `telegram.default_chat_id`
- Each project's `project.yaml` → `telegram.default_chat_id`

If the allowlist ends up empty for any reason, the bot is locked down (fail-safe) — it accepts no messages until at least one chat id is configured. New projects added through the dashboard wizard extend the allowlist live.

---

## Free-text commands

The bot does not require slash prefixes. Free-text messages are classified by Claude using full pipeline context (mode, awaiting approvals, active workspaces, blocked workspaces, deferred workspaces) — so "yes" resolves the one pending approval if there's only one, "the bug is X" gets routed as `provide_input` if a workspace is BLOCKED, etc.

Recognized intents (from `integrations/telegram/intent_parser.py`):

| Intent | Trigger phrases | Effect |
|---|---|---|
| `status` | `/status`, `status`, `what's running`, `pipeline status` | Pipeline summary: mode, uptime, active workspaces, recent completions |
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

For messages that expect discrete choices, Sickle attaches inline keyboard buttons. Pressing a button is equivalent to typing the corresponding free-text — the bot echoes the recorded decision back as a reply to the original message.

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

When Sickle escalates a BLOCKED workspace, the message you receive in Telegram is a *thread anchor*: replying to it (Telegram's "Reply to" feature, not a new message) feeds your reply back to the agent.

- The reply text is stored on `state.human_input_reply` and made available as additional context when the agent resumes.
- The workspace transitions out of BLOCKED back to its `previous_state` and the orchestrator wakes immediately.
- If you reply with `retry`, the workspace re-enters its `previous_state` without adding context — useful when the agent just needs another shot.

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

Sickle sends Telegram notifications in these situations:

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

---

## Manual vs auto mode

Mode is global (set via `global.yaml` → `pipeline.mode` or toggled live with `/auto` / `/manual`).

- **auto** — workspace flows through every stage without gates.
- **manual** — Sickle inserts AWAITING_APPROVAL gates at major transitions (after analysis, after QA, before pushing). Each gate sends a Telegram message with Approve/Reject buttons. Switching to `auto` mid-flight auto-resumes any AWAITING_APPROVAL workspaces to their happy-path next state.

The `/status` reply always shows the current mode in its header.

---

## See also

- [docs/features/telegram-notifications.md](features/telegram-notifications.md) — implementation spec, change log, FRs
- [docs/labels.md](labels.md) — Jira labels (trigger, ignore, model, repo-routing)
- [docs/troubleshooting.md](troubleshooting.md) — what to do when notifications don't arrive

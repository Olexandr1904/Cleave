# Feature: Telegram Notifications

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-05-01
**Author:** Oleksandr Brazhenko

## Description

Telegram bot adapter behind the NotifierInterface. Sends formatted notifications to the operator when the pipeline needs human input, and receives replies to unblock waiting workspaces. The only human touchpoint — if no questions arise, tickets complete the full cycle without human involvement.

## Requirements

- FR1: Send formatted messages to configured `chat_id` with `[PROJECT/REPO]` prefix
- FR2: Support full notification format: ambiguous ticket, scope loop, fix loop, merge conflict, success, failure
- FR3: `wait_for_reply()` blocks until operator replies to a specific message, returns reply text
- FR4: Reply stored in workspace `state.json` as `human_input_reply`
- FR5: Waiting workspace unblocked and agent receives reply as additional context
- FR6: Bot uses async polling (not webhooks) for simplicity on VPS
- FR7: Per-project chat routing via `telegram.chat_id` in project config
- FR8: Configurable reminder sent if no reply after N hours
- FR9: Implements abstract `NotifierInterface`
- FR10: Free-text operator messages classified into intents via `IntentParser` + Claude CLI
- FR11: Intent classification includes pipeline context (mode, awaiting approvals, workspaces) to resolve ambiguous commands (e.g., "yes" → approve the one pending workspace)
- FR12: `/status` command returns pipeline summary (mode, uptime, active workspaces) formatted by `StatusHandler`
- FR13: Drill-down view shows per-workspace detail: stage, branch, Jira URL, PR URL, iteration counts
- FR14: TelegramAdapter polling loop routes non-reply text messages to the `CommandHandler` while continuing to match replies back to `wait_for_reply` futures
- FR15: Inline action buttons on messages that expect discrete choices (approve/reject gates, PR review complete, retry on failure/deferral); escalation messages stay text-reply only
- FR16: `Button` dataclass in `NotifierInterface` — adapter-agnostic; `TelegramAdapter` translates to `InlineKeyboardMarkup`, routes `CallbackQuery` presses to `CommandHandler.handle_callback()`
- FR17: Button confirmations sent as Telegram replies to the original button message (`reply_to_message_id`)
- FR18: Messages with buttons MUST NOT contain redundant text reply hints for the same actions; text hints only for free-text inputs (escalation answers, PR comment decisions)

## Technical Approach

- `TelegramAdapter` class implementing `NotifierInterface`
- Uses python-telegram-bot library (async mode)
- Async polling for incoming messages
- Message-to-workspace routing: each outgoing message tagged with workspace ID, incoming replies matched back
- Notification templates for each message type (escalation, success, failure)
- `IntentParser` (`integrations/telegram/intent_parser.py`) classifies free-text operator messages into structured intents (status, analyze, approve, reject, set_mode, reviewed, unanswered, unknown) via `ClaudeCodeAdapter.quick_query`; system prompt includes live pipeline context (mode, awaiting approvals, active workspaces) for disambiguation
- `StatusHandler` (`integrations/telegram/handlers/status.py`) formats pipeline status: `format_summary()` produces an overview of all active workspaces; `format_drill_down()` produces per-workspace detail with Jira and PR URLs
- `ModeHandler` (`integrations/telegram/handlers/mode.py`) manages auto/manual pipeline mode with JSON file persistence; `get_mode()` returns current mode, `set_mode()` validates, switches, and persists including a `mode_changed_at` timestamp; the orchestrator calls `get_mode()` to decide whether to poll Jira and whether to insert approval gates
- `ApprovalHandler` (`integrations/telegram/handlers/approval.py`) manages approve/reject operations for workspaces in AWAITING_APPROVAL state; `find_awaiting()` locates workspaces waiting for approval (optionally filtered by ticket ID); `resolve_next_state()` maps previous gate to the next pipeline state (ANALYSIS→DEV, QA→PUSHED, PR_REVIEW→DONE)
- `AnalyzeHandler` (`integrations/telegram/handlers/analyze.py`) validates ticket IDs against Jira before creating workspaces in manual mode; `validate_tickets()` fetches each ticket and splits results into valid/invalid lists; `is_already_active()` checks whether a ticket already has a running workspace
- `CommandHandler` (`integrations/telegram/command_handler.py`) is the central dispatcher; it receives raw text from the TelegramAdapter, calls `IntentParser.parse()` with live pipeline context, then routes to the appropriate handler based on intent (status, set_mode, approve, reject, analyze, error, unknown). Also exposes `handle_callback(action, ticket_id, chat_id, message_id)` for inline button presses — bypasses LLM parsing, reuses existing handler logic, sends confirmations as Telegram replies to the button's message

## Dependencies

- python-telegram-bot for Telegram Bot API
- Configuration Cascade for telegram settings (bot_token, chat_id)
- Workspace state for storing human input
- Abstract NotifierInterface from `integrations/base/`

## Acceptance Criteria

- [ ] Sends formatted messages with project/repo prefix
- [ ] All notification types render correctly
- [ ] Replies are received and routed to the correct waiting workspace
- [ ] Workspace unblocks and resumes after reply
- [ ] Reminder sent after configurable timeout
- [ ] Per-project chat routing works

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
| 2026-04-08 | Added IntentParser: classifies Telegram messages into pipeline intents via Claude CLI |
| 2026-04-08 | Added StatusHandler: formats /status summary and per-workspace drill-down with Jira/PR URLs |
| 2026-04-08 | Added ModeHandler: manages auto/manual pipeline mode with JSON file persistence |
| 2026-04-08 | Added ApprovalHandler: find_awaiting() and resolve_next_state() for AWAITING_APPROVAL gate management |
| 2026-04-08 | Added AnalyzeHandler: validate_tickets() and is_already_active() for manual mode ticket validation |
| 2026-04-08 | Added CommandHandler: central dispatcher routing parsed intents to StatusHandler, ModeHandler, ApprovalHandler, and AnalyzeHandler |
| 2026-04-08 | Hooked CommandHandler into TelegramAdapter polling: `_handle_incoming` routes replies to pending futures and all other text to the CommandHandler via `set_command_handler` |
| 2026-04-09 | Wired ModeHandler, IntentParser, and CommandHandler into `main.py` startup flow: ModeHandler loaded from `<config>/daemon_state.json` with `pipeline.mode` default; when TelegramAdapter is active, IntentParser and CommandHandler are constructed and attached via `notifier.set_command_handler()` so free-text operator messages reach the dispatcher |
| 2026-04-09 | Orchestrator auto-resumes AWAITING_APPROVAL workspaces when mode flips to auto mid-flight, transitioning the workspace to the happy-path next state (ANALYSIS→DEV, QA→PUSHED, PR_REVIEW→DONE) and advancing immediately. |
| 2026-04-09 | `ClaudeAdapter` (API) now implements `quick_query` with the same signature as the CLI adapter, so IntentParser works with either LLM backend. |
| 2026-04-09 | `main.py` starts Telegram polling via `notifier.start_polling()` before the orchestrator loop and calls `stop_polling()` on shutdown, so incoming commands are actually received. |
| 2026-04-09 | Orchestrator exposes `analyze_ticket_ids(ticket_ids)` — validates via Jira, routes each to a repo by `jira_repo_label`, and creates workspaces; wired into CommandHandler as `analyze_callback`. |
| 2026-04-09 | CommandHandler now accepts `allowed_chat_ids` (empty/missing → open) and rejects messages from chats outside the allowlist. |
| 2026-04-09 | Orchestrator maintains a 20-entry ring buffer of recently-terminated workspaces exposed via `get_recent_completions()`; StatusHandler.format_summary now renders them in /status. |
| 2026-04-09 | Instrumented TelegramAdapter and CommandHandler with optional event_bus: emits tg_message_sent, tg_message_received, and intent_parsed events. |
| 2026-04-12 | Added retry command: retries a ticket from a specified or inferred stage; added escalation reply handler to unblock BLOCKED workspaces via Telegram reply-to; added typing indicator during intent parsing |
| 2026-04-14 | Extended retry/resume to DEFERRED tickets: `_handle_retry` treats DEFERRED like BLOCKED/FAILED (transitions to `previous_state`); IntentParser system prompt now lists deferred workspaces and advertises retry for DEFERRED tickets; `_build_context` exposes `deferred_workspaces`. |
| 2026-04-16 | Added `set_tracker` and `add_allowed_chat_id` mutators to CommandHandler for wizard hot-reload: tracker can be attached post-init; allowlist extended per-project (no-op when None/'admit all'). |
| 2026-04-21 | Added inline action buttons: `Button` dataclass in `NotifierInterface`; `TelegramAdapter` translates to `InlineKeyboardMarkup` and routes `CallbackQuery` to `CommandHandler.handle_callback()`; approval gates get Approve/Reject buttons, PR review gets Review Complete, failed/deferred get Retry; escalation messages stay text-reply only. |
| 2026-04-24 | `_handle_escalate` now uses `_build_blocked_reason` for the message body (prefers `ba-questions.md` for analysis-stage escalations); drops `[Proceed]/[Retry]` inline buttons; stores only the reason in `human_input_question` (no header, no hint). |
| 2026-04-24 | Added `_notify_verification_blocked`: when stage verification fails (e.g. dev agent made no commit), sends a TG notification with the verification reason and populates `escalation_msg_id`/`escalation_chat_id`/`human_input_question` so the reply flow can unblock the workspace. |
| 2026-04-24 | `StatusHandler.format_drill_down` now shows "Blocked on: <reason>" for BLOCKED workspaces with `human_input_question` set, instead of "Last error: none"; falls back to existing Last error behaviour for all other states. |
| 2026-04-28 | New `clear_gradle:<ticket>` callback action: when an operator taps the "🧹 Clear cache & retry" button on a FAILED notification whose error matches the AAPT2 corruption signature, the bot wipes `<gradle_home>/caches/*/transforms`, transitions the workspace back to its `previous_state`, emits a `gradle_cache_cleared` event with bytes freed, and wakes the orchestrator. Reuses the shared `orchestrator.gradle_remediation` module so the dashboard equivalent stays in sync. |
| 2026-04-29 | Removed Skip button from PR-comment escalations. Operators interpreted Skip as "I'm done, move on" but the prior semantic was "ask me again every 30 min", so they were trapped in a nag loop on a button labelled the wrong way. Buttons are now Fix + Won't Fix only. Skip is still recognized as a free-text reply but means "drop, no GitHub action, advance to DONE" (matches operator intent). The escalation message now includes a footer telling operators they can reply with `fix` or `won't fix: <reason>` to add free-text context. Both button presses and text replies now echo the recorded decision back to TG (e.g. "✓ Recorded FIX (dev-agent will re-engage) for @Copilot on Frag.kt:96") plus emit a `pr_comment_decision_recorded` event. |
| 2026-04-29 | `_notify_deferred` no longer hard-codes "Quota exhausted" for every transient agent failure. The pipeline reuses the DEFERRED retry path for any CLI exit (timeout, network, max_turns, etc.) but the TG message must reflect the actual cause — saying "Quota exhausted" when the user has plenty of quota erodes trust in the bot. Now branches three ways: real quota (matches `usage limit` or `api_error_status: 429`) keeps the existing wording + window debounce; `error_max_turns` says "Agent hit max-turns limit" with a hint about raising the limit / splitting the work; everything else says "Transient agent failure" and surfaces the first 200 chars of the actual error. Quota window debouncing only applies to real quota events so distinct max_turns / network failures aren't silenced by a prior quota hit. |
| 2026-04-30 | `handle_reply` PR_REVIEW branch now uses `_classify_reply` exclusively: fix synonyms produce `✓ Recognized as FIX. FIX (matched: 'yes').` echo including the matched token; won't-fix synonyms produce `✓ Recognized as WON'T FIX.` echo with matched token and GitHub reason; free-text routes to `_stage_reinvestigation` (stubbed, Task 8). Old SKIP-fallthrough and inline decision-label map removed. `pr_comment_decision_recorded` event now includes `matched_token` field. |
| 2026-04-30 | Added `unanswered` intent to IntentParser: allows operators to ask what PR comments are still pending (e.g., `/unanswered`, `/repeat`, `what's pending`); complements the `reviewed` intent for PR review stage interactions. |
| 2026-04-30 | Button-press echo unified with reply-path echo: `pr_fix`/`pr_wontfix` callbacks now produce `✓ Recognized as FIX (matched: 'Fix' button). Dev-agent will re-engage on x.kt:1.` — same `Recognized as` prefix as the text-reply path. `pr_skip` dead branch dropped. `pr_comment_decision_recorded` event payload now includes `matched_token` (`button:fix` / `button:wontfix`) and `via=button`. |
| 2026-04-30 | `CommandHandler._handle_provide_input` now uses `Stage.BLOCKED` instead of the literal `"BLOCKED"` string when filtering blocked workspaces, matching the convention used elsewhere in the same file. Both worked at runtime since `Stage` is a `StrEnum`, but the inconsistency would silently break if the enum is ever migrated to a non-string base. |
| 2026-05-01 | `_handle_retry` now resets `stage_iterations[stage]` to 0 for the target stage before transitioning, so the iteration-cap check does not immediately re-escalate without running the agent. Smart retry detection updated to use `STAGE_REPORT_FILE` constants (agent-written reports) so it confirms stage work product exists, not just that the agent ran. |
| 2026-05-01 | Removed dead `elif action == "skip":` block from `handle_callback` — the skip path was only reachable from a button that no longer exists; skip as a BLOCKED reply-text is handled in `handle_reply`. Documented `skip` reply keyword and the PR_REVIEW reply anchor in `docs/telegram.md`. |
| 2026-05-01 | Added `orchestrator/tg_format.py`: shared Telegram message formatting helpers (`tg_header`, `read_ticket_title`, `strip_markdown`) so Orchestrator and CommandHandler produce consistent ticket message headers without depending on each other. |
| 2026-05-01 | `strip_markdown` now handles `_italic_` (underscore) in addition to `*italic*` (asterisk), matching the design spec. |
| 2026-05-01 | Migrated all Telegram message sites in `orchestrator.py` to use `tg_format.*`; removed `_tg_header` and `_get_ticket_title` static methods; wrapped `_build_blocked_reason` output with `strip_markdown` in escalation and verification-blocked paths; rewrote message bodies for FAILED-generic, PR-opened, Pipeline-complete, dev-agent-fix-failed, re-investigation-failed. |
| 2026-05-01 | Fixed `read_ticket_title` in `tg_format.py`: was reading `meta/ticket.json` (never written); now parses first line of `meta/ticket.md` in `# TICKET-ID: summary` format. |
| 2026-05-01 | `escalation_view.build_escalated_comment_message` now uses `tg_format.tg_header` for the message header; removed backtick-wrapped option text in favour of plain dashes. |
| 2026-05-01 | All 14 ticket-specific confirmation message sites in `CommandHandler` now prefix with `tg_format.tg_header`; backtick characters removed from hint-exhausted and hint-staged messages. |
| 2026-05-03 | Fixed `_build_blocked_reason`: unmapped stages (e.g. `pushed`) no longer fall back to showing the most recent `*-output.md` from an unrelated stage. Fixed `_handle_escalate`: non-agent stages now show retry options instead of "Reply with your answer". Improved max-iterations message to explain the agent ran N times without completing, labels last output clearly, and lists all three options with plain-language descriptions. |

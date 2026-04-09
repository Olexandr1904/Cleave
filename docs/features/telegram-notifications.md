# Feature: Telegram Notifications

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-09
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

## Technical Approach

- `TelegramAdapter` class implementing `NotifierInterface`
- Uses python-telegram-bot library (async mode)
- Async polling for incoming messages
- Message-to-workspace routing: each outgoing message tagged with workspace ID, incoming replies matched back
- Notification templates for each message type (escalation, success, failure)
- `IntentParser` (`integrations/telegram/intent_parser.py`) classifies free-text operator messages into structured intents (status, analyze, approve, reject, set_mode, unknown) via `ClaudeCodeAdapter.quick_query`; system prompt includes live pipeline context (mode, awaiting approvals, active workspaces) for disambiguation
- `StatusHandler` (`integrations/telegram/handlers/status.py`) formats pipeline status: `format_summary()` produces an overview of all active workspaces; `format_drill_down()` produces per-workspace detail with Jira and PR URLs
- `ModeHandler` (`integrations/telegram/handlers/mode.py`) manages auto/manual pipeline mode with JSON file persistence; `get_mode()` returns current mode, `set_mode()` validates, switches, and persists including a `mode_changed_at` timestamp; the orchestrator calls `get_mode()` to decide whether to poll Jira and whether to insert approval gates
- `ApprovalHandler` (`integrations/telegram/handlers/approval.py`) manages approve/reject operations for workspaces in AWAITING_APPROVAL state; `find_awaiting()` locates workspaces waiting for approval (optionally filtered by ticket ID); `resolve_next_state()` maps previous gate to the next pipeline state (ANALYSIS→DEV, QA→PUSHED, PR_REVIEW→DONE)
- `AnalyzeHandler` (`integrations/telegram/handlers/analyze.py`) validates ticket IDs against Jira before creating workspaces in manual mode; `validate_tickets()` fetches each ticket and splits results into valid/invalid lists; `is_already_active()` checks whether a ticket already has a running workspace
- `CommandHandler` (`integrations/telegram/command_handler.py`) is the central dispatcher; it receives raw text from the TelegramAdapter, calls `IntentParser.parse()` with live pipeline context, then routes to the appropriate handler based on intent (status, set_mode, approve, reject, analyze, error, unknown)

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

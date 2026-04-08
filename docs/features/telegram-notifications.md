# Feature: Telegram Notifications

**Status:** In Progress
**Created:** 2026-04-07
**Updated:** 2026-04-08
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

## Technical Approach

- `TelegramAdapter` class implementing `NotifierInterface`
- Uses python-telegram-bot library (async mode)
- Async polling for incoming messages
- Message-to-workspace routing: each outgoing message tagged with workspace ID, incoming replies matched back
- Notification templates for each message type (escalation, success, failure)
- `IntentParser` (`integrations/telegram/intent_parser.py`) classifies free-text operator messages into structured intents (status, analyze, approve, reject, set_mode, unknown) via `ClaudeCodeAdapter.quick_query`; system prompt includes live pipeline context (mode, awaiting approvals, active workspaces) for disambiguation

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

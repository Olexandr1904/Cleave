# Telegram Inline Action Buttons — Design Spec

**Date:** 2026-04-21
**Status:** Approved

## Problem

Users interact with the Sickle pipeline via Telegram. Every action (approve, reject, retry, reviewed) requires typing a text command or replying to a message. For discrete-choice actions this is unnecessarily friction-heavy — inline buttons are a better UX.

At the same time, escalation messages where agents ask free-text questions must stay as reply-to-message interactions — buttons can't replace typed answers.

## Decisions

- **Approach C** — abstract `Button` dataclass in the notifier interface; each adapter translates to its native format (Telegram inline keyboard, future Slack blocks, etc.)
- **After button press** — send a new confirmation message as a Telegram reply to the original button message (`reply_to_message_id`). Do not edit the original message.
- **Buttons are additive** — all existing text commands and reply-to-message flows remain as fallback.

## Data Model

New dataclass in `integrations/base/notifier.py`:

```python
@dataclass
class Button:
    label: str      # User-visible text, e.g. "Approve"
    action: str     # Encoded action, e.g. "approve:ACME-14595"
```

The `action` field uses the format `intent:ticket_id`. Must stay under 64 bytes (Telegram's `callback_data` limit).

## Interface Changes

`NotifierInterface.send_message` gains two optional params:

```python
async def send_message(
    self, chat_id: str, message: str,
    buttons: list[Button] | None = None,
    reply_to_message_id: int | None = None,
) -> int:
```

Both default to `None` — existing callers are unaffected.

## Button Placement

| Message type | Source method | Buttons | Callback actions |
|---|---|---|---|
| Approval gate | `Orchestrator._build_gate_summary` | `[Approve]` `[Reject]` | `approve:TICKET`, `reject:TICKET` |
| PR review | push_and_open_pr notification block | `[Review Complete]` | `reviewed:TICKET` |
| Failed | `Orchestrator._notify_failed` | `[Retry]` | `retry:TICKET` |
| Deferred | `Orchestrator._notify_deferred` | `[Retry Now]` | `retry:TICKET` |
| Escalation / BLOCKED | `Orchestrator._handle_escalate` | **None** | N/A — free-text reply only |

Existing text hints in message bodies ("Reply: proceed or reject", etc.) stay as fallback.

## Callback Flow

```
User taps [Approve] on message 42
  → Telegram sends CallbackQuery(data="approve:ACME-14595", message.id=42)
  → TelegramAdapter._handle_callback() parses action + ticket_id
  → Calls CommandHandler.handle_callback("approve", "ACME-14595", chat_id)
  → CommandHandler reuses existing _handle_approve() logic
  → Sends confirmation via send_message(reply_to_message_id=42)
  → Answers the CallbackQuery (removes the loading spinner on the button)
```

`CommandHandler.handle_callback` is a thin router — it builds a synthetic `ParsedIntent` and calls the existing handler, bypassing LLM intent parsing entirely.

## File Changes

| File | Change |
|---|---|
| `integrations/base/notifier.py` | Add `Button` dataclass. Add `buttons` and `reply_to_message_id` optional params to `send_message` |
| `integrations/telegram/telegram_adapter.py` | Translate `list[Button]` → `InlineKeyboardMarkup`. Pass `reply_to_message_id` to bot API. Register `CallbackQueryHandler` in `start_polling()`. Add `_handle_callback()` method that routes to `CommandHandler.handle_callback()` |
| `integrations/telegram/command_handler.py` | Add `handle_callback(action, ticket_id, chat_id)` — builds synthetic intent, delegates to existing `_handle_approve`, `_handle_reject`, `_handle_reviewed`, `_handle_retry` |
| `orchestrator/orchestrator.py` | `_build_gate_summary` returns `(str, list[Button])` tuple instead of `str`. Callers pass buttons to `send_message`. PR review notification, `_notify_failed`, `_notify_deferred` each build their own `Button` list and pass to `send_message`. `_handle_escalate` sends no buttons. |

## Not Changed

- `intent_parser.py` — buttons bypass LLM parsing entirely
- Workspace state fields — no new fields needed
- Event bus — existing events cover the actions
- Dashboard — no UI changes

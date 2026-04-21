# Telegram Inline Action Buttons — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inline action buttons to Telegram messages that expect discrete user actions (approve, reject, reviewed, retry), while keeping escalation/BLOCKED messages as free-text reply-only.

**Architecture:** Abstract `Button` dataclass in the notifier interface; `TelegramAdapter` translates to `InlineKeyboardMarkup`. A `CallbackQueryHandler` routes button presses to `CommandHandler.handle_callback()` which reuses existing handler logic without LLM parsing. Confirmations are sent as Telegram replies to the original button message.

**Tech Stack:** python-telegram-bot 21.10 (`InlineKeyboardButton`, `InlineKeyboardMarkup`, `CallbackQueryHandler`), existing `NotifierInterface` abstraction.

---

### Task 1: Add `Button` dataclass and update `NotifierInterface`

**Files:**
- Modify: `integrations/base/notifier.py:1-30`
- Test: `tests/unit/test_notifier_button.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_notifier_button.py
"""Tests for the Button dataclass."""

from integrations.base.notifier import Button


def test_button_fields():
    btn = Button(label="Approve", action="approve:T-1")
    assert btn.label == "Approve"
    assert btn.action == "approve:T-1"


def test_button_action_under_64_bytes():
    """Telegram callback_data limit is 64 bytes."""
    btn = Button(label="Approve", action="approve:ACME-14595")
    assert len(btn.action.encode("utf-8")) <= 64
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_notifier_button.py -v`
Expected: FAIL with `ImportError: cannot import name 'Button'`

- [ ] **Step 3: Implement Button dataclass and update send_message signature**

In `integrations/base/notifier.py`, add the dataclass import and `Button`, then update `send_message`:

```python
"""Abstract notifier interface for operator communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Button:
    """A labeled action button for inline display."""
    label: str   # User-visible text, e.g. "Approve"
    action: str  # Encoded action, e.g. "approve:ACME-14595"


class NotifierInterface(ABC):
    """Abstract interface for notification systems (Telegram, Slack, etc.)."""

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        message: str,
        buttons: list[Button] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Send a message to the operator.

        Args:
            chat_id: Target chat.
            message: Text content.
            buttons: Optional inline action buttons.
            reply_to_message_id: If set, send as a reply to this message.

        Returns the message ID for reply tracking.
        """

    @abstractmethod
    async def wait_for_reply(self, chat_id: str, message_id: int, timeout_seconds: int = 0) -> str | None:
        """Wait for a reply to a specific message.

        Args:
            chat_id: Chat to listen in.
            message_id: Original message ID to match replies against.
            timeout_seconds: Max seconds to wait (0 = wait indefinitely).

        Returns:
            The reply text, or None if timed out.
        """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_notifier_button.py -v`
Expected: PASS

- [ ] **Step 5: Run existing tests to check nothing breaks**

Run: `python -m pytest tests/unit/test_telegram_adapter.py tests/unit/test_command_handler.py -v`
Expected: PASS (existing callers use positional args for `chat_id` and `message` only)

- [ ] **Step 6: Commit**

```bash
git add integrations/base/notifier.py tests/unit/test_notifier_button.py
git commit -m "feat(notifier): add Button dataclass and optional buttons/reply_to params to send_message"
```

---

### Task 2: Update `TelegramAdapter` to support buttons, reply-to, and callback queries

**Files:**
- Modify: `integrations/telegram/telegram_adapter.py:1-151`
- Test: `tests/unit/test_telegram_adapter.py`

- [ ] **Step 1: Write failing tests for buttons, reply-to, and callback routing**

Append to `tests/unit/test_telegram_adapter.py`:

```python
from integrations.base.notifier import Button


class TestSendMessageWithButtons:
    async def test_send_message_passes_reply_markup(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 99
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            buttons = [
                Button(label="Approve", action="approve:T-1"),
                Button(label="Reject", action="reject:T-1"),
            ]
            msg_id = await adapter.send_message("123", "Gate summary", buttons=buttons)

            assert msg_id == 99
            call_kwargs = bot_instance.send_message.call_args
            markup = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get("reply_markup")
            assert markup is not None
            # InlineKeyboardMarkup has .inline_keyboard attribute
            assert len(markup.inline_keyboard[0]) == 2
            assert markup.inline_keyboard[0][0].text == "Approve"
            assert markup.inline_keyboard[0][0].callback_data == "approve:T-1"

    async def test_send_message_passes_reply_to_message_id(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 100
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            msg_id = await adapter.send_message("123", "Confirmed", reply_to_message_id=42)

            assert msg_id == 100
            call_kwargs = bot_instance.send_message.call_args
            reply_to = call_kwargs.kwargs.get("reply_to_message_id") or call_kwargs[1].get("reply_to_message_id")
            assert reply_to == 42

    async def test_send_message_without_buttons_sends_no_markup(self):
        with patch("integrations.telegram.telegram_adapter.Bot") as MockBot:
            bot_instance = AsyncMock()
            sent_msg = MagicMock()
            sent_msg.message_id = 101
            bot_instance.send_message = AsyncMock(return_value=sent_msg)
            MockBot.return_value = bot_instance

            adapter = TelegramAdapter(bot_token="fake-token")
            await adapter.send_message("123", "Plain text")

            call_kwargs = bot_instance.send_message.call_args
            # No reply_markup should be passed
            markup = call_kwargs.kwargs.get("reply_markup")
            assert markup is None


class TestCallbackQueryRouting:
    async def test_callback_routes_to_command_handler(self):
        with patch("integrations.telegram.telegram_adapter.Bot"):
            adapter = TelegramAdapter(bot_token="fake-token")
            handler = AsyncMock()
            handler.handle_callback = AsyncMock()
            adapter.set_command_handler(handler)

            update = MagicMock()
            update.callback_query.data = "approve:T-1"
            update.callback_query.message.chat.id = 12345
            update.callback_query.message.message_id = 42
            update.callback_query.answer = AsyncMock()

            await adapter._handle_callback(update, None)

            handler.handle_callback.assert_called_once_with("approve", "T-1", "12345", 42)
            update.callback_query.answer.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_telegram_adapter.py -v`
Expected: FAIL — `send_message` doesn't accept `buttons`/`reply_to_message_id` kwargs; `_handle_callback` doesn't exist

- [ ] **Step 3: Implement the adapter changes**

Update `integrations/telegram/telegram_adapter.py`:

1. Add imports at the top:
```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
```

2. Add import for Button:
```python
from integrations.base.notifier import Button, NotifierInterface
```

3. Update `send_message` to accept and translate buttons + reply_to:
```python
    async def send_message(
        self,
        chat_id: str,
        message: str,
        buttons: list[Button] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Send a message and return the message ID."""
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message,
        }
        if buttons:
            keyboard = [
                [InlineKeyboardButton(b.label, callback_data=b.action) for b in buttons]
            ]
            kwargs["reply_markup"] = InlineKeyboardMarkup(keyboard)
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id

        msg = await self._bot.send_message(**kwargs)
        logger.info("Sent Telegram message %d to chat %s", msg.message_id, chat_id)
        if self._events:
            self._events.emit("tg_message_sent", f"Sent message to chat {chat_id}: {message[:80]}", data={"chat_id": chat_id, "text_preview": message[:200]})
        return msg.message_id
```

4. Add `_handle_callback` method:
```python
    async def _handle_callback(self, update: Update, context: object) -> None:
        """Handle inline keyboard button presses."""
        query = update.callback_query
        if not query or not query.data:
            return

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            await query.answer(text="Invalid action")
            return

        action, ticket_id = parts
        chat_id = str(query.message.chat.id)
        message_id = query.message.message_id

        await query.answer()

        if self._command_handler and hasattr(self._command_handler, "handle_callback"):
            try:
                await self._command_handler.handle_callback(action, ticket_id, chat_id, message_id)
            except Exception as e:
                logger.error("Callback handler error: %s", e, exc_info=True)
```

5. Register `CallbackQueryHandler` in `start_polling`:
```python
    async def start_polling(self) -> None:
        """Start the bot's polling loop for receiving messages."""
        self._app = Application.builder().token(self._bot.token).build()
        self._app.add_handler(
            MessageHandler(filters.TEXT, self._handle_incoming)
        )
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("Telegram bot polling started")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_telegram_adapter.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/telegram_adapter.py tests/unit/test_telegram_adapter.py
git commit -m "feat(telegram): support inline buttons, reply-to, and callback query routing"
```

---

### Task 3: Add `handle_callback` to `CommandHandler`

**Files:**
- Modify: `integrations/telegram/command_handler.py:31-488`
- Test: `tests/unit/test_command_handler.py`

- [ ] **Step 1: Write failing tests for handle_callback**

Append to `tests/unit/test_command_handler.py`:

```python
class TestHandleCallback:
    @pytest.fixture
    def mock_notifier(self):
        notifier = AsyncMock()
        notifier.send_message = AsyncMock(return_value=1)
        notifier.edit_message = AsyncMock()
        return notifier

    @pytest.fixture
    def mock_mode_handler(self):
        handler = MagicMock()
        handler.get_mode.return_value = "manual"
        return handler

    async def test_callback_approve(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("approve", "T-1", "12345", 42)
        ws.transition.assert_called_once_with("DEV")
        # Confirmation sent as reply to the button message
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_reject(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("reject", "T-1", "12345", 42)
        ws.transition.assert_called_once_with("FAILED")
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_reviewed(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "PR_REVIEW")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        handler._wake_fn = MagicMock()
        await handler.handle_callback("reviewed", "T-1", "12345", 42)
        assert ws.state.human_input_reply == "reviewed"
        ws.save_state.assert_called_once()
        handler._wake_fn.assert_called_once()
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_retry(self, mock_notifier, mock_mode_handler):
        ws = _make_workspace("T-1", "FAILED", previous_state="QA")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [ws],
        )
        await handler.handle_callback("retry", "T-1", "12345", 42)
        ws.transition.assert_called_once()
        call_kwargs = mock_notifier.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 42

    async def test_callback_unknown_action_sends_error(self, mock_notifier, mock_mode_handler):
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=mock_notifier,
            mode_handler=mock_mode_handler,
            active_workspaces_fn=lambda: [],
        )
        await handler.handle_callback("bogus", "T-1", "12345", 42)
        call_args = mock_notifier.send_message.call_args[0]
        assert "Unknown" in call_args[1] or "unknown" in call_args[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_command_handler.py::TestHandleCallback -v`
Expected: FAIL — `CommandHandler` has no `handle_callback` method

- [ ] **Step 3: Implement handle_callback**

Add to `CommandHandler` class in `integrations/telegram/command_handler.py`:

```python
    async def handle_callback(self, action: str, ticket_id: str, chat_id: str, message_id: int) -> None:
        """Handle an inline button press. Bypasses LLM intent parsing."""
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            return

        workspaces = self._active_workspaces_fn()

        if action == "approve":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "AWAITING_APPROVAL"), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No workspace awaiting approval for {ticket_id}.", reply_to_message_id=message_id)
                return
            next_state = self._approval_handler.resolve_next_state(ws)
            ws.transition(next_state)
            await self._notifier.send_message(chat_id, f"Approved {ticket_id}. Moving to {next_state}.", reply_to_message_id=message_id)

        elif action == "reject":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "AWAITING_APPROVAL"), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No workspace awaiting approval for {ticket_id}.", reply_to_message_id=message_id)
                return
            ws.transition("FAILED")
            ws.update_state(error="Rejected by operator via Telegram")
            await self._notifier.send_message(chat_id, f"Rejected {ticket_id}. Marked as FAILED.", reply_to_message_id=message_id)

        elif action == "reviewed":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "PR_REVIEW"), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No workspace in PR_REVIEW for {ticket_id}.", reply_to_message_id=message_id)
                return
            ws.state.human_input_reply = "reviewed"
            ws.save_state()
            await self._notifier.send_message(chat_id, f"Got it. Fetching PR comments for {ticket_id} now.", reply_to_message_id=message_id)
            if hasattr(self, '_wake_fn') and self._wake_fn:
                self._wake_fn()

        elif action == "retry":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No active workspace found for {ticket_id}.", reply_to_message_id=message_id)
                return
            ws.state.human_input_pending = False
            ws.state.error = None
            target_state = ws.state.previous_state or "ANALYSIS"
            ws.transition(target_state)
            ws.save_state()
            await self._notifier.send_message(chat_id, f"Retrying {ticket_id} from {target_state}.", reply_to_message_id=message_id)

        else:
            await self._notifier.send_message(chat_id, f"Unknown action: {action}", reply_to_message_id=message_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_command_handler.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_command_handler.py
git commit -m "feat(telegram): add handle_callback for inline button actions"
```

---

### Task 4: Wire buttons into orchestrator messages

**Files:**
- Modify: `orchestrator/orchestrator.py` (lines 639-645, 680-693, 695-710, 843-860, 1052-1071)

- [ ] **Step 1: Add Button import at top of orchestrator.py**

Add to the imports at line 19:

```python
from integrations.base.notifier import Button, NotifierInterface
```

(Replace the existing `from integrations.base.notifier import NotifierInterface`.)

- [ ] **Step 2: Update `_build_gate_summary` to return `(str, list[Button])`**

Change `_build_gate_summary` (line 1052) from returning `str` to returning a tuple:

```python
    def _build_gate_summary(self, workspace: Workspace, gate_state: str) -> tuple[str, list[Button]]:
        """Build a summary message and buttons for an approval gate notification."""
        state = workspace.state
        tid = state.ticket_id
        sep = "─" * 30

        actions = {
            "ANALYSIS": ("Analysis done → ready for development", "proceed = start coding, reject = back to analysis"),
            "QA": ("QA passed → ready to push & open PR", "proceed = push code, reject = back to dev"),
            "PR_REVIEW": (f"PR ready: {state.pr_url or 'N/A'}", "proceed = finalize, reject = back to dev"),
        }
        title, options = actions.get(gate_state, (f"Awaiting approval at {gate_state}", "proceed or reject"))

        text = (
            f"⏸ [{state.company_id}/{state.repo_id}] {tid}\n"
            f"{sep}\n"
            f"{title}\n"
            f"{sep}\n"
            f"↩️ Reply: {options}"
        )
        buttons = [
            Button(label="Approve", action=f"approve:{tid}"),
            Button(label="Reject", action=f"reject:{tid}"),
        ]
        return text, buttons
```

- [ ] **Step 3: Update call sites for `_build_gate_summary`**

There are two call sites. Both need to unpack the tuple and pass buttons.

Call site 1 — line 644 (inside `advance_workspace`, stage-based gate):
```python
                    summary, buttons = self._build_gate_summary(workspace, current_state)
                    await self._notifier.send_message(chat_id, summary, buttons=buttons)
```

Call site 2 — line 824 (inside `_run_action_stage`, action-based gate):
```python
                summary, buttons = self._build_gate_summary(workspace, current_state)
                await self._notifier.send_message(chat_id, summary, buttons=buttons)
```

- [ ] **Step 4: Add buttons to PR review notification**

Update lines 849-860 (the PR created notification block):

```python
                msg = (
                    f"🔗 [{state.company_id}/{state.repo_id}] {state.ticket_id}\n"
                    f"{sep}\n"
                    f"PR created: {pr_url}\n\n"
                    f"Please review the code. When the review is complete,\n"
                    f"reply to THIS message with 'reviewed'.\n"
                    f"{sep}"
                )
                buttons = [Button(label="Review Complete", action=f"reviewed:{state.ticket_id}")]
                msg_id = await self._notifier.send_message(chat_id, msg, buttons=buttons)
```

- [ ] **Step 5: Add button to `_notify_failed`**

Update `_notify_failed` (lines 695-710):

```python
    async def _notify_failed(self, workspace: Workspace, error: str) -> None:
        """Send a one-shot Telegram notification for a permanent failure."""
        if self._notifier is None:
            return
        state = workspace.state
        chat_id = self._get_chat_id(workspace)
        first_line = (error or "").splitlines()[0] if error else ""
        msg = (
            f"\u274c [{state.company_id}/{state.repo_id}] {state.ticket_id} "
            f"FAILED at {state.previous_state or '?'}. Error: {first_line}. "
            f"Reply 'retry {state.ticket_id}' or use the dashboard."
        )
        buttons = [Button(label="Retry", action=f"retry:{state.ticket_id}")]
        try:
            await self._notifier.send_message(chat_id, msg, buttons=buttons)
        except Exception as e:
            logger.warning("Failed to send failure notification: %s", e)
```

- [ ] **Step 6: Add button to `_notify_deferred`**

Update `_notify_deferred` (lines 680-693):

```python
        msg = (
            f"\u23f1 [{state.company_id}/{state.repo_id}] Quota exhausted. "
            f"{state.ticket_id} (at {state.previous_state or '?'}) deferred, "
            f"will retry at {retry_at.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"Other tickets hitting the same quota will defer silently until then."
        )
        buttons = [Button(label="Retry Now", action=f"retry:{state.ticket_id}")]
        try:
            await self._notifier.send_message(chat_id, msg, buttons=buttons)
```

- [ ] **Step 7: Verify `_handle_escalate` sends NO buttons**

Confirm that the `send_message` call at line 1017 does NOT pass buttons — it should remain:

```python
            msg_id = await self._notifier.send_message(chat_id, message)
```

No change needed here — just verify.

- [ ] **Step 8: Run full test suite**

Run: `python -m pytest tests/unit/ -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add orchestrator/orchestrator.py
git commit -m "feat(orchestrator): wire inline buttons into gate, PR review, failed, and deferred notifications"
```

---

### Task 5: Integration smoke test

**Files:**
- Test: `tests/unit/test_telegram_buttons_integration.py`

- [ ] **Step 1: Write an end-to-end-style unit test that traces the full callback flow**

```python
# tests/unit/test_telegram_buttons_integration.py
"""Integration test: button press → adapter → command handler → workspace transition."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from integrations.base.notifier import Button
from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.handlers.mode import ModeHandler


def _make_workspace(ticket_id, state, previous_state=None):
    ws = MagicMock()
    ws_state = MagicMock()
    ws_state.ticket_id = ticket_id
    ws_state.current_state = state
    ws_state.previous_state = previous_state
    ws_state.human_input_pending = state in ("AWAITING_APPROVAL", "BLOCKED")
    ws_state.company_id = "test"
    ws_state.repo_id = "repo"
    ws_state.pr_url = None
    ws_state.error = None
    type(ws).state = PropertyMock(return_value=ws_state)
    return ws


class TestButtonIntegrationFlow:
    """Traces: user taps button → adapter parses callback → handler transitions workspace."""

    @pytest.fixture
    def notifier(self):
        n = AsyncMock()
        n.send_message = AsyncMock(return_value=1)
        n.edit_message = AsyncMock()
        return n

    async def test_approve_button_flow(self, notifier):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=notifier,
            mode_handler=MagicMock(get_mode=MagicMock(return_value="manual")),
            active_workspaces_fn=lambda: [ws],
        )

        # Simulate what the adapter does when it receives a CallbackQuery
        action, ticket_id = "approve:T-1".split(":", 1)
        await handler.handle_callback(action, ticket_id, "12345", 42)

        ws.transition.assert_called_once_with("DEV")
        # Confirmation sent as reply to button message
        call_kwargs = notifier.send_message.call_args
        assert "Approved" in call_kwargs[0][1]
        assert call_kwargs.kwargs["reply_to_message_id"] == 42

    async def test_reject_button_flow(self, notifier):
        ws = _make_workspace("T-1", "AWAITING_APPROVAL", previous_state="QA")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=notifier,
            mode_handler=MagicMock(get_mode=MagicMock(return_value="manual")),
            active_workspaces_fn=lambda: [ws],
        )

        action, ticket_id = "reject:T-1".split(":", 1)
        await handler.handle_callback(action, ticket_id, "12345", 42)

        ws.transition.assert_called_once_with("FAILED")
        call_kwargs = notifier.send_message.call_args
        assert "Rejected" in call_kwargs[0][1]
        assert call_kwargs.kwargs["reply_to_message_id"] == 42

    async def test_stale_button_press_on_already_approved(self, notifier):
        """Button pressed after workspace already moved past AWAITING_APPROVAL."""
        ws = _make_workspace("T-1", "DEV", previous_state="ANALYSIS")
        handler = CommandHandler(
            intent_parser=AsyncMock(),
            notifier=notifier,
            mode_handler=MagicMock(get_mode=MagicMock(return_value="manual")),
            active_workspaces_fn=lambda: [ws],
        )

        await handler.handle_callback("approve", "T-1", "12345", 42)

        # Should not transition — workspace is no longer in AWAITING_APPROVAL
        ws.transition.assert_not_called()
        call_args = notifier.send_message.call_args[0]
        assert "No workspace" in call_args[1]
```

- [ ] **Step 2: Run integration test**

Run: `python -m pytest tests/unit/test_telegram_buttons_integration.py -v`
Expected: ALL PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_telegram_buttons_integration.py
git commit -m "test: add integration tests for inline button callback flow"
```

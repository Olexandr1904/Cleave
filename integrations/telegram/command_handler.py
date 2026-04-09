"""Command handler — dispatches parsed intents to handler modules."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from integrations.telegram.handlers.approval import ApprovalHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.handlers.status import StatusHandler
from integrations.telegram.intent_parser import IntentParser, ParsedIntent

logger = logging.getLogger(__name__)


class CommandHandler:
    """Routes incoming Telegram messages to the appropriate handler."""

    def __init__(
        self,
        intent_parser: IntentParser,
        notifier: Any,
        mode_handler: ModeHandler,
        active_workspaces_fn: Callable[[], list[Any]],
        jira_base_url: str = "",
        started_at: str = "",
        tracker: Any | None = None,
        analyze_callback: Callable | None = None,
        recent_completions_fn: Callable[[], list[tuple[str, str, float]]] | None = None,
        allowed_chat_ids: set[str] | None = None,
    ) -> None:
        self._intent_parser = intent_parser
        self._notifier = notifier
        self._mode_handler = mode_handler
        self._active_workspaces_fn = active_workspaces_fn
        self._status_handler = StatusHandler(jira_base_url=jira_base_url)
        self._approval_handler = ApprovalHandler()
        self._started_at = started_at
        self._tracker = tracker
        self._analyze_callback = analyze_callback
        self._recent_completions_fn = recent_completions_fn
        # None or empty set disables the allowlist — use an empty set to block
        # nobody only if you explicitly want open access. Normal operation
        # should pass the operator's chat_id(s).
        self._allowed_chat_ids = allowed_chat_ids
        self._last_poll_time: float = time.time()

    def update_last_poll_time(self) -> None:
        """Called by orchestrator after each Jira poll."""
        self._last_poll_time = time.time()

    async def handle_message(self, text: str, chat_id: str) -> None:
        """Process an incoming Telegram message."""
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            logger.warning(
                "Rejecting command from unauthorized chat_id=%s", chat_id,
            )
            return
        workspaces = self._active_workspaces_fn()
        context = self._build_context(workspaces)
        intent = await self._intent_parser.parse(text, context)
        logger.info("Parsed intent: %s (params=%s)", intent.intent, intent.params)

        if intent.intent == "status":
            await self._handle_status(intent, chat_id, workspaces)
        elif intent.intent == "set_mode":
            await self._handle_set_mode(intent, chat_id)
        elif intent.intent == "approve":
            await self._handle_approve(intent, chat_id, workspaces)
        elif intent.intent == "reject":
            await self._handle_reject(intent, chat_id, workspaces)
        elif intent.intent == "analyze":
            await self._handle_analyze(intent, chat_id, workspaces)
        elif intent.intent == "error":
            await self._notifier.send_message(chat_id, intent.reply)
        else:
            await self._notifier.send_message(chat_id, intent.reply or
                "I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, switch modes.")

    def _build_context(self, workspaces: list[Any]) -> dict[str, Any]:
        awaiting = [
            f"{ws.state.ticket_id} ({ws.state.previous_state})"
            for ws in workspaces
            if ws.state.current_state == "AWAITING_APPROVAL"
        ]
        active = [
            f"{ws.state.ticket_id} — {ws.state.current_state}"
            for ws in workspaces
            if ws.state.current_state not in ("DONE", "FAILED", "ARCHIVED")
        ]
        return {
            "mode": self._mode_handler.get_mode(),
            "awaiting_approval": awaiting,
            "active_workspaces": active,
        }

    async def _handle_status(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any]) -> None:
        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            matches = [ws for ws in workspaces if ws.state.ticket_id == ticket_id]
            if matches:
                msg = self._status_handler.format_drill_down(matches[0])
            else:
                msg = f"No active workspace found for {ticket_id}."
        else:
            now = time.time()
            uptime = 0.0
            if self._started_at:
                try:
                    start = datetime.fromisoformat(self._started_at)
                    uptime = (datetime.now(timezone.utc) - start).total_seconds()
                except (ValueError, TypeError):
                    pass
            poll_ago = now - self._last_poll_time
            active = [ws for ws in workspaces if ws.state.current_state not in ("DONE", "FAILED", "ARCHIVED")]
            recent = self._recent_completions_fn() if self._recent_completions_fn else []
            msg = self._status_handler.format_summary(
                mode=self._mode_handler.get_mode(),
                uptime_seconds=uptime,
                last_poll_ago_seconds=poll_ago,
                active_workspaces=active,
                recent_completions=recent,
            )
        await self._notifier.send_message(chat_id, msg)

    async def _handle_set_mode(self, intent: ParsedIntent, chat_id: str) -> None:
        mode = intent.params.get("mode", "")
        try:
            self._mode_handler.set_mode(mode)
            await self._notifier.send_message(chat_id, intent.reply or f"Switched to {mode} mode.")
        except ValueError as e:
            await self._notifier.send_message(chat_id, str(e))

    async def _handle_approve(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any]) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)
        if not awaiting:
            await self._notifier.send_message(chat_id, "No workspaces awaiting approval.")
            return
        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._notifier.send_message(chat_id, f"Multiple workspaces awaiting approval: {tickets}. Please specify which one.")
            return
        ws = awaiting[0]
        next_state = self._approval_handler.resolve_next_state(ws)
        ws.transition(next_state)
        await self._notifier.send_message(chat_id, intent.reply or f"Approved {ws.state.ticket_id}. Moving to {next_state}.")

    async def _handle_reject(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any]) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)
        if not awaiting:
            await self._notifier.send_message(chat_id, "No workspaces awaiting approval.")
            return
        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._notifier.send_message(
                chat_id,
                f"Multiple workspaces awaiting approval: {tickets}. Please specify which one to reject.",
            )
            return
        ws = awaiting[0]
        ws.transition("FAILED")
        ws.update_state(error="Rejected by operator via Telegram")
        await self._notifier.send_message(chat_id, intent.reply or f"Rejected {ws.state.ticket_id}. Marked as FAILED.")

    async def _handle_analyze(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any]) -> None:
        ticket_ids = intent.params.get("ticket_ids", [])
        if not ticket_ids:
            await self._notifier.send_message(chat_id, "Please specify ticket IDs to analyze.")
            return
        if not self._analyze_callback:
            await self._notifier.send_message(
                chat_id,
                "Analyze is not available — no orchestrator callback configured.",
            )
            return
        try:
            result = await self._analyze_callback(ticket_ids)
        except Exception as e:
            logger.error("analyze_callback failed: %s", e, exc_info=True)
            await self._notifier.send_message(chat_id, f"Analyze failed: {e}")
            return
        # result is expected to be dict with keys: valid (list of ticket IDs),
        # invalid (list of "TICKET: reason" strings). Missing keys default to [].
        valid = list(result.get("valid", [])) if isinstance(result, dict) else []
        invalid = list(result.get("invalid", [])) if isinstance(result, dict) else []
        lines: list[str] = []
        if valid:
            lines.append(f"Queued {len(valid)} ticket(s) for analysis: {', '.join(valid)}")
        if invalid:
            lines.append("Could not queue:")
            for entry in invalid:
                lines.append(f"  - {entry}")
        if not lines:
            lines.append(intent.reply or "No tickets were queued.")
        await self._notifier.send_message(chat_id, "\n".join(lines))

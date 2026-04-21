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
        event_bus: Any | None = None,
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
        self._events = event_bus
        self._last_poll_time: float = time.time()
        # ticket_id -> (unblocked_at, resumed_state) for the last few minutes,
        # so a second answer arriving after we already started can be acknowledged.
        self._recently_unblocked: dict[str, tuple[float, str]] = {}

    def update_last_poll_time(self) -> None:
        """Called by orchestrator after each Jira poll."""
        self._last_poll_time = time.time()

    def set_tracker(self, tracker) -> None:
        """Attach a tracker after init (used by wizard hot-reload)."""
        self._tracker = tracker

    def add_allowed_chat_id(self, chat_id: str) -> None:
        """Extend the chat allowlist with a new id.

        No-op if the startup allowlist was None ('admit all' semantic preserved).
        """
        if self._allowed_chat_ids is None:
            return
        self._allowed_chat_ids.add(chat_id)

    async def _reply(self, chat_id: str, text: str, processing_msg_id: int | None) -> None:
        """Send response: edit the processing indicator if present, else send new."""
        if processing_msg_id and hasattr(self._notifier, "edit_message"):
            try:
                await self._notifier.edit_message(chat_id, processing_msg_id, text)
                return
            except Exception:
                logger.debug("Failed to edit processing indicator, sending new message", exc_info=True)
        await self._notifier.send_message(chat_id, text)

    async def handle_message(self, text: str, chat_id: str) -> None:
        """Process an incoming Telegram message."""
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            logger.warning(
                "Rejecting command from unauthorized chat_id=%s", chat_id,
            )
            return
        # Send a visible processing message so the user knows we're working
        processing_msg_id = None
        preview = text[:100].replace("\n", " ")
        try:
            processing_msg_id = await self._notifier.send_message(
                chat_id, f"\u2699\ufe0f Processing... ({preview})",
            )
        except Exception:
            logger.debug("Failed to send processing indicator", exc_info=True)

        workspaces = self._active_workspaces_fn()
        context = self._build_context(workspaces)
        intent = await self._intent_parser.parse(text, context)
        logger.info("Parsed intent: %s (params=%s)", intent.intent, intent.params)
        if self._events:
            self._events.emit("intent_parsed", f"Intent: {intent.intent} (params={intent.params})", data={"intent": intent.intent, "params": intent.params, "raw_text": text[:200]})

        if intent.intent == "status":
            await self._handle_status(intent, chat_id, workspaces, processing_msg_id)
        elif intent.intent == "set_mode":
            await self._handle_set_mode(intent, chat_id, processing_msg_id)
        elif intent.intent == "approve":
            await self._handle_approve(intent, chat_id, workspaces, processing_msg_id)
        elif intent.intent == "reject":
            await self._handle_reject(intent, chat_id, workspaces, processing_msg_id)
        elif intent.intent == "analyze":
            await self._handle_analyze(intent, chat_id, workspaces, processing_msg_id)
        elif intent.intent == "retry":
            await self._handle_retry(intent, chat_id, workspaces, processing_msg_id)
        elif intent.intent == "provide_input":
            await self._handle_provide_input(intent, chat_id, workspaces, processing_msg_id)
        elif intent.intent == "error":
            await self._reply(chat_id, intent.reply, processing_msg_id)
        else:
            await self._reply(chat_id, intent.reply or
                "I didn't understand that. I can do: status, analyze, approve, reject, retry, set_mode.", processing_msg_id)

    def _build_context(self, workspaces: list[Any]) -> dict[str, Any]:
        awaiting = [
            f"{ws.state.ticket_id} ({ws.state.previous_state})"
            for ws in workspaces
            if ws.state.current_state == "AWAITING_APPROVAL"
        ]
        blocked = [
            f"{ws.state.ticket_id} ({ws.state.previous_state or 'unknown'})"
            for ws in workspaces
            if ws.state.current_state == "BLOCKED"
        ]
        deferred = [
            f"{ws.state.ticket_id} ({ws.state.previous_state or 'unknown'}, retry at {ws.state.retry_at or '?'})"
            for ws in workspaces
            if ws.state.current_state == "DEFERRED"
        ]
        active = [
            f"{ws.state.ticket_id} — {ws.state.current_state}"
            for ws in workspaces
            if ws.state.current_state not in ("DONE", "FAILED", "ARCHIVED")
        ]
        return {
            "mode": self._mode_handler.get_mode(),
            "awaiting_approval": awaiting,
            "blocked_workspaces": blocked,
            "deferred_workspaces": deferred,
            "active_workspaces": active,
        }

    async def _handle_status(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
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
        await self._reply(chat_id, msg, processing_msg_id)

    async def _handle_set_mode(self, intent: ParsedIntent, chat_id: str, processing_msg_id: int | None = None) -> None:
        mode = intent.params.get("mode", "")
        try:
            self._mode_handler.set_mode(mode)
            await self._reply(chat_id, intent.reply or f"Switched to {mode} mode.", processing_msg_id)
        except ValueError as e:
            await self._reply(chat_id, str(e), processing_msg_id)

    async def _handle_approve(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)
        if not awaiting:
            await self._reply(chat_id, "No workspaces awaiting approval.", processing_msg_id)
            return
        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._reply(chat_id, f"Multiple workspaces awaiting approval: {tickets}. Please specify which one.", processing_msg_id)
            return
        ws = awaiting[0]
        next_state = self._approval_handler.resolve_next_state(ws)
        ws.transition(next_state)
        await self._reply(chat_id, intent.reply or f"Approved {ws.state.ticket_id}. Moving to {next_state}.", processing_msg_id)

    async def _handle_reject(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)
        if not awaiting:
            await self._reply(chat_id, "No workspaces awaiting approval.", processing_msg_id)
            return
        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._reply(
                chat_id,
                f"Multiple workspaces awaiting approval: {tickets}. Please specify which one to reject.",
                processing_msg_id,
            )
            return
        ws = awaiting[0]
        ws.transition("FAILED")
        ws.update_state(error="Rejected by operator via Telegram")
        await self._reply(chat_id, intent.reply or f"Rejected {ws.state.ticket_id}. Marked as FAILED.", processing_msg_id)

    async def _handle_analyze(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_ids = intent.params.get("ticket_ids", [])
        if not ticket_ids:
            await self._reply(chat_id, "Please specify ticket IDs to analyze.", processing_msg_id)
            return
        if not self._analyze_callback:
            await self._reply(
                chat_id,
                "Analyze is not available — no orchestrator callback configured.",
                processing_msg_id,
            )
            return

        try:
            result = await self._analyze_callback(ticket_ids)
        except Exception as e:
            logger.error("analyze_callback failed: %s", e, exc_info=True)
            await self._reply(chat_id, f"Analyze failed: {e}", processing_msg_id)
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
        await self._reply(chat_id, "\n".join(lines), processing_msg_id)

    _VALID_RETRY_STATES = {
        "analysis": "ANALYSIS",
        "dev": "DEV",
        "scope_check": "SCOPE_CHECK",
        "qa": "QA",
        "push": "PUSHED",
    }

    async def _handle_retry(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        if not ticket_id:
            await self._reply(chat_id, "Please specify a ticket ID to retry.", processing_msg_id)
            return

        matches = [ws for ws in workspaces if ws.state.ticket_id == ticket_id]
        if not matches:
            await self._reply(chat_id, f"No active workspace found for {ticket_id}.", processing_msg_id)
            return

        ws = matches[0]
        from_stage = intent.params.get("from_stage")

        if from_stage:
            target_state = self._VALID_RETRY_STATES.get(from_stage)
            if not target_state:
                valid = ", ".join(self._VALID_RETRY_STATES.keys())
                await self._reply(
                    chat_id, f"Unknown stage '{from_stage}'. Valid stages: {valid}",
                    processing_msg_id,
                )
                return
        else:
            # Default: re-run from the stage where it got stuck
            prev = ws.state.previous_state or ws.state.current_state
            target_state = self._VALID_RETRY_STATES.get(prev.lower(), prev)
            # If blocked/failed/deferred, use previous_state
            if ws.state.current_state in ("BLOCKED", "FAILED", "DEFERRED"):
                target_state = ws.state.previous_state or "ANALYSIS"

        ws.state.human_input_pending = False
        ws.state.error = None
        ws.transition(target_state)
        ws.save_state()

        await self._reply(
            chat_id,
            f"Retrying {ticket_id} from {target_state}.",
            processing_msg_id,
        )
        logger.info("Retry %s -> %s", ticket_id, target_state)

    async def _handle_provide_input(
        self,
        intent: ParsedIntent,
        chat_id: str,
        workspaces: list[Any],
        processing_msg_id: int | None = None,
    ) -> None:
        """Resume a BLOCKED workspace with free-text input the user typed."""
        blocked = [ws for ws in workspaces if ws.state.current_state == "BLOCKED"]
        if not blocked:
            recent = self._format_recent_unblock_notice()
            if recent:
                await self._reply(
                    chat_id,
                    f"{recent} Your new message was NOT added — that input is already being processed. "
                    f"If the pipeline blocks again with a new question, you can answer it then, "
                    f"or use `retry <ticket>` to re-run the stage.",
                    processing_msg_id,
                )
            else:
                await self._reply(
                    chat_id,
                    "No blocked workspaces are waiting for input.",
                    processing_msg_id,
                )
            return

        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            target = next((ws for ws in blocked if ws.state.ticket_id == ticket_id), None)
            if not target:
                await self._reply(
                    chat_id, f"No blocked workspace found for {ticket_id}.", processing_msg_id,
                )
                return
        elif len(blocked) == 1:
            target = blocked[0]
        else:
            tickets = ", ".join(ws.state.ticket_id for ws in blocked)
            await self._reply(
                chat_id,
                f"Multiple blocked workspaces: {tickets}. Please prefix your answer with the ticket ID.",
                processing_msg_id,
            )
            return

        input_text = intent.params.get("input_text", "").strip()
        if not input_text:
            await self._reply(chat_id, "Empty input — nothing to resume with.", processing_msg_id)
            return

        target.state.human_input_reply = input_text
        target.state.human_input_pending = False
        resume_state = target.state.previous_state or "ANALYSIS"
        target.transition(resume_state)
        target.save_state()
        self._recently_unblocked[target.state.ticket_id] = (time.time(), resume_state)

        await self._reply(
            chat_id,
            intent.reply or f"Got it. Resuming {target.state.ticket_id} from {resume_state} with your input.",
            processing_msg_id,
        )
        logger.info(
            "Unblocked %s via provide_input -> %s", target.state.ticket_id, resume_state,
        )

    def _format_recent_unblock_notice(self, window_seconds: float = 300.0) -> str | None:
        """Return a human-readable note about recently-unblocked tickets, or None."""
        now = time.time()
        # Drop stale entries
        self._recently_unblocked = {
            t: (ts, st) for t, (ts, st) in self._recently_unblocked.items()
            if now - ts < window_seconds
        }
        if not self._recently_unblocked:
            return None
        parts = [
            f"{ticket} (resumed from {state}, {int(now - ts)}s ago)"
            for ticket, (ts, state) in self._recently_unblocked.items()
        ]
        return f"Already started: {', '.join(parts)}."

    async def handle_reply(self, reply_to_msg_id: int, text: str, chat_id: str) -> bool:
        """Handle a reply to an escalation message. Returns True if matched."""
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            return False
        workspaces = self._active_workspaces_fn()
        for ws in workspaces:
            if (
                ws.state.current_state == "BLOCKED"
                and ws.state.escalation_msg_id == reply_to_msg_id
            ):
                ws.state.human_input_reply = text
                ws.state.human_input_pending = False
                resume_state = ws.state.previous_state or "ANALYSIS"
                ws.transition(resume_state)
                ws.save_state()
                self._recently_unblocked[ws.state.ticket_id] = (time.time(), resume_state)
                await self._notifier.send_message(
                    chat_id,
                    f"Got it. Resuming {ws.state.ticket_id} from {resume_state} with your input.",
                )
                logger.info(
                    "Unblocked %s via reply to msg %d",
                    ws.state.ticket_id, reply_to_msg_id,
                )
                return True
        return False

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

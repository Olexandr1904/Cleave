"""Command handler — dispatches parsed intents to handler modules."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from integrations.telegram.handlers.approval import ApprovalHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.handlers.status import StatusHandler
from integrations.telegram.intent_parser import IntentParser, ParsedIntent
from workspace.workspace import Stage

logger = logging.getLogger(__name__)


def _write_human_input(workspace: Any, text: str) -> None:
    """Write human reply to meta/human-input.md so the agent sees it on next run."""
    meta = Path(workspace.meta_dir) if hasattr(workspace, 'meta_dir') else Path(workspace.state.workspace_root) / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    path = meta / "human-input.md"
    # Append to preserve history of multiple replies
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"\n## Human input ({timestamp})\n\n{text}\n"
    path.write_text(existing + entry, encoding="utf-8")


def _ensure_msg_ids(c: dict) -> list[int]:
    """Lazy migration: old entries had 'msg_id', new ones have 'msg_ids'.

    Mutates c in place and returns the list.
    """
    if "msg_ids" in c:
        return c["msg_ids"]
    if "msg_id" in c:
        c["msg_ids"] = [c.pop("msg_id")]
        return c["msg_ids"]
    c["msg_ids"] = []
    return c["msg_ids"]


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
        elif intent.intent == "reviewed":
            await self._handle_reviewed(intent, chat_id, workspaces, processing_msg_id)
        else:
            await self._reply(chat_id, intent.reply or
                "I didn't understand that. I can do: status, analyze, approve, reject, retry, set_mode, reviewed.", processing_msg_id)

    def _build_context(self, workspaces: list[Any]) -> dict[str, Any]:
        awaiting = [
            f"{ws.state.ticket_id} ({ws.state.previous_state})"
            for ws in workspaces
            if ws.state.current_state == Stage.AWAITING_APPROVAL
        ]
        blocked = [
            f"{ws.state.ticket_id} ({ws.state.previous_state or 'unknown'})"
            for ws in workspaces
            if ws.state.current_state == Stage.BLOCKED
        ]
        deferred = [
            f"{ws.state.ticket_id} ({ws.state.previous_state or 'unknown'}, retry at {ws.state.retry_at or '?'})"
            for ws in workspaces
            if ws.state.current_state == Stage.DEFERRED
        ]
        active = [
            f"{ws.state.ticket_id} — {ws.state.current_state}"
            for ws in workspaces
            if ws.state.current_state not in (Stage.DONE, Stage.FAILED, Stage.ARCHIVED)
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
            active = [ws for ws in workspaces if ws.state.current_state not in (Stage.DONE, Stage.FAILED, Stage.ARCHIVED)]
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
        ws.transition(Stage.FAILED)
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
        "analysis": Stage.ANALYSIS,
        "dev": Stage.DEV,
        "scope_check": Stage.SCOPE_CHECK,
        "qa": Stage.QA,
        "push": Stage.PUSHED,
    }

    async def _handle_reviewed(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        """Signal that code review is complete for a PR_REVIEW workspace."""
        pr_workspaces = [ws for ws in workspaces if ws.state.current_state == Stage.PR_REVIEW]
        if not pr_workspaces:
            await self._reply(chat_id, "No workspaces in PR_REVIEW state.", processing_msg_id)
            return

        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            target = next((ws for ws in pr_workspaces if ws.state.ticket_id == ticket_id), None)
        elif len(pr_workspaces) == 1:
            target = pr_workspaces[0]
        else:
            ids = ", ".join(ws.state.ticket_id for ws in pr_workspaces)
            await self._reply(chat_id, f"Multiple PRs in review: {ids}. Specify: 'reviewed TICKET-ID'", processing_msg_id)
            return

        if not target:
            await self._reply(chat_id, f"Workspace {ticket_id} not found in PR_REVIEW.", processing_msg_id)
            return

        target.state.human_input_reply = "reviewed"
        target.save_state()
        await self._reply(
            chat_id,
            f"Got it. Fetching PR comments for {target.state.ticket_id} now.",
            processing_msg_id,
        )
        # Wake the orchestrator immediately
        if hasattr(self, '_wake_fn') and self._wake_fn:
            self._wake_fn()

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
            # Smart retry: detect furthest completed stage from artifacts
            reports = Path(ws.reports_dir) if hasattr(ws, 'reports_dir') else Path(ws.state.workspace_root) / "reports"
            if (reports / "qa-agent-output.md").exists():
                target_state = Stage.PUSHED
            elif (reports / "scope-guard-agent-output.md").exists():
                target_state = Stage.QA
            elif (reports / "dev-agent-output.md").exists():
                target_state = Stage.SCOPE_CHECK
            elif (reports / "ba.md").exists() or (reports / "ba-agent-output.md").exists():
                target_state = Stage.DEV
            else:
                target_state = ws.state.previous_state or Stage.ANALYSIS

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
        _write_human_input(target, input_text)
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

        # Check escalated PR comment replies first (pending_review_comments)
        for ws in workspaces:
            if ws.state.current_state != "PR_REVIEW":
                continue
            pending = ws.state.pending_review_comments or []
            for c in pending:
                if reply_to_msg_id in _ensure_msg_ids(c):
                    raw = text.strip().lower()
                    if raw in ("fix", "fxi", "fifx", "fixx", "fx", "fi", "yes", "fix it"):
                        decision = "fix"
                    elif raw.startswith("won't fix") or raw.startswith("wont fix"):
                        decision = raw
                    elif raw == "skip":
                        decision = "skip"
                    else:
                        # Free-text reply that doesn't match a recognized prefix.
                        # Stored verbatim; _execute_review_decisions falls
                        # through to "skip" semantics for these. Confirm
                        # explicitly so operators don't assume the bot
                        # interpreted their prose as fix/won't-fix.
                        decision = raw
                    c["decision"] = decision
                    ws.save_state()
                    file_line = f"{c.get('file','?')}:{c.get('line','?')}"
                    decision_label = {
                        "fix": "FIX (dev-agent will re-engage)",
                        "skip": "SKIP (drop, no action)",
                    }.get(decision)
                    if decision_label is None:
                        if decision.startswith("won't fix") or decision.startswith("wont fix"):
                            wf_prefix_len = len("won't fix") if decision.startswith("won't fix") else len("wont fix")
                            wf_reason = decision[wf_prefix_len:].lstrip(": ").strip() or "operator decision"
                            decision_label = f"WON'T FIX (will reply on GitHub: {wf_reason})"
                        else:
                            decision_label = f"SKIP (free-text reply, dropped: {decision[:60]!r})"
                    confirm = f"✓ Recorded {decision_label} for @{c.get('author','?')} on {file_line}"
                    if self._events is not None:
                        self._events.emit(
                            "pr_comment_decision_recorded",
                            f"{ws.state.ticket_id}: {decision} for comment {c.get('comment_id')}",
                            ticket_id=ws.state.ticket_id,
                            data={
                                "comment_id": c.get("comment_id"),
                                "decision": decision,
                                "via": "reply",
                                "raw_text": text.strip()[:200],
                            },
                        )
                    undecided = [x for x in pending if x.get("decision") is None]
                    if undecided:
                        await self._notifier.send_message(
                            chat_id,
                            f"{confirm}\n{len(undecided)} comment(s) remaining.",
                        )
                    else:
                        await self._notifier.send_message(
                            chat_id,
                            f"{confirm}\nAll decisions in for {ws.state.ticket_id}. Executing now.",
                        )
                        if hasattr(self, '_wake_fn') and self._wake_fn:
                            self._wake_fn()
                    return True

        for ws in workspaces:
            if ws.state.escalation_msg_id != reply_to_msg_id:
                continue

            # PR_REVIEW: user signals review is done — store reply and wake immediately
            if ws.state.current_state == "PR_REVIEW":
                ws.state.human_input_reply = text
                ws.save_state()
                await self._notifier.send_message(
                    chat_id,
                    f"Got it. Fetching PR comments for {ws.state.ticket_id} now.",
                )
                if hasattr(self, '_wake_fn') and self._wake_fn:
                    self._wake_fn()
                logger.info("PR review signal for %s via reply to msg %d", ws.state.ticket_id, reply_to_msg_id)
                return True

            # BLOCKED: resume or skip
            if ws.state.current_state == Stage.BLOCKED:
                reply_lower = text.strip().lower()

                if reply_lower == "skip":
                    # Advance past the blocked stage to the next one
                    _NEXT = {
                        Stage.ANALYSIS: Stage.DEV, Stage.DEV: Stage.SCOPE_CHECK,
                        Stage.SCOPE_CHECK: Stage.QA, Stage.QA: Stage.PUSHED,
                        Stage.PUSHED: Stage.PR_REVIEW, Stage.PR_REVIEW: Stage.DONE,
                    }
                    prev = ws.state.previous_state or Stage.ANALYSIS
                    resume_state = _NEXT.get(prev, Stage.DONE)
                    ws.state.human_input_pending = False
                    ws.state.error = None
                    ws.transition(resume_state)
                    ws.save_state()
                    await self._notifier.send_message(
                        chat_id,
                        f"Skipped {prev} for {ws.state.ticket_id}. Advanced to {resume_state}.",
                    )
                    if hasattr(self, '_wake_fn') and self._wake_fn:
                        self._wake_fn()
                    return True

                if reply_lower == "retry":
                    resume_state = ws.state.previous_state or Stage.ANALYSIS
                    ws.state.human_input_pending = False
                    ws.state.error = None
                    ws.transition(resume_state)
                    ws.save_state()
                    await self._notifier.send_message(
                        chat_id,
                        f"Retrying {resume_state} for {ws.state.ticket_id}.",
                    )
                    if hasattr(self, '_wake_fn') and self._wake_fn:
                        self._wake_fn()
                    return True

                # Default: provide input and resume
                ws.state.human_input_reply = text
                ws.state.human_input_pending = False
                _write_human_input(ws, text)
                resume_state = ws.state.previous_state or Stage.ANALYSIS
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
            target_state = ws.state.previous_state or Stage.ANALYSIS
            ws.transition(target_state)
            ws.save_state()
            await self._notifier.send_message(chat_id, f"Retrying {ticket_id} from {target_state}.", reply_to_message_id=message_id)
            if hasattr(self, '_wake_fn') and self._wake_fn:
                self._wake_fn()

        elif action == "skip":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No active workspace found for {ticket_id}.", reply_to_message_id=message_id)
                return
            _NEXT = {
                Stage.ANALYSIS: Stage.DEV, Stage.DEV: Stage.SCOPE_CHECK,
                Stage.SCOPE_CHECK: Stage.QA, Stage.QA: Stage.PUSHED,
                Stage.PUSHED: Stage.PR_REVIEW, Stage.PR_REVIEW: Stage.DONE,
            }
            prev = ws.state.previous_state or ws.state.current_state
            target = _NEXT.get(prev, Stage.DONE)
            ws.state.human_input_pending = False
            ws.state.error = None
            ws.transition(target)
            ws.save_state()
            await self._notifier.send_message(chat_id, f"Skipped {prev} for {ticket_id}. Advanced to {target}.", reply_to_message_id=message_id)
            if hasattr(self, '_wake_fn') and self._wake_fn:
                self._wake_fn()

        elif action == "clear_gradle":
            from orchestrator.gradle_remediation import clear_gradle_transforms

            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id), None)
            if not ws:
                await self._notifier.send_message(
                    chat_id, f"No active workspace found for {ticket_id}.",
                    reply_to_message_id=message_id,
                )
                return
            try:
                freed = clear_gradle_transforms()
            except Exception as e:
                logger.exception("Gradle cache clear failed for %s", ticket_id)
                await self._notifier.send_message(
                    chat_id, f"Failed to clear Gradle cache: {e}",
                    reply_to_message_id=message_id,
                )
                return
            mb = freed / 1024 / 1024
            target = ws.state.previous_state or Stage.ANALYSIS
            ws.state.human_input_pending = False
            ws.state.error = None
            ws.transition(target)
            ws.save_state()
            if self._events is not None:
                self._events.emit(
                    "gradle_cache_cleared",
                    f"Cleared Gradle cache for {ticket_id} ({freed} bytes)",
                    ticket_id=ticket_id,
                    data={"bytes_freed": freed, "new_state": target},
                )
            await self._notifier.send_message(
                chat_id,
                f"Cleared {mb:.0f} MB of Gradle transforms cache. Retrying {ticket_id} from {target}.",
                reply_to_message_id=message_id,
            )
            if hasattr(self, '_wake_fn') and self._wake_fn:
                self._wake_fn()

        elif action in ("pr_fix", "pr_skip", "pr_wontfix"):
            # PR comment decision via button — ticket_id is "TICKET:COMMENT_ID"
            parts = ticket_id.split(":", 1)
            tid = parts[0]
            comment_id_str = parts[1] if len(parts) > 1 else ""
            ws = next((w for w in workspaces if w.state.ticket_id == tid), None)
            if not ws or not ws.state.pending_review_comments:
                await self._notifier.send_message(chat_id, f"No pending comments for {tid}.", reply_to_message_id=message_id)
                return

            decision_map = {"pr_fix": "fix", "pr_skip": "skip", "pr_wontfix": "won't fix: operator decision"}
            decision = decision_map[action]

            matched_comment = None
            for c in ws.state.pending_review_comments:
                if str(c.get("comment_id")) == comment_id_str:
                    c["decision"] = decision
                    matched_comment = c
                    break

            if matched_comment is None:
                await self._notifier.send_message(chat_id, f"Comment not found.", reply_to_message_id=message_id)
                return

            ws.save_state()
            # Echo what got recorded so operators see exactly what happened —
            # button taps in TG are silent unless we explicitly confirm.
            file_line = f"{matched_comment.get('file','?')}:{matched_comment.get('line','?')}"
            decision_label = {
                "fix": "FIX (dev-agent will re-engage)",
                "won't fix: operator decision": "WON'T FIX (will reply on GitHub)",
                "skip": "SKIP (drop, no action)",
            }.get(decision, decision.upper())
            confirm = f"✓ Recorded {decision_label} for @{matched_comment.get('author','?')} on {file_line}"
            if self._events is not None:
                self._events.emit(
                    "pr_comment_decision_recorded",
                    f"{tid}: {decision} for comment {comment_id_str}",
                    ticket_id=tid,
                    data={
                        "comment_id": comment_id_str,
                        "decision": decision,
                        "via": "button",
                    },
                )

            undecided = [x for x in ws.state.pending_review_comments if x.get("decision") is None]
            if undecided:
                msg_text = f"{confirm}\n{len(undecided)} comment(s) remaining."
            else:
                msg_text = f"{confirm}\nAll decisions in for {tid}. Executing now."
                if hasattr(self, '_wake_fn') and self._wake_fn:
                    self._wake_fn()
            await self._notifier.send_message(chat_id, msg_text, reply_to_message_id=message_id)

        else:
            await self._notifier.send_message(chat_id, f"Unknown action: {action}", reply_to_message_id=message_id)

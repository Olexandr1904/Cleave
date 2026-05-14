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
from orchestrator import tg_format
from orchestrator.constants import REPORT_BA, REPORT_DEV, REPORT_QA, REPORT_SCOPE_GUARD
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


_FIX_TOKENS = ("fix it", "fix", "fxi", "fifx", "fixx", "fx", "fi", "yes")
_WONT_FIX_TOKENS = (
    "won't fix", "wont fix", "do not fix", "don't fix", "dont fix",
    "not fix", "no fix",
)


def _classify_reply(text: str) -> tuple[str, str, str]:
    """Classify an operator reply into (decision, matched_token, wf_reason).

    decision ∈ {'fix', 'wont_fix', 'reinvestigate'}
    matched_token: empty for reinvestigate, else the canonical token used.
    wf_reason: only meaningful for 'wont_fix'; the text after ':' or whitespace.
    """
    raw = text.strip()
    lower = raw.lower()
    if not lower:
        return "reinvestigate", "", ""

    # Fix synonyms — must be exact match (no trailing reason)
    for tok in _FIX_TOKENS:
        if lower == tok:
            return "fix", tok, ""

    # Won't-fix synonyms — token at start, optional ':' or whitespace + reason
    for tok in _WONT_FIX_TOKENS:
        if lower == tok:
            return "wont_fix", tok, ""
        if lower.startswith(tok):
            sep_char = lower[len(tok)] if len(lower) > len(tok) else ""
            if sep_char in (":", " ", "\t"):
                # Extract reason from RAW text to preserve case
                rest_raw = raw[len(tok):].lstrip(": ").strip()
                return "wont_fix", tok, rest_raw

    return "reinvestigate", "", ""


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
        get_trackers: Callable[[], dict[str, Any]] | None = None,
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
        # Resolver: callers inject a callable that returns the current tracker dict.
        # The legacy `tracker` kwarg is wrapped for back-compat.
        if get_trackers is not None:
            self._get_trackers = get_trackers
        elif tracker is not None:
            self._get_trackers = lambda: {"_legacy": tracker}
        else:
            self._get_trackers = lambda: {}
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

    def set_trackers_resolver(self, get_trackers: Callable[[], dict[str, Any]]) -> None:
        """Set a callable resolver that returns the current tracker dict."""
        self._get_trackers = get_trackers

    def set_tracker(self, tracker) -> None:
        """Deprecated: use set_trackers_resolver instead. Wraps a single tracker
        in a resolver for back-compat."""
        if tracker is None:
            self._get_trackers = lambda: {}
        else:
            self._get_trackers = lambda: {"_legacy": tracker}

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
        elif intent.intent == "unanswered":
            await self._handle_unanswered(intent, chat_id, processing_msg_id)
            return
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
            if (reports / REPORT_QA).exists():
                target_state = Stage.PUSHED
            elif (reports / REPORT_SCOPE_GUARD).exists():
                target_state = Stage.QA
            elif (reports / REPORT_DEV).exists():
                target_state = Stage.SCOPE_CHECK
            elif (reports / REPORT_BA).exists():
                target_state = Stage.DEV
            else:
                target_state = ws.state.previous_state or Stage.ANALYSIS

        ws.state.human_input_pending = False
        ws.state.error = None
        # Clear iteration counter for the target stage so the cap check doesn't
        # immediately re-escalate without running the agent.
        stage_key = {v: k for k, v in self._VALID_RETRY_STATES.items()}.get(target_state)
        if stage_key:
            ws.state.stage_iterations[stage_key] = 0
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
        blocked = [ws for ws in workspaces if ws.state.current_state == Stage.BLOCKED]
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
                if reply_to_msg_id not in _ensure_msg_ids(c):
                    continue
                decision, matched_token, wf_reason = _classify_reply(text)

                if decision == "reinvestigate":
                    return await self._stage_reinvestigation(c, ws, text.strip(), chat_id)

                file_line = f"{c.get('file','?')}:{c.get('line','?')}"
                if decision == "fix":
                    c["decision"] = "fix"
                    decision_label = f"FIX (matched: {matched_token!r}). Dev-agent will re-engage on {file_line}."
                    recorded_label = "FIX"
                    stored_decision = "fix"
                else:  # wont_fix
                    reason_text = wf_reason or "operator decision"
                    c["decision"] = f"won't fix: {reason_text}"
                    decision_label = (
                        f"WON'T FIX (matched: {matched_token!r}). "
                        f'Posting on GitHub: "{reason_text}".'
                    )
                    recorded_label = "WON'T FIX"
                    stored_decision = c["decision"]

                ws.save_state()
                _title = tg_format.read_ticket_title(ws)
                _emoji = "✅" if decision == "fix" else "❌"
                _hdr = tg_format.tg_header(_emoji, ws.state.company_id, ws.state.ticket_id, _title)
                confirm = f"{_hdr}\n✓ Recognized as {recorded_label}. {decision_label}"
                if self._events is not None:
                    self._events.emit(
                        "pr_comment_decision_recorded",
                        f"{ws.state.ticket_id}: {stored_decision} for comment {c.get('comment_id')}",
                        ticket_id=ws.state.ticket_id,
                        data={
                            "comment_id": c.get("comment_id"),
                            "decision": stored_decision,
                            "via": "reply",
                            "matched_token": matched_token,
                            "raw_text": text.strip()[:200],
                        },
                    )
                undecided = [x for x in pending if x.get("decision") is None]
                if undecided:
                    from integrations.base.notifier import Button
                    # The button label's count reflects the count at send time. If the
                    # operator decides on N-1 comments before tapping it, the recall
                    # loops over live state and resends only the actual remaining ones —
                    # the label is a hint, not a guarantee.
                    btn = [Button(
                        label=f"Show {len(undecided)} unanswered",
                        action=f"unanswered:{ws.state.ticket_id}",
                    )]
                    await self._notifier.send_message(
                        chat_id, f"{confirm}\n{len(undecided)} comment(s) remaining.", buttons=btn,
                    )
                else:
                    await self._notifier.send_message(
                        chat_id, f"{confirm}\nAll decisions in for {ws.state.ticket_id}. Executing now.",
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
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id,
                    f"{_hdr}\nGot it. Fetching PR comments now.",
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
                    _title = tg_format.read_ticket_title(ws)
                    _hdr = tg_format.tg_header("⏭", ws.state.company_id, ws.state.ticket_id, _title)
                    await self._notifier.send_message(
                        chat_id,
                        f"{_hdr}\nSkipped {prev}. Advanced to {resume_state}.",
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
                    _title = tg_format.read_ticket_title(ws)
                    _hdr = tg_format.tg_header("🔄", ws.state.company_id, ws.state.ticket_id, _title)
                    await self._notifier.send_message(
                        chat_id,
                        f"{_hdr}\nRetrying {resume_state}.",
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
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("▶", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id,
                    f"{_hdr}\nResuming from {resume_state} with your input.",
                )
                logger.info(
                    "Unblocked %s via reply to msg %d",
                    ws.state.ticket_id, reply_to_msg_id,
                )
                return True
        return False

    async def _handle_unanswered(
        self, intent, chat_id: str, processing_msg_id: int | None, *, via: str = "command",
    ) -> None:
        """Re-send all undecided PR comments. Triggered by /unanswered intent."""
        from orchestrator.escalation_view import build_escalated_comment_message

        workspaces = self._active_workspaces_fn()
        target_id = (intent.params.get("ticket_id") or "").strip()
        matches = [
            w for w in workspaces
            if w.state.current_state == "PR_REVIEW"
            and w.state.pending_review_comments
            and (not target_id or w.state.ticket_id == target_id)
        ]
        if not matches:
            await self._reply(chat_id, "No tickets have unanswered PR comments.", processing_msg_id)
            return

        total = 0
        for ws in matches:
            ws_total = 0
            for c in ws.state.pending_review_comments:
                if c.get("decision") is not None:
                    continue
                text, buttons = build_escalated_comment_message(
                    ws.state, c, ws.state.pr_number,
                    ticket_title=c.get("ticket_title", ""),
                    recall=True,
                )
                new_msg_id = await self._notifier.send_message(chat_id, text, buttons=buttons)
                if new_msg_id:
                    _ensure_msg_ids(c).append(new_msg_id)
                    ws_total += 1
            ws.save_state()
            total += ws_total
            if self._events is not None:
                self._events.emit(
                    "pr_comments_unanswered_recalled",
                    f"{ws.state.ticket_id}: recalled {ws_total} unanswered comment(s)",
                    ticket_id=ws.state.ticket_id,
                    data={"ticket_id": ws.state.ticket_id, "count": ws_total, "via": via},
                )
        await self._reply(
            chat_id, f"Resent {total} unanswered comment(s).", processing_msg_id,
        )

    async def _stage_reinvestigation(self, c, ws, hint_text: str, chat_id: str) -> bool:
        """Stage a re-investigation request on the comment entry.

        Returns True (always handled, even when capped).
        """
        rounds = int(c.get("hint_rounds", 0) or 0)
        file_line = f"{c.get('file','?')}:{c.get('line','?')}"
        if rounds >= 3:
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("⚠️", ws.state.company_id, ws.state.ticket_id, _title)
            msg = (
                f"{_hdr}\n"
                f"Hint limit reached (3/3) for @{c.get('author','?')} on {file_line}. "
                f"Reply \"fix\" or \"won't fix: <reason>\" to close this comment."
            )
            await self._notifier.send_message(chat_id, msg)
            if self._events is not None:
                self._events.emit(
                    "pr_comment_hint_exhausted",
                    f"{ws.state.ticket_id}: hint cap reached for comment {c.get('comment_id')}",
                    ticket_id=ws.state.ticket_id,
                    data={
                        "comment_id": c.get("comment_id"),
                        "attempted_hint_excerpt": hint_text[:120],
                    },
                )
            return True

        c["last_hint"] = hint_text
        c["pending_reinvestigation"] = True
        ws.save_state()

        # rounds is the count of completed re-investigations. If a hint is
        # already staged but not yet processed by the orchestrator, sending a
        # second hint just overwrites last_hint — the round counter doesn't
        # tick until the orchestrator actually re-runs the classifier. This
        # means a rapid second hint shows the same round number, which is
        # correct: only one re-investigation will happen.
        next_round = rounds + 1
        _title = tg_format.read_ticket_title(ws)
        _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
        msg = (
            f"{_hdr}\n"
            f"Recognized as hint (round {next_round}/3). "
            f"Re-checking @{c.get('author','?')}'s comment on {file_line} with your context."
        )
        await self._notifier.send_message(chat_id, msg)
        if self._events is not None:
            self._events.emit(
                "pr_comment_reinvestigation_staged",
                f"{ws.state.ticket_id}: hint round {next_round} for comment {c.get('comment_id')}",
                ticket_id=ws.state.ticket_id,
                data={
                    "comment_id": c.get("comment_id"),
                    "hint_round": next_round,
                    "hint_excerpt": hint_text[:200],
                },
            )
        if hasattr(self, "_wake_fn") and self._wake_fn:
            self._wake_fn()
        return True

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
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("✅", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nApproved. Moving to {next_state}.", reply_to_message_id=message_id)

        elif action == "reject":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "AWAITING_APPROVAL"), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No workspace awaiting approval for {ticket_id}.", reply_to_message_id=message_id)
                return
            ws.transition("FAILED")
            ws.update_state(error="Rejected by operator via Telegram")
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("❌", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nRejected. Marked as FAILED.", reply_to_message_id=message_id)

        elif action == "reviewed":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "PR_REVIEW"), None)
            if not ws:
                await self._notifier.send_message(chat_id, f"No workspace in PR_REVIEW for {ticket_id}.", reply_to_message_id=message_id)
                return
            ws.state.human_input_reply = "reviewed"
            ws.save_state()
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nGot it. Fetching PR comments now.", reply_to_message_id=message_id)
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
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🔄", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\nRetrying from {target_state}.", reply_to_message_id=message_id)
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
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("⚠️", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id, f"{_hdr}\nFailed to clear Gradle cache: {e}",
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
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🧹", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(
                chat_id,
                f"{_hdr}\nCleared {mb:.0f} MB of Gradle transforms cache. Retrying from {target}.",
                reply_to_message_id=message_id,
            )
            if hasattr(self, '_wake_fn') and self._wake_fn:
                self._wake_fn()

        elif action == "unanswered":
            intent = ParsedIntent(intent="unanswered", params={"ticket_id": ticket_id}, reply="")
            await self._handle_unanswered(intent, chat_id, processing_msg_id=None, via="button")

        elif action in ("pr_fix", "pr_wontfix"):
            parts = ticket_id.split(":", 1)
            tid = parts[0]
            comment_id_str = parts[1] if len(parts) > 1 else ""
            ws = next((w for w in workspaces if w.state.ticket_id == tid), None)
            if not ws or not ws.state.pending_review_comments:
                await self._notifier.send_message(chat_id, f"No pending comments for {tid}.", reply_to_message_id=message_id)
                return

            matched_comment = None
            for c in ws.state.pending_review_comments:
                if str(c.get("comment_id")) == comment_id_str:
                    if action == "pr_fix":
                        c["decision"] = "fix"
                        stored_decision = "fix"
                        recorded_label = "FIX"
                        matched_token = "button:fix"
                        btn_label = "'Fix' button"
                        action_tail = "Dev-agent will re-engage"
                    else:
                        c["decision"] = "won't fix: operator decision"
                        stored_decision = c["decision"]
                        recorded_label = "WON'T FIX"
                        matched_token = "button:wontfix"
                        btn_label = "'Won't Fix' button"
                        action_tail = 'Posting on GitHub: "operator decision"'
                    matched_comment = c
                    break

            if matched_comment is None:
                await self._notifier.send_message(chat_id, "Comment not found.", reply_to_message_id=message_id)
                return

            ws.save_state()
            file_line = f"{matched_comment.get('file','?')}:{matched_comment.get('line','?')}"
            _title = tg_format.read_ticket_title(ws)
            _emoji = "✅" if action == "pr_fix" else "❌"
            _hdr = tg_format.tg_header(_emoji, ws.state.company_id, ws.state.ticket_id, _title)
            confirm = (
                f"{_hdr}\n"
                f"✓ Recognized as {recorded_label} (matched: {btn_label}). "
                f"{action_tail} on {file_line}."
            )
            if self._events is not None:
                self._events.emit(
                    "pr_comment_decision_recorded",
                    f"{tid}: {stored_decision} for comment {comment_id_str}",
                    ticket_id=tid,
                    data={
                        "comment_id": comment_id_str,
                        "decision": stored_decision,
                        "via": "button",
                        "matched_token": matched_token,
                    },
                )

            undecided = [x for x in ws.state.pending_review_comments if x.get("decision") is None]
            if undecided:
                from integrations.base.notifier import Button
                btn = [Button(
                    label=f"Show {len(undecided)} unanswered",
                    action=f"unanswered:{tid}",
                )]
                msg_text = f"{confirm}\n{len(undecided)} comment(s) remaining."
                await self._notifier.send_message(chat_id, msg_text, buttons=btn, reply_to_message_id=message_id)
            else:
                msg_text = f"{confirm}\nAll decisions in for {tid}. Executing now."
                if hasattr(self, '_wake_fn') and self._wake_fn:
                    self._wake_fn()
                await self._notifier.send_message(chat_id, msg_text, reply_to_message_id=message_id)

        else:
            await self._notifier.send_message(chat_id, f"Unknown action: {action}", reply_to_message_id=message_id)

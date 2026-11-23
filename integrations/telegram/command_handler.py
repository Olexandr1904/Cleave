"""Command handler — dispatches parsed intents to handler modules."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from integrations.telegram.handlers.approval import ApprovalHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.handlers.status import StatusHandler, status_buttons
from integrations.telegram.intent_parser import IntentParser, ParsedIntent, parse_fixed_intent
from integrations.telegram.switch_commands import (
    format_switch_status,
    parse_switch_command,
    resolve_scope,
)
from orchestrator import tg_format, ticket_actions
from orchestrator.reviewer.grammar import parse_reply
from orchestrator.skillflow.replies import SKILL_OFF
from orchestrator.switches import GLOBAL, switch_scopes
from orchestrator.tg_i18n import ENGLISH, N_, detail, telegram_language, tr
from integrations.base.notifier import Button
from workspace.workspace import Stage

logger = logging.getLogger(__name__)


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


_REVIEW_STORE_UNREAD = N_("Reply not handled: the reviewer's store could not be read; nothing was done.")
_REVIEWER_OFF = N_("Reviewer mode is off on this daemon: nothing was done.")
_REVIEWER_NOT_A_REPLY = N_("Reply to the reviewer's message to act on it; nothing was done.")
_SKILL_STORE_UNREAD = N_("Reply not handled: the skill-run store could not be read; nothing was done.")

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

    # The reviewer's replies (has_repos, is_anchor, handle_reply, handle_callback); None: no reviewer on this daemon.
    _reviewer: Any | None = None
    # The skill flow's replies (is_anchor, handle_reply, handle_callback, answer); None: no skill flow on this daemon.
    _skill: Any | None = None
    # Wakes the orchestrator's loop; main.py sets it. None: nothing to wake.
    _wake_fn: Callable[[], None] | None = None
    # The Telegram language of a project (its id; "" for the global one) and of a chat: a ticket's answers are in its
    # project's, the others in the chat's. English unless the daemon passes them.
    _language_of: Callable[[str], str] = staticmethod(lambda project_id: ENGLISH)
    _chat_language: Callable[[str], str] = staticmethod(lambda chat_id: ENGLISH)

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
        switches: Any | None = None,
        get_projects: Callable[[], dict[str, Any]] | None = None,
        reviewer: Any | None = None,
        reviewer_chat_ids: set[str] | None = None,
        skill: Any | None = None,
        language_of: Callable[[str], str] | None = None,
        chat_language: Callable[[str], str] | None = None,
    ) -> None:
        if language_of is not None:
            self._language_of = language_of
        if chat_language is not None:
            self._chat_language = chat_language
        self._switches = switches
        self._reviewer = reviewer
        self._skill = skill
        # The chats the reviewer's replies and taps are read in. Never "every chat": unlike allowed_chat_ids, none
        # (or an empty set) is no chat, so an empty allowlist leaves the reviewer off.
        self._reviewer_chat_ids: set[str] = set(reviewer_chat_ids or ())
        self._get_projects = get_projects or (lambda: {})
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
        """Extend the chat allowlist with a new id, and the reviewer's chats with it.

        The allowlist is unchanged if the startup allowlist was None ('admit all' semantic preserved).
        """
        self._reviewer_chat_ids.add(chat_id)
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
        """Process an incoming Telegram message, answered in the chat's language."""
        with telegram_language(self._chat_language(chat_id)):
            await self._handle_message(text, chat_id)

    async def _handle_message(self, text: str, chat_id: str) -> None:
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            logger.warning(
                "Rejecting command from unauthorized chat_id=%s", chat_id,
            )
            return
        command = parse_switch_command(text) if self._switches is not None else None
        if command is not None:
            await self._handle_switch_command(command, chat_id)
            return
        if self._is_reviewer_command(text):
            await self._notifier.send_message(chat_id, tr(_REVIEWER_NOT_A_REPLY))
            return
        fixed = parse_fixed_intent(text)
        if fixed is not None:  # no model turn and no "Processing..." line: it answers at once
            workspaces = self._active_workspaces_fn()
            if fixed.intent == "status":
                await self._handle_status(fixed, chat_id, workspaces)
            else:
                await self._handle_retry(fixed, chat_id, workspaces)
            return
        # Send a visible processing message so the user knows we're working
        processing_msg_id = None
        preview = text[:100].replace("\n", " ")
        try:
            processing_msg_id = await self._notifier.send_message(
                chat_id, tr("\u2699\ufe0f Processing... ({preview})", preview=preview),
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
                tr("I didn't understand that. I can do: status, analyze, approve, reject, retry, set_mode, reviewed."),
                processing_msg_id)

    def _is_reviewer_command(self, text: str) -> bool:
        """Whether a message, while a repo is in reviewer mode, is a reviewer command that names a review's numbers
        or is no bare event: sent without Telegram's Reply (or as a reply to a message that is no anchor), it would
        reach the intent parser, which reads "post 1 2 4 approve" as an approval of a ticket. It holds in every
        chat the ticket pipeline admits, not only the reviewer's: those forms are never ticket words. A bare
        "approve" (an event-only post) stays the ticket pipeline's, and with no repo reviewed nothing is held."""
        if self._reviewer is None or not self._reviewer.has_repos():
            return False
        cmd = parse_reply(text)
        if cmd is None:
            return False
        return bool(cmd.numbers) or cmd.all_confirmed or cmd.action != "post"

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

    @staticmethod
    def _resolve_key(key: str, workspaces: list[Any]) -> tuple[str, str]:
        """The active ticket a typed key names, and "" — or the key and why no ticket is chosen. A key names a
        ticket in any case, or by its number alone when exactly one ticket ends in it; a key that names none comes
        back upper-cased, for the "not found" answer."""
        keys = [ws.state.ticket_id for ws in workspaces]
        if exact := next((k for k in keys if k.lower() == key.lower()), None):
            return exact, ""
        if key.isdigit():
            ending = [k for k in keys if k.rpartition("-")[2] == key]
            if len(ending) == 1:
                return ending[0], ""
            if ending:
                return key, tr("Several tickets end in {number}: {keys}. Send the full key.", number=key,
                               keys=", ".join(ending))
        return key.upper(), ""

    async def _handle_status(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            ticket_id, ambiguous = self._resolve_key(ticket_id, workspaces)
            if ambiguous:
                await self._reply(chat_id, ambiguous, processing_msg_id)
                return
            matches = [ws for ws in workspaces if ws.state.ticket_id == ticket_id]
            if matches:
                msg = self._status_handler.format_drill_down(matches[0])
            else:
                msg = tr("No active workspace found for {ticket_id}.", ticket_id=ticket_id)
            buttons = status_buttons(matches[:1])
        else:
            msg = self._summary_text(workspaces)
            buttons = status_buttons(self._listed(workspaces))
            if self._switches is not None:
                scopes = switch_scopes(self._get_projects())
                msg = format_switch_status(self._switches.snapshot(scopes)) + "\n\n" + msg
        await self._send_panel(chat_id, msg, buttons, processing_msg_id)

    async def _send_panel(self, chat_id: str, text: str, buttons: list[Any], processing_msg_id: int | None) -> None:
        """`text` with `buttons`. An edit of the "Processing..." line carries no buttons: with buttons that line is
        deleted and the text sent anew."""
        if not buttons:
            await self._reply(chat_id, text, processing_msg_id)
            return
        if processing_msg_id and hasattr(self._notifier, "delete_message"):
            try:
                await self._notifier.delete_message(chat_id, processing_msg_id)
            except Exception:
                logger.debug("Failed to delete processing indicator", exc_info=True)
        await self._notifier.send_message(chat_id, text, buttons=buttons)

    @staticmethod
    def _listed(workspaces: list[Any]) -> list[Any]:
        """The tickets the summary lists: a failed one too, which waits for its Retry or Skip."""
        return [ws for ws in workspaces if ws.state.current_state not in (Stage.DONE, Stage.ARCHIVED)]

    def _summary_text(self, workspaces: list[Any]) -> str:
        now = time.time()
        uptime = 0.0
        if self._started_at:
            try:
                start = datetime.fromisoformat(self._started_at)
                uptime = (datetime.now(timezone.utc) - start).total_seconds()
            except (ValueError, TypeError):
                pass
        poll_ago = now - self._last_poll_time
        active = self._listed(workspaces)
        recent = self._recent_completions_fn() if self._recent_completions_fn else []
        return self._status_handler.format_summary(
            mode=self._mode_handler.get_mode(),
            uptime_seconds=uptime,
            last_poll_ago_seconds=poll_ago,
            active_workspaces=active,
            recent_completions=recent,
        )

    async def _handle_switch_command(self, command: Any, chat_id: str) -> None:
        projects = self._get_projects()
        scopes = switch_scopes(projects)
        if command.kind == "status":
            await self._handle_status(ParsedIntent("status", {}), chat_id, self._active_workspaces_fn())
            return
        scope = GLOBAL
        if command.repo:
            scope, error = resolve_scope(command.repo, scopes[command.mode])
            if scope is None and command.mode == "reviews":
                other, other_error = resolve_scope(command.repo, scopes["tickets"])
                error = (tr("Reviews are not enabled for {scope} (reviewer.enabled in YAML)", scope=other)
                         if other else other_error)
            if scope is None:
                await self._notifier.send_message(chat_id, error)
                return
        self._switches.set(command.mode, command.state, scope, by="telegram")
        await self._notifier.send_message(chat_id, format_switch_status(self._switches.snapshot(scopes)))

    async def _handle_set_mode(self, intent: ParsedIntent, chat_id: str, processing_msg_id: int | None = None) -> None:
        mode = intent.params.get("mode", "")
        try:
            self._mode_handler.set_mode(mode)
            await self._reply(chat_id, intent.reply or tr("Switched to {mode} mode.", mode=mode), processing_msg_id)
        except ValueError as e:
            await self._reply(chat_id, detail(str(e)), processing_msg_id)

    async def _handle_approve(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)
        if not awaiting:
            await self._reply(chat_id, tr("No workspaces awaiting approval."), processing_msg_id)
            return
        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._reply(
                chat_id,
                tr("Multiple workspaces awaiting approval: {tickets}. Please specify which one.", tickets=tickets),
                processing_msg_id,
            )
            return
        ws = awaiting[0]
        result = ticket_actions.approve(ws, wake=self._wake_fn)
        await self._reply(
            chat_id,
            intent.reply or tr("Approved {ticket_id}. Moving to {state}.", ticket_id=ws.state.ticket_id,
                               state=result.state),
            processing_msg_id,
        )

    async def _handle_reject(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        awaiting = self._approval_handler.find_awaiting(workspaces, ticket_id)
        if not awaiting:
            await self._reply(chat_id, tr("No workspaces awaiting approval."), processing_msg_id)
            return
        if len(awaiting) > 1 and not ticket_id:
            tickets = ", ".join(ws.state.ticket_id for ws in awaiting)
            await self._reply(
                chat_id,
                tr("Multiple workspaces awaiting approval: {tickets}. Please specify which one to reject.",
                   tickets=tickets),
                processing_msg_id,
            )
            return
        ws = awaiting[0]
        result = ticket_actions.reject(ws, via="Telegram", wake=self._wake_fn)
        if result.state == Stage.FAILED:
            outcome = tr("Rejected {ticket_id}. Marked as FAILED.", ticket_id=ws.state.ticket_id)
        else:
            outcome = tr("Rejected {ticket_id}. Back to {state}.", ticket_id=ws.state.ticket_id, state=result.state)
        await self._reply(chat_id, intent.reply or outcome, processing_msg_id)

    async def _handle_analyze(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_ids = intent.params.get("ticket_ids", [])
        if not ticket_ids:
            await self._reply(chat_id, tr("Please specify ticket IDs to analyze."), processing_msg_id)
            return
        if not self._analyze_callback:
            await self._reply(
                chat_id,
                tr("Analyze is not available — no orchestrator callback configured."),
                processing_msg_id,
            )
            return

        try:
            result = await self._analyze_callback(ticket_ids)
        except Exception as e:
            logger.error("analyze_callback failed: %s", e, exc_info=True)
            await self._reply(chat_id, tr("Analyze failed: {error}", error=detail(str(e))), processing_msg_id)
            return
        # result is expected to be dict with keys: valid (list of ticket IDs),
        # invalid (list of "TICKET: reason" strings). Missing keys default to [].
        valid = list(result.get("valid", [])) if isinstance(result, dict) else []
        invalid = list(result.get("invalid", [])) if isinstance(result, dict) else []
        lines: list[str] = []
        if valid:
            lines.append(tr("Queued {n} ticket(s) for analysis: {tickets}", n=len(valid), tickets=", ".join(valid)))
        if invalid:
            lines.append(tr("Could not queue:"))
            for entry in invalid:
                lines.append(f"  - {detail(entry)}")
        if not lines:
            lines.append(intent.reply or tr("No tickets were queued."))
        await self._reply(chat_id, "\n".join(lines), processing_msg_id)

    async def _handle_reviewed(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        """Signal that code review is complete for a PR_REVIEW workspace."""
        pr_workspaces = [ws for ws in workspaces if ws.state.current_state == Stage.PR_REVIEW]
        if not pr_workspaces:
            await self._reply(chat_id, tr("No workspaces in PR_REVIEW state."), processing_msg_id)
            return

        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            target = next((ws for ws in pr_workspaces if ws.state.ticket_id == ticket_id), None)
        elif len(pr_workspaces) == 1:
            target = pr_workspaces[0]
        else:
            ids = ", ".join(ws.state.ticket_id for ws in pr_workspaces)
            await self._reply(
                chat_id, tr("Multiple PRs in review: {ids}. Specify: 'reviewed TICKET-ID'", ids=ids),
                processing_msg_id,
            )
            return

        if not target:
            await self._reply(
                chat_id, tr("Workspace {ticket_id} not found in PR_REVIEW.", ticket_id=ticket_id), processing_msg_id,
            )
            return

        ticket_actions.reviewed(target, wake=self._wake_fn)
        await self._reply(
            chat_id,
            tr("Got it. Fetching PR comments for {ticket_id} now.", ticket_id=target.state.ticket_id),
            processing_msg_id,
        )

    async def _handle_retry(self, intent: ParsedIntent, chat_id: str, workspaces: list[Any], processing_msg_id: int | None = None) -> None:
        ticket_id = intent.params.get("ticket_id")
        if not ticket_id:
            await self._reply(chat_id, tr("Please specify a ticket ID to retry."), processing_msg_id)
            return
        ticket_id, ambiguous = self._resolve_key(ticket_id, workspaces)
        if ambiguous:
            await self._reply(chat_id, ambiguous, processing_msg_id)
            return

        matches = [ws for ws in workspaces if ws.state.ticket_id == ticket_id]
        if not matches:
            await self._reply(
                chat_id, tr("No active workspace found for {ticket_id}.", ticket_id=ticket_id), processing_msg_id,
            )
            return

        ws = matches[0]
        from_stage = intent.params.get("from_stage")
        result = ticket_actions.retry(ws, stage=from_stage, furthest=not from_stage, wake=self._wake_fn)
        if result.refused:
            await self._reply(chat_id, result.refused, processing_msg_id)
            return

        await self._reply(
            chat_id,
            tr("Retrying {ticket_id} from {state}.", ticket_id=ticket_id, state=result.state),
            processing_msg_id,
        )
        logger.info("Retry %s -> %s", ticket_id, result.state)

    async def _skip(self, ws: Any, chat_id: str, reply_to: int | None = None) -> None:
        """Advance a stopped ticket past the stage it stopped at, to the next one: the "skip" reply to an escalation
        and the Skip button. A ticket that runs again, or cannot go there from where it is now, is left as it is."""
        prev = ws.state.previous_state or Stage.ANALYSIS
        answer: dict[str, Any] = {"reply_to_message_id": reply_to} if reply_to else {}
        result = ticket_actions.skip(ws, wake=self._wake_fn)
        if result.refused:
            await self._notifier.send_message(chat_id, result.refused, **answer)
            return
        _title = tg_format.read_ticket_title(ws)
        _hdr = tg_format.tg_header("⏭", ws.state.company_id, ws.state.ticket_id, _title)
        await self._notifier.send_message(
            chat_id, f"{_hdr}\n" + tr("Skipped {stage}. Advanced to {state}.", stage=prev, state=result.state),
            **answer,
        )

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
                    tr("{recent} Your new message was NOT added — that input is already being processed. "
                       "If the pipeline blocks again with a new question, you can answer it then, "
                       "or use `retry <ticket>` to re-run the stage.", recent=recent),
                    processing_msg_id,
                )
            else:
                await self._reply(
                    chat_id,
                    tr("No blocked workspaces are waiting for input."),
                    processing_msg_id,
                )
            return

        ticket_id = intent.params.get("ticket_id")
        if ticket_id:
            target = next((ws for ws in blocked if ws.state.ticket_id == ticket_id), None)
            if not target:
                await self._reply(
                    chat_id, tr("No blocked workspace found for {ticket_id}.", ticket_id=ticket_id),
                    processing_msg_id,
                )
                return
        elif len(blocked) == 1:
            target = blocked[0]
        else:
            tickets = ", ".join(ws.state.ticket_id for ws in blocked)
            await self._reply(
                chat_id,
                tr("Multiple blocked workspaces: {tickets}. Please prefix your answer with the ticket ID.",
                   tickets=tickets),
                processing_msg_id,
            )
            return

        input_text = intent.params.get("input_text", "").strip()
        result = ticket_actions.answer(target, input_text, wake=self._wake_fn)
        if result.refused:
            await self._reply(chat_id, result.refused, processing_msg_id)
            return
        self._recently_unblocked[target.state.ticket_id] = (time.time(), result.state)

        await self._reply(
            chat_id,
            intent.reply or tr("Got it. Resuming {ticket_id} from {state} with your input.",
                               ticket_id=target.state.ticket_id, state=result.state),
            processing_msg_id,
        )
        logger.info(
            "Unblocked %s via provide_input -> %s", target.state.ticket_id, result.state,
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
            tr("{ticket} (resumed from {state}, {seconds}s ago)", ticket=ticket, state=state, seconds=int(now - ts))
            for ticket, (ts, state) in self._recently_unblocked.items()
        ]
        return tr("Already started: {tickets}.", tickets=", ".join(parts))

    async def handle_reply(
        self, reply_to_msg_id: int, text: str, chat_id: str, sent_at: datetime | None = None,
    ) -> bool:
        """Handle a reply to a skill run's message, a reviewer message or an escalation message. Returns True if
        matched.

        A reply to a skill run's message goes to the skill flow, with `sent_at` for its age limit, before anything
        else: the intent parser would read its "ok" as the approval of a ticket's gate. When the skill-run store
        cannot be read, the ticket-reply matching still runs, and a reply it does not match is answered and kept
        from the intent parser all the same.

        A reply in one of the reviewer's chats to a reviewer message goes to the reviewer, with `sent_at` (the
        reply's Telegram date) for its age limit. Such a reply never reaches the intent parser, which would read the
        message's hint ("post 1 2 4 approve") as an approval. When the reviewer's store cannot be read, the
        ticket-reply matching still runs; a reply it does not match is answered and kept from the intent parser
        all the same. In any other chat the reviewer does not act: a reply there to one of its messages is dropped
        without an answer, and so is one whose anchor lookup fails, so that it never reaches the intent parser.
        """
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            return False
        if self._skill is not None:
            try:
                skill_anchored = self._skill.is_anchor(chat_id, reply_to_msg_id)
            except Exception as e:
                logger.warning("Skill-run reply anchor not looked up: %s", type(e).__name__)
                if not await self._pipeline_reply(reply_to_msg_id, text, chat_id):
                    # A dead end, best effort: a reply to it is the skill flow's too; the store may be what failed.
                    with telegram_language(self._chat_language(chat_id)):
                        await self._skill.answer(chat_id, reply_to_msg_id, tr(_SKILL_STORE_UNREAD))
                return True
            if skill_anchored:
                await self._skill.handle_reply(chat_id, reply_to_msg_id, text, sent_at=sent_at)
                return True
        if self._reviewer is None:
            return await self._pipeline_reply(reply_to_msg_id, text, chat_id)
        if chat_id not in self._reviewer_chat_ids:
            if self._reviewer_anchor_or_unknown(chat_id, reply_to_msg_id):
                logger.warning(
                    "Reply to reviewer message %s dropped: chat %s is not one of the reviewer's",
                    reply_to_msg_id, chat_id,
                )
                return True
            return await self._pipeline_reply(reply_to_msg_id, text, chat_id)
        try:
            anchored = self._reviewer.is_anchor(chat_id, reply_to_msg_id)
        except Exception as e:
            logger.warning("Reply anchor not looked up: %s", type(e).__name__)
            if not await self._pipeline_reply(reply_to_msg_id, text, chat_id):
                with telegram_language(self._chat_language(chat_id)):
                    message_id = await self._notifier.send_message(chat_id, tr(_REVIEW_STORE_UNREAD))
                try:  # so a reply to the answer is the reviewer's too; the store may be what failed
                    self._reviewer.anchor_answer(chat_id, message_id)
                except Exception as anchor_error:
                    logger.warning("Reply-not-handled answer not anchored: %s", type(anchor_error).__name__)
            return True
        if not anchored:
            return await self._pipeline_reply(reply_to_msg_id, text, chat_id)
        await self._reviewer.handle_reply(chat_id, reply_to_msg_id, text, sent_at=sent_at)
        return True

    def _reviewer_anchor_or_unknown(self, chat_id: str, message_id: int) -> bool:
        """Whether the message is the reviewer's, or may be: a failed lookup counts as one (fails closed)."""
        try:
            return self._reviewer.is_anchor(chat_id, message_id)
        except Exception as e:
            logger.warning("Reply anchor not looked up: %s", type(e).__name__)
            return True

    async def _pipeline_reply(self, reply_to_msg_id: int, text: str, chat_id: str) -> bool:
        """Handle a reply to a ticket's escalation message, answered in the ticket's project's language. Returns
        True if matched."""
        ws = self._replied_workspace(reply_to_msg_id)
        language = self._language_of(ws.state.company_id) if ws is not None else self._chat_language(chat_id)
        with telegram_language(language):
            return await self._ticket_reply(reply_to_msg_id, text, chat_id)

    def _replied_workspace(self, reply_to_msg_id: int) -> Any | None:
        """The workspace whose message the reply answers, looked up as _ticket_reply matches it: a pending PR
        comment's message first, then an escalation message."""
        workspaces = self._active_workspaces_fn()
        for ws in workspaces:
            if ws.state.current_state == "PR_REVIEW" and any(
                    reply_to_msg_id in _ensure_msg_ids(c) for c in ws.state.pending_review_comments or []):
                return ws
        return next((ws for ws in workspaces if ws.state.escalation_msg_id == reply_to_msg_id), None)

    async def _ticket_reply(self, reply_to_msg_id: int, text: str, chat_id: str) -> bool:
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
                d = ticket_actions.decide_comment(
                    ws, str(c.get("comment_id")), decision, wf_reason, via="reply", matched_token=matched_token,
                    raw_text=text.strip()[:200], events=self._events, wake=self._wake_fn)
                if d.refused:
                    await self._notifier.send_message(chat_id, d.refused, reply_to_message_id=reply_to_msg_id)
                    return True
                if decision == "fix":
                    decision_label = tr("FIX (matched: {token!r}). Dev-agent will re-engage on {file_line}.",
                                        token=matched_token, file_line=file_line)
                    recorded_label = "FIX"
                else:  # wont_fix
                    decision_label = tr("WON'T FIX (matched: {token!r}). Posting on GitHub: \"{reason}\".",
                                        token=matched_token, reason=wf_reason or "operator decision")
                    recorded_label = "WON'T FIX"

                _title = tg_format.read_ticket_title(ws)
                _emoji = "✅" if decision == "fix" else "❌"
                _hdr = tg_format.tg_header(_emoji, ws.state.company_id, ws.state.ticket_id, _title)
                confirm = f"{_hdr}\n" + tr("✓ Recognized as {label}. {decision}", label=recorded_label,
                                           decision=decision_label)
                if d.remaining:
                    # The button label's count reflects the count at send time. If the
                    # operator decides on N-1 comments before tapping it, the recall
                    # loops over live state and resends only the actual remaining ones —
                    # the label is a hint, not a guarantee.
                    btn = [Button(
                        label=tr("Show {n} unanswered", n=d.remaining),
                        action=f"unanswered:{ws.state.ticket_id}",
                    )]
                    await self._notifier.send_message(
                        chat_id, f"{confirm}\n" + tr("{n} comment(s) remaining.", n=d.remaining), buttons=btn,
                    )
                else:
                    await self._notifier.send_message(
                        chat_id, f"{confirm}\n" + tr("All decisions in for {ticket_id}. Executing now.",
                                                     ticket_id=ws.state.ticket_id),
                    )
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
                    f"{_hdr}\n" + tr("Got it. Fetching PR comments now."),
                )
                if hasattr(self, '_wake_fn') and self._wake_fn:
                    self._wake_fn()
                logger.info("PR review signal for %s via reply to msg %d", ws.state.ticket_id, reply_to_msg_id)
                return True

            # BLOCKED: resume or skip
            if ws.state.current_state == Stage.BLOCKED:
                reply_lower = text.strip().lower()

                if reply_lower == "skip":
                    await self._skip(ws, chat_id)
                    return True

                if reply_lower == "retry":
                    result = ticket_actions.retry(ws, wake=self._wake_fn)
                    if result.refused:
                        await self._notifier.send_message(chat_id, result.refused)
                        return True
                    _title = tg_format.read_ticket_title(ws)
                    _hdr = tg_format.tg_header("🔄", ws.state.company_id, ws.state.ticket_id, _title)
                    await self._notifier.send_message(
                        chat_id,
                        f"{_hdr}\n" + tr("Retrying {state}.", state=result.state),
                    )
                    return True

                # Default: provide input and resume
                result = ticket_actions.answer(ws, text, wake=self._wake_fn)
                if result.refused:
                    await self._notifier.send_message(chat_id, result.refused)
                    return True
                self._recently_unblocked[ws.state.ticket_id] = (time.time(), result.state)
                _title = tg_format.read_ticket_title(ws)
                _hdr = tg_format.tg_header("▶", ws.state.company_id, ws.state.ticket_id, _title)
                await self._notifier.send_message(
                    chat_id,
                    f"{_hdr}\n" + tr("Resuming from {state} with your input.", state=result.state),
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
            await self._reply(chat_id, tr("No tickets have unanswered PR comments."), processing_msg_id)
            return

        total = 0
        for ws in matches:
            ws_total = 0
            for c in ws.state.pending_review_comments:
                if c.get("decision") is not None:
                    continue
                with telegram_language(self._language_of(ws.state.company_id)):  # the ticket's, as at escalation
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
            chat_id, tr("Resent {n} unanswered comment(s).", n=total), processing_msg_id,
        )

    async def _stage_reinvestigation(self, c, ws, hint_text: str, chat_id: str) -> bool:
        """Stage a re-investigation request on the comment entry.

        Returns True (always handled, even when capped).
        """
        rounds = int(c.get("hint_rounds", 0) or 0)
        file_line = f"{c.get('file','?')}:{c.get('line','?')}"
        d = ticket_actions.decide_comment(ws, str(c.get("comment_id")), "reinvestigate", hint_text, via="reply",
                                          events=self._events, wake=self._wake_fn)
        if d.refused:
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("⚠️", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\n{d.refused}")
            return True

        # rounds is the count of completed re-investigations. If a hint is
        # already staged but not yet processed by the orchestrator, sending a
        # second hint just overwrites last_hint — the round counter doesn't
        # tick until the orchestrator actually re-runs the classifier. This
        # means a rapid second hint shows the same round number, which is
        # correct: only one re-investigation will happen.
        next_round = rounds + 1
        _title = tg_format.read_ticket_title(ws)
        _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
        msg = f"{_hdr}\n" + tr(
            "Recognized as hint (round {round}/3). Re-checking @{author}'s comment on {file_line} with your context.",
            round=next_round, author=c.get('author', '?'), file_line=file_line,
        )
        await self._notifier.send_message(chat_id, msg)
        return True

    async def handle_callback(self, action: str, ticket_id: str, chat_id: str, message_id: int) -> None:
        """Handle an inline button press. Bypasses LLM intent parsing. An `sk_` action is the skill flow's and an
        `rv_` action the reviewer's, never a workspace's; an `rv_` action is read in the reviewer's chats only."""
        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            return

        if action.startswith("sk_"):
            if self._skill is None:
                try:
                    with telegram_language(self._chat_language(chat_id)):
                        await self._notifier.send_message(chat_id, tr(SKILL_OFF), reply_to_message_id=message_id)
                except Exception as e:  # the class only: a Telegram error's text can hold the bot token
                    logger.warning("Skill-flow-off answer not sent: %s", type(e).__name__)
            else:
                await self._skill.handle_callback(action, ticket_id, chat_id, message_id)
            return

        if action.startswith("rv_"):
            if chat_id not in self._reviewer_chat_ids:
                return
            if self._reviewer is None:
                with telegram_language(self._chat_language(chat_id)):
                    await self._notifier.send_message(chat_id, tr(_REVIEWER_OFF), reply_to_message_id=message_id)
            else:
                await self._reviewer.handle_callback(action, ticket_id, chat_id, message_id)
            return

        # A pr_fix / pr_wontfix tap names "<ticket>:<comment id>".
        tid = ticket_id.split(":", 1)[0]
        ws = next((w for w in self._active_workspaces_fn() if w.state.ticket_id == tid), None)
        language = self._language_of(ws.state.company_id) if ws is not None else self._chat_language(chat_id)
        with telegram_language(language):
            await self._ticket_callback(action, ticket_id, chat_id, message_id)

    async def _ticket_callback(self, action: str, ticket_id: str, chat_id: str, message_id: int) -> None:
        """A tap on a ticket message's button, answered in the current language."""
        workspaces = self._active_workspaces_fn()

        if action == "approve":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "AWAITING_APPROVAL"), None)
            if not ws:
                await self._notifier.send_message(
                    chat_id, tr("No workspace awaiting approval for {ticket_id}.", ticket_id=ticket_id),
                    reply_to_message_id=message_id,
                )
                return
            result = ticket_actions.approve(ws, wake=self._wake_fn)
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("✅", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(
                chat_id, f"{_hdr}\n" + tr("Approved. Moving to {state}.", state=result.state),
                reply_to_message_id=message_id,
            )

        elif action == "reject":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "AWAITING_APPROVAL"), None)
            if not ws:
                await self._notifier.send_message(
                    chat_id, tr("No workspace awaiting approval for {ticket_id}.", ticket_id=ticket_id),
                    reply_to_message_id=message_id,
                )
                return
            result = ticket_actions.reject(ws, via="Telegram", wake=self._wake_fn)
            if result.state == Stage.FAILED:
                _outcome = tr("Rejected. Marked as FAILED.")
            else:
                _outcome = tr("Rejected. Back to {state}.", state=result.state)
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("❌", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(chat_id, f"{_hdr}\n{_outcome}", reply_to_message_id=message_id)

        elif action == "reviewed":
            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id and w.state.current_state == "PR_REVIEW"), None)
            if not ws:
                await self._notifier.send_message(
                    chat_id, tr("No workspace in PR_REVIEW for {ticket_id}.", ticket_id=ticket_id),
                    reply_to_message_id=message_id,
                )
                return
            ticket_actions.reviewed(ws, wake=self._wake_fn)
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🔍", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(
                chat_id, f"{_hdr}\n" + tr("Got it. Fetching PR comments now."), reply_to_message_id=message_id,
            )

        elif action in ("retry", "retry_from", "retry_pick", "skip"):
            key, _, stage = ticket_id.partition(":")
            ws = next((w for w in workspaces if w.state.ticket_id == key), None)
            if not ws:
                await self._notifier.send_message(
                    chat_id, tr("No active workspace found for {ticket_id}.", ticket_id=key),
                    reply_to_message_id=message_id,
                )
                return
            if action == "skip":
                await self._skip(ws, chat_id, reply_to=message_id)
                return
            if action == "retry_pick":
                await self._notifier.send_message(
                    chat_id, tr("{ticket_id}: retry from which stage?", ticket_id=key), reply_to_message_id=message_id,
                    buttons=[Button(state, f"retry_from:{key}:{name}", row=1 + i // 4)
                             for i, (name, state) in enumerate(ticket_actions.RETRY_STAGES.items())],
                )
                return
            if action == "retry_from":
                result = ticket_actions.retry(ws, stage=stage, wake=self._wake_fn)
            else:  # the stage it stopped at, as the "retry" reply to its escalation
                result = ticket_actions.retry(ws, wake=self._wake_fn)
            if result.refused:
                await self._notifier.send_message(chat_id, result.refused, reply_to_message_id=message_id)
                return
            _title = tg_format.read_ticket_title(ws)
            _hdr = tg_format.tg_header("🔄", ws.state.company_id, ws.state.ticket_id, _title)
            await self._notifier.send_message(
                chat_id, f"{_hdr}\n" + tr("Retrying from {state}.", state=result.state),
                reply_to_message_id=message_id,
            )

        elif action == "clear_gradle":
            from orchestrator.gradle_remediation import clear_gradle_transforms

            ws = next((w for w in workspaces if w.state.ticket_id == ticket_id), None)
            if not ws:
                await self._notifier.send_message(
                    chat_id, tr("No active workspace found for {ticket_id}.", ticket_id=ticket_id),
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
                    chat_id, f"{_hdr}\n" + tr("Failed to clear Gradle cache: {error}", error=detail(str(e))),
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
                f"{_hdr}\n" + tr("Cleared {mb:.0f} MB of Gradle transforms cache. Retrying from {state}.",
                                 mb=mb, state=target),
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
                await self._notifier.send_message(
                    chat_id, tr("No pending comments for {ticket_id}.", ticket_id=tid), reply_to_message_id=message_id,
                )
                return

            if action == "pr_fix":
                d = ticket_actions.decide_comment(ws, comment_id_str, "fix", via="button",
                                                  matched_token="button:fix", events=self._events,
                                                  wake=self._wake_fn)
                recognized = N_("✓ Recognized as FIX (matched: 'Fix' button). "
                                "Dev-agent will re-engage on {file_line}.")
            else:
                d = ticket_actions.decide_comment(ws, comment_id_str, "wont_fix", via="button",
                                                  matched_token="button:wontfix", events=self._events,
                                                  wake=self._wake_fn)
                recognized = N_("✓ Recognized as WON'T FIX (matched: 'Won't Fix' button). "
                                "Posting on GitHub: \"operator decision\" on {file_line}.")
            if d.refused:
                await self._notifier.send_message(chat_id, d.refused, reply_to_message_id=message_id)
                return

            file_line = f"{d.comment.get('file','?')}:{d.comment.get('line','?')}"
            _title = tg_format.read_ticket_title(ws)
            _emoji = "✅" if action == "pr_fix" else "❌"
            _hdr = tg_format.tg_header(_emoji, ws.state.company_id, ws.state.ticket_id, _title)
            confirm = f"{_hdr}\n" + tr(recognized, file_line=file_line)

            if d.remaining:
                btn = [Button(
                    label=tr("Show {n} unanswered", n=d.remaining),
                    action=f"unanswered:{tid}",
                )]
                msg_text = f"{confirm}\n" + tr("{n} comment(s) remaining.", n=d.remaining)
                await self._notifier.send_message(chat_id, msg_text, buttons=btn, reply_to_message_id=message_id)
            else:
                msg_text = f"{confirm}\n" + tr("All decisions in for {ticket_id}. Executing now.", ticket_id=tid)
                await self._notifier.send_message(chat_id, msg_text, reply_to_message_id=message_id)

        else:
            await self._notifier.send_message(
                chat_id, tr("Unknown action: {action}", action=action), reply_to_message_id=message_id,
            )

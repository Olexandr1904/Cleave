"""The operator's actions on a ticket: approve, reject, retry, skip, answer the question, and reviewed.

Telegram and the dashboard both call these, so each action's checks and moves exist once. They are sync, take a
Workspace, and never send anything: the caller words the answer from the TicketResult. A refusal is in the current
language (tg_i18n), for the caller to pass on as it is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integrations.telegram.handlers.approval import APPROVAL_REJECT_STATE, ApprovalHandler
from integrations.telegram.handlers.status import STOPPED_STATES
from orchestrator.constants import (
    JUDGING_STATES,
    REPORT_BA,
    REPORT_DEV,
    REPORT_QA,
    REPORT_SCOPE_GUARD,
)
from orchestrator.pipeline.driver import reset_attempts_from
from orchestrator.tg_i18n import tr
from workspace.workspace import InvalidTransitionError, Stage


@dataclass(frozen=True)
class TicketResult:
    state: str          # the ticket's state after the call
    refused: str = ""   # why nothing was done, in the current language; "" when it was done


# The stages a retry may name, in pipeline order.
RETRY_STAGES: dict[str, str] = {
    "analysis": Stage.ANALYSIS,
    "plan_review": Stage.PLAN_REVIEW,
    "dev": Stage.DEV,
    "scope_check": Stage.SCOPE_CHECK,
    "code_review": Stage.CODE_REVIEW,
    "qa": Stage.QA,
    "push": Stage.PUSHED,
}

# Where "skip" sends a ticket: past the stage it stopped at, to the next one.
SKIP_NEXT: dict[str, str] = {
    Stage.ANALYSIS: Stage.PLAN_REVIEW,
    Stage.PLAN_REVIEW: Stage.DEV,
    Stage.DEV: Stage.SCOPE_CHECK,
    Stage.SCOPE_CHECK: Stage.CODE_REVIEW,
    Stage.CODE_REVIEW: Stage.QA, Stage.QA: Stage.PUSHED,
    Stage.PUSHED: Stage.PR_REVIEW, Stage.PR_REVIEW: Stage.DONE,
}


def _nothing_done(ws: Any) -> TicketResult:
    return TicketResult(ws.state.current_state, tr("{ticket_id} is {state} now: nothing was done.",
                                                   ticket_id=ws.state.ticket_id, state=ws.state.current_state))


def _not_awaiting(ws: Any) -> TicketResult | None:
    if ws.state.current_state == Stage.AWAITING_APPROVAL:
        return None
    return TicketResult(ws.state.current_state,
                        tr("No workspace awaiting approval for {ticket_id}.", ticket_id=ws.state.ticket_id))


def approve(ws: Any, *, wake: Callable[[], None] | None = None) -> TicketResult:
    """Move a ticket waiting at a gate past it."""
    if refused := _not_awaiting(ws):
        return refused
    next_state = ApprovalHandler().resolve_next_state(ws)
    ws.transition(next_state)
    if wake:
        wake()
    return TicketResult(next_state)


def reject(ws: Any, *, via: str, wake: Callable[[], None] | None = None) -> TicketResult:
    """Send a ticket waiting at a gate back where that gate's reject goes, else fail it. `via` ("Telegram" or
    "dashboard") is named in the failed ticket's error."""
    if refused := _not_awaiting(ws):
        return refused
    # The plan-review gate's own message promises "back to analysis"; marking the ticket FAILED there would make
    # that a lie. Other gates fail the ticket.
    target = APPROVAL_REJECT_STATE.get(ws.state.previous_state, "FAILED")
    ws.transition(target)
    if target == "FAILED":
        ws.update_state(error=f"Rejected by operator via {via}")
    if wake:
        wake()
    return TicketResult(target)


def retry_target(ws: Any, stage: str | None) -> tuple[str, str]:
    """The state a retry of `ws` goes to — the named stage's, else the furthest stage its reports show it
    reached — and "", or "" and why there is none."""
    if stage:
        target_state = RETRY_STAGES.get(stage)
        if not target_state:
            valid = ", ".join(RETRY_STAGES.keys())
            return "", tr("Unknown stage '{stage}'. Valid stages: {valid}", stage=stage, valid=valid)
        return target_state, ""
    # Smart retry: detect furthest completed stage from artifacts
    reports = Path(ws.reports_dir) if hasattr(ws, 'reports_dir') else Path(ws.state.workspace_root) / "reports"
    if ws.state.current_state == Stage.BLOCKED and ws.state.previous_state in JUDGING_STATES:
        return ws.state.previous_state, ""  # back to the judge that blocked it
    if (reports / REPORT_QA).exists():
        return Stage.PUSHED, ""
    if (reports / REPORT_SCOPE_GUARD).exists():
        return Stage.QA, ""
    if (reports / REPORT_DEV).exists():
        return Stage.SCOPE_CHECK, ""
    if (reports / REPORT_BA).exists():
        # Not DEV: retrying into dev would hand the dev agent a plan
        # no reviewer signed off on. Costs one extra reviewer run.
        return Stage.PLAN_REVIEW, ""
    return ws.state.previous_state or Stage.ANALYSIS, ""


def retry(ws: Any, *, stage: str | None = None, furthest: bool = False,
          wake: Callable[[], None] | None = None) -> TicketResult:
    """Run a ticket again: from the named `stage`, else (`furthest`) from the furthest stage its reports show it
    reached, else from the stage it stopped at."""
    if stage:
        target_state, refused = retry_target(ws, stage)
    elif furthest:
        target_state, refused = retry_target(ws, None)
    else:
        target_state, refused = ws.state.previous_state or Stage.ANALYSIS, ""
    if refused:
        return TicketResult(ws.state.current_state, refused)
    return _restart(ws, target_state, stage_named=bool(stage), wake=wake)


def _restart(ws: Any, target_state: str, *, stage_named: bool = False,
             wake: Callable[[], None] | None = None) -> TicketResult:
    """Send `ws` to `target_state` with fresh attempts there and at every later stage, and wake the loop; refused
    when the ticket cannot go there from where it is now. `stage_named`: the operator named the stage, which then
    runs, also a judge that has just rejected the work for the last time."""
    try:
        ws.transition(target_state)
    except InvalidTransitionError:
        return _nothing_done(ws)
    if stage_named:
        ws.state.rejected_at = None
    ws.state.human_input_pending = False
    ws.state.error = None
    reset_attempts_from(ws, target_state)
    ws.save_state()
    if wake:
        wake()
    return TicketResult(target_state)


def skip(ws: Any, *, wake: Callable[[], None] | None = None) -> TicketResult:
    """Advance a stopped ticket past the stage it stopped at, to the next one. A ticket that runs again, or cannot
    go there from where it is now, is left as it is."""
    prev = ws.state.previous_state or Stage.ANALYSIS
    resume_state = SKIP_NEXT.get(prev, Stage.DONE)
    if ws.state.current_state not in STOPPED_STATES:
        return _nothing_done(ws)
    try:
        ws.transition(resume_state)
    except InvalidTransitionError:
        return _nothing_done(ws)
    ws.state.human_input_pending = False
    ws.state.error = None
    ws.save_state()
    if wake:
        wake()
    return TicketResult(resume_state)


def answer(ws: Any, text: str, *, wake: Callable[[], None] | None = None) -> TicketResult:
    """Resume a BLOCKED ticket with the operator's answer to its question, recorded for the agent with the
    question it answers."""
    if ws.state.current_state != Stage.BLOCKED:
        return _nothing_done(ws)
    if not text.strip():
        return TicketResult(ws.state.current_state, tr("Empty input — nothing to resume with."))
    ws.state.human_input_reply = text
    ws.state.human_input_pending = False
    _write_human_input(ws, text)
    resume_state = ws.state.previous_state or Stage.ANALYSIS
    reset_attempts_from(ws, resume_state)
    ws.transition(resume_state)
    ws.save_state()
    if wake:
        wake()
    return TicketResult(resume_state)


def reviewed(ws: Any, *, wake: Callable[[], None] | None = None) -> TicketResult:
    """Signal that the PR of a ticket in PR_REVIEW is reviewed: its comments are fetched next."""
    if ws.state.current_state != Stage.PR_REVIEW:
        return TicketResult(ws.state.current_state,
                            tr("No workspace in PR_REVIEW for {ticket_id}.", ticket_id=ws.state.ticket_id))
    ws.state.human_input_reply = "reviewed"
    ws.save_state()
    if wake:
        wake()
    return TicketResult(ws.state.current_state)


@dataclass(frozen=True)
class CommentDecision:
    refused: str = ""          # why nothing was recorded, in the current language; "" when it was
    comment: dict | None = None
    stored: str = ""           # "fix", "won't fix: <reason>", or "reinvestigate"
    remaining: int = 0         # comments of the ticket still undecided


HINT_ROUNDS_MAX = 3


def decide_comment(ws: Any, comment_id: str, decision: str, text: str = "", *, via: str, matched_token: str = "",
                   raw_text: str = "", events: Any = None,
                   wake: Callable[[], None] | None = None) -> CommentDecision:
    """Record the operator's decision on a pending PR comment of a ticket in PR_REVIEW. `decision` is "fix",
    "wont_fix" (`text` is the reason) or "reinvestigate" (`text` is the hint, capped at HINT_ROUNDS_MAX rounds).
    `via` ("button", "reply" or "dashboard") goes into the event. The loop wakes when a fix or won't-fix leaves
    no comment undecided, and for every hint."""
    if ws.state.current_state != Stage.PR_REVIEW:
        return CommentDecision(refused=tr("No workspace in PR_REVIEW for {ticket_id}.", ticket_id=ws.state.ticket_id))
    pending = ws.state.pending_review_comments or []
    c = next((x for x in pending if str(x.get("comment_id")) == str(comment_id)), None)
    if c is None:
        return CommentDecision(refused=tr("Comment not found."))
    ticket_id = ws.state.ticket_id

    if decision == "reinvestigate":
        rounds = int(c.get("hint_rounds", 0) or 0)
        if rounds >= HINT_ROUNDS_MAX:
            if events is not None:
                events.emit("pr_comment_hint_exhausted",
                            f"{ticket_id}: hint cap reached for comment {c.get('comment_id')}",
                            ticket_id=ticket_id,
                            data={"comment_id": c.get("comment_id"), "attempted_hint_excerpt": text[:120]})
            return CommentDecision(comment=c, remaining=_undecided(pending), refused=tr(
                "Hint limit reached (3/3) for @{author} on {file_line}. "
                "Reply \"fix\" or \"won't fix: <reason>\" to close this comment.",
                author=c.get("author", "?"), file_line=f"{c.get('file', '?')}:{c.get('line', '?')}"))
        # rounds counts completed re-investigations: a second hint staged before the first ran only overwrites it.
        c["last_hint"] = text
        c["pending_reinvestigation"] = True
        ws.save_state()
        if events is not None:
            events.emit("pr_comment_reinvestigation_staged",
                        f"{ticket_id}: hint round {rounds + 1} for comment {c.get('comment_id')}",
                        ticket_id=ticket_id,
                        data={"comment_id": c.get("comment_id"), "hint_round": rounds + 1,
                              "hint_excerpt": text[:200]})
        if wake:
            wake()
        return CommentDecision(comment=c, stored="reinvestigate", remaining=_undecided(pending))

    stored = "fix" if decision == "fix" else f"won't fix: {text or 'operator decision'}"
    c["decision"] = stored
    ws.save_state()
    data = {"comment_id": c.get("comment_id"), "decision": stored, "via": via}
    if matched_token:
        data["matched_token"] = matched_token
    if raw_text:
        data["raw_text"] = raw_text
    if events is not None:
        events.emit("pr_comment_decision_recorded", f"{ticket_id}: {stored} for comment {c.get('comment_id')}",
                    ticket_id=ticket_id, data=data)
    remaining = _undecided(pending)
    if remaining == 0 and wake:
        wake()
    return CommentDecision(comment=c, stored=stored, remaining=remaining)


def _undecided(pending: list) -> int:
    return len([x for x in pending if x.get("decision") is None])


def _write_human_input(workspace: Any, text: str) -> None:
    """Write human reply to meta/human-input.md so the agent sees it on next run.

    The escalation it answers is recorded with it. Operators answer in a word ("yes", "A"): alone, such a reply
    names nothing, and an agent that must check what was allowed could not tell from the record.
    """
    meta = Path(workspace.meta_dir) if hasattr(workspace, 'meta_dir') else Path(workspace.state.workspace_root) / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    path = meta / "human-input.md"
    # Append to preserve history of multiple replies
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    question = getattr(workspace.state, "human_input_question", None)
    asked = ""
    if isinstance(question, str) and question.strip():
        asked = "In reply to:\n" + "\n".join(f"> {line}".rstrip() for line in question.strip().splitlines()) + "\n\n"
    entry = f"\n## Human input ({timestamp})\n\n{asked}{text}\n"
    path.write_text(existing + entry, encoding="utf-8")

"""Telegram message factory functions, one per pipeline event.

These functions know how to build a message + buttons for one event type.
They take a NotifierInterface as a parameter — no module-level state, no
class instance, no shared mutable holders.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from integrations.base.notifier import Button, NotifierInterface
from orchestrator import tg_format
from orchestrator.gradle_remediation import (
    ARCH_MISMATCH_HELP,
    looks_like_aapt2_arch_mismatch,
    looks_like_gradle_cache_corruption,
)
from workspace.workspace import Workspace

logger = logging.getLogger(__name__)


async def notify_deferred(
    notifier: NotifierInterface | None,
    chat_id: str,
    workspace: Workspace,
    retry_at: datetime,
    reason: str | None,
    current_quota_window_end: datetime | None,
) -> datetime | None:
    """Send a one-shot Telegram notification for transient agent failure
    deferrals.

    Picks a headline from the actual cause — not all DEFERREDs are quota
    hits, even though the pipeline reuses the quota retry path for any
    transient CLI failure. Hard-coding "Quota exhausted" misled operators
    when the real cause was, e.g., the agent hitting `max_turns`.

    Quota-window debouncing only applies when the cause IS a real
    usage-limit hit; other transient failures skip the silence window so
    each ticket's distinct reason still surfaces.

    Returns the new quota_window_end (or unchanged value if not modified).
    """
    state = workspace.state
    new_window_end = current_quota_window_end
    is_real_quota = bool(
        reason and (
            "usage limit" in reason.lower()
            or "api_error_status\":429" in reason
            or '"api_error_status": 429' in reason
        )
    )

    # Window debouncing is for the case where one quota hit cascades
    # across many tickets — silence the same announcement.
    now = datetime.now(timezone.utc)
    if (
        is_real_quota
        and current_quota_window_end is not None
        and now < current_quota_window_end
    ):
        return new_window_end

    if notifier is None:
        if is_real_quota:
            new_window_end = retry_at
        return new_window_end

    title = tg_format.read_ticket_title(workspace)
    hdr = tg_format.tg_header("⏱", state.company_id, state.ticket_id, title)

    # Pick a headline that names the actual cause.
    if is_real_quota:
        headline = (
            f"Quota exhausted at {state.previous_state or '?'}, deferred "
            f"until {retry_at.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"Other tickets hitting the same quota will defer silently."
        )
    elif reason and "error_max_turns" in reason:
        headline = (
            f"Agent hit max-turns limit at {state.previous_state or '?'}, "
            f"deferred until {retry_at.strftime('%H:%M')} UTC for retry. "
            f"If this recurs the agent task may be too complex — consider "
            f"raising `max_turns` or splitting the work."
        )
    else:
        short = (reason or "").splitlines()[0][:200] if reason else "no detail captured"
        headline = (
            f"Transient agent failure at {state.previous_state or '?'}, "
            f"deferred until {retry_at.strftime('%H:%M')} UTC. "
            f"Reason: {short}"
        )

    msg = f"{hdr}\n{headline}"
    buttons = [Button(label="Retry Now", action=f"retry:{state.ticket_id}")]
    try:
        await notifier.send_message(chat_id, msg, buttons=buttons)
        if is_real_quota:
            new_window_end = retry_at
    except Exception as e:
        logger.warning("Failed to send deferred notification: %s", e)

    return new_window_end


async def notify_failed(
    notifier: NotifierInterface | None,
    chat_id: str,
    workspace: Workspace,
    error: str,
) -> None:
    """Send a one-shot Telegram notification for a permanent failure."""
    if notifier is None:
        return
    state = workspace.state
    title = tg_format.read_ticket_title(workspace)
    hdr = tg_format.tg_header("❌", state.company_id, state.ticket_id, title)
    first_line = (error or "").splitlines()[0] if error else ""
    stage = state.previous_state or "?"
    buttons = [Button(label="Retry", action=f"retry:{state.ticket_id}")]
    # Architecture mismatch (x86-64 aapt2 on non-x86 host) is a host-setup
    # issue — the pipeline cannot apt-install or rewrite gradle.properties.
    # Surface a distinct message with concrete fix options and NO clear-
    # cache button (which loops forever on this failure).
    if looks_like_aapt2_arch_mismatch(error):
        sep_inner = "─" * 30
        msg = (
            f"{hdr}\n"
            f"FAILED at {stage}.\n"
            f"⚠️ Architecture mismatch (x86-64 aapt2 on non-x86 host).\n"
            f"{sep_inner}\n"
            f"{ARCH_MISMATCH_HELP}"
        )
    elif looks_like_gradle_cache_corruption(error):
        msg = (
            f"{hdr}\n"
            f"FAILED at {stage}. Error: {first_line}.\n"
            f"Detected Gradle cache corruption — tap below to clear it."
        )
        buttons.insert(
            0,
            Button(label="🧹 Clear cache & retry", action=f"clear_gradle:{state.ticket_id}"),
        )
    else:
        msg = (
            f"{hdr}\n"
            f"FAILED at {stage}.\n\n"
            f"Reason: {first_line}\n\n"
            f"Options:\n"
            f"- Tap Retry to re-run from {stage}\n"
            f"- Send \"retry {state.ticket_id} from dev\" to restart from an earlier stage"
        )
    try:
        await notifier.send_message(chat_id, msg, buttons=buttons)
    except Exception as e:
        logger.warning("Failed to send failure notification: %s", e)


async def notify_rerun(
    notifier: NotifierInterface | None,
    chat_id: str,
    workspace: Workspace,
    branch: str,
    reason: str,
) -> None:
    """Send Telegram notification when a rerun is triggered from the dashboard."""
    if notifier is None:
        return
    state = workspace.state
    title = tg_format.read_ticket_title(workspace)
    hdr = tg_format.tg_header("🔄", state.company_id, state.ticket_id, title)
    first_line = reason.splitlines()[0][:80] if reason else ""
    msg = (
        f"{hdr}\n"
        f"Rerun started from dashboard.\n"
        f"Branch: {branch}\n"
        f"Reason: {first_line}"
    )
    try:
        await notifier.send_message(chat_id, msg)
    except Exception as e:
        logger.warning("Failed to send rerun notification: %s", e)


async def notify_verification_blocked(
    notifier: NotifierInterface | None,
    chat_id: str,
    workspace: Workspace,
    stage_id: str,
    verify_reason: str,
    build_blocked_reason_fn: Callable[[Workspace, str], str],
) -> None:
    """Send a TG notification for a stage that just failed verification.

    Mirrors _handle_escalate semantics (populates escalation_msg_id so the
    reply flow in command_handler.handle_reply can unblock), but uses a
    distinct header to flag that this is a mechanical verification failure
    rather than an agent-requested escalation.
    """
    if not notifier:
        return
    if not chat_id:
        return

    sep = "─" * 30
    title = tg_format.read_ticket_title(workspace)
    hdr = tg_format.tg_header("⚠️", workspace.state.company_id, workspace.state.ticket_id, title)
    header = f"{hdr}\nStage: {stage_id} — verification failed\n"

    agent_reason = tg_format.strip_markdown(build_blocked_reason_fn(workspace, stage_id))
    combined = f"Verification failed: {verify_reason}\n\n{agent_reason}"
    hint = (
        f"\n{sep}\n"
        f"↩️ Reply with your answer or additional context, or:\n"
        f"  Reply \"skip\" — advance past this stage to the next one\n"
        f"  Reply \"retry\" — re-run this stage"
    )
    message = f"{header}\n{combined}{hint}"

    try:
        msg_id = await notifier.send_message(chat_id, message)
        workspace.update_state(human_input_question=combined)
        workspace.state.escalation_msg_id = msg_id
        workspace.state.escalation_chat_id = chat_id
        workspace.save_state()
        logger.info(
            "Verification-blocked %s via Telegram (msg_id=%d)",
            workspace.state.ticket_id, msg_id,
        )
    except Exception as e:
        logger.warning(
            "Failed to send verification-blocked notification for %s: %s",
            workspace.state.ticket_id, e,
        )

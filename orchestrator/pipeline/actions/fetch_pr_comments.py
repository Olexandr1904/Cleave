"""fetch_pr_comments action: poll PR review comments, classify, escalate,
or execute human decisions.

The longest single action in the pipeline. Branches:
  - No PR yet → done
  - No new comments and CI green → done
  - Pending entries from prior cycle → re-investigate
  - Awaiting human decisions → execute_review_decisions
  - New comments to triage → classify + escalate or auto-fix
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from config.schemas import RepoConfig
from integrations.base.notifier import Button, NotifierInterface
from integrations.base.tracker import TrackerInterface
from integrations.base.vcs import VCSInterface
from orchestrator import tg_format
from orchestrator.git_ops import git_diff_files, git_head_sha
from orchestrator.stage_verifier import ActionResult
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _log_pipeline(workspace: Workspace, entry: str) -> None:
    """Append a timestamped entry to ai_pipeline/<ticket>/pipeline-log.md."""
    log_path = workspace.reports_dir / "pipeline-log.md"
    timestamp = datetime.now(timezone.utc).strftime("%H:%M")
    line = f"- **{timestamp}** {entry}\n"
    if log_path.exists():
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    else:
        log_path.write_text(
            f"# Pipeline Log — {workspace.state.ticket_id}\n\n{line}",
            encoding="utf-8",
        )


def _emit(event_bus: Any | None, event_type: str, message: str, **kwargs: Any) -> None:
    if event_bus is not None:
        event_bus.emit(event_type, message, **kwargs)


async def send_escalated_comment_tg(
    workspace: Workspace, cc: Any, pr_number: int,
    *,
    notifier: NotifierInterface | None,
    get_chat_id: Any,
) -> int:
    """Send a single escalated comment to TG. Returns the message ID."""
    from orchestrator.escalation_view import build_escalated_comment_message

    state = workspace.state
    if not notifier:
        return 0
    chat_id = get_chat_id()
    title = tg_format.read_ticket_title(workspace)
    text, buttons = build_escalated_comment_message(
        state, cc, pr_number, ticket_title=title,
    )
    if chat_id:
        return await notifier.send_message(chat_id, text, buttons=buttons)
    return 0


async def reinvestigate_pending(
    workspace: Workspace,
    *,
    agent_runtime: Any,
    notifier: NotifierInterface | None,
    get_chat_id: Any,
    event_bus: Any | None = None,
) -> None:
    """Process any pending re-investigation requests on this workspace.

    For each entry with pending_reinvestigation=True (and not decided), call
    the classifier with operator_hint, update the entry in place, and re-send
    a fresh escalated TG message. Always re-escalates — never acts silently
    after a hint.
    """
    import sys
    # Preserve the existing monkeypatch hook: tests/runtime can patch
    # `orchestrator.orchestrator.classify_comments` to override classification.
    _orc_mod = sys.modules.get("orchestrator.orchestrator")
    classify_fn = getattr(_orc_mod, "classify_comments", None) if _orc_mod else None
    if classify_fn is None:
        from orchestrator.comment_classifier import classify_comments as classify_fn
    from orchestrator.resolution_report import update_entry

    state = workspace.state
    pending = state.pending_review_comments or []
    if not pending:
        return
    pr_number = state.pr_number
    if not pr_number:
        return

    for c in pending:
        if not c.get("pending_reinvestigation"):
            continue
        if c.get("decision") is not None:
            # Operator decided via button while this was queued — clear the flag
            c["pending_reinvestigation"] = False
            workspace.save_state()
            continue
        comment_stub = SimpleNamespace(
            id=c["comment_id"], author=c.get("author", ""),
            path=c.get("file", ""), line=c.get("line"),
            body=c.get("body", ""),
        )
        old_verdict = c.get("verdict")
        old_classification = "ESCALATE"
        try:
            classified = await classify_fn(
                [comment_stub], workspace, agent_runtime,
                operator_hint=c.get("last_hint") or "",
            )
        except Exception as e:
            logger.error("Re-investigation failed for comment %s: %s", c["comment_id"], e)
            retry_count = int(c.get("reinvestigation_retry_count", 0) or 0)
            if retry_count < 1:
                # First failure — leave flag set so next tick retries
                c["reinvestigation_retry_count"] = retry_count + 1
                workspace.save_state()
                continue
            # Second failure — surface and clear
            if notifier:
                chat_id = get_chat_id()
                if chat_id:
                    title = tg_format.read_ticket_title(workspace)
                    hdr = tg_format.tg_header("⚠️", workspace.state.company_id, workspace.state.ticket_id, title)
                    await notifier.send_message(
                        chat_id,
                        f"{hdr}\n"
                        f"Re-investigation failed for @{c.get('author','?')}'s comment "
                        f"on {c.get('file','?')}:{c.get('line','?')}\n\n"
                        f"The agent was unable to re-classify this comment after your hint. Options:\n"
                        f"- Reply \"fix\" to send the dev-agent in anyway\n"
                        f"- Reply \"won't fix: <reason>\" to close the comment on GitHub",
                    )
            c["pending_reinvestigation"] = False
            c["reinvestigation_retry_count"] = 0
            workspace.save_state()
            continue

        if not classified:
            logger.warning("Re-investigation returned no result for comment %s", c["comment_id"])
            c["pending_reinvestigation"] = False
            workspace.save_state()
            continue

        cc = classified[0]
        c["verdict"] = cc.verdict
        c["reason"] = cc.reason
        # Note: classification can change (e.g., ESCALATE → AUTO_FIX), but we
        # always re-escalate for human confirmation. We don't auto-act.
        c["hint_rounds"] = int(c.get("hint_rounds", 0) or 0) + 1
        c["reinvestigation_retry_count"] = 0
        c["pending_reinvestigation"] = False
        report_path = workspace.reports_dir / "pr-review-resolution.md"
        update_entry(report_path, c["comment_id"], {
            "verdict": cc.verdict,
            "reason": cc.reason,
            "hint_round": str(c["hint_rounds"]),
            "hint": (c.get("last_hint") or "")[:200],
        })
        workspace.save_state()

        new_msg_id = await send_escalated_comment_tg(
            workspace, cc, pr_number,
            notifier=notifier, get_chat_id=get_chat_id,
        )
        if new_msg_id:
            c.setdefault("msg_ids", []).append(new_msg_id)
            workspace.save_state()

        if event_bus is not None:
            _emit(
                event_bus,
                "pr_comment_reinvestigation_completed",
                f"{state.ticket_id}: re-checked comment {c['comment_id']}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={
                    "comment_id": c["comment_id"],
                    "hint_round": c["hint_rounds"],
                    "old_verdict": old_verdict,
                    "new_verdict": cc.verdict,
                    "old_classification": old_classification,
                    "new_classification": cc.classification,
                },
            )


async def execute_review_decisions(
    workspace: Workspace,
    *,
    get_vcs: Any,
    get_chat_id: Any,
    notifier: NotifierInterface | None,
) -> ActionResult:
    """Execute all pending review decisions.

    Writes decisions to resolution report via add_entry/update_entry.
    WON'T_FIX replies + resolves immediately. FIX gets PENDING entry.
    SKIP gets recorded.
    """
    from orchestrator.resolution_report import update_entry

    state = workspace.state
    pending = state.pending_review_comments or []
    pr_number = state.pr_number
    report_path = workspace.reports_dir / "pr-review-resolution.md"
    vcs, _repo_config = get_vcs()

    fixes_needed = []
    wont_fix = []
    skipped_comments = []

    def _is_fix(d: str) -> bool:
        """Match 'fix' with common typos."""
        d = d.lower().strip()
        return d in ("fix", "fxi", "fifx", "fixx", "fx", "yes", "fix it")

    for c in pending:
        decision = (c.get("decision") or "").lower().strip()
        cid = c["comment_id"]
        if _is_fix(decision):
            fixes_needed.append(c)
            github_reply_status = "Posted (will fix)"
            if vcs and pr_number:
                try:
                    await vcs.reply_to_comment(
                        pr_number, cid, f"Will fix: {c.get('reason', 'operator decision')}",
                    )
                except Exception as e:
                    logger.warning("Failed to post 'Will fix' on comment %d: %s", cid, e)
                    github_reply_status = f"Failed: {str(e)[:120]}"
            update_entry(report_path, cid, {
                "verdict": c.get("verdict", "Unsure"),
                "decision": "FIX",
                "verified": "PENDING",
                "github_reply": github_reply_status,
                "fail_count": "0",
                "decided_at": _now(),
            })
        elif decision.startswith("won't fix") or decision.startswith("wont fix"):
            reason = decision.split(":", 1)[1].strip() if ":" in decision else "Operator decision"
            wont_fix.append({**c, "wont_fix_reason": reason})
            github_reply_status = "Posted"
            resolved_status = "YES"
            if vcs and pr_number:
                try:
                    await vcs.reply_to_comment(pr_number, cid, f"Won't fix: {reason}")
                except Exception as e:
                    logger.warning("Failed to reply on comment %d: %s", cid, e)
                    github_reply_status = f"Failed: {str(e)[:120]}"
                    resolved_status = "NO"
                try:
                    await vcs.resolve_comment(pr_number, cid)
                except Exception as e:
                    logger.warning("Failed to resolve comment %d: %s", cid, e)
                    resolved_status = "NO"
            update_entry(report_path, cid, {
                "verdict": c.get("verdict", "Unsure"),
                "decision": "WON'T_FIX",
                "verified": "N/A",
                "github_reply": github_reply_status,
                "resolved": resolved_status,
                "decided_at": _now(),
            })
        else:
            # "Skip" (or any unrecognized free-text reply) means "drop from
            # pending, no GitHub action, don't nag me again". The PR
            # conversation stays open on GitHub — the operator can revisit
            # via the GitHub UI later — but the pipeline does not
            # re-escalate every 30 min for a comment the operator
            # already saw and chose to ignore.
            skipped_comments.append(c)
            update_entry(report_path, cid, {
                "decision": "SKIP",
                "resolved": "NO",
                "decided_at": _now(),
            })

    if fixes_needed:
        fix_md = "# PR Comment Fixes Required\n\n"
        for f in fixes_needed:
            fix_md += f"## Fix: {f['file']}:{f.get('line', '?')}\n"
            fix_md += f"Comment by @{f['author']}: {f['body'][:200]}\n"
            fix_md += f"Reason: {f['reason']}\n\n"
        (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")

    state.pending_review_comments = None
    if fixes_needed:
        for stage in ("dev", "scope_check", "qa"):
            state.stage_iterations.pop(stage, None)
    workspace.save_state()

    if fixes_needed:
        return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})

    # Skipped comments → AWAITING_APPROVAL, NOT silent DONE. Marking a
    # ticket DONE while review comments are still open on the PR hides
    # incomplete work. Instead we hand the decision back to the operator
    # explicitly: Approve to merge as-is (comments stay open on the PR
    # for manual follow-up) or Reject to send the workspace back so the
    # dev agent can re-engage. One TG message, then silence — no
    # 30-minute re-escalation loop.
    if skipped_comments:
        if notifier:
            chat_id = get_chat_id()
            if chat_id:
                sep = "─" * 30
                title = tg_format.read_ticket_title(workspace)
                hdr = tg_format.tg_header("⏸", state.company_id, state.ticket_id, title)
                lines = [hdr]
                lines.append(f"{len(skipped_comments)} comment(s) marked Skip — still open on the PR:")
                for sc in skipped_comments:
                    lines.append(f"  • @{sc.get('author','?')} on {sc.get('file','?')}:{sc.get('line','?')}")
                lines.append(sep)
                lines.append(
                    "Approve → mark DONE, leave comments open on GitHub for manual follow-up.\n"
                    "Reject → reopen for dev-agent to address the comments."
                )
                buttons = [
                    Button(label="Approve", action=f"approve:{state.ticket_id}"),
                    Button(label="Reject", action=f"reject:{state.ticket_id}"),
                ]
                try:
                    await notifier.send_message(
                        chat_id, "\n".join(lines), buttons=buttons,
                    )
                except Exception as e:
                    logger.warning("Failed to send PR-review pause: %s", e)
        return ActionResult(
            success=True, next_state=Stage.AWAITING_APPROVAL, error="", metadata={},
        )

    return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})


async def action_fetch_pr_comments(
    workspace: Workspace,
    stage_def: Any,
    *,
    get_vcs: Any,
    get_chat_id: Any,
    tracker: TrackerInterface | None,
    notifier: NotifierInterface | None,
    agent_runtime: Any,
    event_bus: Any | None = None,
) -> ActionResult:
    """PR review comment resolution flow.

    Uses resolution_report as the single source of truth for comment
    decisions and verification state.

    `get_vcs` / `get_chat_id` are zero-arg thunks the orchestrator shim
    passes in so test fixtures that mock the Orchestrator don't have to
    configure those helpers up-front. Materialized once below the
    pr_number guard — every downstream branch needs at least one of them.
    """
    from orchestrator.comment_classifier import classify_comments
    from orchestrator.resolution_report import read_entries, add_entry, update_entry

    state = workspace.state
    pr_number = state.pr_number
    report_path = workspace.reports_dir / "pr-review-resolution.md"

    if not pr_number:
        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

    vcs, _repo_config = get_vcs()
    chat_id = get_chat_id()

    # Phase 1: Check PENDING verifications from previous cycle
    entries = read_entries(report_path)
    pending_verify = {
        cid: e for cid, e in entries.items()
        if e.get("verified") == "PENDING"
    }
    if pending_verify:
        sha = git_head_sha(workspace)
        last_verified = state.last_verified_sha or ""
        changed_files = git_diff_files(workspace, since_sha=last_verified)
        for cid, entry in pending_verify.items():
            file_path = entry.get("file", "")
            if file_path in changed_files:
                if vcs:
                    try:
                        await vcs.reply_to_comment(pr_number, cid, f"Fixed in commit {sha[:8]}")
                        await vcs.resolve_comment(pr_number, cid)
                    except Exception as e:
                        logger.warning("Failed to resolve comment %d: %s", cid, e)
                update_entry(report_path, cid, {
                    "verified": "YES",
                    "fixed_in": sha[:8],
                    "verified_at": _now(),
                })
            else:
                fail_count = int(entry.get("fail_count", "0")) + 1
                update_entry(report_path, cid, {
                    "verified": "FAILED",
                    "fail_count": str(fail_count),
                })
                if fail_count >= 2 and notifier:
                    if chat_id:
                        title = tg_format.read_ticket_title(workspace)
                        hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                        await notifier.send_message(
                            chat_id,
                            f"{hdr}\n"
                            f"Dev-agent failed to apply the fix for comment #{cid} twice.\n\n"
                            f"File: {entry.get('file', '?')}:{entry.get('line', '?')}\n\n"
                            f"Options:\n"
                            f"- Reply \"fix\" to retry once more\n"
                            f"- Reply \"won't fix: <reason>\" to close the comment without fixing",
                        )
        # Advance the cursor so the next cycle diffs from here
        state.last_verified_sha = sha
        workspace.save_state()

    # Phase 1.5: Process any pending re-investigations from operator hints
    await reinvestigate_pending(
        workspace,
        agent_runtime=agent_runtime,
        notifier=notifier, get_chat_id=lambda: chat_id,
        event_bus=event_bus,
    )

    # Phase 2: Check pending escalated decisions
    pending = state.pending_review_comments or []
    undecided = [c for c in pending if c.get("decision") is None]

    if pending and not undecided:
        return await execute_review_decisions(
            workspace,
            get_vcs=lambda: (vcs, _repo_config),
            get_chat_id=lambda: chat_id,
            notifier=notifier,
        )

    if undecided:
        return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

    # Phase 3: Wait for 'reviewed' signal
    reply = (state.human_input_reply or "").lower()
    if "reviewed" not in reply and "proceed" not in reply:
        return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

    state.human_input_reply = None
    state.review_cycle = (state.review_cycle or 0) + 1
    state.stage_iterations["pr_review"] = 0
    workspace.save_state()

    # Phase 4: Fetch comments, filter out already-decided (by comment ID in resolution report)
    if not vcs:
        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

    try:
        all_comments = await vcs.get_pr_comments(pr_number)
    except Exception as e:
        logger.error("Failed to fetch PR comments for %s: %s", state.ticket_id, e)
        return ActionResult(success=False, next_state="", error=f"Failed to fetch: {e}", metadata={})

    # Filter: only root comments not already in the resolution report
    decided_ids = set(entries.keys())
    replied_to_ids = set()
    for c in all_comments:
        if c.in_reply_to_id and c.body.strip().lower().startswith(("won't fix", "wont fix", "fixed")):
            replied_to_ids.add(c.in_reply_to_id)

    comments = [
        c for c in all_comments
        if not c.in_reply_to_id
        and c.id not in replied_to_ids
        and c.id not in decided_ids
    ]
    logger.info(
        "PR #%d: %d total, %d already decided, %d replied, %d new to process",
        pr_number, len(all_comments), len(decided_ids), len(replied_to_ids), len(comments),
    )

    if not comments:
        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

    # Write comments for reference
    comment_md = "# PR Review Comments\n\n"
    for c in comments:
        comment_md += f"## Comment by {c.author}\n"
        if c.path:
            comment_md += f"File: `{c.path}`"
            if c.line:
                comment_md += f" (line {c.line})"
            comment_md += "\n"
        comment_md += f"\n{c.body}\n\n---\n\n"
    (workspace.reports_dir / "pr-review-comments.md").write_text(comment_md, encoding="utf-8")

    # Phase 5: Classify new comments, write to resolution report
    classified = await classify_comments(comments, workspace, agent_runtime)

    auto_fixed, auto_rejected, escalated = [], [], []
    for cc in classified:
        if cc.classification == "AUTO_FIX":
            github_reply_status = "Posted (will fix)"
            if vcs:
                try:
                    await vcs.reply_to_comment(
                        pr_number, cc.comment_id, f"Will fix: {cc.reason}",
                    )
                except Exception as e:
                    logger.warning("Failed to post 'Will fix' on comment %d: %s", cc.comment_id, e)
                    github_reply_status = f"Failed: {str(e)[:120]}"
            add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                "classification": "AUTO_FIX",
                "verdict": cc.verdict,
                "file": cc.file or "",
                "line": str(cc.line or "?"),
                "author": cc.author or "",
                "reason": cc.reason or "",
                "verified": "PENDING",
                "github_reply": github_reply_status,
                "fail_count": "0",
                "cycle": str(state.review_cycle),
            })
            auto_fixed.append(cc)
        elif cc.classification == "AUTO_REJECT":
            # Phase 6: AUTO_REJECT replies + resolves immediately
            github_reply_status = "Posted"
            resolved_status = "YES"
            try:
                await vcs.reply_to_comment(pr_number, cc.comment_id, f"Won't fix: {cc.reason}")
            except Exception as e:
                logger.warning("Failed to reply on comment %d: %s", cc.comment_id, e)
                github_reply_status = f"Failed: {str(e)[:120]}"
                resolved_status = "NO"
            try:
                await vcs.resolve_comment(pr_number, cc.comment_id)
            except Exception as e:
                logger.warning("Failed to resolve comment %d: %s", cc.comment_id, e)
                resolved_status = "NO"
            add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
                "classification": "AUTO_REJECT",
                "verdict": cc.verdict,
                "file": cc.file or "",
                "line": str(cc.line or "?"),
                "author": cc.author or "",
                "reason": cc.reason or "",
                "verified": "N/A",
                "github_reply": github_reply_status,
                "resolved": resolved_status,
                "cycle": str(state.review_cycle),
            })
            auto_rejected.append(cc)
        else:
            # ESCALATE goes to TG
            escalated.append(cc)

    # TG summary for auto-handled
    if (auto_fixed or auto_rejected) and notifier:
        if chat_id:
            sep = "─" * 30
            title = tg_format.read_ticket_title(workspace)
            hdr = tg_format.tg_header("🤖", state.company_id, state.ticket_id, title)
            sep = "─" * 30
            lines = [hdr]
            lines.append(f"PR #{pr_number} — Auto-processed {len(auto_fixed) + len(auto_rejected)} comment(s):")
            lines.append(sep)
            for af in auto_fixed:
                lines.append(f"✅ FIX: {af.reason} ({af.file}:{af.line or '?'})")
            for ar in auto_rejected:
                lines.append(f"❌ REJECT: {ar.body[:60]} — {ar.reason}")
            lines.append(sep)
            summary_buttons = None
            if escalated:
                lines.append(f"Waiting for your decisions on {len(escalated)} escalated comment(s).")
                summary_buttons = [
                    Button(label=f"Show {len(escalated)} unanswered", action=f"unanswered:{state.ticket_id}"),
                ]
            await notifier.send_message(chat_id, "\n".join(lines), buttons=summary_buttons)

    # Phase 7: Handle escalated or collect FIX items
    if not escalated:
        summary = f"PR review cycle {state.review_cycle}: {len(auto_fixed)} fix, {len(auto_rejected)} rejected"
        _log_pipeline(workspace, f"{summary}. Report: `ai_pipeline/{state.ticket_id}/pr-review-resolution.md`")
        if auto_fixed:
            # Write fix instructions for the dev agent
            fix_md = "# PR Comment Fixes Required\n\n"
            for af in auto_fixed:
                fix_md += f"## Fix: {af.file}:{af.line or '?'}\n"
                fix_md += f"Comment by @{af.author}: {af.body[:200]}\n"
                fix_md += f"What to do: {af.suggested_fix or af.reason}\n\n"
            (workspace.reports_dir / "pr-comment-fixes.md").write_text(fix_md, encoding="utf-8")
            for stage in ("dev", "scope_check", "qa"):
                state.stage_iterations.pop(stage, None)
            workspace.save_state()
            return ActionResult(success=True, next_state=Stage.DEV, error="", metadata={})
        return ActionResult(success=True, next_state=Stage.DONE, error="", metadata={})

    # Store escalated with TG msg_ids
    pending_comments = []
    title = tg_format.read_ticket_title(workspace)
    for cc in escalated:
        msg_id = await send_escalated_comment_tg(
            workspace, cc, pr_number,
            notifier=notifier, get_chat_id=lambda: chat_id,
        )
        pending_comments.append({
            "comment_id": cc.comment_id, "msg_ids": [msg_id], "decision": None,
            "author": cc.author, "file": cc.file, "line": cc.line,
            "body": cc.body, "reason": cc.reason,
            "verdict": cc.verdict,
            "hint_rounds": 0, "last_hint": None, "pending_reinvestigation": False,
            "ticket_title": title,
        })
        add_entry(report_path, state.ticket_id, pr_number, cc.comment_id, {
            "classification": "ESCALATE",
            "verdict": cc.verdict,
            "file": cc.file or "",
            "line": str(cc.line or "?"),
            "author": cc.author or "",
            "reason": cc.reason or "",
            "verified": "N/A",
            "decision": "PENDING_HUMAN",
            "cycle": str(state.review_cycle),
        })

    state.pending_review_comments = pending_comments
    workspace.save_state()
    return ActionResult(success=False, next_state="", error="", metadata={}, skipped=True)

"""Tests for Orchestrator._execute_review_decisions — Skip semantic.

Skip used to re-escalate every 30-min pr_review cycle, which trapped operators
in a nag loop on a button labelled "Skip". The intent of Skip is "drop this,
move on" — these tests pin that contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


def _make_workspace(tmp_path, pending_comments):
    ws = MagicMock()
    ws.reports_dir = tmp_path / "reports"
    ws.reports_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="acme-app",
        current_state="PR_REVIEW",
        pending_review_comments=pending_comments,
        pr_number=42,
    )
    ws.save_state = MagicMock()
    return ws


def _make_orch(notifier=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch._notifier = notifier
    orch._events = None
    orch._get_chat_id = MagicMock(return_value="chat-1")
    orch._get_vcs_for_workspace = MagicMock(return_value=(None, None))
    orch._now = MagicMock(return_value="2026-04-29T10:00:00Z")
    return orch


@pytest.mark.asyncio
async def test_skip_routes_to_awaiting_approval_not_done(tmp_path):
    """Skip must NOT silently mark DONE — that hides unresolved review work.
    Goes to AWAITING_APPROVAL so the operator explicitly decides whether to
    merge as-is or send back for a fix."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    ws = _make_workspace(tmp_path, [
        {"comment_id": 1, "decision": "skip", "author": "Copilot",
         "file": "x.kt", "line": 10, "body": "...", "reason": "..."},
    ])

    result = await orch._execute_review_decisions(ws)

    assert result.success is True
    assert result.next_state == Stage.AWAITING_APPROVAL
    # Approve/Reject buttons attached so operator can decide via TG
    buttons = notifier.send_message.call_args.kwargs.get("buttons") or []
    labels = [b.label for b in buttons]
    assert "Approve" in labels
    assert "Reject" in labels


@pytest.mark.asyncio
async def test_skip_sends_one_shot_summary_not_nag_loop(tmp_path):
    """Send one summary listing the skipped comments. No 'still unresolved'
    wording (the old prompt that told operators to reply again — which
    triggered the original nag loop they complained about)."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    ws = _make_workspace(tmp_path, [
        {"comment_id": 1, "decision": "skip", "author": "Copilot",
         "file": "Frag.kt", "line": 96, "body": "...", "reason": "..."},
    ])

    await orch._execute_review_decisions(ws)

    notifier.send_message.assert_awaited_once()
    msg = notifier.send_message.call_args.args[1]
    assert "still unresolved" not in msg.lower()
    assert "Frag.kt:96" in msg
    # Operator-facing language about the choice
    assert "Approve" in msg
    assert "Reject" in msg


@pytest.mark.asyncio
async def test_fix_decision_still_returns_to_dev(tmp_path):
    """Sanity: the Fix path is not affected by the Skip change — still routes
    back to DEV so dev-agent can address the comment."""
    orch = _make_orch()
    ws = _make_workspace(tmp_path, [
        {"comment_id": 1, "decision": "fix", "author": "Copilot",
         "file": "x.kt", "line": 10, "body": "...", "reason": "..."},
    ])

    result = await orch._execute_review_decisions(ws)

    assert result.success is True
    assert result.next_state == Stage.DEV


@pytest.mark.asyncio
async def test_mixed_skip_and_fix_routes_to_dev(tmp_path):
    """A Fix decision wins over Skip: route to DEV. The Skip comments are
    still recorded as such in the resolution report but don't block DEV."""
    orch = _make_orch()
    ws = _make_workspace(tmp_path, [
        {"comment_id": 1, "decision": "skip", "author": "Copilot",
         "file": "a.kt", "line": 1, "body": "...", "reason": "..."},
        {"comment_id": 2, "decision": "fix", "author": "Reviewer",
         "file": "b.kt", "line": 2, "body": "...", "reason": "..."},
    ])

    result = await orch._execute_review_decisions(ws)

    assert result.success is True
    assert result.next_state == Stage.DEV

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
async def test_skip_advances_to_done_not_nag_loop(tmp_path):
    """Pressing Skip on the only pending comment must advance the workspace
    to DONE, NOT re-escalate it as 'still unresolved' on the next cycle."""
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    orch = _make_orch(notifier)

    ws = _make_workspace(tmp_path, [
        {"comment_id": 1, "decision": "skip", "author": "Copilot",
         "file": "x.kt", "line": 10, "body": "...", "reason": "..."},
    ])

    result = await orch._execute_review_decisions(ws)

    assert result.success is True
    assert result.next_state == Stage.DONE
    assert getattr(result, "skipped", False) is False or result.skipped is False
    # pending_review_comments cleared so next pr_review cycle doesn't see them
    assert ws.state.pending_review_comments is None or ws.state.pending_review_comments == []


@pytest.mark.asyncio
async def test_skip_sends_summary_message_not_unresolved_nag(tmp_path):
    """The TG message for skipped comments must read as a one-shot summary
    (the operator already saw the comment and chose to drop it), not as an
    'unresolved' escalation that asks them to reply again."""
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
    # NOT the old "still unresolved" wording (which implied re-asking)
    assert "still unresolved" not in msg.lower()
    # Contains the new explicit "left unresolved on GitHub" framing
    assert "Skipped" in msg
    assert "GitHub" in msg
    # No prompt asking the operator to reply with fix / won't fix again
    assert "Reply" not in msg or "Resolve" in msg  # informational, not a prompt


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

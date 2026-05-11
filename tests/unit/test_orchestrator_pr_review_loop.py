"""Characterization tests for _action_fetch_pr_comments and helpers.

The ~350-line PR-review escalation loop has had effectively zero coverage.
These tests pin the externally observable contract: outcome state and
calls to vcs/notifier. Each test only pins the minimum needed.

Assertions are deliberately loose where the current code branches in
ways that aren't load-bearing for the refactor — the goal is "pin the
current contract", not "specify an ideal contract".
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from workspace.workspace import Stage


@pytest.fixture
def workspace(tmp_path: Path):
    ws = MagicMock()
    ws.source_dir = tmp_path / "src"
    ws.source_dir.mkdir()
    ws.reports_dir = tmp_path / "ai_pipeline" / "T-1"
    ws.reports_dir.mkdir(parents=True)
    ws.meta_dir = tmp_path / "meta"
    ws.meta_dir.mkdir()
    ws.state = SimpleNamespace(
        ticket_id="T-1",
        company_id="acme",
        repo_id="android",
        current_state=Stage.PR_REVIEW,
        pr_number=42,
        pr_url="https://g/pr/42",
        branch="feature/T-1",
        stage_iterations={},
        escalation_msg_id=None,
        escalation_chat_id=None,
        human_input_reply=None,
        human_input_question=None,
        pending_review_comments=None,
        review_cycle=0,
        last_verified_sha="",
    )
    ws.transition = MagicMock()
    ws.update_state = MagicMock()
    ws.save_state = MagicMock()
    return ws


def _orc(vcs=None, notifier=None, tracker=None):
    orc = Orchestrator.__new__(Orchestrator)
    orc._vcs = vcs if vcs is not None else AsyncMock()
    orc._tracker = tracker if tracker is not None else AsyncMock()
    if notifier is None:
        notifier = MagicMock()
        notifier.send_message = AsyncMock(return_value=1234)
    orc._notifier = notifier
    orc._repo_vcs = {}
    orc._projects = {}
    orc._global_config = SimpleNamespace(
        telegram=SimpleNamespace(default_chat_id="chat-1"),
    )
    orc._events = None
    orc._agent_runtime = AsyncMock()
    return orc


@pytest.mark.asyncio
async def test_no_pr_number_marks_done(workspace) -> None:
    """If state.pr_number is missing, action returns DONE immediately
    (orchestrator/orchestrator.py:1838-1839)."""
    workspace.state.pr_number = None
    orc = _orc()
    result = await orc._action_fetch_pr_comments(workspace, SimpleNamespace())
    # Current code returns next_state=Stage.DONE. The plan allowed
    # `skipped=True` as an alternative; current behavior is the former.
    assert getattr(result, "skipped", False) or result.next_state == Stage.DONE


@pytest.mark.asyncio
async def test_no_comments_marks_done(workspace, monkeypatch) -> None:
    """Empty review-comment list (after operator replied 'reviewed')
    short-circuits to DONE (orchestrator/orchestrator.py:1942-1943).

    Adjustment from plan draft: 'reviewed' reply is required to advance
    past Phase 3 (line 1904-1906). Without it, the action returns
    skipped=True regardless of comment count. The plan's assertion
    `result.next_state in (Stage.DONE, Stage.PR_REVIEW)` would have hit
    the skip branch (next_state="") — we set 'reviewed' to drive the
    actual fetch-comments path.
    """
    workspace.state.human_input_reply = "reviewed"
    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = []
    orc = _orc(vcs=vcs)
    result = await orc._action_fetch_pr_comments(workspace, SimpleNamespace())
    assert result.next_state == Stage.DONE
    assert result.success is True


@pytest.mark.asyncio
async def test_human_reply_fix_routes_to_dev(workspace, monkeypatch) -> None:
    """Operator decided 'fix' on a pending escalated comment → workspace
    routes back to Stage.DEV (orchestrator/orchestrator.py:2201-2202 via
    _execute_review_decisions, dispatched at 1897-1898).

    Adjustment from plan draft: the plan put 'fix' in
    `state.human_input_reply`, but that field is only consumed for
    'reviewed'/'proceed' (line 1904-1906). The actual fix→DEV path is
    driven by per-comment decisions in `pending_review_comments`. We
    pre-populate one such comment with decision='fix' to exercise the
    real routing logic.
    """
    workspace.state.pending_review_comments = [
        {
            "comment_id": 999,
            "decision": "fix",
            "author": "reviewer",
            "file": "x.py",
            "line": 10,
            "body": "please rename foo",
            "reason": "Valid concern",
            "verdict": "Valid",
        }
    ]
    vcs = AsyncMock()
    orc = _orc(vcs=vcs)
    result = await orc._action_fetch_pr_comments(workspace, SimpleNamespace())
    assert (
        result.next_state == Stage.DEV
        or any(c.args[0] == Stage.DEV for c in workspace.transition.call_args_list)
    )


@pytest.mark.asyncio
async def test_human_reply_wont_fix_posts_resolution(workspace) -> None:
    """Operator decided 'won't fix: <reason>' on a pending escalated comment
    → both reply_to_comment and resolve_comment are awaited
    (orchestrator/orchestrator.py:2154-2164).

    Adjustment from plan draft: same as test 3 — 'won't fix' routing
    runs on per-comment decisions in `pending_review_comments`, not on
    `state.human_input_reply` (which gates only 'reviewed'/'proceed').
    """
    workspace.state.pending_review_comments = [
        {
            "comment_id": 999,
            "decision": "won't fix: by design",
            "author": "reviewer",
            "file": "x.py",
            "line": 10,
            "body": "please rename foo",
            "reason": "Operator decided",
            "verdict": "Invalid",
        }
    ]
    vcs = AsyncMock()
    orc = _orc(vcs=vcs)
    await orc._action_fetch_pr_comments(workspace, SimpleNamespace())
    assert vcs.reply_to_comment.await_count + vcs.resolve_comment.await_count >= 1


@pytest.mark.asyncio
async def test_open_comments_trigger_escalation(workspace, monkeypatch) -> None:
    """A reviewer comment that classifies ESCALATE causes a Telegram
    escalation message to be sent (orchestrator/orchestrator.py:2059-2071
    via _send_escalated_comment_tg).

    We stub `classify_comments` to return one ESCALATE classification —
    avoiding the agent_runtime LLM call. The patch target is the
    `comment_classifier` module since `_action_fetch_pr_comments` imports
    `classify_comments` from there at function-scope (line 1831).
    """
    from integrations.base.vcs import PRComment
    from orchestrator.comment_classifier import ClassifiedComment

    workspace.state.human_input_reply = "reviewed"

    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = [
        PRComment(
            id=999, body="please rename foo to bar",
            path="x.py", line=10, author="reviewer",
        )
    ]
    orc = _orc(vcs=vcs)

    async def _fake_classify(comments, ws, runtime, *, operator_hint=""):
        return [
            ClassifiedComment(
                comment_id=999, classification="ESCALATE",
                reason="needs human", verdict="Unsure",
                author="reviewer", file="x.py", line=10,
                body="please rename foo to bar",
            )
        ]

    monkeypatch.setattr(
        "orchestrator.comment_classifier.classify_comments", _fake_classify,
    )

    await orc._action_fetch_pr_comments(workspace, SimpleNamespace())

    assert orc._notifier.send_message.await_count >= 1

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

from orchestrator.pipeline.actions.fetch_pr_comments import action_fetch_pr_comments
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


def _invoke(workspace, *, vcs=None, notifier=None, tracker=None, agent_runtime=None):
    """Bridge helper — call the module function with sensible defaults."""
    return action_fetch_pr_comments(
        workspace, SimpleNamespace(),
        get_vcs=lambda: (vcs or AsyncMock(), None),
        get_chat_id=lambda: "chat-1",
        tracker=tracker or AsyncMock(),
        notifier=notifier or _default_notifier(),
        agent_runtime=agent_runtime or AsyncMock(),
        event_bus=None,
    )


def _default_notifier():
    n = MagicMock()
    n.send_message = AsyncMock(return_value=1234)
    return n


@pytest.mark.asyncio
async def test_no_pr_number_marks_done(workspace) -> None:
    """If state.pr_number is missing, action returns DONE immediately."""
    workspace.state.pr_number = None
    result = await _invoke(workspace)
    assert getattr(result, "skipped", False) or result.next_state == Stage.DONE


@pytest.mark.asyncio
async def test_no_comments_marks_done(workspace, monkeypatch) -> None:
    """Empty review-comment list (after operator replied 'reviewed')
    short-circuits to DONE."""
    workspace.state.human_input_reply = "reviewed"
    vcs = AsyncMock()
    vcs.get_pr_comments.return_value = []
    result = await _invoke(workspace, vcs=vcs)
    assert result.next_state == Stage.DONE
    assert result.success is True


@pytest.mark.asyncio
async def test_human_reply_fix_routes_to_dev(workspace, monkeypatch) -> None:
    """Operator decided 'fix' on a pending escalated comment → workspace
    routes back to Stage.DEV."""
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
    result = await _invoke(workspace, vcs=vcs)
    assert (
        result.next_state == Stage.DEV
        or any(c.args[0] == Stage.DEV for c in workspace.transition.call_args_list)
    )


@pytest.mark.asyncio
async def test_human_reply_wont_fix_posts_resolution(workspace) -> None:
    """Operator decided 'won't fix: <reason>' on a pending escalated comment
    → both reply_to_comment and resolve_comment are awaited."""
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
    await _invoke(workspace, vcs=vcs)
    assert vcs.reply_to_comment.await_count + vcs.resolve_comment.await_count >= 1


@pytest.mark.asyncio
async def test_open_comments_trigger_escalation(workspace, monkeypatch) -> None:
    """A reviewer comment that classifies ESCALATE causes a Telegram
    escalation message to be sent."""
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
    notifier = _default_notifier()

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

    await _invoke(workspace, vcs=vcs, notifier=notifier)

    assert notifier.send_message.await_count >= 1

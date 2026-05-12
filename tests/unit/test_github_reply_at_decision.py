"""Every PR comment must receive a reply on GitHub at decision time —
not silently waiting for the post-fix verification reply."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.pipeline.actions.fetch_pr_comments import (
    action_fetch_pr_comments,
    execute_review_decisions,
)


def _notifier():
    n = MagicMock()
    n.send_message = AsyncMock(return_value=999)
    return n


class TestAutoFixReply:
    @pytest.mark.asyncio
    async def test_auto_fix_posts_will_fix_reply_at_classification_time(self, tmp_path, monkeypatch):
        from orchestrator.comment_classifier import ClassifiedComment

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()

        async def fake_classify(comments, ws, runtime, *, operator_hint=""):
            return [ClassifiedComment(
                comment_id=42, classification="AUTO_FIX", verdict="Valid",
                reason="Annotation missing — repo convention requires it.",
                suggested_fix="Add @Inject", author="C", file="x.kt", line=10, body="b",
            )]
        import orchestrator.comment_classifier as cc_mod
        monkeypatch.setattr(cc_mod, "classify_comments", fake_classify, raising=False)

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=0,
            stage_iterations={}, human_input_reply="reviewed",
        )
        ws.save_state = MagicMock()

        async def fake_get_comments(_pr):
            return [SimpleNamespace(
                id=42, in_reply_to_id=None, body="please fix this",
                author="C", path="x.kt", line=10,
            )]
        vcs.get_pr_comments = fake_get_comments

        await action_fetch_pr_comments(
            ws, None,
            get_vcs=lambda: (vcs, None),
            get_chat_id=lambda: "chat-1",
            tracker=None,
            notifier=_notifier(),
            agent_runtime=MagicMock(),
            event_bus=None,
        )

        # AUTO_FIX must post 'Will fix: ...' on GitHub at classification time
        vcs.reply_to_comment.assert_awaited()
        called_with = vcs.reply_to_comment.call_args.args
        assert called_with[0] == 42  # pr_number
        assert called_with[1] == 42  # comment_id
        assert called_with[2].startswith("Will fix: ")
        assert "Annotation missing" in called_with[2]

        # AUTO_FIX must NOT resolve at classification time
        vcs.resolve_comment.assert_not_awaited()


class TestGitHubReplyFailureAudit:
    @pytest.mark.asyncio
    async def test_auto_fix_records_failed_status_when_github_throws(self, tmp_path, monkeypatch):
        """When the GitHub reply API throws, the resolution report should NOT
        record 'Posted' — it should reflect the actual failure."""
        from orchestrator.comment_classifier import ClassifiedComment
        import orchestrator.comment_classifier as cc_mod

        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock(side_effect=RuntimeError("GitHub down"))
        vcs.resolve_comment = AsyncMock()

        async def fake_classify(comments, ws, runtime, *, operator_hint=""):
            return [ClassifiedComment(
                comment_id=42, classification="AUTO_FIX", verdict="Valid",
                reason="reason", suggested_fix="fix", author="C", file="x.kt", line=10, body="b",
            )]
        monkeypatch.setattr(cc_mod, "classify_comments", fake_classify, raising=False)

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", company_id="acme", repo_id="app",
            pr_number=42, current_state="PR_REVIEW",
            pending_review_comments=[], review_cycle=0,
            stage_iterations={}, human_input_reply="reviewed",
        )
        ws.save_state = MagicMock()

        async def fake_get_comments(_pr):
            return [SimpleNamespace(
                id=42, in_reply_to_id=None, body="please fix",
                author="C", path="x.kt", line=10,
            )]
        vcs.get_pr_comments = fake_get_comments

        await action_fetch_pr_comments(
            ws, None,
            get_vcs=lambda: (vcs, None),
            get_chat_id=lambda: "chat-1",
            tracker=None,
            notifier=_notifier(),
            agent_runtime=MagicMock(),
            event_bus=None,
        )

        # Audit log must reflect the failure
        report_path = tmp_path / "pr-review-resolution.md"
        assert report_path.exists()
        content = report_path.read_text()
        # add_entry writes "- Github Reply: ..." (title-cased key)
        assert "Github Reply: Failed" in content


class TestEscalateFixDecisionReply:
    @pytest.mark.asyncio
    async def test_operator_fix_decision_posts_will_fix_reply(self, tmp_path):
        vcs = MagicMock()
        vcs.reply_to_comment = AsyncMock()
        vcs.resolve_comment = AsyncMock()

        ws = MagicMock()
        ws.reports_dir = tmp_path
        ws.state = SimpleNamespace(
            ticket_id="T-1", pr_number=42, review_cycle=1, stage_iterations={},
            pending_review_comments=[
                {"comment_id": 7, "msg_ids": [100], "decision": "fix",
                 "author": "C", "file": "x.kt", "line": 10, "body": "b",
                 "reason": "Reviewer is right — annotation needed.", "verdict": "Valid",
                 "hint_rounds": 0, "last_hint": None,
                 "pending_reinvestigation": False},
            ],
        )
        ws.save_state = MagicMock()

        await execute_review_decisions(
            ws,
            get_vcs=lambda: (vcs, None),
            get_chat_id=lambda: "chat-1",
            notifier=_notifier(),
        )

        vcs.reply_to_comment.assert_awaited()
        called_with = vcs.reply_to_comment.call_args.args
        assert called_with[0] == 42
        assert called_with[1] == 7
        assert called_with[2].startswith("Will fix: ")
        assert "annotation needed" in called_with[2].lower()

        # Don't resolve until the fix is verified post-push
        vcs.resolve_comment.assert_not_awaited()

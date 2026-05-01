"""Tests for the shared escalated-comment message renderer."""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.escalation_view import build_escalated_comment_message


def _state():
    return SimpleNamespace(ticket_id="T-1", company_id="acme", repo_id="app")


def _cc():
    return {
        "comment_id": 99, "author": "Copilot", "file": "app/x.kt", "line": 10,
        "body": "Use @Inject here", "reason": "Repo convention requires @Inject.",
        "verdict": "Valid",
    }


class TestBuildMessage:
    def test_includes_verdict_line(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "Valid — Repo convention requires @Inject." in text

    def test_not_valid_verdict_renders(self):
        cc = _cc()
        cc["verdict"] = "Not valid"
        cc["reason"] = "Reviewer is wrong, file follows existing pattern."
        text, _ = build_escalated_comment_message(
            _state(), cc, pr_number=42, ticket_title="A ticket",
        )
        assert "Not valid — Reviewer is wrong" in text

    def test_initial_message_no_recall_prefix(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "still pending" not in text

    def test_recall_message_has_prefix(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket", recall=True,
        )
        assert text.startswith("🔁 (still pending)")

    def test_buttons_are_fix_and_wont_fix(self):
        _, buttons = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert [b.label for b in buttons] == ["Fix", "Won't Fix"]
        assert buttons[0].action == "pr_fix:T-1:99"
        assert buttons[1].action == "pr_wontfix:T-1:99"

    def test_unsure_verdict_falls_back_to_reason_only(self):
        cc = _cc()
        cc["verdict"] = "Unsure"
        text, _ = build_escalated_comment_message(
            _state(), cc, pr_number=42, ticket_title="A ticket",
        )
        # Defensive fallback: render reason without verdict prefix
        assert "Repo convention requires @Inject." in text
        assert "Unsure —" not in text

    def test_accepts_attribute_object(self):
        """Renderer accepts either dict or object with attributes (e.g., ClassifiedComment)."""
        cc_obj = SimpleNamespace(
            comment_id=99, author="Copilot", file="app/x.kt", line=10,
            body="b", reason="Repo convention requires @Inject.", verdict="Valid",
        )
        text, buttons = build_escalated_comment_message(
            _state(), cc_obj, pr_number=42, ticket_title="A ticket",
        )
        assert "Valid — Repo convention requires @Inject." in text
        assert buttons[0].action == "pr_fix:T-1:99"

    def test_header_uses_underscore_separator(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "_" * 30 in text

    def test_no_backticks_in_message(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="A ticket",
        )
        assert "`" not in text

    def test_header_contains_ticket_and_project(self):
        text, _ = build_escalated_comment_message(
            _state(), _cc(), pr_number=42, ticket_title="Fix crash",
        )
        assert "[acme]" in text
        assert "T-1" in text
        assert "Fix crash" in text

"""Tests for orchestrator/tg_format.py."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.tg_format import read_ticket_title, strip_markdown, tg_header

_SEP = "_" * 30


class TestTgHeader:
    def test_with_emoji_and_title(self):
        result = tg_header("❌", "acme", "T-123", "Fix login crash")
        assert result == f"❌ [acme] T-123\nFix login crash\n{_SEP}"

    def test_without_title(self):
        result = tg_header("✅", "acme", "T-123")
        assert result == f"✅ [acme] T-123\n{_SEP}"

    def test_empty_title_omits_title_line(self):
        result = tg_header("✅", "acme", "T-123", "")
        lines = result.splitlines()
        assert lines[0] == "✅ [acme] T-123"
        assert lines[1] == _SEP
        assert len(lines) == 2

    def test_no_emoji_omits_leading_space(self):
        result = tg_header("", "acme", "T-123", "Some title")
        assert result.startswith("[acme]")
        assert not result.startswith(" ")

    def test_separator_is_exactly_30_underscores(self):
        result = tg_header("✅", "acme", "T-1")
        sep_line = result.splitlines()[-1]
        assert sep_line == _SEP
        assert len(sep_line) == 30

    def test_project_id_no_repo(self):
        result = tg_header("🔔", "myproject", "TICK-99", "Some issue")
        assert "[myproject]" in result
        assert "/" not in result.splitlines()[0]


class TestStripMarkdown:
    def test_removes_inline_backticks(self):
        assert strip_markdown("`fix`") == "fix"

    def test_removes_double_backtick_code(self):
        assert strip_markdown("send `retry T-1 from dev` to restart") == "send retry T-1 from dev to restart"

    def test_removes_bold(self):
        assert strip_markdown("**Status: PASS**") == "Status: PASS"

    def test_removes_bold_with_surrounding_text(self):
        assert strip_markdown("Result: **PASS** confirmed") == "Result: PASS confirmed"

    def test_removes_triple_backtick_blocks(self):
        text = "```python\ncode here\n```"
        result = strip_markdown(text)
        assert "```" not in result
        assert "code here" in result

    def test_removes_table_separator_row(self):
        text = "| Check | Result |\n|---|---|\n| Files | None |"
        result = strip_markdown(text)
        assert "|---|" not in result
        assert "---|" not in result

    def test_converts_table_row_to_plain(self):
        text = "| Unauthorized files | None |"
        result = strip_markdown(text)
        assert "|" not in result
        assert "Unauthorized files" in result
        assert "None" in result

    def test_removes_heading_hashes(self):
        assert strip_markdown("## Summary") == "Summary"
        assert strip_markdown("# Title") == "Title"

    def test_leaves_unicode_bullets_untouched(self):
        text = "• item one\n• item two"
        assert strip_markdown(text) == text

    def test_leaves_box_drawing_untouched(self):
        text = "─" * 30
        assert strip_markdown(text) == text

    def test_leaves_emojis_untouched(self):
        text = "✅ done\n❌ failed"
        assert strip_markdown(text) == text

    def test_leaves_arrows_untouched(self):
        assert strip_markdown("ANALYSIS → DEV") == "ANALYSIS → DEV"

    def test_empty_string(self):
        assert strip_markdown("") == ""

    def test_plain_text_unchanged(self):
        text = "Pipeline stuck at ANALYSIS. Check reports/ for details."
        assert strip_markdown(text) == text

    def test_removes_underscore_italic(self):
        assert strip_markdown("_status_") == "status"

    def test_removes_underscore_italic_inline(self):
        assert strip_markdown("The _status_ is clear") == "The status is clear"

    def test_multiline_table(self):
        text = (
            "The diff was clean:\n\n"
            "| Check | Result |\n"
            "|---|---|\n"
            "| Unauthorized files | None |\n"
            "| Protected files | None |\n\n"
            "Advances to QA."
        )
        result = strip_markdown(text)
        assert "|" not in result
        assert "Unauthorized files" in result
        assert "None" in result
        assert "Advances to QA." in result


class TestReadTicketTitle:
    def test_reads_summary_field(self, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "ticket.json").write_text(
            json.dumps({"summary": "Fix login crash on Samsung", "key": "T-1"}),
            encoding="utf-8",
        )
        ws = SimpleNamespace(meta_dir=meta_dir)
        assert read_ticket_title(ws) == "Fix login crash on Samsung"

    def test_returns_empty_when_file_missing(self, tmp_path):
        ws = SimpleNamespace(meta_dir=tmp_path / "nonexistent")
        assert read_ticket_title(ws) == ""

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "ticket.json").write_text("not valid json", encoding="utf-8")
        ws = SimpleNamespace(meta_dir=meta_dir)
        assert read_ticket_title(ws) == ""

    def test_returns_empty_when_summary_key_missing(self, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "ticket.json").write_text(
            json.dumps({"key": "T-1", "status": "Open"}), encoding="utf-8",
        )
        ws = SimpleNamespace(meta_dir=meta_dir)
        assert read_ticket_title(ws) == ""

    def test_handles_no_meta_dir_attribute(self):
        ws = SimpleNamespace()  # no meta_dir attribute
        assert read_ticket_title(ws) == ""

"""Tests for the resolution report — single source of truth for PR comment decisions."""

from __future__ import annotations

from pathlib import Path

from orchestrator.resolution_report import (
    add_entry,
    read_entries,
    update_entry,
)


class TestReadEntries:
    def test_empty_file(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        entries = read_entries(report)
        assert entries == {}

    def test_reads_existing_entries(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        report.write_text(
            "# PR Review Resolution — T-1\nPR: #42\n\n"
            "## Comment #12345\n"
            "- File: a.kt:10\n"
            "- Author: @Copilot\n"
            "- Body: Fix this\n"
            "- Decision: FIX\n"
            "- Verified: PENDING\n"
        )
        entries = read_entries(report)
        assert 12345 in entries
        assert entries[12345]["decision"] == "FIX"
        assert entries[12345]["verified"] == "PENDING"
        assert entries[12345]["file"] == "a.kt:10"


class TestAddEntry:
    def test_creates_file_if_missing(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 12345, {
            "file": "a.kt:10",
            "author": "@Copilot",
            "body": "Fix this",
            "decision": "FIX",
            "decided_by": "auto-classifier",
            "verified": "PENDING",
        })
        assert report.exists()
        entries = read_entries(report)
        assert 12345 in entries

    def test_appends_to_existing(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 111, {"file": "a.kt:1", "decision": "FIX", "verified": "PENDING"})
        add_entry(report, "T-1", 42, 222, {"file": "b.kt:2", "decision": "WON'T_FIX"})
        entries = read_entries(report)
        assert len(entries) == 2


class TestUpdateEntry:
    def test_updates_existing_field(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 12345, {
            "file": "a.kt:10",
            "decision": "FIX",
            "verified": "PENDING",
        })
        update_entry(report, 12345, {"verified": "YES", "verify_commit": "abc123"})
        entries = read_entries(report)
        assert entries[12345]["verified"] == "YES"
        assert entries[12345]["verify_commit"] == "abc123"

    def test_preserves_other_fields(self, tmp_path):
        report = tmp_path / "pr-review-resolution.md"
        add_entry(report, "T-1", 42, 12345, {
            "file": "a.kt:10",
            "decision": "FIX",
            "verified": "PENDING",
        })
        update_entry(report, 12345, {"verified": "YES"})
        entries = read_entries(report)
        assert entries[12345]["file"] == "a.kt:10"
        assert entries[12345]["decision"] == "FIX"

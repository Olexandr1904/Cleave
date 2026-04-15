"""Tests for Claude Code CLI quota-error classification."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from integrations.llm.claude_code_adapter import (
    QuotaExhaustedError,
    _classify_cli_error,
)


class TestClassifyCliError:
    def test_structured_json_with_marker_and_epoch(self):
        # Epoch ms for 2026-04-14T20:00:00 UTC = 1776196800000
        stdout = json.dumps({
            "is_error": True,
            "result": "Claude AI usage limit reached|1776196800000",
        })
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        assert err.retry_at == datetime(2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc)

    def test_structured_json_is_error_but_no_marker(self):
        stdout = json.dumps({
            "is_error": True,
            "result": "Something else went wrong",
        })
        err = _classify_cli_error(stdout, "")
        assert err is None

    def test_substring_fallback_rate_limit(self):
        stdout = "Error: rate_limit hit, please slow down"
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        assert err.retry_at is None

    def test_substring_fallback_case_insensitive(self):
        stdout = ""
        stderr = "Claude AI USAGE LIMIT REACHED"
        err = _classify_cli_error(stdout, stderr)
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        assert err.retry_at is None

    def test_substring_fallback_overloaded(self):
        err = _classify_cli_error("overloaded_error: try later", "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)

    def test_unrelated_error_returns_none(self):
        err = _classify_cli_error("file not found", "")
        assert err is None

    def test_innocuous_quota_substring_does_not_match(self):
        # A path or unrelated diagnostic that merely contains "quota"
        # must NOT be classified as a quota hit.
        err = _classify_cli_error("failed to read /home/user/quota/cfg.yaml", "")
        assert err is None

    def test_empty_returns_none(self):
        assert _classify_cli_error("", "") is None

    def test_content_field_variant(self):
        # Some CLI versions put text in `content` instead of `result`
        stdout = json.dumps({
            "is_error": True,
            "content": "Claude AI usage limit reached|1776196800000",
        })
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert err.retry_at == datetime(2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc)

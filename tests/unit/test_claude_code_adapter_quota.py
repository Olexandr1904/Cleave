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

    def test_api_error_status_429(self):
        # Real format observed when a Max-subscription session limit is hit:
        # CLI exits non-zero, JSON has is_error=true and api_error_status=429,
        # result text reads "You've hit your limit · resets <time>".
        stdout = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your limit · resets 5:50pm (UTC)",
        })
        err = _classify_cli_error(stdout, "")
        assert err is not None
        assert isinstance(err, QuotaExhaustedError)
        # Reset time is human-readable text; we don't parse it. retry_at None
        # means agent_runtime applies the default DEFAULT_QUOTA_RETRY_DELAY.
        assert err.retry_at is None
        assert "hit your limit" in str(err).lower()

    def test_api_error_status_429_without_is_error_returns_none(self):
        # Defensive: only treat as quota when is_error is also true.
        stdout = json.dumps({
            "is_error": False,
            "api_error_status": 429,
            "result": "transient retry handled internally",
        })
        assert _classify_cli_error(stdout, "") is None

    def test_api_error_status_non_429_returns_none(self):
        # A 500 from the backend is not a quota hit; let it surface as-is.
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 500,
            "result": "Internal server error",
        })
        assert _classify_cli_error(stdout, "") is None

"""Tests for comment classification."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.comment_classifier import ClassifiedComment, parse_classifications


def test_parse_valid_json():
    raw = json.dumps([
        {
            "comment_id": 123,
            "classification": "AUTO_FIX",
            "reason": "Missing @PreviewAcme annotation",
            "suggested_fix": "Replace @Preview with @PreviewAcme on line 10",
        },
        {
            "comment_id": 456,
            "classification": "ESCALATE",
            "reason": "Reviewer suggests dimen resource — valid but adds scope",
            "suggested_fix": "",
        },
    ])
    results = parse_classifications(raw)
    assert len(results) == 2
    assert results[0].classification == "AUTO_FIX"
    assert results[0].comment_id == 123
    assert results[1].classification == "ESCALATE"


def test_parse_json_wrapped_in_text():
    raw = "Here is my analysis:\n" + json.dumps([
        {"comment_id": 1, "classification": "ESCALATE", "reason": "unsure", "suggested_fix": ""}
    ]) + "\nDone."
    results = parse_classifications(raw)
    assert len(results) == 1
    assert results[0].classification == "ESCALATE"


def test_parse_invalid_json_returns_empty():
    results = parse_classifications("not json at all")
    assert results == []


def test_parse_missing_fields_defaults_to_escalate():
    raw = json.dumps([{"comment_id": 789}])
    results = parse_classifications(raw)
    assert len(results) == 1
    assert results[0].classification == "ESCALATE"
    assert results[0].reason == "classification missing"


def test_parse_invalid_classification_defaults_to_escalate():
    raw = json.dumps([{"comment_id": 1, "classification": "BANANA", "reason": "test"}])
    results = parse_classifications(raw)
    assert results[0].classification == "ESCALATE"


class TestVerdict:
    def test_parses_verdict_valid(self):
        raw = json.dumps([{
            "comment_id": 1, "classification": "ESCALATE",
            "reason": "ok", "verdict": "Valid",
        }])
        result = parse_classifications(raw)
        assert len(result) == 1
        assert result[0].verdict == "Valid"

    def test_parses_verdict_not_valid(self):
        raw = json.dumps([{
            "comment_id": 2, "classification": "ESCALATE",
            "reason": "ok", "verdict": "Not valid",
        }])
        result = parse_classifications(raw)
        assert result[0].verdict == "Not valid"

    def test_missing_verdict_defaults_to_unsure_with_warning(self, caplog):
        raw = json.dumps([{
            "comment_id": 3, "classification": "ESCALATE",
            "reason": "ok",
        }])
        with caplog.at_level("WARNING"):
            result = parse_classifications(raw)
        assert result[0].verdict == "Unsure"
        assert any("verdict" in rec.message.lower() for rec in caplog.records)

    def test_invalid_verdict_value_defaults_to_unsure(self):
        raw = json.dumps([{
            "comment_id": 4, "classification": "ESCALATE",
            "reason": "ok", "verdict": "MAYBE",
        }])
        result = parse_classifications(raw)
        assert result[0].verdict == "Unsure"


class TestOperatorHint:
    @pytest.mark.asyncio
    async def test_operator_hint_threaded_to_agent_runtime(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = '[{"comment_id": 1, "classification": "ESCALATE", "verdict": "Valid", "reason": "ok"}]'
        runtime.execute = AsyncMock(return_value=result)

        ws = MagicMock()
        comments = [SimpleNamespace(id=1, author="C", path="x.kt", line=1, body="b")]

        await classify_comments(comments, ws, runtime, operator_hint="check repo X")

        ctx = runtime.execute.call_args.kwargs["extra_context"]
        assert ctx["operator_hint"] == "check repo X"

    @pytest.mark.asyncio
    async def test_default_operator_hint_is_empty_string(self):
        from orchestrator.comment_classifier import classify_comments

        runtime = MagicMock()
        result = MagicMock()
        result.success = True
        result.output = "[]"
        runtime.execute = AsyncMock(return_value=result)
        ws = MagicMock()

        await classify_comments([], ws, runtime)

        ctx = runtime.execute.call_args.kwargs["extra_context"]
        assert ctx["operator_hint"] == ""

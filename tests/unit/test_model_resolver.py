"""Tests for orchestrator/model_resolver.py."""

from __future__ import annotations

import pytest

from orchestrator.model_resolver import (
    LABEL_PREFIX,
    SHORT_NAME_TO_MODEL,
    ResolutionResult,
    model_short_name,
    resolve_ticket_model,
)


def test_empty_labels_returns_none():
    result = resolve_ticket_model([])
    assert result == ResolutionResult(model=None, warning=None)


def test_no_model_label_returns_none():
    result = resolve_ticket_model(["ai-pipeline", "frontend", "bug"])
    assert result == ResolutionResult(model=None, warning=None)


def test_single_valid_label_haiku():
    result = resolve_ticket_model(["model-haiku"])
    assert result.model == SHORT_NAME_TO_MODEL["haiku"]
    assert result.warning is None


def test_single_valid_label_opus():
    result = resolve_ticket_model(["model-opus"])
    assert result.model == SHORT_NAME_TO_MODEL["opus"]
    assert result.warning is None


def test_single_valid_label_sonnet():
    result = resolve_ticket_model(["model-sonnet"])
    assert result.model == SHORT_NAME_TO_MODEL["sonnet"]
    assert result.warning is None


def test_valid_label_with_unrelated_labels():
    result = resolve_ticket_model(["ai-pipeline", "model-opus", "frontend"])
    assert result.model == SHORT_NAME_TO_MODEL["opus"]
    assert result.warning is None


def test_unknown_short_name_returns_warning():
    result = resolve_ticket_model(["model-llama"])
    assert result.model is None
    assert result.warning is not None
    assert "model-llama" in result.warning
    assert "model-haiku" in result.warning
    assert "model-opus" in result.warning
    assert "model-sonnet" in result.warning


def test_two_valid_labels_returns_warning():
    result = resolve_ticket_model(["model-opus", "model-haiku"])
    assert result.model is None
    assert result.warning is not None
    assert "model-opus" in result.warning
    assert "model-haiku" in result.warning


def test_valid_plus_unknown_treated_as_ambiguous():
    result = resolve_ticket_model(["model-opus", "model-llama"])
    assert result.model is None
    assert result.warning is not None
    assert "model-opus" in result.warning
    assert "model-llama" in result.warning


def test_short_name_case_insensitive():
    """`model-OPUS` matches `model-opus` (case-insensitive on the short name)."""
    result = resolve_ticket_model(["model-OPUS"])
    assert result.model == SHORT_NAME_TO_MODEL["opus"]
    assert result.warning is None


def test_prefix_must_be_lowercase():
    """`MODEL-opus` does NOT match — prefix must be lowercase."""
    result = resolve_ticket_model(["MODEL-opus"])
    assert result.model is None
    assert result.warning is None


def test_label_prefix_constant():
    """The prefix is `model-`, exposed as a module constant."""
    assert LABEL_PREFIX == "model-"


def test_short_name_map_covers_three_models():
    """The mapping covers exactly haiku, opus, sonnet."""
    assert set(SHORT_NAME_TO_MODEL.keys()) == {"haiku", "opus", "sonnet"}


def test_model_short_name_reverse_map():
    """Reverse-map from full model id to short name for dashboard pills."""
    for short, full in SHORT_NAME_TO_MODEL.items():
        assert model_short_name(full) == short


def test_model_short_name_unknown_returns_none():
    assert model_short_name("claude-some-future-model") is None
    assert model_short_name("") is None

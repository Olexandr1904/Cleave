"""Resolve a Jira ticket's labels to a Claude model id.

Pure function module — no I/O, no side effects. Called once per workspace
at creation time; the result is snapshotted into WorkspaceState.model.

Keep SHORT_NAME_TO_MODEL in sync with dashboard/settings_store.ALLOWED_MODELS.
When a model version is bumped, update both lists together.
"""

from __future__ import annotations

from dataclasses import dataclass

LABEL_PREFIX = "model-"

SHORT_NAME_TO_MODEL: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
}


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving a ticket's labels.

    model    — full Claude model id, or None to use the existing fallback chain
    warning  — human-readable text to post as a Jira comment, or None
    """

    model: str | None
    warning: str | None


def resolve_ticket_model(labels: list[str]) -> ResolutionResult:
    """Inspect a ticket's labels and resolve to a Claude model id.

    Returns ResolutionResult(model=None, warning=None) when no model-* label
    is present. Returns (model_id, None) when exactly one valid label is
    present. Returns (None, warning_text) when labels are ambiguous or the
    short name is unknown.
    """
    model_labels = [lbl for lbl in labels if lbl.startswith(LABEL_PREFIX)]

    if not model_labels:
        return ResolutionResult(model=None, warning=None)

    if len(model_labels) > 1:
        return ResolutionResult(
            model=None,
            warning=(
                f"Multiple model labels found ({', '.join(f'`{l}`' for l in model_labels)}). "
                f"Falling back to global default. Please remove all but one."
            ),
        )

    label = model_labels[0]
    short_name = label[len(LABEL_PREFIX):].lower()

    if short_name not in SHORT_NAME_TO_MODEL:
        supported = ", ".join(f"`{LABEL_PREFIX}{n}`" for n in SHORT_NAME_TO_MODEL)
        return ResolutionResult(
            model=None,
            warning=(
                f"Unknown model label `{label}`. "
                f"Supported: {supported}. Falling back to global default."
            ),
        )

    return ResolutionResult(model=SHORT_NAME_TO_MODEL[short_name], warning=None)

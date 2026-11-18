"""What the skill runner's steps share: the stop and the deferral that end a step, the filling of a repo's
per-ticket path patterns, and the stop of a failed session turn."""
from __future__ import annotations

from orchestrator.skillflow.checkout import SkillCheckout
from orchestrator.skillflow.session import TurnOutcome


class Stop(Exception):
    """Cleave stops the run with this reason, `detail` shown under it; Approve tries the step again."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.detail = detail


class Deferred(Exception):
    """The run is deferred: its job ends here."""


def fill(pattern: str, key: str) -> str:
    # Not str.format: a pattern may hold other braces.
    return pattern.replace("{key}", key)


def session_failed(checkout: SkillCheckout, outcome: TurnOutcome) -> Stop:
    return Stop(f"session failed: {checkout.redact(outcome.error or 'no error text')}")

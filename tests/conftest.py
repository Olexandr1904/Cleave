"""Suite-wide safety: no test may signal a real process."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def gradle_signals(monkeypatch):
    """The pids of the Gradle daemons the code under test would stop, recorded instead of signalled: a real
    signal would reach the host's own Gradle daemons (a CI runner's, say)."""
    sent: list[int] = []
    monkeypatch.setattr("orchestrator.gradle_remediation._terminate", sent.append)
    return sent

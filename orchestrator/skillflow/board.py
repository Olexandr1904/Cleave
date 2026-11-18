"""ComparisonBoard: the dashboard's read-only view of every comparison and the tally of verdicts."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from orchestrator.reviewer.service import utcnow
from orchestrator.skillflow.compare_models import Comparison
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.sides import SideView, Workspaces, skill_view, staged_view
from orchestrator.skillflow.store import SkillStore

logger = logging.getLogger(__name__)


class ComparisonBoard:
    def __init__(self, comparisons: ComparisonStore, runs: SkillStore, workspaces: Workspaces,
                 now: Callable[[], datetime] = utcnow):
        self._comparisons = comparisons
        self._runs = runs
        self._workspaces = workspaces
        self._now = now

    def overview(self) -> dict:
        now = self._now()
        rows = []
        for c in self._comparisons.newest_first():
            rows.append({
                "id": c.id, "scope": c.scope, "ticket_id": c.ticket_id, "summary": c.summary, "skill": c.skill,
                "state": c.state, "verdict": c.verdict, "note": c.note, "created_at": c.created_at,
                "decided_at": c.decided_at,
                # A comparison stuck once and closed later keeps the old reason.
                "reason": c.reason if c.state == "stuck" else "",
                "skill_side": self._side(c, "skill", now).as_dict(),
                "staged_side": self._side(c, "staged", now).as_dict(),
            })
        # The tally over every comparison, the rows the newest 200 only.
        return {"rows": rows, "tally": self._comparisons.tally()}

    def _side(self, c: Comparison, name: str, now: datetime) -> SideView:
        try:
            # Once a verdict is in, the board shows the sides the operator decided on, not where they went since.
            stored = c.sides.get(name) if c.state != "open" else None
            if stored:
                try:
                    return SideView.from_dict(stored)
                except TypeError:
                    logger.warning("Comparison %s: stored %s side not read", c.id, name)
            rest_since = c.rest_since.get(name, "")
            if name == "skill":
                return skill_view(self._runs.run(c.run_id), rest_since, now)
            return staged_view(self._workspaces.find(c), rest_since, now)
        # AttributeError: stored sides or rest times that are no object.
        except (OSError, ValueError, TypeError, AttributeError) as e:
            # One side that cannot be read (a workspace, a stored field of a wrong shape) must not take the whole
            # board down: it shows as gone.
            logger.warning("Comparison %s: %s side not read: %s", c.id, name, type(e).__name__)
            return staged_view(None, "", now)

"""The steps of a comparison's verdict: the only writes to GitHub and the tracker a comparison makes. Each step
runs while it is not in steps_done and is recorded once it returns, so a retry or a restart continues from the step
that did not finish, and a step that finished is not made again."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from orchestrator.pr_creation import STAGED_MARK, tracker_writes_for_pr
from orchestrator.reviewer.service import _where
from orchestrator.skillflow.compare_messages import (
    BOTH_DRAFTS_LINE,
    CLOSED_LINE,
    NO_WORKSPACE_LINE,
    READY_LINE,
    SKILL_DROPPED_LINE,
    SKILL_KEPT_LINE,
    SKILL_LEFT_LINE,
    STAGED_WORKSPACE_LINE,
    TRACKER_DONE_LINE,
    TRACKER_NONE_LINE,
    TRACKER_PROBLEM_LINE,
)
from orchestrator.skillflow.compare_models import STEPS, Comparison
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.models import RUN_STATES
from orchestrator.skillflow.publish import FORGE_ERRORS
from orchestrator.skillflow.runner import WORKING, SkillRepo
from orchestrator.skillflow.sides import Workspaces, end_workspace
from orchestrator.skillflow.store import SkillStore
from workspace.workspace import InvalidTransitionError

logger = logging.getLogger(__name__)

FORGE_STEPS = ("ready", "title", "comment", "close")
_MARKS = {"staged": STAGED_MARK, "skill": " [skill]"}
_OTHER = {"staged": "skill", "skill": "staged"}
_DROPPABLE = tuple(state for state in RUN_STATES if state not in (*WORKING, "dropped"))


class StepFailed(Exception):
    """A step of the verdict did not finish. `reason` may hold forge text: the keeper redacts it before it is
    stored or shown."""

    def __init__(self, step: str, pr_number: int, reason: str) -> None:
        super().__init__(step)  # not the reason: it is not redacted yet
        self.step, self.pr_number, self.reason = step, pr_number, reason


class Verdict:
    """The steps of one comparison's verdict, from its steps_done on; each step recorded once it returns."""

    def __init__(self, comparisons: ComparisonStore, runs: SkillStore, workspaces: Workspaces, repo: SkillRepo,
                 tracker, now: Callable[[], datetime]) -> None:
        self._comparisons = comparisons
        self._runs = runs
        self._workspaces = workspaces
        self._repo = repo
        self._forge = repo.vcs
        self._tracker = tracker
        self._now = now

    async def run(self, c: Comparison) -> Comparison:
        """The comparison after its last recorded step: every step done, or the skill step waiting for a run the
        runner works (the next call tries it again). Raises StepFailed, for any exception of a step."""
        for step in STEPS:
            if step in c.steps_done:
                continue
            pr = 0
            try:
                pr = pr_of(c, step)
                lines = await getattr(self, f"_step_{step}")(c)
                if lines is None:
                    return c
                # expect: a step is recorded once, whoever else reads the row.
                recorded = self._comparisons.claim(
                    c.id, ("deciding",), self._stamp(), "deciding", expect={"steps_done": c.steps_done},
                    steps_done=[*c.steps_done, step], report=[*c.report, *lines])
            except Exception as e:
                if step in FORGE_STEPS and isinstance(e, FORGE_ERRORS):
                    raise StepFailed(step, pr, str(e) or type(e).__name__) from e
                # A bug parks the verdict on its step: it must not make the step's writes again every cycle.
                logger.warning("Comparison %s: verdict step %s: %s", c.id, step, _where(e))
                raise StepFailed(step, pr, type(e).__name__) from e
            if recorded is None:
                logger.warning("Comparison %s: verdict step %s not recorded: the comparison moved on", c.id, step)
                return c
            logger.info("Comparison %s: verdict step %s done (pr=%d)", c.id, step, pr)
            c = recorded
        return c

    async def _step_ready(self, c: Comparison) -> list:
        if _winner(c):
            await self._forge.mark_ready(pr_of(c, "ready"))
        return []

    async def _step_title(self, c: Comparison) -> list:
        winner = _winner(c)
        if not winner:
            return []
        number = pr_of(c, "title")
        info = await self._forge.pr_info(number)
        title = info.title.removesuffix(_MARKS[winner])
        if title != info.title:
            await self._forge.set_title(number, title)
        return [[READY_LINE, {"number": number, "url": info.url}]]

    async def _step_comment(self, c: Comparison) -> list:
        winner, loser = _winner(c), pr_of(c, "comment")
        # A PR already closed (by hand, or by an earlier try of this step that was not recorded) gets no comment.
        if loser and (await self._forge.pr_info(loser)).state == "open":
            await self._forge.comment_on_pr(
                loser, f"Closed by Cleave: the comparison of the two flows chose the {winner} flow's PR "
                       f"#{pr_of(c, 'ready')}.")
        return []

    async def _step_close(self, c: Comparison) -> list:
        if not _winner(c):
            return [[BOTH_DRAFTS_LINE, {}]]
        loser = pr_of(c, "close")
        if not loser:
            return []
        if (await self._forge.pr_info(loser)).state == "open":
            await self._forge.close_pr(loser)
        return [[CLOSED_LINE, {"number": loser}]]

    async def _step_tracker(self, c: Comparison) -> list:
        winner = _winner(c)
        if not winner:
            return []
        if self._tracker is None:
            return [[TRACKER_NONE_LINE, {}]]
        # Made once: a failed write is reported, never retried.
        problems = await tracker_writes_for_pr(self._tracker, self._repo.cfg, c.ticket_id,
                                               c.sides[winner]["pr_url"])
        return [[TRACKER_PROBLEM_LINE, {"problem": problem}] for problem in problems] or [[TRACKER_DONE_LINE, {}]]

    async def _step_skill(self, c: Comparison) -> list | None:
        """The run's lines; None while the runner works the run (taken up again just before the verdict): the
        step waits for it, as only the runner may change a run in those states."""
        stamp = self._stamp()
        if _winner(c) == "skill":
            if self._runs.claim(c.run_id, ("pr_open",), stamp, "pr_open", released=True) is not None:
                return [[SKILL_KEPT_LINE, {}]]
        elif (self._runs.claim(c.run_id, _DROPPABLE, stamp, "dropped", reason=f"comparison verdict: {c.verdict}")
              is not None or self._runs.claim(c.run_id, ("dropped",), stamp, "dropped") is not None):
            return [[SKILL_DROPPED_LINE, {}]]
        run = self._runs.run(c.run_id)
        if run is not None and run.state in WORKING:
            return None
        if run is not None and _winner(c) == "skill":   # parked with its PR (failed, say): not the verdict's to end
            return [[SKILL_LEFT_LINE, {"state": run.state}]]
        return []

    async def _step_staged(self, c: Comparison) -> list:
        ws = self._workspaces.find(c)
        if ws is None:
            return [[NO_WORKSPACE_LINE, {}]]
        try:
            state = str(end_workspace(ws))
        except InvalidTransitionError as e:   # the workspace moved on: it is not this verdict's to end
            logger.warning("Comparison %s: staged workspace %s not ended: %s", c.id, ws.root, type(e).__name__)
            state = type(e).__name__
        return [[STAGED_WORKSPACE_LINE, {"state": state}]]

    def _stamp(self) -> str:
        return self._now().isoformat()


def pr_of(c: Comparison, step: str) -> int:
    """The PR a forge step writes to: the winner's for ready and title, the loser's for comment and close; 0 for
    neither and the other steps."""
    winner = _winner(c)
    if not winner or step not in FORGE_STEPS:
        return 0
    side = winner if step in ("ready", "title") else _OTHER[winner]
    return c.sides.get(side, {}).get("pr_number", 0)


def _winner(c: Comparison) -> str:
    return "" if c.verdict == "neither" else c.verdict

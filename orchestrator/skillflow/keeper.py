"""The comparison keeper: watches every open comparison until both its sides are at rest and sends the operator the
comparison message, carries out each verdict tapped (verdict.py), and sends every comparison's messages from its
outbox. It runs in its own task and runs no git; besides the reply handler it is the one writer of comparisons."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from integrations.base.notifier import Button
from orchestrator.reviewer.service import _where, utcnow
from orchestrator.skillflow.chat import SendError, SkillChat
from orchestrator.skillflow.compare_messages import (
    comparison_message,
    moved_on_message,
    retry_buttons,
    stuck_message,
    verdict_buttons,
    verdict_message,
)
from orchestrator.skillflow.compare_models import STEPS, Comparison
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.runner import SkillRepo
from orchestrator.skillflow.sides import SideView, Workspaces, pair_fingerprint, skill_view, staged_view
from orchestrator.skillflow.store import SkillStore
from orchestrator.skillflow.verdict import FORGE_STEPS, StepFailed, Verdict, pr_of
from orchestrator.tg_i18n import telegram_language

logger = logging.getLogger(__name__)

REASON_LIMIT = 300   # characters of a stuck verdict's reason kept


class ComparisonKeeper:
    """Works the open and deciding comparisons of the served scopes, one cycle at a time; a comparison of another
    scope is left as it is until its scope is served."""

    def __init__(self, repos: list[SkillRepo], comparisons: ComparisonStore, runs: SkillStore, chat: SkillChat,
                 workspaces: Workspaces, *, tracker_for: Callable[[str], Any], language_of: Callable[[str], str],
                 event_bus=None, now: Callable[[], datetime] = utcnow, idle_seconds: float = 30.0) -> None:
        self._repos = {repo.scope: repo for repo in repos}
        self._comparisons = comparisons
        self._runs = runs
        self._chat = chat
        self._workspaces = workspaces
        self._tracker_for = tracker_for
        self._language_of = language_of
        self._event_bus = event_bus
        self._now = now
        self._idle = idle_seconds
        self._woken = asyncio.Event()

    def wake(self) -> None:
        self._woken.set()

    async def loop(self, stop: asyncio.Event) -> None:
        """cycle() until `stop`, waiting for wake(), idle_seconds or `stop` between two. Never raises (cancellation
        aside)."""
        while not stop.is_set():
            self._woken.clear()  # before the cycle: a tap from here on wakes the wait below
            try:
                await self.cycle()
            except Exception as e:
                logger.warning("Comparison cycle failed: %s", _where(e))
            await self._idle_wait(stop)

    async def _idle_wait(self, stop: asyncio.Event) -> None:
        waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._woken.wait())]
        try:
            await asyncio.wait(waits, timeout=self._idle, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    async def cycle(self) -> None:
        """The outboxes, then every open comparison, then every deciding one, then the outboxes again: what this
        cycle queued goes out in it. A comparison that raises holds back only itself."""
        await self.flush_outbox()
        for state, handle in (("open", self.watch), ("deciding", self.decide)):
            for c in self._comparisons.in_states((state,)):
                if c.scope not in self._repos:
                    continue
                try:
                    await handle(c)
                except Exception as e:
                    logger.warning("Comparison %s (%s %s) not handled: %s", c.id, c.scope, c.ticket_id, _where(e))
        await self.flush_outbox()

    async def watch(self, c: Comparison) -> None:
        """Keep the comparison's rest times, reopen it when a side moved on under its message, and queue a new
        comparison message when both sides are at rest as no message showed them yet."""
        now, stamp = self._now(), self._stamp()
        skill, staged = self._views(c, now)
        rest = dict(c.rest_since)
        for side, view in (("skill", skill), ("staged", staged)):
            if view.at_rest:
                rest[side] = rest.get(side) or stamp
            else:
                rest.pop(side, None)
        changes = {"rest_since": rest} if rest != c.rest_since else {}
        fingerprint = pair_fingerprint(skill, staged)
        if not (skill.at_rest and staged.at_rest):
            if c.fingerprint:   # the standing message's buttons are refused from now on, and one still queued
                changes.update(fingerprint="", ask_message_ids=[], outbox=_without_buttons(c.outbox))
            if changes:
                self._comparisons.claim(c.id, ("open",), stamp, "open", expect={"outbox": c.outbox}, **changes)
            return
        if fingerprint == c.fingerprint:
            if changes:
                self._comparisons.claim(c.id, ("open",), stamp, "open", **changes)
            return
        with telegram_language(self._language_of(c.scope)):
            message = {"text": comparison_message(c, skill, staged), "buttons": verdict_buttons(c.id, skill, staged)}
        # A message with buttons still queued is superseded by this one.
        self._comparisons.claim(
            c.id, ("open",), stamp, "open", expect={"messages": c.messages, "outbox": c.outbox},
            fingerprint=fingerprint, messages=c.messages + 1, ask_message_ids=[],
            sides={"skill": skill.as_dict(), "staged": staged.as_dict()}, outbox=_without_buttons(c.outbox),
            message=message, **changes)

    async def decide(self, c: Comparison) -> None:
        """Carry out the verdict from its next step: undone when a side moved on before its first step, stuck on a
        step that failed (or on the first, when the sides cannot be read before it), closed once every step is
        done."""
        repo = self._repos[c.scope]
        if not c.steps_done:
            try:
                moved = self._moved_side(c)
            except Exception as e:   # a side that cannot be read: parked, never left deciding with no word
                logger.warning("Comparison %s: sides not read: %s", c.id, _where(e))
                self._stuck(repo, c, _next_step_failed(c, e))
                return
            if moved:
                with telegram_language(self._language_of(c.scope)):
                    message = {"text": moved_on_message(c, moved), "buttons": []}
                self._comparisons.claim(c.id, ("deciding",), self._stamp(), "open", verdict="", decided_at="",
                                        fingerprint="", ask_message_ids=[], message=message)
                return
        try:
            verdict = Verdict(self._comparisons, self._runs, self._workspaces, repo, self._tracker_for(repo.project_id),
                              self._now)
            done = await verdict.run(c)
        except StepFailed as e:
            self._stuck(repo, c, e)
            return
        except Exception as e:   # before any step: it is the next step's failure
            logger.warning("Comparison %s: verdict not started: %s", c.id, _where(e))
            self._stuck(repo, c, _next_step_failed(c, e))
            return
        if any(step not in done.steps_done for step in STEPS):   # a step waits: the next cycle tries it again
            return
        with telegram_language(self._language_of(c.scope)):
            message = {"text": verdict_message(done), "buttons": []}
        stamp = self._stamp()
        closed = self._comparisons.claim(c.id, ("deciding",), stamp, "closed", closed_at=stamp, message=message)
        if closed is not None and self._event_bus is not None:
            self._event_bus.emit(
                "comparison_verdict", f"{c.ticket_id}: comparison verdict {c.verdict}",
                project_id=repo.project_id, ticket_id=c.ticket_id, data={"verdict": c.verdict, "scope": c.scope})

    def _stuck(self, repo: SkillRepo, c: Comparison, failed: StepFailed) -> None:
        reason = repo.checkout.redact(failed.reason)[:REASON_LIMIT]
        logger.warning("Comparison %s: verdict %s stuck at step %s", c.id, c.verdict, failed.step)
        with telegram_language(self._language_of(c.scope)):
            message = {"text": stuck_message(c, failed.step, failed.pr_number, reason),
                       "buttons": retry_buttons(c.id)}
        self._comparisons.claim(c.id, ("deciding",), self._stamp(), "stuck", reason=reason,
                                messages=c.messages + 1, ask_message_ids=[], message=message)

    def _moved_side(self, c: Comparison) -> str:
        """The side that moved on since the message the verdict was tapped on, "" when neither did."""
        skill, staged = self._views(c, self._now())
        if pair_fingerprint(skill, staged) == c.fingerprint:
            return ""
        return "skill" if skill.fingerprint != c.sides.get("skill", {}).get("fingerprint") else "staged"

    def _views(self, c: Comparison, now: datetime) -> tuple[SideView, SideView]:
        return (skill_view(self._runs.run(c.run_id), c.rest_since.get("skill", ""), now),
                staged_view(self._workspaces.find(c), c.rest_since.get("staged", ""), now))

    async def flush_outbox(self) -> None:
        """Send every comparison's outbox in order, each entry dropped from the store once it is out; a failed send
        leaves that comparison's rest for the next call. Only the keeper's own task calls it."""
        for c in self._comparisons.unsent():
            try:
                await self._send_outbox(c)
            except Exception as e:  # an outbox of a wrong shape holds back only its own comparison
                logger.warning("Comparison %s: outbox not sent: %s", c.id, type(e).__name__)

    async def _send_outbox(self, c: Comparison) -> None:
        for entry in c.outbox:
            buttons = [Button(label, action) for label, action in entry["buttons"]] or None
            # The parts of this entry an earlier flush got out before a later part failed: not sent again.
            earlier = entry.get("ids", [])
            try:
                ids = [*earlier, *await self._chat.send(c, entry["text"], buttons, skip=len(earlier))]
            except SendError as e:
                logger.warning("Comparison %s: message not sent: %s", c.id, e)
                if e.ids:
                    self._comparisons.mark_parts(c.id, e.ids, self._stamp())
                return
            # A tap counts only on the message that asked for it, on any of its parts.
            asked = ids if buttons and ids else None
            self._comparisons.drop_sent(c.id, 1, self._stamp(), ask_message_ids=asked)

    def _stamp(self) -> str:
        return self._now().isoformat()


def _next_step_failed(c: Comparison, e: Exception) -> StepFailed:
    """The failure before any step, as the next step's: with the PR that step writes to. A neither verdict writes
    to no PR: its next step is named past the forge steps."""
    skipped = FORGE_STEPS if c.verdict == "neither" else ()
    step = next((step for step in STEPS if step not in c.steps_done and step not in skipped), STEPS[-1])
    try:
        pr = pr_of(c, step)
    except (AttributeError, TypeError):   # stored sides of a wrong shape: parked all the same
        pr = 0
    return StepFailed(step, pr, type(e).__name__)


def _without_buttons(outbox: list[dict]) -> list[dict]:
    """The outbox less every entry with buttons, a half-sent one too: its buttons would be refused once out."""
    return [entry for entry in outbox if not entry["buttons"]]

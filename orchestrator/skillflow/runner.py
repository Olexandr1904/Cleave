"""The skill runner: takes the queued skill runs one at a time, from the checkout's prepare through the session's
turns to how each turn ended, and sends every run's messages from its outbox. A change is committed by the session,
checked, gated, pushed and opened as a draft PR (the steps in publish.py); a dropped or released run's worktree is
removed. A run parks where the operator moves it on, is deferred over a usage limit or a failing login, and is
interrupted by a restart. Every message it builds is in the language of the run's project (language_of)."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from config.schemas import RepoConfig
from orchestrator.reviewer.service import _where, utcnow
from orchestrator.skillflow.chat import SendError, SkillChat
from orchestrator.skillflow.checkout import CheckoutError, SkillCheckout
from orchestrator.skillflow.messages import (
    cleave_stop_message,
    deferred_line,
    failed_line,
    interrupted_message,
    no_change_message,
    parked_buttons,
    result_note,
    stop_message,
    waiting_message,
    worktree_left_line,
)
from orchestrator.skillflow.models import ENDED_STATES, SkillRun
from orchestrator.skillflow.prompts import CONTINUE, CUT_PREFIX, HostFacts, first_prompt, resume_prompt
from orchestrator.skillflow.publish import PublishSteps
from orchestrator.skillflow.session import MissingBuildVariable, TurnOutcome, run_turn, skill_env
from orchestrator.skillflow.status import read_turn_end
from orchestrator.skillflow.steps import Deferred, Stop, fill, session_failed
from orchestrator.skillflow.store import SkillStore
from orchestrator.switches import scope_of
from orchestrator.tg_i18n import ENGLISH, telegram_language

logger = logging.getLogger(__name__)

LOGIN_BACKOFF = timedelta(minutes=15)
QUOTA_RETRY = timedelta(hours=1)    # when the CLI gives no reset time, or one already past
# The third deferral in a row stops the run: session output that fakes a usage limit or a login failure cannot loop.
MAX_DEFERRALS = 3
WORKING = ("running", "gates")      # no reply takes a run in these states: the runner alone saves it
_DEFERRED_TOO_OFTEN = f"deferred {MAX_DEFERRALS} times (usage limit or claude login)"
_INTERRUPTED = "daemon restarted"


@dataclass(frozen=True)
class SkillRepo:
    project_id: str
    repo_id: str          # the key of project.repos
    cfg: RepoConfig
    checkout: SkillCheckout
    vcs: Any              # the repo's GitHubAdapter

    @property
    def scope(self) -> str:
        return scope_of(self.project_id, self.repo_id)


class SkillRunner(PublishSteps):
    """Works one skill run at a time. A reply handler shares the store: the runner takes a run only with a claim,
    saves it freely while it is running or in its gates (no reply takes it there), and saves it for the last time
    when it moves it on; from then on a reply may have it. The build slot is held only while a turn or the gates
    run."""

    def __init__(
        self,
        repos: list[SkillRepo],
        store: SkillStore,
        chat: SkillChat,
        llm,
        *,
        slot: asyncio.Semaphore,
        switches,
        credential_failing: Callable[[str], bool],
        get_mode: Callable[[], str],
        runs_dir: Path,
        secret_paths: list[Path],
        event_bus=None,
        now: Callable[[], datetime] = utcnow,
        environ: Mapping[str, str] = os.environ,
        idle_seconds: float = 30.0,
        language_of: Callable[[str], str] = lambda scope: ENGLISH,
    ) -> None:
        self._repos = {repo.scope: repo for repo in repos}
        self._store = store
        self._chat = chat
        self._llm = llm
        self._slot = slot
        self._switches = switches
        self._credential_failing = credential_failing
        self._get_mode = get_mode
        self._runs_dir = runs_dir
        self._secret_paths = secret_paths
        self._event_bus = event_bus
        self._now = now
        self._environ = environ
        self._idle = idle_seconds
        self._language_of = language_of  # (scope): the Telegram language of the run's project
        self._woken = asyncio.Event()
        # The end of the current usage-limit or login wait: no run starts before it.
        self._wait_until: datetime | None = None
        # Whether the run of the current job was taken from deferred and has ended no turn ok since: the operator
        # already knows of its wait.
        self._from_deferred = False

    def wake(self) -> None:
        self._woken.set()

    async def start(self) -> None:
        """recover() once, at start-up; the loop sends what it queued. Never raises."""
        try:
            self.recover()
        except Exception as e:
            logger.warning("Skill runs not recovered: %s", _where(e))

    async def loop(self, stop: asyncio.Event) -> None:
        """The outboxes, then the next run, until `stop`; with none to run, the dropped runs' worktrees, then it
        waits for wake(), idle_seconds or `stop`. Never raises (cancellation aside)."""
        while not stop.is_set():
            self._woken.clear()  # before the read: a run queued from here on wakes the wait below
            try:
                await self.flush_outbox()
                run = self.next_run()
                if run is not None:
                    await self.run_job(run)
                    continue
                await self.sweep_dropped()
            except Exception as e:
                logger.warning("Skill-run cycle failed: %s", _where(e))
            await self._idle_wait(stop)

    async def _idle_wait(self, stop: asyncio.Event) -> None:
        waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._woken.wait())]
        try:
            await asyncio.wait(waits, timeout=self._idle, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    def recover(self) -> None:
        """A run still running or in its gates at start-up was interrupted: it parks, Approve continuing it at the
        same step. A run that cannot be moved holds back only itself."""
        stamp = self._stamp()
        for run in self._store.in_states(WORKING):
            try:
                with telegram_language(self._language_of(run.scope)):
                    message = {"text": interrupted_message(run), "buttons": True}
                    moved = self._store.claim(run.id, (run.state,), stamp, "interrupted", reason=_INTERRUPTED,
                                              retry_at="", outbox=[*run.outbox, message])
                    if moved is not None:
                        self._emit(moved)
            except Exception as e:  # a field of a wrong shape
                logger.warning("Skill run %s not recovered: %s", run.id, type(e).__name__)

    def next_run(self) -> SkillRun | None:
        """The oldest run that is queued, or deferred and due, whose repo is configured, has tickets running and
        its forge credential not failing; None while the claude login fails or a usage-limit or login wait is
        on."""
        now = self._now()
        if self._credential_failing("claude_login"):
            return None
        if self._wait_until is not None and now < self._wait_until:
            return None
        for run in self._store.in_states(("queued", "deferred")):
            try:
                if self._runnable(run, now):
                    return run
            except Exception as e:  # a field of a wrong shape holds back only its own run
                logger.warning("Skill run %s not read for running: %s", run.id, type(e).__name__)
        return None

    def _runnable(self, run: SkillRun, now: datetime) -> bool:
        if run.state == "deferred" and datetime.fromisoformat(run.retry_at) > now:
            return False
        repo = self._repos.get(run.scope)
        return (repo is not None
                and self._switches.is_running("tickets", repo.project_id, repo.repo_id)
                and not self._credential_failing(f"forge:{run.scope}"))

    async def run_job(self, run: SkillRun) -> None:
        """Take the run and work it from its phase until it parks or ends. It never stays running: anything
        unexpected fails it (cancellation aside: recover() interrupts it then)."""
        with telegram_language(self._language_of(run.scope)):
            repo = self._repos[run.scope]
            # Only the runner moves a run into or out of deferred, so the state next_run read is the one it is taken
            # from (or the claim finds it dropped).
            self._from_deferred = run.state == "deferred"
            taken = self._store.claim(run.id, ("queued", "deferred"), self._stamp(), "running", reason="", retry_at="")
            if taken is None:  # a reply or a drop has it
                return
            self._emit(taken)
            try:
                await self._job(repo, taken)
            except Deferred:
                pass
            except Exception as e:
                logger.warning("Skill run %s (%s %s): %s", taken.id, taken.scope, taken.ticket_id, _where(e))
                await self._fail(taken, f"internal error: {type(e).__name__}")

    async def _job(self, repo: SkillRepo, run: SkillRun) -> None:
        """The run from its phase on. Cleave's own stop parks here, inside run_job's try: a save that raises while
        parking leaves the run to _fail, not running."""
        try:
            if run.phase == "work":
                await self._work(repo, run)
            else:
                await self._publish(repo, run)
        except Stop as e:
            detail = e.detail
            await self._park(run, "stopped", str(e), lambda r: cleave_stop_message(r, detail), buttons=True)

    async def flush_outbox(self) -> None:
        """Send every run's outbox in order, each entry dropped from the store once it is out; a failed send
        leaves that run's rest for the next call. It has one caller at a time, the runner's own task (the loop and
        the job it runs): two flushes at once would both send an entry before either dropped it."""
        for run in self._store.unsent_runs():
            try:
                # The buttons are built here, for whichever run's outbox this is: in that run's language.
                with telegram_language(self._language_of(run.scope)):
                    await self._send_outbox(run)
            except Exception as e:  # an outbox of a wrong shape holds back only its own run
                logger.warning("Skill run %s: outbox not sent: %s", run.id, type(e).__name__)

    async def _send_outbox(self, run: SkillRun) -> None:
        for entry in run.outbox:
            buttons = parked_buttons(run.id) if entry["buttons"] else None
            # The parts of this entry an earlier flush got out before a later part failed: not sent again.
            earlier = entry.get("ids", [])
            text = entry["text"]
            if not earlier:
                # Translated once, before its first part goes out, and kept with the run: a send again sends the
                # same parts.
                translated = await self._chat.localize(text)
                if translated != text:
                    self._store.keep_translation(run.id, text, translated, self._stamp())
                    text = translated
            try:
                ids = [*earlier, *await self._chat.send(run, text, buttons, skip=len(earlier))]
            except SendError as e:
                logger.warning("Skill run %s: message not sent: %s", run.id, e)
                if e.ids:   # on the stored row: the runner saves no run object here
                    self._store.mark_parts(run.id, e.ids, self._stamp())
                return
            # An approval counts only on the message that asked for it, on any of its parts.
            asked = ids if buttons and ids else None
            self._store.drop_sent(run.id, 1, self._stamp(), ask_message_ids=asked)

    async def sweep_dropped(self) -> None:
        """Remove the worktree of each dropped run, and of each open PR the comparison released, not cleaned yet,
        once; a worktree git refuses to remove is left as it is and the operator told. It runs in the runner's own
        task, as all of Cleave's git in the checkout does; a run that cannot be handled holds back only itself."""
        for run in self._store.in_states(("dropped", "pr_open")):
            if run.cleaned or not (run.state == "dropped" or run.released):
                continue
            try:
                await self._clean(run)
            except Exception as e:
                logger.warning("Skill run %s: worktree not cleaned: %s", run.id, type(e).__name__)

    async def _clean(self, run: SkillRun) -> None:
        with telegram_language(self._language_of(run.scope)):
            repo = self._repos.get(run.scope)
            if repo is None:  # its repo runs no skill flow on this daemon now
                return
            changes: dict[str, Any] = {"cleaned": True}
            # The worktree is the ticket's, not the run's: another run of the ticket that has not ended works in it.
            if not any(other.id != run.id and other.state not in ENDED_STATES
                       for other in self._store.runs_for_ticket(run.scope, run.ticket_id)):
                worktree = self._worktree(repo, run)
                if why := await repo.checkout.remove_worktree(worktree):
                    # The outbox as it is after the removal's await: an entry sent meanwhile must not come back.
                    outbox = self._store.run(run.id).outbox
                    changes["outbox"] = [*outbox, {"text": worktree_left_line(run, worktree, why), "buttons": False}]
            # A dropped run and a released open PR are final for the replies: this claim can take it from no one.
            self._store.claim(run.id, (run.state,), self._stamp(), run.state, **changes)

    async def _work(self, repo: SkillRepo, run: SkillRun) -> None:
        """The work phase: a run without a session is prepared; then a turn, and what its end means."""
        if not run.session_id:
            await self._prepare(repo, run)
        cold_if_failed = run.cut and not run.session_started
        prompt, resume = self._prompt(repo, run)
        outcome = await self._turn(repo, run, prompt, resume)
        if outcome.kind == "failed" and cold_if_failed and outcome.no_session:
            run.session_id = str(uuid4())
            outcome = await self._turn(repo, run, self._first_prompt(repo, run), resume=False)
        if outcome.kind == "failed":
            raise session_failed(repo.checkout, outcome)
        await self._read_turn(repo, run, outcome.text)

    async def _prepare(self, repo: SkillRepo, run: SkillRun) -> None:
        """Move the checkout to the base, check it and the build's variables, copy the ticket in and read the host
        facts; then give the run its session. A failure stops the run with none of it kept: Approve prepares
        again."""
        checkout, flow, key = repo.checkout, repo.cfg.skill_flow, run.ticket_id
        try:
            await checkout.prepare()
            if missing := checkout.missing_files(flow.required_files):
                raise Stop(f"required file missing in the skill checkout: {', '.join(missing)}")
            self._env(repo)
            checkout.write_ticket(self._runs_dir / run.id / "ticket", self._notes_rel(repo, run))
        except CheckoutError as e:
            raise Stop(str(e)) from None
        # Cleave's own staged branch and its PR are not someone else's work on the ticket.
        staged = run.facts.get("staged_branch", "")
        try:
            branches = [name for name in await checkout.branches_with(key) if name != staged]
            prs = [f"#{number} {title} ({head})"
                   for number, title, head in await repo.vcs.open_prs_for_key(key) if head != staged]
        except Exception as e:
            # The class only: a git or forge error's text is not Cleave's.
            raise Stop(f"host facts not read: {type(e).__name__}") from None
        run.facts = {**run.facts, "other_branches": branches, "other_prs": prs}
        run.session_id, run.unattended, run.started_at = str(uuid4()), self._get_mode() == "auto", self._stamp()
        self._save(run)

    def _prompt(self, repo: SkillRepo, run: SkillRun) -> tuple[str, bool]:
        """The work turn's prompt, and whether it resumes the session."""
        if not run.session_started:
            if run.cut:
                # A cut first turn may or may not have made the session: _work starts cold if the CLI knows none.
                # The operator's reply to the cut turn goes with it; the bare CONTINUE would drop it.
                return (CUT_PREFIX + resume_prompt(run.pending, run.approved) if run.pending else CONTINUE), True
            return self._first_prompt(repo, run), False
        prompt = resume_prompt(run.pending, run.approved)
        return (CUT_PREFIX + prompt if run.cut else prompt), True

    def _first_prompt(self, repo: SkillRepo, run: SkillRun) -> str:
        facts = HostFacts(repo.checkout.base_branch, self._notes_rel(repo, run),
                          run.facts.get("other_branches", []), run.facts.get("other_prs", []))
        return first_prompt(run, repo.cfg.skill_flow, facts)

    async def _turn(self, repo: SkillRepo, run: SkillRun, prompt: str, resume: bool) -> TurnOutcome:
        """One session turn, in the slot, its usage added to the run; an ok or failed outcome. A usage limit or a
        failing login defers the run (Deferred)."""
        env = self._env(repo)
        async with self._slot:
            # Saved once the slot is held, right before the CLI starts: whatever ends this turn other than ok (a
            # limit, a login, a failure, a restart), the session may hold it half-done, and the next turn must
            # continue it, not start it again. A restart while the run only waited for the slot cut no turn.
            run.cut = True
            run.turn_count += 1
            self._save(run)
            outcome = await run_turn(
                self._llm, checkout_dir=repo.checkout.path, session_dir=self._runs_dir / run.id / "session",
                prompt=prompt, session_id=run.session_id, resume=resume, model=run.model, cfg=repo.cfg.skill_flow,
                secret_paths=self._secret_paths, env=env, turn=run.turn_count,
            )
        run.cost_usd += outcome.cost_usd
        run.turns += outcome.turns
        run.tokens_in += outcome.tokens_in
        run.tokens_out += outcome.tokens_out
        if outcome.kind in ("quota", "login"):
            await self._defer(run, outcome)
        if outcome.kind == "ok":
            run.session_started, run.cut, run.deferrals = True, False, 0
            run.pending, run.approved = "", False
            # The wait the operator was told of is over: a new deferral in this job is a new one to tell.
            self._from_deferred = False
        self._save(run)
        return outcome

    async def _defer(self, run: SkillRun, outcome: TurnOutcome) -> None:
        """Defer the run past the usage limit's reset or the login backoff, with its line unless the run was taken
        from deferred (a wait the operator was told of); the third deferral in a row stops it instead. Raises
        Deferred or Stop."""
        now = self._now()
        if outcome.kind == "quota":
            retry_at, reason = outcome.retry_at, "usage limit reached"
            if retry_at is None or retry_at <= now:  # never runnable at once
                retry_at = now + QUOTA_RETRY
        else:
            retry_at, reason = now + LOGIN_BACKOFF, "claude login failing"
        retry_at = retry_at.astimezone(timezone.utc)
        self._wait_until = retry_at  # every run would meet the same limit or login
        run.deferrals += 1
        if run.deferrals >= MAX_DEFERRALS:
            raise Stop(_DEFERRED_TOO_OFTEN)
        await self._park(run, "deferred", reason, None if self._from_deferred else deferred_line,
                         retry_at=retry_at.isoformat())
        raise Deferred

    async def _read_turn(self, repo: SkillRepo, run: SkillRun, text: str) -> None:
        """What the turn's end means: a question parks the run as waiting, the skill's stop as stopped; a result
        without a change ends it as no_change, one with a change goes on to the commit."""
        end = read_turn_end(text, run.ticket_id)
        notes = repo.checkout.path / self._notes_rel(repo, run)
        if end.kind == "waiting":
            await self._park(run, "waiting", "", lambda r: waiting_message(r, text, notes), buttons=True)
            return
        run.outcome, run.status_line, run.needs_user = end.status.outcome, end.status.line, end.status.needs_user
        if end.kind == "stop":
            await self._park(run, "stopped", "", lambda r: stop_message(r, text, notes), buttons=True)
            return
        try:
            worktree = self._worktree(repo, run)
            changed = await repo.checkout.has_change(worktree)
        except CheckoutError as e:
            raise Stop(f"worktree state not read: {e}") from None
        if not changed:
            # A worktree_dir that is not where the skill makes its worktree reads as no change: the message says so.
            missing = None if worktree.is_dir() else worktree
            # No buttons: only a reply moves a run that found nothing to change.
            await self._park(run, "no_change", "", lambda r: no_change_message(r, text, notes, missing))
            return
        if run.needs_user:
            await self._say(run, result_note(run, text, notes))
        run.phase = "commit"
        self._save(run)
        await self._publish(repo, run)

    def _change(self, run: SkillRun, state: str, reason: str, retry_at: str = "",
                message: Callable[[SkillRun], str] | None = None, buttons: bool = False) -> None:
        """Move the run to `state`, its message (built from the run as it now is) in its outbox, saved together;
        and the event."""
        run.state, run.reason, run.retry_at = state, reason, retry_at
        run.updated_at = self._stamp()  # deferred_line reads it
        if message is not None:
            run.outbox.append({"text": message(run), "buttons": buttons})
        self._store.save(run, run.updated_at)
        self._emit(run)

    async def _park(self, run: SkillRun, state: str, reason: str, message: Callable[[SkillRun], str] | None = None,
                    *, buttons: bool = False, retry_at: str = "") -> None:
        """Move the run out of running (parked, deferred or ended) and send its message. This is the object's last
        save: a reply may take the run from here on."""
        self._change(run, state, reason, retry_at, message, buttons)
        await self._flush(run)

    async def _say(self, run: SkillRun, text: str) -> None:
        """A line for the operator while the run goes on."""
        run.outbox.append({"text": text, "buttons": False})
        self._save(run)
        await self._flush(run)

    async def _flush(self, run: SkillRun) -> None:
        await self.flush_outbox()
        # What went out is gone from the stored outbox: a later save of this object must not bring it back.
        if (stored := self._store.run(run.id)) is not None:
            run.outbox, run.unsent, run.ask_message_ids = stored.outbox, stored.unsent, stored.ask_message_ids

    async def _fail(self, run: SkillRun, reason: str) -> None:
        """Fail the run as the store has it: the object may be ahead of its row (a save that raised half-way)."""
        stored = self._store.run(run.id)
        if stored is None or stored.state not in WORKING:  # parked already: a reply may have taken it since
            return
        await self._park(stored, "failed", reason, failed_line)

    def _env(self, repo: SkillRepo) -> dict[str, str]:
        git = repo.cfg.git
        try:
            return skill_env(self._environ, repo.cfg.skill_flow.build_env, git.commit_author_name,
                             git.commit_author_email)
        except MissingBuildVariable as e:
            raise Stop(f"build variable {e} is not set in the daemon's environment") from None

    def _notes_rel(self, repo: SkillRepo, run: SkillRun) -> str:
        return fill(repo.cfg.skill_flow.notes_dir, run.ticket_id)

    def _worktree(self, repo: SkillRepo, run: SkillRun) -> Path:
        return repo.checkout.worktree(fill(repo.cfg.skill_flow.worktree_dir, run.ticket_id))

    def _emit(self, run: SkillRun) -> None:
        if self._event_bus is None:
            return
        repo = self._repos.get(run.scope)
        self._event_bus.emit(
            "skill_run_state", f"{run.ticket_id}: skill run {run.state}",
            project_id=repo.project_id if repo else None, ticket_id=run.ticket_id,
            data={"run_id": run.id, "state": run.state, "reason": run.reason, "phase": run.phase},
        )

    def _save(self, run: SkillRun) -> None:
        self._store.save(run, self._stamp())

    def _stamp(self) -> str:
        return self._now().isoformat()

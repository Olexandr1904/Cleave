"""The skill runner's publish steps: the session commits the change and writes the PR description, the commit is
checked, Cleave's gates run on it (a failing one goes back to the session), and the commit they passed is pushed and
opened as a draft PR."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from orchestrator.pr_creation import pr_title
from orchestrator.quality_gates import GateResult
from orchestrator.skillflow.checkout import CheckoutError
from orchestrator.skillflow.gates import failing, gate_commands, run_gates
from orchestrator.skillflow.messages import pr_message, relay
from orchestrator.skillflow.models import PHASES, SkillRun
from orchestrator.skillflow.prompts import CUT_PREFIX, DESCRIPTION_FILE, commit_prompt, fix_prompt
from orchestrator.skillflow.steps import Stop, session_failed

if TYPE_CHECKING:
    from orchestrator.skillflow.runner import SkillRepo

DESCRIPTION_LIMIT = 60000           # characters of the PR body (GitHub takes 65536)
# How many times the push step gates a branch again that moved since its gates before it stops the run: a gate that
# commits on every run would otherwise gate for ever. Two: one move by hand and one by a gate are both gated.
MAX_REGATES = 2
_GATE_TAIL = 600                    # characters of a failing gate's output in the stop message
_FORGE_REASON = 300
FORGE_ERRORS = (RuntimeError, httpx.HTTPError)    # what the GitHub adapter raises when a request fails


def _gate_detail(failures: list[GateResult]) -> str:
    """Each failing gate's command, exit code and the end of its output, for the stop message."""
    return "\n\n".join(f"{f.name}: `{f.command}` exited {f.exit_code}\n{f.output_tail.rstrip()[-_GATE_TAIL:]}"
                       for f in failures)


class PublishSteps:
    """The commit, gates and push steps of SkillRunner (a mixin): it uses the host's _turn, _save, _park, _emit,
    _env, _worktree, _notes_rel, _slot, _from_deferred and _stamp."""

    async def _publish(self, repo: SkillRepo, run: SkillRun) -> None:
        """The commit, gates and push steps, from run.phase on. A step that passes moves the run to the next and
        saves it; one that does not stops the run (Stop, Deferred) in the step Approve continues it from. Any
        other phase, work among them (it is _work's), fails the run: nothing falls through to the push."""
        if run.phase not in PHASES[1:]:   # commit, gates, push
            raise ValueError(f"unknown phase {run.phase!r}")
        if run.phase == "commit":
            await self._commit(repo, run)
        elif run.phase == "gates":
            await self._gates_again(repo, run)
        passed = await self._gates(repo, run) if run.phase == "gates" else None
        await self._push(repo, run, passed)

    async def _commit(self, repo: SkillRepo, run: SkillRun) -> None:
        """The session commits the change and writes the PR description; once that passes the commit check, the
        gates start with all their rounds."""
        text = await self._publish_turn(repo, run, commit_prompt(self._description_path(repo, run)))
        await self._check_commit(repo, run, text)
        run.phase, run.gate_round = "gates", 0

    async def _gates_again(self, repo: SkillRepo, run: SkillRun) -> None:
        """The gates step of a run taken up again. The tree is checked first: a fix turn that did not end ok, or a
        hand, may have changed it since. Approve gives gates that still failed their rounds again."""
        if not self._from_deferred:
            run.gate_round = 0
        await self._check_commit(repo, run, "")

    async def _gates(self, repo: SkillRepo, run: SkillRun) -> list[GateResult]:
        """Cleave's gates on the checked tree. A failing gate goes back to the session, at most gate_rounds times,
        and its fix is checked and gated again; then the run goes on to the push with the commit they passed: the
        head read before they ran, so a commit made while they ran is gated again by the push step."""
        run.state = "gates"
        self._save(run)
        self._emit(run)
        while True:
            judged = await self._head(repo, run)
            results = await self._run_gates(repo, run)
            if not (failures := failing(results)):
                break
            if run.gate_round >= repo.cfg.skill_flow.gate_rounds:
                names = ", ".join(f.name for f in failures)
                raise Stop(f"gates still failing after {run.gate_round} rounds: {names}",
                           repo.checkout.redact(_gate_detail(failures)))
            run.gate_round += 1
            text = await self._publish_turn(repo, run, fix_prompt(failures, self._description_path(repo, run)))
            await self._check_commit(repo, run, text)
        run.gated_sha = judged
        run.phase = "push"
        self._save(run)
        return results

    async def _run_gates(self, repo: SkillRepo, run: SkillRun) -> list[GateResult]:
        """The gates once, in the slot, with the session's own environment: no token reaches a gate command."""
        env, worktree = self._env(repo), self._worktree(repo, run)
        try:
            changed = await repo.checkout.changed_files(worktree)
        except CheckoutError as e:
            raise Stop(f"changed files not read: {e}") from None
        async with self._slot:
            return await run_gates(worktree, gate_commands(repo.cfg), env, changed_files=changed)

    async def _push(self, repo: SkillRepo, run: SkillRun, passed: list[GateResult] | None) -> None:
        """Push the commit the gates passed, then take the branch's open PR or open a draft one; the run ends as
        pr_open. The commit is checked first, and a branch that moved since its gates (a hand, between a stop and
        Approve, or a gate that commits) is gated again before anything is pushed, at most MAX_REGATES times."""
        regated = 0
        while True:
            await self._check_commit(repo, run, "")
            if await self._head(repo, run) == run.gated_sha:
                break
            if regated == MAX_REGATES:
                raise Stop(f"the branch kept moving: it moved each of the {MAX_REGATES} times its gates ran again")
            regated += 1
            # Back in the gates step: a stop in them is a stop there, and Approve gives their rounds again.
            run.phase = "gates"
            self._save(run)
            passed = await self._gates(repo, run)
        # Saved before git runs: a push that raises, or is cut by a restart, may have landed, and the branch it
        # put on the forge is still this run's own.
        run.push_branch = run.branch
        self._save(run)
        try:
            await repo.checkout.push(run.branch, run.gated_sha)
        except CheckoutError as e:
            raise Stop(f"push failed: {e}") from None
        # Saved at once: whatever ends the run from here (a PR not opened, an internal error that parks the stored
        # row), a Drop must not tell the operator nothing was pushed.
        run.pushed = True
        self._save(run)
        # After the push, never instead of it: a PR open for the branch since an earlier try shows this commit too.
        found = await repo.vcs.find_pr_by_branch(run.branch) or await self._open_pr(repo, run)
        run.pr_number, run.pr_url = found
        self._save(run)  # the PR stands: kept even when its message cannot be built
        # Only a run whose gates all passed reaches this step. On a retry their results are gone with the job that
        # ran them, and the gates that passed are the configured ones.
        gates = passed if passed is not None else [GateResult(name, command, 0, True, "")
                                                    for name, command in gate_commands(repo.cfg).items()]
        await self._park(run, "pr_open", "", lambda r: pr_message(r, gates))

    async def _open_pr(self, repo: SkillRepo, run: SkillRun) -> tuple[int, str]:
        title = pr_title(run.ticket_id, run.summary, " [skill]")
        body = repo.checkout.redact(self._description(repo, run))[:DESCRIPTION_LIMIT]
        try:
            return await repo.vcs.open_pr(title, body, run.branch, repo.checkout.base_branch, draft=True)
        except FORGE_ERRORS as e:
            # The PR may stand all the same (an answer lost to a timeout, its retry told "already exists"), and
            # the first find_pr_by_branch answers None also when its own request failed.
            if (found := await repo.vcs.find_pr_by_branch(run.branch)) is not None:
                return found
            # Redacted before it is cut, so a token straddling the cut cannot leak in part.
            text = " ".join(repo.checkout.redact(str(e)).split())[:_FORGE_REASON]
            raise Stop(f"pull request not opened: {text}") from None

    async def _publish_turn(self, repo: SkillRepo, run: SkillRun, prompt: str) -> str:
        """A commit or fix turn of the session, which holds the change: its final text. After a turn that did not
        end ok the prompt says it may repeat one."""
        outcome = await self._turn(repo, run, CUT_PREFIX + prompt if run.cut else prompt, resume=True)
        if outcome.kind == "failed":
            raise session_failed(repo.checkout, outcome)
        return outcome.text

    async def _check_commit(self, repo: SkillRepo, run: SkillRun, text: str) -> None:
        """The commit check. A problem stops the run in the commit step with the session's answer, `text`: Approve
        asks for the commit again."""
        branch, problem = await self._commit_problem(repo, run)
        if problem:
            run.phase = "commit"
            raise Stop(f"commit check failed: {problem}", relay(text))
        run.branch = branch
        self._save(run)  # the gates and the push go by it

    async def _commit_problem(self, repo: SkillRepo, run: SkillRun) -> tuple[str, str]:
        """(branch, problem): the change on a branch Cleave may push (none already on the forge but the one this
        run's last push went to), everything committed and past the base, and the PR description written; the
        problem is "" when there is none."""
        try:
            branch, problem = await repo.checkout.commit_state(self._worktree(repo, run))
            # A branch already on the forge is someone else's (a colleague's that carries the key, a release
            # branch): the push would fast-forward it under the operator's account. Only the branch this run's own
            # push may have put there is its own, landed or not as far as Cleave knows.
            own = branch == run.push_branch
            foreign = bool(branch) and not own and await repo.checkout.on_forge(branch)
        except CheckoutError as e:
            return "", str(e)
        # Neither the base nor the staged flow's own branch: a push there would land on someone else's work.
        if branch and branch in (repo.checkout.base_branch, run.facts.get("staged_branch")):
            return branch, f"the change is on {branch}, which Cleave never pushes"
        if foreign:
            return branch, f"the change is on {branch}, which is already on the forge and is not this run's"
        if problem:
            return branch, problem
        if self._description_path(repo, run).is_symlink():
            return branch, "the PR description file is a symlink"
        if not self._description(repo, run).strip():
            return branch, "the PR description file is missing"
        return branch, ""

    def _description(self, repo: SkillRepo, run: SkillRun) -> str:
        """The PR description the session wrote in the main checkout; "" when there is none. A symlink is never
        read: it can lead to any file the daemon can read, and the text is published."""
        path = self._description_path(repo, run)
        if path.is_symlink():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _description_path(self, repo: SkillRepo, run: SkillRun) -> Path:
        return repo.checkout.path / self._notes_rel(repo, run) / DESCRIPTION_FILE

    async def _head(self, repo: SkillRepo, run: SkillRun) -> str:
        try:
            return await repo.checkout.head(self._worktree(repo, run))
        except CheckoutError as e:
            raise Stop(f"commit not read: {e}") from None

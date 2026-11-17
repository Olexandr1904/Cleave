"""SkillIntake: a skill run for the ticket of a workspace the staged flow has just created, with a copy of the
ticket's files."""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from config.schemas import RepoConfig, SkillFlowConfig
from integrations.base.tracker import TicketData
from orchestrator.reviewer.service import utcnow
from orchestrator.skillflow.models import ENDED_STATES, SkillRun
from orchestrator.skillflow.store import SkillStore
from orchestrator.switches import scope_of
from workspace.workspace import Stage


def choose_skill(cfg: SkillFlowConfig, issue_type: str) -> str:
    bug = issue_type.casefold() in {t.casefold() for t in cfg.bug_types}
    return cfg.bug_skill if bug else cfg.task_skill


def _symlinks(directory: str, names: list[str]) -> list[str]:
    # Left out rather than copied as links: the session reads the copy, and a link would take it outside the
    # ticket's files.
    return [name for name in names if (Path(directory) / name).is_symlink()]


class SkillIntake:
    def __init__(self, store: SkillStore, runs_dir: Path, chat_id_for: Callable[[RepoConfig], str],
                 wake: Callable[[], None], now: Callable[[], datetime] = utcnow, *,
                 serves: Callable[[str], bool] = lambda scope: True):
        self._store = store
        self._runs_dir = runs_dir
        self._chat_id_for = chat_id_for
        self._wake = wake
        self._now = now
        self._serves = serves   # whether the runner takes this scope's runs

    def create(self, workspace, ticket: TicketData, project_id: str, repo_cfg: RepoConfig) -> SkillRun | None:
        """A queued run with its copy at runs_dir/<run id>/ticket/; None when the repo runs only the staged flow or
        no runner serves it, the workspace resumes an existing PR, or the ticket already has a run that has not
        ended."""
        if repo_cfg.ticket_flow != "both" or workspace.state.current_state == Stage.PR_REVIEW:
            return None
        scope = scope_of(project_id, workspace.state.repo_id)
        # A run no runner takes would stay queued with nobody told.
        if not self._serves(scope):
            return None
        if any(run.state not in ENDED_STATES for run in self._store.runs_for_ticket(scope, ticket.id)):
            return None
        data = {
            "skill": choose_skill(repo_cfg.skill_flow, ticket.issue_type),
            "summary": ticket.summary,
            "model": workspace.state.model,
            "chat_id": self._chat_id_for(repo_cfg),
            "facts": {"assignee": ticket.assignee or "", "status": ticket.status,
                      "staged_branch": workspace.state.branch or ""},
        }
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        # The copy is made before the run is stored, so no queued run is ever without its ticket files; the run's
        # id names the directory only once the store has given it.
        staging = Path(tempfile.mkdtemp(prefix=".intake-", dir=self._runs_dir))
        try:
            # Taken now: the staged flow's agents add their own files to meta_dir later.
            shutil.copytree(workspace.meta_dir, staging / "ticket", ignore=_symlinks)
            run = self._store.add_run(scope, ticket.id, self._now().isoformat(), **data)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            staging.rename(self._runs_dir / run.id)
        except BaseException as e:
            # The run is stored by now: it ends, or it would stay queued without its ticket files.
            self._store.claim(run.id, ("queued",), self._now().isoformat(), "failed",
                              reason=f"ticket copy not stored: {type(e).__name__}")
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._wake()
        return run

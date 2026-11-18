"""The two sides of a comparison as they are now, finding the staged side's workspace, and ending it."""
from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orchestrator.skillflow.compare_models import SKILL_AT_REST, STAGED_AT_REST, Comparison
from orchestrator.skillflow.models import SkillRun
from workspace.workspace import InvalidTransitionError, Stage, Workspace, archive_workspace

logger = logging.getLogger(__name__)

# The figures each agent run's _write_log appends to its log, after the line's stamp.
_TOKENS = re.compile(r"\binput_tokens=(\d+) output_tokens=(\d+)\b")
_STAMP = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")


@dataclass(frozen=True)
class SideView:
    state: str            # the run's or the workspace's state; "gone" when there is none to read
    at_rest: bool
    pr_number: int        # 0: no PR
    pr_url: str
    outcome: str          # skill: run.outcome, else run.reason; staged: state.error or ""
    minutes: int          # from intake to when it came to rest (rest_since), else to `now`
    tokens_in: int
    tokens_out: int
    fingerprint: str      # changes whenever the side moved on

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SideView:
        return cls(**data)


_GONE = SideView("gone", True, 0, "", "", 0, 0, 0, "gone")


def skill_view(run: SkillRun | None, rest_since: str, now: datetime) -> SideView:
    if run is None:
        return _GONE
    return SideView(state=run.state, at_rest=run.state in SKILL_AT_REST, pr_number=run.pr_number,
                    pr_url=run.pr_url, outcome=run.outcome or run.reason,
                    minutes=_minutes(run.created_at, rest_since, now), tokens_in=run.tokens_in,
                    tokens_out=run.tokens_out,
                    fingerprint=f"{run.state}|{run.turn_count}|{run.reason}|{run.pr_number}")


def staged_view(ws: Workspace | None, rest_since: str, now: datetime) -> SideView:
    if ws is None:
        return _GONE
    state = ws.state
    pr_number, error = state.pr_number or 0, state.error or ""
    # Not ws.logs_dir: it makes the directory, and a read must not bring back a workspace that was removed.
    tokens_in, tokens_out = staged_tokens(ws.root / "logs", datetime.fromisoformat(state.started_at))
    # Not last_updated_at: a compared PR review rewrites it every poll cycle without the side moving on.
    return SideView(state=state.current_state, at_rest=state.current_state in STAGED_AT_REST,
                    pr_number=pr_number, pr_url=state.pr_url or "", outcome=error,
                    minutes=_minutes(state.started_at, rest_since, now), tokens_in=tokens_in,
                    tokens_out=tokens_out, fingerprint=f"{state.current_state}|{pr_number}|{error}")


def staged_tokens(logs_dir: Path, since: datetime) -> tuple[int, int]:
    """Input and output tokens over every agent run logged in `logs_dir` at or after `since`; a log that cannot be
    read counts 0, a line whose stamp cannot be read is skipped."""
    tokens_in = tokens_out = 0
    if not logs_dir.is_dir():
        return 0, 0
    for log in sorted(logs_dir.glob("*.log")):
        # Only <agent>.log: an agent's tool log can hold any text, its events log has figures of its own.
        if log.name.endswith((".events.log", "-tools.log")):
            continue
        try:
            text = log.read_text(errors="replace")
        except OSError:   # no warning: every keeper cycle and board refresh reads it again
            continue
        for line in text.splitlines():
            # Every workspace of a ticket has the same root and its logs outlive a clean: an earlier workspace's
            # runs are in the same files.
            if (m := _TOKENS.search(line)) and (logged := _logged_at(line)) is not None and logged >= since:
                tokens_in += int(m[1])
                tokens_out += int(m[2])
    return tokens_in, tokens_out


def _logged_at(line: str) -> datetime | None:
    """When the line was written; None when its stamp cannot be read."""
    if not (m := _STAMP.match(line)):
        return None
    try:
        # _write_log stamps with time.strftime: the host's local time, with no zone.
        return datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return None


def pair_fingerprint(skill: SideView, staged: SideView) -> str:
    return f"{skill.fingerprint}#{staged.fingerprint}"


def _minutes(start: str, rest_since: str, now: datetime) -> int:
    end = datetime.fromisoformat(rest_since) if rest_since else now
    return int((end - datetime.fromisoformat(start)).total_seconds() // 60)


class Workspaces:
    """Finds a comparison's staged workspace: among the active ones, else on disk once it left that list."""

    def __init__(self, active: Callable[[], list[Workspace]]):
        self._active = active

    def find(self, c: Comparison) -> Workspace | None:
        for ws in self._active():
            if _is_for(ws, c):
                return ws
        if not c.workspace_root:
            return None
        ws = Workspace(c.workspace_root)
        if not ws.state_path.exists():
            return None
        try:
            ws.state
        # AttributeError, KeyError: a state.json that is JSON but not an object.
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            logger.warning("Workspace %s not read: %s", c.workspace_root, type(e).__name__)
            return None
        # The ticket may have been run again since: a workspace started at another time is not this comparison's.
        return ws if _is_for(ws, c) else None


def _is_for(ws: Workspace, c: Comparison) -> bool:
    return ws.state.ticket_id == c.ticket_id and ws.state.started_at == c.workspace_started_at


def end_workspace(ws: Workspace) -> str:
    """End the staged workspace at the verdict; the state it ended in. A workspace already ended (DONE, ARCHIVED)
    is left as it is, so a replay of the step changes nothing; one that moved on (DEFERRED, a working state) raises
    InvalidTransitionError: it is not the verdict's to end."""
    current = ws.state.current_state
    if current == Stage.PR_REVIEW:
        # Not through on_ticket_done: the verdict makes the tracker write and the notice itself.
        ws.transition(Stage.DONE)
    elif current in (Stage.BLOCKED, Stage.FAILED):
        if current == Stage.BLOCKED:
            ws.transition(Stage.FAILED)
        archive_workspace(ws)   # the dashboard's archive, from FAILED on
    elif current not in (Stage.DONE, Stage.ARCHIVED):
        raise InvalidTransitionError(f"{current} is not ended by a verdict")
    return ws.state.current_state

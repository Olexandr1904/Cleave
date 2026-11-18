"""The skill flow's Telegram messages: Cleave's lines in the current Telegram language around the skill's own text,
relayed unchanged, as are the status line, the outcome, the reason and the paths. Each closing line offers only what
a reply or tap can do in the run's state."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from integrations.base.notifier import Button
from orchestrator.quality_gates import GateResult
from orchestrator.skillflow.models import SkillRun
from orchestrator.tg_i18n import N_, tr

# Characters of the skill's text a message relays: a long report would otherwise take many Telegram parts, and the
# session's own files are in the notes directory.
RELAY_LIMIT = 9000
_CUT = N_("… (cut: see the notes directory for the session's files)")
# The leading "stopped" as the status reader finds it, with the markup and the colon around it.
_STOPPED = re.compile(r"^[\W_]*stopped(?![^\W_])[\s`*_~\])]*:?[\s`*_~]*", re.IGNORECASE)


def header(run: SkillRun) -> str:
    return tr("{ticket} · skill run ({skill})", ticket=run.ticket_id, skill=run.skill)


def relay(text: str) -> str:
    if len(text) <= RELAY_LIMIT:
        return text
    return f"{text[:RELAY_LIMIT]}\n{tr(_CUT)}"


def parked_buttons(run_id: str) -> list[Button]:
    return [Button(tr("Approve"), f"sk_ok:{run_id}"), Button(tr("Drop"), f"sk_drop:{run_id}")]


def waiting_message(run: SkillRun, text: str, notes: Path) -> str:
    return _relaying(header(run), text, notes, tr("Reply to answer, or tap Approve to say ok."))


def stop_message(run: SkillRun, text: str, notes: Path) -> str:
    """The skill's own stop: its outcome, "stopped: <condition>", gives the condition."""
    condition = _STOPPED.sub("", run.outcome, count=1).strip()
    return _relaying(_stopped(run, condition), text, notes,
                     tr("Reply with new information to continue, or tap Drop."))


def cleave_stop_message(run: SkillRun, detail: str = "") -> str:
    """Cleave stopped the run; run.reason says why and `detail` (a gate's output, say) shows it."""
    first = _stopped(run, run.reason)
    closing = tr("Approve tries this step again; a reply goes to the session.")
    return f"{first}\n\n{detail}\n\n{closing}" if detail else f"{first}\n{closing}"


def result_note(run: SkillRun, text: str, notes: Path) -> str:
    """A result with "needs user: yes": the operator is told, and the run goes on without waiting."""
    return _relaying(tr("{header} — {outcome}, needs you", header=header(run), outcome=run.outcome), text, notes,
                     tr("Cleave goes on to the commit, its gates and the draft PR."))


def no_change_message(run: SkillRun, text: str, notes: Path, missing_worktree: Path | None = None) -> str:
    """`missing_worktree`: the worktree path Cleave looked in and found nothing at, which is why it saw no change."""
    first = tr("{header} — {outcome} (no change)", header=header(run), outcome=run.outcome)
    if missing_worktree is not None:
        first = f"{first}\n{tr('No worktree at {path}.', path=missing_worktree)}"
    return _relaying(first, text, notes, tr("A reply resumes the session."))


def pr_message(run: SkillRun, gates: list[GateResult]) -> str:
    results = ", ".join(f"{g.name} {'ok' if g.passed else f'exit {g.exit_code}'}" for g in gates)
    lines = [tr("{header} — draft PR opened: {url}", header=header(run), url=run.pr_url), run.status_line,
             tr("Gates: {results}", results=results or tr("none configured"))]
    return "\n".join(line for line in lines if line)


def deferred_line(run: SkillRun) -> str:
    """The retry time in UTC, with its day when that is not the day the run was deferred on."""
    at = _utc(run.retry_at)
    when = f"{at:%H:%M}" if at.date() == _utc(run.updated_at).date() else f"{at.day} {at:%b %H:%M}"
    return tr("{header} — deferred until {when} UTC ({reason})", header=header(run), when=when, reason=run.reason)


def interrupted_message(run: SkillRun) -> str:
    first = tr("{header} — interrupted by a restart at the {phase} step", header=header(run), phase=run.phase)
    return f"{first}\n{tr('Approve continues it; Drop ends it.')}"


def failed_line(run: SkillRun) -> str:
    return tr("{header} — failed: {reason}", header=header(run), reason=run.reason)


def worktree_left_line(run: SkillRun, worktree: Path, why: str) -> str:
    return tr("{header} — worktree left at {path}: {why}", header=header(run), path=worktree, why=why)


def _stopped(run: SkillRun, why: str) -> str:
    if not why:
        return tr("{header} — stopped", header=header(run))
    return tr("{header} — stopped: {why}", header=header(run), why=why)


def _relaying(first: str, text: str, notes: Path, closing: str) -> str:
    # An empty text (a turn that ended saying nothing) leaves out its block rather than a run of blank lines.
    return "\n\n".join(block for block in (first, relay(text), f"{tr('Notes: {path}', path=notes)}\n{closing}")
                       if block)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(timezone.utc)

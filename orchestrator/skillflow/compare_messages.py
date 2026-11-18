"""The comparison's Telegram messages and buttons, in the current Telegram language. Side states, URLs, outcomes
and reasons are shown as they are.

A verdict's closing lines are stored in c.report as [template, values] pairs, the template one of the *_LINE
constants below, so the closing message is translated only when it is built."""
from __future__ import annotations

from orchestrator.skillflow.compare_models import Comparison
from orchestrator.skillflow.sides import SideView
from orchestrator.tg_i18n import N_, tr

READY_LINE = N_("Ready for review: PR #{number} {url}")
CLOSED_LINE = N_("Closed: PR #{number}")
BOTH_DRAFTS_LINE = N_("Both PRs stay drafts.")
TRACKER_DONE_LINE = N_('Tracker: the "PR opened" comment and the move to review are made.')
TRACKER_PROBLEM_LINE = N_("Tracker: {problem}")
TRACKER_NONE_LINE = N_("Tracker: none configured.")
STAGED_WORKSPACE_LINE = N_("Staged workspace: {state}")
NO_WORKSPACE_LINE = N_("Staged workspace: not found.")
SKILL_DROPPED_LINE = N_("Skill run: dropped.")
SKILL_KEPT_LINE = N_("Skill run: kept; its worktree is removed.")
SKILL_LEFT_LINE = N_("Skill run: {state}; left as it is.")

# The verdict's steps as a stuck message names them; a step not listed is named as it is.
_STEP_NAMES = {
    "ready": N_("marking PR #{number} ready"),
    "title": N_("retitling PR #{number}"),
    "comment": N_("commenting on PR #{number}"),
    "close": N_("closing PR #{number}"),
    "tracker": N_("tracker writes"),
    "skill": N_("ending the skill run"),
    "staged": N_("ending the staged workspace"),
}


def comparison_message(c: Comparison, skill: SideView, staged: SideView) -> str:
    return "\n".join([
        tr("{ticket} · comparison: {summary}", ticket=c.ticket_id, summary=c.summary),
        tr("Staged: {side}", side=_side(staged)),
        tr("Skill ({skill}): {side}", skill=c.skill, side=_side(skill)),
        tr("Tap the better side. A reply to this message is kept as the verdict's note."),
    ])


def verdict_buttons(cid: str, skill: SideView, staged: SideView) -> list[list[str]]:
    buttons = []
    if staged.pr_number:
        buttons.append([tr("Staged"), f"sk_win_staged:{cid}"])
    if skill.pr_number:
        buttons.append([tr("Skill"), f"sk_win_skill:{cid}"])
    buttons.append([tr("Neither"), f"sk_win_none:{cid}"])
    return buttons


def retry_buttons(cid: str) -> list[list[str]]:
    return [[tr("Retry"), f"sk_retry:{cid}"]]


def moved_on_message(c: Comparison, side: str) -> str:
    return tr("{ticket} · comparison: the {side} side moved on after your verdict; nothing was done. A new message"
              " comes when both sides are at rest.", ticket=c.ticket_id, side=side_name(side))


def stuck_message(c: Comparison, step: str, pr_number: int, reason: str) -> str:
    name = tr(_STEP_NAMES[step], number=pr_number) if step in _STEP_NAMES else step
    return tr("{ticket} · verdict {verdict} not finished: {step} failed: {reason}\nRetry continues from that step.",
              ticket=c.ticket_id, verdict=_verdict_name(c.verdict), step=name, reason=reason)


def verdict_message(c: Comparison) -> str:
    lines = [tr("{ticket} · verdict: {verdict}", ticket=c.ticket_id, verdict=_verdict_name(c.verdict))]
    lines += [tr(template, **values) for template, values in c.report]
    return "\n".join(lines)


def side_name(side: str) -> str:
    return tr("staged") if side == "staged" else tr("skill")


def duration(minutes: int) -> str:
    if minutes < 60:
        return tr("{minutes} min", minutes=minutes)
    return tr("{hours} h {minutes:02d} min", hours=minutes // 60, minutes=minutes % 60)


def _verdict_name(verdict: str) -> str:
    return tr("neither") if verdict == "neither" else side_name(verdict)


def _side(view: SideView) -> str:
    time = duration(view.minutes)
    if view.pr_number:
        return tr("PR #{number} {url} · {state} · {time} · {tokens_in} in / {tokens_out} out tokens",
                  number=view.pr_number, url=view.pr_url, state=view.state, time=time,
                  tokens_in=view.tokens_in, tokens_out=view.tokens_out)
    return tr("no PR · {state}{outcome} · {time} · {tokens_in} in / {tokens_out} out tokens",
              state=view.state, outcome=f": {view.outcome}" if view.outcome else "", time=time,
              tokens_in=view.tokens_in, tokens_out=view.tokens_out)

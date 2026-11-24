"""What blocks a PR's merge, in Cleave's words (GitHub's own text is never shown), and when Re-run CI applies: a
merge GitHub blocks while no check ran on the PR's head, as after a stacked PR's base merged and GitHub retargeted
it (a base change starts no workflow)."""
from __future__ import annotations

import unicodedata

from orchestrator.reviewer.models import Check, MergeState

GENERIC = "GitHub refused the merge (GitHub 405): checks, reviews or conflicts block it."
_NAME_CHARS = 40
_BASE_CHARS = 60
_NAMED = 5
REVIEW_BLOCKS = ("REVIEW_REQUIRED", "CHANGES_REQUESTED")  # a merge blocked on a review: no commit starts what it needs


def _clean(text: str, limit: int) -> str:
    """GitHub's text on one line: control and format characters dropped, whitespace runs one space, cut to `limit`."""
    kept = "".join(" " if ch.isspace() else ch for ch in text
                   if ch.isspace() or not unicodedata.category(ch).startswith("C"))
    kept = " ".join(kept.split())
    return kept if len(kept) <= limit else kept[:limit - 1] + "…"


def _names(checks: list[Check]) -> str:
    named = ", ".join(f"{_clean(c.name, _NAME_CHARS)} ({c.word})" for c in checks[:_NAMED])
    more = len(checks) - _NAMED
    return f"{named} and {more} more" if more > 0 else named


def refusal_text(state: MergeState | None, number: int, *, rerun: bool | None = None) -> str:
    """Why GitHub refused the merge (405), from the PR's merge state; the generic text without one. With no check on
    the head it points to Re-run CI where the card offers it (`rerun`; by default no_checks), else to a push."""
    if state is None:
        return GENERIC
    if rerun is None:
        rerun = no_checks(state, state.head_sha)
    base, sha = _clean(state.base, _BASE_CHARS), state.head_sha[:9]
    refused = "GitHub refused the merge: "
    if state.status == "DIRTY":
        return f"{refused}#{number} has conflicts with {base}."
    if state.status == "BEHIND":
        return f"{refused}#{number} is behind {base}: update its branch."
    if state.status == "DRAFT":
        return f"{refused}#{number} is a draft."
    if state.review == "CHANGES_REQUESTED":
        return f"{refused}changes were requested on #{number}."
    if state.review == "REVIEW_REQUIRED":
        return f"{refused}#{number} needs an approving review."
    if not state.checks:
        how = "Re-run CI on its card starts them." if rerun else "push a commit to start them."
        return f"{refused}no checks ran on {sha}, and {base} waits for them (a base that changed starts none): {how}"
    failed = [c for c in state.checks if c.state == "failed"]
    if failed:
        return f"{refused}checks failed on {sha}: {_names(failed)}."
    pending = [c for c in state.checks if c.state == "pending"]
    if pending:
        return f"{refused}checks still run on {sha}: {_names(pending)}. Merge once they pass."
    if state.status == "UNKNOWN":
        return f"{refused}GitHub is still working out whether #{number} can merge: try again in a minute."
    return f"{refused}a rule of {base} blocks it, though no check on {sha} runs or failed: see #{number} on GitHub."


def no_checks(state: MergeState, head: str) -> bool:
    """Re-run CI applies: GitHub blocks the merge, no check ran on `head` (still the PR's head), no review blocks it
    (an empty commit starts nothing a review needs) and its branch is in the PR's own repo (Cleave cannot push to a
    fork's). A merge nothing blocks (a PR into a branch no workflow runs for) and one GitHub still computes (UNKNOWN)
    do not."""
    return (state.status == "BLOCKED" and not state.checks and state.head_sha == head and not state.cross_repo
            and state.review not in REVIEW_BLOCKS)


def rerun_message(ticket_key: str, head: str) -> str:
    """The empty commit's message: the ticket key first, as the PR's other commits have it."""
    words = f"re-run CI (no checks ran on {head[:9]})"
    return f"{ticket_key}: {words}" if ticket_key else words[0].upper() + words[1:]

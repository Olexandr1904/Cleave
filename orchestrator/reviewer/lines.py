"""The reviewer's Telegram lines, in English."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from orchestrator.reviewer.board import TurnChange
from orchestrator.reviewer.models import BoardRow


def turn_line(scope: str, row: BoardRow, change: TurnChange) -> str:
    if change.action == "answer_threads":
        return (
            f"[{scope}] #{row.number}: replies in {len(change.threads)} thread(s)"
            f" — handle them locally · {row.url}"
        )
    return f"[{scope}] #{row.number}: your turn — {change.action} · {row.url}"


def large_line(scope: str, row: BoardRow) -> str:
    return (
        f"[{scope}] #{row.number} is large ({row.changed_files} files)"
        f" — no automatic review · {row.url}"
    )


def readiness_failed_line(scope: str, reason: str, retry_minutes: int) -> str:
    return (
        f"[{scope}] readiness command failed: {reason}. Keeping the last board;"
        f" next try in {retry_minutes} min."
    )


def poll_failed_line(scope: str, reason: str, minutes: int) -> str:
    return (
        f"[{scope}] poll failed: {reason}. Keeping the last board;"
        f" next try in {minutes} min."
    )


def doc_missing_line(scope: str, doc: str, base: str) -> str:
    return f"[{scope}] reviews are off: {doc} is not on {base}."


def job_failed_line(scope: str, number: int, kind: str, reason: str) -> str:
    return f"[{scope}] #{number}: {kind} failed — {reason}"


def job_deferred_line(scope: str, number: int, kind: str, retry_at: datetime) -> str:
    return (
        f"[{scope}] #{number}: {kind} deferred — usage limit reached;"
        f" next try at {retry_at:%H:%M} UTC"
    )


def login_line() -> str:
    return (
        "[reviewer] claude is not logged in on the host. Log in from a GUI Terminal"
        " (run claude, then /login); reviews wait until then."
    )


def drift_line(scope: str, doc: str, section: str) -> str:
    return (
        f'[{scope}] The "{section}" section of {doc} changed on the base branch:'
        " check that Cleave's Prepare still matches it."
    )


def worktree_left_line(scope: str, number: int, path: Path, reason: str) -> str:
    return f"[{scope}] #{number}: worktree {path} was not removed — {reason}"


def outdated_line(state: str, reason: str) -> str:
    if state == "superseded":
        if reason == "new head":
            return "Outdated: there is a review of a newer head. Nothing was done."
        return "Outdated: there is a newer review. Nothing was done."
    if state == "stale":
        return f"Outdated: this report no longer matches the PR ({reason}). Nothing was done."
    return f'This job ended {state}: only "review again" and "skip" work here. Nothing was done.'


def pr_closed_line(number: int) -> str:
    return f"PR #{number} is closed: nothing posted."


def head_moved_line(old: str, new: str) -> str:
    return (f"Head moved ({old[:9]} → {new[:9]}): nothing posted."
            ' Reply "review again" for a review of the new head.')


def already_reviewed_line(when: str) -> str:
    return (f"You already submitted a review on this PR after the report ({when}): nothing posted."
            ' Add "anyway" to your reply to post regardless.')


def body_problem_line(number: int, problem: str) -> str:
    return f'#{number}: its text {problem}: nothing posted. Fix it with "edit {number}: …".'


def recheck_body_problem_line(number: int, problem: str) -> str:
    """A re-check message takes no "edit"."""
    return (f"#{number}: its text {problem}: nothing posted. A re-check reply cannot be edited here:"
            " answer that thread on GitHub yourself.")


def not_posted_line(number: int, why: str) -> str:
    return f"Not posted on #{number}: {why}."


def nothing_left_line(number: int, head: str) -> str:
    return f"Nothing left to post on #{number} at {head[:9]}: what you named is already posted."


def summary_landed_line(number: int, head: str) -> str:
    return f'The Summary already landed at #{number} at {head[:9]}: add "no summary". Nothing was done.'


def posted_line(number: int, head: str, event: str, urls: Sequence[str], inline: Sequence[int],
                moved: Sequence[int], in_body: Sequence[int], skipped: Sequence[int]) -> str:
    k = len(urls)
    parts = [f"{label}: {_spaced(numbers)}" for label, numbers in (
        ("inline", inline), ("moved anchor", moved), ("in the review body", in_body),
        ("already posted, skipped", skipped)) if numbers]
    return (f"Posted on #{number} at {head[:9]} — {event}, {k} review{'' if k == 1 else 's'}: {' '.join(urls)}\n"
            + (" · ".join(parts) or "Summary only"))


def replies_posted_line(number: int, results: Sequence[str]) -> str:
    """`results`: one line per named reply, in order."""
    return f"Posted replies on #{number}:\n" + "\n".join(results)


def replies_partial_line(number: int, why: str, results: Sequence[str], by_hand: Sequence[str], retry: str,
                         url: str = "") -> str:
    """Some of the named replies landed. `why`: what did not; `results`: a line per reply that landed (or was
    posted before); `by_hand`: what only the operator can finish; `retry`: the reply that posts the rest, ""
    when none can; `url`: set when a step may have landed unseen."""
    text = f"Partly posted replies on #{number} — {why}.\n" + "\n".join(results)
    text += "".join(f"\n{line}" for line in by_hand)
    if url:
        text += f"\nCheck the PR on GitHub before posting again: {url}"
    return text + (f'\nReply "{retry}" to post the rest.' if retry else "")


def left_out_line(number: int, why: str, url: str) -> str:
    """The replies tool plans nothing, but GitHub does not show what it leaves out as posted."""
    return (f"Nothing posted on #{number}: the replies tool has nothing to post, but GitHub does not show"
            f" everything posted ({why}). Check the PR on GitHub: {url}")


def partial_line(number: int, head: str, landed_batches: Sequence[tuple[int, str]], total: int,
                 landed: Sequence[int], missing: Sequence[int], why: str, retry: str,
                 unconfirmed: Sequence[int] = ()) -> str:
    """`retry` is the reply that posts the rest, "" when nothing is left to post; `unconfirmed` the batches the
    tool reports posted that GitHub does not show."""
    batches = ", ".join(str(i) for i, _ in landed_batches)
    urls = " ".join(url for _, url in landed_batches)
    text = (f"Partly posted on #{number} at {head[:9]}: batch {batches} of {total} landed ({urls});"
            f" landed {_spaced(landed) or 'none'}; not landed {_spaced(missing) or 'none'} — {why}.")
    if unconfirmed:
        text += (f" The tool also reports batch {', '.join(map(str, unconfirmed))} posted, which GitHub does not"
                 " show: check the PR on GitHub.")
    return text + (f'\nReply "{retry}" to post the rest.' if retry else "")


def not_shown_line(number: int, why: str, url: str) -> str:
    """A post that may have landed (the tool reports it, or GitHub shows something else new) but that GitHub
    does not show as planned."""
    return (f"Posting on #{number} ran, but GitHub does not show it as planned ({why})."
            f" Check the PR on GitHub before posting again: {url}")


def already_approved_line(number: int, head: str) -> str:
    return f"Already approved #{number} at {head[:9]}. Nothing was done."


def approve_unknown_line(number: int, head: str) -> str:
    return (f'An approve of #{number} at {head[:9]} may have landed: check the PR, then add "anyway" to your reply'
            " to approve again. Nothing was done.")


def not_understood_line(kind: str) -> str:
    """`kind`: the anchored job's (review or recheck)."""
    if kind == "recheck":
        examples = '"post replies 1 3", "text 1" or "review again"'
    else:
        examples = '"post 1 2 4 approve", "text 2", "edit 2: …", "draft 7" or "review again"'
    return f"Not understood; nothing was done. Reply like {examples}."


def interrupted_line(scope: str, number: int, kind: str = "review", numbers: Sequence[int] = ()) -> str:
    """`kind`: the Post's (review, approve or replies); `numbers`: the ones it named. A re-check's replies take no
    "anyway": only those GitHub does not show are named again."""
    if kind == "replies":
        named = f" {_spaced(numbers)}" if numbers else ""
        return (f"[{scope}] #{number}: a post of replies{named} was interrupted by a restart. Check the PR, then"
                ' reply "post replies …" with the ones GitHub does not show.')
    return (f'[{scope}] #{number}: a post was interrupted by a restart. Check the PR, then reply "post …" again'
            ' with "anyway": what landed is skipped.')


def unverified_line(number: int, error: str, url: str) -> str:
    return (f"Posting on #{number} ran, but GitHub could not be read to check it ({error})."
            f" Check the PR before posting again: {url}")


def approved_line(number: int, head: str, url: str) -> str:
    return f"Approved #{number} at {head[:9]} without a body: {url}"


def github_unreadable_line(number: int, error: str) -> str:
    return f"GitHub could not be read for #{number} ({error}): nothing posted."


def internal_error_line(number: int, name: str) -> str:
    """Posting stopped on an error of Cleave's once the confirm run (or the approve) had run: it may have landed."""
    return (f"Posting on #{number} ran, but stopped on an internal error ({name}): it may have landed. Check the PR"
            " before posting again.")


def edited_line(scope: str, pr: int, n: int, item_text: str) -> str:
    return (f"[{scope}] #{pr}: new text (nothing posted)\n\n{item_text}"
            f'\n\nReply "post {n}" to post it, or "edit {n}: …" again.')


def drafted_line(scope: str, pr: int, n: int, item_text: str) -> str:
    return (f"[{scope}] #{pr}: drafted as a question (nothing posted)\n\n{item_text}"
            f'\n\nReply "post {n}" to post it.')


def small_failed_line(scope: str, pr: int, kind: str, n: int, reason: str) -> str:
    """`kind`: edit or draft."""
    return f"[{scope}] #{pr}: {kind} of {n} failed — {reason}"


def _spaced(numbers: Iterable[int]) -> str:
    return " ".join(map(str, numbers))

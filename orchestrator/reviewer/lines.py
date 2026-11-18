"""The reviewer's Telegram lines, in the current Telegram language. A reason or an error a line names is Cleave's own
text when the catalogue holds it, else it is translated like a model's (tg_i18n.detail)."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from orchestrator.reviewer.board import TurnChange
from orchestrator.reviewer.models import BoardRow
from orchestrator.tg_i18n import ENGLISH, N_, count, current_language, detail, tr

# The job kinds and turn actions a line names, for tr(): an action the readiness tool names otherwise stays as is.
_NAMES = (N_("review"), N_("recheck"), N_("address"), N_("edit"), N_("draft"))


def turn_line(scope: str, row: BoardRow, change: TurnChange) -> str:
    if change.action == "answer_threads":
        return tr(
            "[{scope}] #{number}: replies in {threads} thread(s) — handle them locally · {url}",
            scope=scope, number=row.number, threads=len(change.threads), url=row.url,
        )
    return tr("[{scope}] #{number}: your turn — {action} · {url}", scope=scope, number=row.number,
              action=tr(change.action), url=row.url)


def large_line(scope: str, row: BoardRow) -> str:
    return tr("[{scope}] #{number} is large ({files} files) — no automatic review · {url}",
              scope=scope, number=row.number, files=row.changed_files, url=row.url)


def readiness_failed_line(scope: str, reason: str, retry_minutes: int) -> str:
    return tr("[{scope}] readiness command failed: {reason}. Keeping the last board; next try in {minutes} min.",
              scope=scope, reason=detail(reason), minutes=retry_minutes)


def poll_failed_line(scope: str, reason: str, minutes: int) -> str:
    return tr("[{scope}] poll failed: {reason}. Keeping the last board; next try in {minutes} min.",
              scope=scope, reason=detail(reason), minutes=minutes)


def doc_missing_line(scope: str, doc: str, base: str) -> str:
    return tr("[{scope}] reviews are off: {doc} is not on {base}.", scope=scope, doc=doc, base=base)


def job_failed_line(scope: str, number: int, kind: str, reason: str) -> str:
    return tr("[{scope}] #{number}: {kind} failed — {reason}", scope=scope, number=number, kind=tr(kind),
              reason=detail(reason))


def job_deferred_line(scope: str, number: int, kind: str, retry_at: datetime) -> str:
    return tr("[{scope}] #{number}: {kind} deferred — usage limit reached; next try at {at} UTC",
              scope=scope, number=number, kind=tr(kind), at=f"{retry_at:%H:%M}")


def login_line() -> str:
    return tr(
        "[reviewer] claude is not logged in on the host. Log in from a GUI Terminal"
        " (run claude, then /login); reviews wait until then."
    )


def drift_line(scope: str, doc: str, section: str) -> str:
    return tr(
        '[{scope}] The "{section}" section of {doc} changed on the base branch:'
        " check that Cleave's Prepare still matches it.",
        scope=scope, section=section, doc=doc,
    )


def outdated_line(state: str, reason: str) -> str:
    if state == "superseded":
        if reason == "new head":
            return tr("Outdated: there is a review of a newer head. Nothing was done.")
        return tr("Outdated: there is a newer review. Nothing was done.")
    if state == "stale":
        return tr("Outdated: this report no longer matches the PR ({reason}). Nothing was done.",
                  reason=detail(reason))
    return tr('This job ended {state}: only "review again" and "skip" work here. Nothing was done.', state=state)


def pr_closed_line(number: int) -> str:
    return tr("PR #{number} is closed: nothing posted.", number=number)


def head_moved_line(old: str, new: str) -> str:
    return tr('Head moved ({old} → {new}): nothing posted. Reply "review again" for a review of the new head.',
              old=old[:9], new=new[:9])


def already_reviewed_line(when: str) -> str:
    return tr('You already submitted a review on this PR after the report ({when}): nothing posted.'
              ' Add "anyway" to your reply to post regardless.', when=when)


def body_problem_line(number: int, problem: str) -> str:
    return tr('#{number}: its text {problem}: nothing posted. Fix it with "edit {number}: …".', number=number,
              problem=detail(problem))


def recheck_body_problem_line(number: int, problem: str) -> str:
    """A re-check message takes no "edit"."""
    return tr("#{number}: its text {problem}: nothing posted. A re-check reply cannot be edited here:"
              " answer that thread on GitHub yourself.", number=number, problem=detail(problem))


def address_body_problem_line(number: int, problem: str) -> str:
    """An address message takes no "edit": a new run rewrites the reply."""
    return tr('#{number}: its text {problem}: nothing posted. Reply "again: …" to have it rewritten.', number=number,
              problem=detail(problem))


def not_posted_line(number: int, why: str) -> str:
    return tr("Not posted on #{number}: {why}.", number=number, why=detail(why))


def not_pushed_line(number: int, why: str) -> str:
    return tr("Not pushed to #{number}: {why}.", number=number, why=detail(why))


def pushed_line(number: int, sha: str, url: str, fixed: Sequence[int]) -> str:
    """`fixed`: the numbers of the replies that report the fix and are not posted yet."""
    text = tr("Pushed {sha} to #{number}: {url}", sha=sha[:9], number=number, url=url)
    if fixed:
        text += "\n" + tr('Reply "post replies {numbers}" to answer the fixed threads.', numbers=_spaced(fixed))
    return text


def nothing_left_line(number: int, head: str) -> str:
    return tr("Nothing left to post on #{number} at {head}: what you named is already posted.", number=number,
              head=head[:9])


def summary_landed_line(number: int, head: str) -> str:
    return tr('The Summary already landed at #{number} at {head}: add "no summary". Nothing was done.',
              number=number, head=head[:9])


def posted_line(number: int, head: str, event: str, urls: Sequence[str], inline: Sequence[int],
                moved: Sequence[int], in_body: Sequence[int], skipped: Sequence[int]) -> str:
    parts = [f"{label}: {_spaced(numbers)}" for label, numbers in (
        (tr("inline"), inline), (tr("moved anchor"), moved), (tr("in the review body"), in_body),
        (tr("already posted, skipped"), skipped)) if numbers]
    return (tr("Posted on #{number} at {head} — {event}, {reviews}: {urls}", number=number, head=head[:9],
               event=event, reviews=count(len(urls), "review"), urls=" ".join(urls)) + "\n"
            + (" · ".join(parts) or tr("Summary only")))


def replies_posted_line(number: int, results: Sequence[str]) -> str:
    """`results`: one line per named reply, in order."""
    return tr("Posted replies on #{number}:", number=number) + "\n" + "\n".join(results)


def replies_partial_line(number: int, why: str, results: Sequence[str], by_hand: Sequence[str], retry: str,
                         url: str = "") -> str:
    """Some of the named replies landed. `why`: what did not; `results`: a line per reply that landed (or was
    posted before); `by_hand`: what only the operator can finish; `retry`: the reply that posts the rest, ""
    when none can; `url`: set when a step may have landed unseen."""
    text = tr("Partly posted replies on #{number} — {why}.", number=number, why=detail(why)) + "\n"
    text += "\n".join(results)
    text += "".join(f"\n{line}" for line in by_hand)
    if url:
        text += "\n" + tr("Check the PR on GitHub before posting again: {url}", url=url)
    return text + ("\n" + tr('Reply "{retry}" to post the rest.', retry=retry) if retry else "")


def left_out_line(number: int, why: str, url: str) -> str:
    """The replies tool plans nothing, but GitHub does not show what it leaves out as posted."""
    return tr("Nothing posted on #{number}: the replies tool has nothing to post, but GitHub does not show"
              " everything posted ({why}). Check the PR on GitHub: {url}", number=number, why=detail(why), url=url)


def partial_line(number: int, head: str, landed_batches: Sequence[tuple[int, str]], total: int,
                 landed: Sequence[int], missing: Sequence[int], why: str, retry: str,
                 unconfirmed: Sequence[int] = ()) -> str:
    """`retry` is the reply that posts the rest, "" when nothing is left to post; `unconfirmed` the batches the
    tool reports posted that GitHub does not show."""
    batches = ", ".join(str(i) for i, _ in landed_batches)
    urls = " ".join(url for _, url in landed_batches)
    none = tr("none")
    text = tr("Partly posted on #{number} at {head}: batch {batches} of {total} landed ({urls});"
              " landed {landed}; not landed {missing} — {why}.", number=number, head=head[:9], batches=batches,
              total=total, urls=urls, landed=_spaced(landed) or none, missing=_spaced(missing) or none,
              why=detail(why))
    if unconfirmed:
        text += " " + tr("The tool also reports batch {batches} posted, which GitHub does not show: check the PR"
                         " on GitHub.", batches=", ".join(map(str, unconfirmed)))
    return text + ("\n" + tr('Reply "{retry}" to post the rest.', retry=retry) if retry else "")


def not_shown_line(number: int, why: str, url: str) -> str:
    """A post that may have landed (the tool reports it, or GitHub shows something else new) but that GitHub
    does not show as planned."""
    return tr("Posting on #{number} ran, but GitHub does not show it as planned ({why})."
              " Check the PR on GitHub before posting again: {url}", number=number, why=detail(why), url=url)


def already_approved_line(number: int, head: str) -> str:
    return tr("Already approved #{number} at {head}. Nothing was done.", number=number, head=head[:9])


def approve_unknown_line(number: int, head: str) -> str:
    return tr('An approve of #{number} at {head} may have landed: check the PR, then add "anyway" to your reply'
              " to approve again. Nothing was done.", number=number, head=head[:9])


def not_understood_line(kind: str) -> str:
    """`kind`: the anchored job's (review, recheck or address)."""
    if kind == "address":
        return tr('Not understood; nothing was done. Reply like "push", "post replies 1 3", "text 2", "diff" or '
                  '"again: …".')
    if kind == "recheck":
        return tr('Not understood; nothing was done. Reply like "post replies 1 3", "text 1" or "review again".')
    return tr('Not understood; nothing was done. Reply like "post 1 2 4 approve", "text 2", "edit 2: …",'
              ' "draft 7" or "review again".')


def interrupted_line(scope: str, number: int, kind: str = "review", numbers: Sequence[int] = ()) -> str:
    """`kind`: the Post's (review, approve, replies or push); `numbers`: the ones it named. A re-check's replies take
    no "anyway": only those GitHub does not show are named again."""
    if kind == "push":
        return tr('[{scope}] #{number}: a push was interrupted by a restart. Check the PR\'s commits, then reply'
                  ' "push" again if the fix is not there.', scope=scope, number=number)
    if kind == "replies":
        named = f" {_spaced(numbers)}" if numbers else ""
        return tr('[{scope}] #{number}: a post of replies{named} was interrupted by a restart. Check the PR, then'
                  ' reply "post replies …" with the ones GitHub does not show.', scope=scope, number=number,
                  named=named)
    return tr('[{scope}] #{number}: a post was interrupted by a restart. Check the PR, then reply "post …" again'
              ' with "anyway": what landed is skipped.', scope=scope, number=number)


def unverified_line(number: int, error: str, url: str) -> str:
    return tr("Posting on #{number} ran, but GitHub could not be read to check it ({error})."
              " Check the PR before posting again: {url}", number=number, error=detail(error), url=url)


def approved_line(number: int, head: str, url: str) -> str:
    return tr("Approved #{number} at {head} without a body: {url}", number=number, head=head[:9], url=url)


def github_unreadable_line(number: int, error: str) -> str:
    return tr("GitHub could not be read for #{number} ({error}): nothing posted.", number=number,
              error=detail(error))


def internal_error_line(number: int, name: str) -> str:
    """Posting stopped on an error of Cleave's once the confirm run (or the approve) had run: it may have landed."""
    return tr("Posting on #{number} ran, but stopped on an internal error ({name}): it may have landed. Check the PR"
              " before posting again.", number=number, name=name)


def edited_line(scope: str, pr: int, n: int, item_text: str) -> str:
    return (tr("[{scope}] #{pr}: new text (nothing posted)", scope=scope, pr=pr) + f"\n\n{item_text}\n\n"
            + tr('Reply "post {n}" to post it, or "edit {n}: …" again.', n=n) + github_note())


def drafted_line(scope: str, pr: int, n: int, item_text: str) -> str:
    return (tr("[{scope}] #{pr}: drafted as a question (nothing posted)", scope=scope, pr=pr)
            + f"\n\n{item_text}\n\n" + tr('Reply "post {n}" to post it.', n=n) + github_note())


def small_failed_line(scope: str, pr: int, kind: str, n: int, reason: str) -> str:
    """`kind`: edit or draft."""
    return tr("[{scope}] #{pr}: {kind} of {n} failed — {reason}", scope=scope, pr=pr, kind=tr(kind), n=n,
              reason=detail(reason))


def github_note() -> str:
    """After a finding's or a reply's text shown in another language than English: what GitHub gets. "" in
    English."""
    if current_language() == ENGLISH:
        return ""
    return "\n\n" + tr("(GitHub gets the English text.)")


def _spaced(numbers: Iterable[int]) -> str:
    return " ".join(map(str, numbers))

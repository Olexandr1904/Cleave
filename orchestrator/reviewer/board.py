"""The operator's board: snapshot + forge details -> rows, and which of my turns are new or gone."""
from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.reviewer.models import BoardRow, BoardSnapshot, PRDetails

JOB_ACTIONS = frozenset({"review", "recheck"})
# The author-side turns an address job takes, in a repo with process.address_doc.
ADDRESS_ACTIONS = frozenset({"answer_threads", "resolve_threads", "address_review"})
# The kinds of job whose session writes the PR's worktree and whose patch "push" commits: an address job (review
# threads) and a findings job (the confirmed findings of the operator's self-review).
FIX_KINDS = ("address", "findings")
TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def job_kind(action: str, address: bool) -> str | None:
    """The kind of job a turn of `action` gets: review, recheck, address (only when `address`), else None (a
    turn line)."""
    if action in JOB_ACTIONS:
        return action
    if address and action in ADDRESS_ACTIONS:
        return "address"
    return None


@dataclass(frozen=True)
class TurnChange:
    number: int
    action: str
    threads: tuple[str, ...]


@dataclass
class BoardUpdate:
    rows: dict[int, BoardRow]
    new_turns: list[TurnChange]
    gone_turns: list[TurnChange]


def _roles(details: PRDetails, me: str, reviewed: bool) -> list[str]:
    roles = []
    if details.author == me:
        roles.append("author")
    if me in details.assignees:
        roles.append("assignee")
    if me in details.requested_reviewers or reviewed:
        roles.append("reviewer")
    return roles


def _changes(number: int, turns: list[dict], against: set[str]) -> list[TurnChange]:
    """One change per action not in `against`, with the threads of the first turn of that action.

    An action can repeat (one `address_review` turn per reviewer); it is still one turn line.
    """
    changes: dict[str, TurnChange] = {}
    for t in turns:
        if t["action"] not in against:
            changes.setdefault(t["action"], TurnChange(number, t["action"], tuple(t["threads"])))
    return list(changes.values())


def _thread_turns(turns: list[dict]) -> list[dict]:
    """My turns that name threads: a re-check of a posted reply waits for a change among these."""
    return [t for t in turns if t["threads"]]


def build_board(
    snapshot: BoardSnapshot,
    details: dict[int, PRDetails],
    reviewed: dict[int, bool],
    previous: dict[int, BoardRow],
    pipeline_prs: set[int],
    now: str,
) -> BoardUpdate:
    me = snapshot.me
    rows: dict[int, BoardRow] = {}
    new_turns: list[TurnChange] = []
    for pr in snapshot.prs:
        d = details.get(pr.number)
        if d is None or d.state != "open":  # not open any more, maybe closed after the list
            continue
        roles = _roles(d, me, reviewed.get(pr.number, False))
        if not roles:
            continue
        my_turns = [
            {"action": t.action, "threads": list(t.threads)} for t in pr.turns if t.login == me
        ]
        other_turns = [{"login": t.login, "action": t.action} for t in pr.turns if t.login != me]
        prev = previous.get(pr.number)
        changed = prev is None or (
            (prev.roles, prev.my_turns, prev.other_turns, prev.head_sha, prev.draft)
            != (roles, my_turns, other_turns, d.head_sha, d.draft)
        )
        ticket = TICKET_KEY.search(d.title)
        same_head = prev is not None and prev.head_sha == d.head_sha
        threads_changed = prev is None or _thread_turns(prev.my_turns) != _thread_turns(my_turns)
        rows[pr.number] = BoardRow(
            number=pr.number,
            title=d.title,
            url=d.url,
            author=d.author,
            roles=roles,
            head_sha=d.head_sha,
            head_first_seen_at=prev.head_first_seen_at if same_head else now,
            draft=d.draft,
            changed_files=d.changed_files,
            my_turns=my_turns,
            other_turns=other_turns,
            threads_truncated=pr.threads_truncated,
            pipeline=pr.number in pipeline_prs,
            ticket_key=ticket.group(0) if ticket else "",
            updated_at=d.updated_at,
            last_change=now if changed else prev.last_change,
            # A row stored before this field starts from its last change.
            thread_turns_change=now if threads_changed else (prev.thread_turns_change or prev.last_change),
        )
        new_turns += _changes(pr.number, my_turns, set(prev.my_actions) if prev else set())
    gone_turns: list[TurnChange] = []
    for number, prev in previous.items():
        row = rows.get(number)
        gone_turns += _changes(number, prev.my_turns, set(row.my_actions) if row else set())
    return BoardUpdate(rows, new_turns, gone_turns)

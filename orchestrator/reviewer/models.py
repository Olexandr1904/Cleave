"""Reviewer-mode data: readiness snapshot, PR details, board rows, repo state, jobs."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

JOB_STATES = (
    "queued", "running", "done", "posted", "failed",
    "deferred", "superseded", "skipped", "cancelled", "stale",
)


@dataclass(frozen=True)
class Turn:
    login: str
    action: str
    threads: tuple[str, ...] = ()
    reviewer: str = ""


@dataclass(frozen=True)
class SnapshotPR:
    number: int
    title: str
    url: str
    author: str
    draft: bool
    threads_truncated: bool
    turns: tuple[Turn, ...]
    extra: dict  # snapshot fields the core does not use


@dataclass(frozen=True)
class BoardSnapshot:
    me: str
    prs: tuple[SnapshotPR, ...]


@dataclass(frozen=True)
class PRDetails:
    number: int
    title: str
    url: str
    author: str
    state: str
    head_sha: str
    draft: bool
    assignees: tuple[str, ...]
    requested_reviewers: tuple[str, ...]
    changed_files: int
    updated_at: str


@dataclass
class BoardRow:
    # Fields added later must have defaults: rows already stored lack them.
    number: int
    title: str
    url: str
    author: str
    roles: list[str]
    head_sha: str
    head_first_seen_at: str
    draft: bool
    changed_files: int
    my_turns: list[dict]  # {"action": str, "threads": list[str]}
    other_turns: list[dict]  # {"login": str, "action": str}
    threads_truncated: bool
    pipeline: bool
    ticket_key: str
    updated_at: str
    last_change: str

    @property
    def my_actions(self) -> list[str]:
        return [t["action"] for t in self.my_turns]

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, text: str) -> BoardRow:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in json.loads(text).items() if k in known})


@dataclass
class RepoState:
    # Fields added later must have defaults: rows already stored lack them.
    scope: str
    first_poll_done: bool = False
    status: str = "new"  # new, ok, token_failing, readiness_failed, doc_missing, error
    reason: str = ""
    failures: int = 0
    last_poll_at: str = ""
    last_ok_at: str = ""
    next_poll_at: str = ""


@dataclass
class Job:
    id: str
    scope: str
    number: int
    kind: str
    head_sha: str
    key: str
    state: str  # one of JOB_STATES
    reason: str
    created_at: str
    updated_at: str

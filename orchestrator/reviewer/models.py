"""Reviewer-mode data: readiness snapshot, PR details and comments, board rows, repo state, jobs, job runs."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class PRFull:
    number: int
    title: str
    body: str
    url: str
    state: str
    head_sha: str
    base: str
    additions: int
    deletions: int
    changed_files: int


@dataclass(frozen=True)
class PRComments:
    inline: tuple[dict, ...]  # {"login", "path", "line", "id", "body"}; line may be None
    reviews: tuple[dict, ...]  # {"login", "state", "body"}, reviews with a body only
    conversation: tuple[dict, ...]  # {"login", "body"}


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
    me: str = ""  # the readiness snapshot's login


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
    retry_at: str = ""


@dataclass
class JobRun:
    # Fields added later must have defaults: runs already stored lack them.
    job_id: str
    started_at: str = ""
    finished_at: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    error: str = ""
    job_dir: str = ""
    title: str = ""
    url: str = ""
    ticket_key: str = ""
    files: int = 0
    report: dict = field(default_factory=dict)
    replies: list = field(default_factory=list)
    tg_lines: dict = field(default_factory=dict)  # keys are strings in JSON
    message_ids: list[int] = field(default_factory=list)
    message_sent: bool = False
    notes: list[str] = field(default_factory=list)
    send_attempts: int = 0  # failed deliveries of the message
    last_attempt_at: str = ""  # the last delivery attempt
    deferrals: int = 0  # the job's usage-limit and login deferrals so far
    requeues: int = 0  # the job's "GitHub unavailable" requeues since its last Prepare that got through

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, text: str) -> JobRun:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in json.loads(text).items() if k in known})

"""Reviewer-mode data: readiness snapshot, PR details and comments, board rows, repo state, jobs, job runs,
reply anchors, posts and confirmations."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

JOB_STATES = (
    "queued", "running", "done", "posted", "failed",
    "deferred", "superseded", "skipped", "cancelled", "stale",
)
# The states of a review or a re-check whose report (or replies plan) is current: a post, a command about it (text,
# edit, draft) and the runner's saves of its run need one of them.
POSTABLE_STATES = ("done", "posted")


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
class Check:
    """One check on a PR's head: a check run or a commit status."""
    name: str
    state: str  # pending, passed or failed
    word: str  # GitHub's status or conclusion, lowercase, "_" as a space: queued, in progress, failure…


@dataclass(frozen=True)
class MergeState:
    """What GitHub says of merging a PR: its mergeStateStatus (BLOCKED, BEHIND, DIRTY, CLEAN, UNKNOWN…), its base and
    head branches, its head, whether the head branch is in another repo, the review decision ("" for none) and the
    head's checks, None when no check ran on it."""
    status: str
    base: str
    head_ref: str
    head_sha: str
    cross_repo: bool
    review: str
    checks: tuple[Check, ...] | None


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
    closed_at: str = ""  # "" while open
    author: str = ""
    head_ref: str = ""  # the PR branch's name
    head_repo: str = ""  # the PR branch's repo, owner/name; "" when it is gone


@dataclass(frozen=True)
class ForgeReview:
    id: int
    login: str  # "ghost" when the account was deleted
    state: str
    commit_id: str
    submitted_at: str  # "" while pending
    body: str
    url: str


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
    my_turns: list[dict]  # {"action": str, "threads": list[str]}, and "reviewer": str on a request_review turn
    other_turns: list[dict]  # {"login": str, "action": str}
    threads_truncated: bool
    pipeline: bool
    ticket_key: str
    updated_at: str
    last_change: str
    thread_turns_change: str = ""  # when my own turns that name threads last changed ("" in rows stored before)
    no_checks: bool = False  # my merge turn, which GitHub blocks while no check ran on the head: Re-run CI applies

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
    request: dict = field(default_factory=dict)  # a small job: {"job": <review job id>, "number", "instruction"}
    drafted: list[int] = field(default_factory=list)
    posted_at_head: int = 0
    summary_posted: bool = False
    # The Telegram language of the job's message and its model-written texts translated into it (text → translation),
    # fixed before its first part goes out so every part comes from them. "": a run from before languages (English).
    language: str = ""
    translations: dict = field(default_factory=dict)
    # An address job's patch (fix.patch in its copy): {"files": [{"path", "added", "deleted"}], "protected":
    # [path], "bytes"}; {} when the session changed no code.
    fix: dict = field(default_factory=dict)
    # A findings job's verdicts.json, parsed: [{"n", "verdict", "why"}], one per confirmed finding.
    verdicts: list = field(default_factory=list)
    # A review job's run: its message offers "fix" (an own PR in a repo with address_doc), decided when the run
    # finished so every delivery attempt builds the same message.
    findings_fixable: bool = False

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, text: str) -> JobRun:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in json.loads(text).items() if k in known})


@dataclass(frozen=True)
class Anchor:
    """What a reviewer message points at: a job, or a PR (scope and number)."""
    job_id: str
    scope: str
    number: int


@dataclass
class Post:
    # Fields added later must have defaults: posts already stored lack them.
    id: str
    job_id: str
    scope: str
    number: int
    head_sha: str  # a push's: the head before it
    kind: str  # review, approve, replies, push
    state: str  # posting, posted, partial, failed, unverified, interrupted
    created_at: str
    updated_at: str = ""
    numbers: list[int] = field(default_factory=list)
    event: str = ""
    # {"n", "path", "line", "title", "where"}: path, line and title are the report item's, never the payload
    # comment's (a moved anchor keeps its report line); where is inline, moved or body (review), or reply,
    # comment or resolve (replies: path "", line None, title "").
    landed: list[dict] = field(default_factory=list)
    summary_posted: bool = False
    comments: int = 0
    review_ids: list[int] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    error: str = ""
    sha: str = ""  # a push's new commit

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Post:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in json.loads(text).items() if k in known})


@dataclass
class Confirmation:
    id: str
    job_id: str
    chat_id: str
    state: str  # pending, confirmed, declined
    created_at: str
    command: dict

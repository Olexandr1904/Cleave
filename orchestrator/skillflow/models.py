"""Skill-flow data: a run of a repo's skill on one ticket, and the run states."""
from __future__ import annotations

from dataclasses import dataclass, field

RUN_STATES = ("queued", "running", "waiting", "gates", "pr_open", "stopped", "no_change", "failed",
              "deferred", "dropped", "interrupted")
PARKED_STATES = ("waiting", "stopped", "interrupted")        # Approve moves these on
REPLY_STATES = (*PARKED_STATES, "no_change")                 # a text reply resumes the session
DROPPABLE_STATES = ("queued", "deferred", *PARKED_STATES)
FINAL_STATES = ("pr_open", "no_change", "failed", "dropped")
# A run of the ticket in any other state may still work in the ticket's worktree, and keeps the ticket from
# getting another run.
ENDED_STATES = ("dropped", "failed")
PHASES = ("work", "commit", "gates", "push")


@dataclass
class SkillRun:
    # Fields added later must have defaults: runs already stored lack them.
    id: str
    scope: str            # "<project>/<repo>"
    ticket_id: str
    state: str
    retry_at: str
    created_at: str
    updated_at: str
    unsent: int = 0       # messages waiting to go out (a column, so the runner can query it)
    # everything below is one JSON object in the `data` column
    skill: str = ""
    summary: str = ""     # the ticket's title
    model: str = ""
    chat_id: str = ""
    reason: str = ""
    phase: str = "work"
    pending: str = ""     # the operator's text for the next work turn
    approved: bool = False
    session_id: str = ""
    session_started: bool = False   # a turn of this session has ended ok
    cut: bool = False     # the last turn started and did not end ok (limit, login, failure, restart)
    unattended: bool = False
    # assignee, status, staged_branch (intake); other_branches, other_prs (the runner's prepare, for the first prompt)
    facts: dict = field(default_factory=dict)
    status_line: str = ""
    outcome: str = ""
    needs_user: bool = False
    branch: str = ""
    gated_sha: str = ""   # the commit the gates passed: the only one the push sends
    pushed: bool = False  # a push of the branch landed: never cleared, whatever the run does next
    # The branch of the last push started, saved before git runs: a push that raised or was cut by a restart may
    # have reached the forge all the same.
    push_branch: str = ""
    gate_round: int = 0
    deferrals: int = 0
    pr_number: int = 0
    pr_url: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    tokens_in: int = 0    # the host runs on a subscription: tokens, not dollars, are what a run spends
    tokens_out: int = 0
    turn_count: int = 0
    started_at: str = ""
    cleaned: bool = False           # a dropped or released run's worktree was removed, or the refusal reported
    # The comparison has its verdict: the worktree is no longer kept for it (the sweep removes it).
    released: bool = False
    # {"text": str, "buttons": bool, "ids": [int]}: `ids` (only once a part went out and a later one failed) are
    # the parts already sent, which the next send leaves out.
    outbox: list[dict] = field(default_factory=list)
    # Every part of the newest message that went out with buttons. A row of an older version kept only its last
    # part, as `ask_message_id`, which is no field now and is skipped on load: such a run takes no Approve or Drop
    # until its next message with buttons goes out.
    ask_message_ids: list[int] = field(default_factory=list)

"""Comparison data: one ticket run by both flows, the operator's verdict on it, and their states."""
from __future__ import annotations

from dataclasses import dataclass, field

COMPARISON_STATES = ("open", "deciding", "stuck", "closed")
VERDICTS = ("staged", "skill", "neither")
SKILL_AT_REST = ("pr_open", "stopped", "no_change", "failed", "dropped")
STAGED_AT_REST = ("PR_REVIEW", "BLOCKED", "FAILED", "DONE", "ARCHIVED")
STEPS = ("ready", "title", "comment", "close", "tracker", "skill", "staged")   # the verdict's steps, in order
ID_PREFIX = "cmp:"   # a comparison id is "cmp:<12 hex>": it is also the run_id of its anchors in skill_anchors


@dataclass
class Comparison:
    # Fields added later must have defaults: rows already stored lack them.
    id: str
    scope: str
    ticket_id: str
    run_id: str
    state: str
    created_at: str
    updated_at: str
    unsent: int = 0
    # one JSON object in `data`:
    chat_id: str = ""
    summary: str = ""
    skill: str = ""                  # the skill's name, for the message
    workspace_root: str = ""
    workspace_started_at: str = ""   # with ticket_id, which workspace this is
    rest_since: dict = field(default_factory=dict)   # {"skill": iso, "staged": iso}: since when each side rests
    fingerprint: str = ""            # the pair the newest message shows; "" while no message stands
    messages: int = 0                # comparison messages built so far: a tap's claim expects this value
    ask_message_ids: list[int] = field(default_factory=list)   # every part of the newest message with buttons
    # {"text": str, "buttons": [[label, action], …], "ids": [int]}
    outbox: list[dict] = field(default_factory=list)
    # {"skill": SideView.as_dict(), "staged": …} as the newest message shows them
    sides: dict = field(default_factory=dict)
    verdict: str = ""
    note: str = ""
    decided_at: str = ""
    closed_at: str = ""
    steps_done: list[str] = field(default_factory=list)
    reason: str = ""                 # why the verdict is stuck
    # The verdict's closing lines as [template, values] pairs, translated only when the message is built.
    report: list[list] = field(default_factory=list)

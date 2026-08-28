"""What dev-agent is told to fix, and what the PR is told publicly.

On PR #1509 the responder classified 44 comments ESCALATE/Valid, the operator
answered FIX, and the pipeline posted `Will fix: <escalation reason>` — reasons
reading "needs human judgment", "expands scope", once literally "Agent failed".
Every entry then recorded `Verified: FAILED`: dev-agent never touched the
commented files. The `reason` field exists to justify escalating; feeding it to
dev-agent as the instruction told the agent the work was not its to do.
"""

from orchestrator.pipeline.actions.fetch_pr_comments import (
    build_fix_instruction,
    build_public_reply,
)

COMMENT_BODY = (
    "`updateReplyDraftOnLatest` targets the row that is latest at update time "
    "(ORDER BY created_at DESC). If a new push arrives between (a) setting "
    "SENDING in the receiver and (b) the worker updating to FAILED, this UPDATE "
    "can write the FAILED state onto a different row, leaving the original row "
    "stuck in SENDING forever."
)
ESCALATION_REASON = (
    "Real race: updateReplyDraftOnLatest writes to latest-at-update-time row. "
    "Fix requires propagating push_id through Intent/Work input - non-trivial "
    "design change requiring human judgment."
)


def test_fix_instruction_carries_the_reviewer_words():
    instruction = build_fix_instruction(
        file="app/src/main/java/Worker.kt", line=113,
        author="Copilot", body=COMMENT_BODY,
    )
    assert "updateReplyDraftOnLatest" in instruction
    assert "app/src/main/java/Worker.kt:113" in instruction


def test_fix_instruction_never_carries_the_escalation_reason():
    instruction = build_fix_instruction(
        file="app/src/main/java/Worker.kt", line=113,
        author="Copilot", body=COMMENT_BODY, reason=ESCALATION_REASON,
    )
    assert "human judgment" not in instruction.lower()
    assert "non-trivial" not in instruction.lower()


def test_fix_instruction_keeps_the_full_comment():
    """Truncating at 200 chars cut off the technical content of every fix."""
    instruction = build_fix_instruction(
        file="a.kt", line=1, author="Copilot", body=COMMENT_BODY,
    )
    assert COMMENT_BODY in instruction


def test_fix_instruction_includes_suggested_fix_when_present():
    instruction = build_fix_instruction(
        file="a.kt", line=1, author="Copilot", body=COMMENT_BODY,
        suggested_fix="Pass push_id via WorkManager input data.",
    )
    assert "Pass push_id via WorkManager input data." in instruction


def test_fix_instruction_forbids_silent_skipping():
    instruction = build_fix_instruction(
        file="a.kt", line=1, author="Copilot", body=COMMENT_BODY,
    )
    assert "developer.md" in instruction


def test_public_reply_does_not_leak_escalation_rationale():
    reply = build_public_reply(reason=ESCALATION_REASON)
    assert "human judgment" not in reply.lower()
    assert "Agent failed" not in reply
    assert reply.startswith("Will fix")

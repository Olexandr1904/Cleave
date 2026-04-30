# Design: PR Review Second-Cycle Hygiene

**Status:** Design
**Created:** 2026-04-30
**Author:** Oleksandr Brazhenko

**See also:** [2026-04-30 PR Review Flow Improvements](2026-04-30-pr-review-flow-improvements-design.md) — base flow this builds on.

## Problem

The PR-review flow (shipped in [2026-04-30 PR Review Flow Improvements](2026-04-30-pr-review-flow-improvements-design.md)) handles the first review cycle well: fetch comments, classify, escalate, decide, fix, push. But on the second and subsequent cycles, the operator-confidence picture deteriorates:

1. **Thread replies are silently dropped.** A reviewer replies inside a comment thread ("Actually, can you also handle case X?", "lgtm", "what about null?") — and the pipeline drops it on the floor. The orchestrator's [Filter A at orchestrator.py:1714](../../../orchestrator/orchestrator.py) skips ALL comments with `in_reply_to_id` set, so reviewer follow-ups never make it into the next classification round.

2. **GitHub-side manual resolution isn't consulted.** A reviewer can mark a comment thread resolved via GitHub UI without the bot ever knowing. The bot may continue to escalate or process that comment on next cycle.

3. **Filter B's bot-reply-detection regex is incomplete.** It looks for `"won't fix"`, `"wont fix"`, `"fixed"` — but the new flow posts `"Will fix:"` (Task 15). Soft-duplicated by the `decided_ids` filter, but a defense-in-depth gap.

4. **`pending_reinvestigation` flag can persist across crashes.** If the orchestrator dies mid-flow after `_stage_reinvestigation` set the flag but before `_reinvestigate_pending` could clear it, the workspace can stay in an "in-flight hint" state indefinitely.

These gaps surfaced during real use of ACME-14463 / PR #1498, where reviewer follow-ups can't be detected and the operator has no way to confirm "did the bot read the new question?".

## Goals

- Reviewer thread replies (follow-up questions, acknowledgments) are picked up on the next cycle, classified, and routed through the same AUTO_FIX/AUTO_REJECT/ESCALATE paths as parent comments.
- "lgtm" / "thanks" / acknowledgment replies are auto-resolved with a polite GitHub reply ("Acknowledged.") — operator is not involved.
- Threads the reviewer manually resolved on GitHub UI are skipped on next cycle (no spurious re-escalation).
- Filter B regex recognizes the bot's `"Will fix"` replies as bot-replies (defense-in-depth).
- Stuck `pending_reinvestigation` flags self-clear after two no-progress ticks.
- The full second-cycle story is covered by an integration test against a fake VCS.

## Non-Goals

- Multi-PR coordination.
- Suggesting fixes for thread replies that arrive while a fix is already in flight (preserve the existing single-classification-at-a-time invariant).
- Rate-limiting GraphQL calls (current usage is bounded by PR comment count).
- Deeper conversation modelling beyond the linear `thread_ids` chain (no branching, no nested replies — GitHub's review-comment threads are flat).

## Flow Overview

```
Reviewer leaves a follow-up reply on a comment thread
    │
    ├─ Operator signals `reviewed` in TG (existing trigger)
    │
    ├─ Phase 4: fetch comments + per-thread isResolved status
    │
    ├─ Step A: drop threads where isResolved=True on GitHub
    │
    ├─ Step B: split into "new parents" and "threads with new replies"
    │     (compare GitHub thread IDs against stored thread_ids in resolution report)
    │
    ├─ Step C: classify NEW PARENTS as today
    │     (existing flow — write entry with thread_ids = [parent_id])
    │
    ├─ Step D: classify THREADS WITH NEW REPLIES
    │   ├─ Build thread_context (full conversation, in posted-at order)
    │   ├─ Call classify_comments(parent, thread_context={parent_id: [...]})
    │   ├─ Agent classifies the thread as a whole, basing the decision on
    │   │  the latest concern the reviewer raised
    │   ├─ Acknowledgment short-circuit: agent returns AUTO_REJECT with
    │   │  reason="Reviewer acknowledged — no action needed" → bot posts
    │   │  "Acknowledged." and resolves (NOT "Won't fix:")
    │   ├─ Otherwise route through standard AUTO_FIX/AUTO_REJECT/ESCALATE
    │   └─ Update entry: thread_ids ← thread_ids ∪ all newly-seen IDs
    │
    └─ Step E: bot posts its replies → append new reply IDs to thread_ids
```

## Data Model Changes

### `thread_ids` field on resolution-report entries

```
## Comment #12345
- Classification: ESCALATE
- Verdict: Valid
- File: app/x.kt
- Line: 13
- Author: Copilot
- Reason: ...
- Decision: FIX
- Verified: PENDING
- Thread Ids: 12345,67890,67891,67892
- ...
```

Stored as a comma-separated string. `read_entries` parses to `list[int]` via `[int(x) for x in s.split(",") if x.strip()]`. `add_entry` and `update_entry` accept `thread_ids` as a list. Empty list → empty string (not `"0"` or any sentinel).

The chain contains, in conversation order:
- Parent comment ID (always first)
- Every reviewer reply we've classified
- Every bot reply we've posted (Will fix / Won't fix / Acknowledged / Fixed in commit X)

When a parent is first classified, `thread_ids = [parent_id]`. When the bot posts a reply, append its ID. When subsequent thread replies are classified, append every newly-seen ID.

### `PRComment` dataclass — `is_thread_resolved`

`integrations/base/vcs.py` `PRComment` gets:

```python
is_thread_resolved: bool = False
```

Pure additive. The default `False` keeps existing call sites working unchanged.

### `classify_comments` — `thread_context` kwarg

`orchestrator/comment_classifier.py`:

```python
async def classify_comments(
    comments: list[Any],
    workspace: Any,
    agent_runtime: Any,
    *,
    operator_hint: str = "",
    thread_context: dict[int, list[dict]] | None = None,
) -> list[ClassifiedComment]:
```

`thread_context` maps `parent_comment_id → [{author, body, posted_at}, ...]`. Passed to the agent via `extra_context`. When empty, the prompt's `{thread_context_block}` substitutes to an empty string.

The block format (rendered at substitution time, not in the agent prompt itself):

```
## Prior conversation for comment 12345

@reviewer (2026-04-30 09:15): Use @Inject here.
@bot (2026-04-30 09:20): Will fix: existing pattern requires @Inject.
@reviewer (2026-04-30 11:42): Actually, can you also handle the case where Inject is null?
```

## Component Changes

### 1. `integrations/base/vcs.py`

`PRComment` gains `is_thread_resolved: bool = False`. No other changes.

### 2. `integrations/github/github_adapter.py`

`get_pr_comments(pr_number)` keeps its REST call to `/pulls/{pr_number}/comments` (which returns the flat list of review comments). It then runs ONE GraphQL query to fetch each thread's `isResolved` and the comment-to-thread mapping:

```graphql
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          comments(first: 50) {
            nodes { databaseId }
          }
        }
      }
    }
  }
}
```

Builds a map `comment_id → is_thread_resolved` from the response. Each `PRComment` gets its `is_thread_resolved` populated from this map. If the GraphQL call fails (network, rate limit, etc.), log a warning and leave `is_thread_resolved=False` for all comments — the REST data is still useful, the resolution check just degrades to "process everything".

### 3. `orchestrator/comment_classifier.py`

Update `classify_comments` to accept `thread_context` and thread it through `extra_context`:

```python
extra_context = {
    "pr_comments_json": json.dumps(comment_data, indent=2),
    "operator_hint": operator_hint,
    "thread_context_block": _render_thread_context_block(thread_context, comments),
}
```

`_render_thread_context_block(thread_context, comments)` produces the formatted block (or empty string if nothing relevant). Helper lives at module scope, ~25 lines.

### 4. `agents/pr-comment-responder-agent.md`

Add a section after "Operator Hint":

```markdown
## Thread Context

If `{thread_context_block}` is non-empty, the comment you're classifying has prior replies. Read the full conversation. Decide based on the *latest* concern the reviewer raised — earlier replies are context, not the issue to resolve.

If the latest reply is acknowledgment ("ok thanks", "lgtm", "great", "👍", or similar), classify as `AUTO_REJECT` with `reason: "Reviewer acknowledged — no action needed"`. The pipeline detects this exact reason and posts a polite "Acknowledged." reply (not "Won't fix:") on GitHub before resolving.
```

### 5. `orchestrator/resolution_report.py`

`add_entry` and `update_entry` accept `thread_ids: list[int] | None = None` as part of the field dict. When present, format as `Thread Ids: <comma-separated>` and write to the entry. When absent, leave existing value unchanged (or empty string for new entries).

`read_entries` parses `Thread Ids:` lines as before, returning the int list.

### 6. `orchestrator/orchestrator.py`

`_action_fetch_pr_comments` Phase 4 is split into Steps A–E (per the flow diagram above):

- **New helper `_group_comments_into_threads(all_comments) -> dict[int, list[PRComment]]`.** Walks `all_comments`, builds a map keyed by parent comment ID, with each value being `[parent, reply1, reply2, ...]` in posted-at order. Orphan replies (parent ID missing from the list) are logged as warnings and discarded.

- **New helper `_build_thread_context(thread: list[PRComment]) -> list[dict]`.** Produces the `[{author, body, posted_at}, ...]` list. Includes the bot's own replies (so the agent sees its prior commitment).

- **Step A — isResolved filter.** After grouping, drop threads where `parent.is_thread_resolved is True`.

- **Step B — split into buckets.** For each remaining thread:
  - If `parent.id` is NOT in `decided_ids` → "new parent" bucket
  - Else if the thread has any IDs not in the parent's stored `thread_ids` → "new replies" bucket
  - Else → skip (nothing new to process)

- **Step C — classify new parents.** As today. After classification, write entry with `thread_ids = [parent.id]`.

- **Step D — classify threads with new replies.** Build `thread_context` for each. Call `classify_comments` with `thread_context=...`. Route the result through the same AUTO_FIX/AUTO_REJECT/ESCALATE flow as Step C. After classification, update the entry's `thread_ids` to be the union of stored + all newly-seen IDs.

- **Acknowledgment short-circuit (in AUTO_REJECT branch around [orchestrator.py:1716](../../../orchestrator/orchestrator.py)):** if `cc.reason.startswith("Reviewer acknowledged")`, post `"Acknowledged."` instead of `"Won't fix: <reason>"` and resolve. Resolution-report decision is recorded as `ACK_RESOLVED` (new value, distinct from `WON'T_FIX`).

- **Step E — bot replies append to chain.** Whenever the bot posts a reply (Will fix / Won't fix / Acknowledged / Fixed in commit), the response includes the new comment ID. After the bot post call returns, append that ID to the parent's `thread_ids` and write to the report.

- **Filter B regex update** at [orchestrator.py:1707-1710](../../../orchestrator/orchestrator.py):

```python
if c.in_reply_to_id and c.body.strip().lower().startswith(
    ("won't fix", "wont fix", "will fix", "fixed", "acknowledged")
):
```

(`"acknowledged"` added so future cycles also treat the new ack-reply as a bot reply.)

### 7. `_reinvestigate_pending` defensive flag clear

In `orchestrator/orchestrator.py`, `_reinvestigate_pending` gains stuck-flag detection. Track `reinvestigation_stale_ticks: int` on each entry. Logic:

- At loop start: if `pending_reinvestigation` is True, increment `reinvestigation_stale_ticks`.
- After successful re-classification: reset `reinvestigation_stale_ticks = 0`.
- If `reinvestigation_stale_ticks >= 2` AND no progress detected (last_hint, hint_rounds unchanged from start of tick): force-clear flag, reset counter, surface to TG: `"Re-investigation flag cleared due to inactivity for @<author> on <file>:<line>. Reply Fix or Won't Fix to close."`

This catches partial-save scenarios where the orchestrator died after `_stage_reinvestigation` set the flag but before `_reinvestigate_pending` could clear it.

### 8. Concurrency invariant

A thread can only be classified by ONE call to `classify_comments` per cycle. The flow ordering already enforces this:

- Phase 1: PENDING-fix verification (no classifier calls)
- Phase 1.5: re-investigation of pending hints (single-comment classifier calls)
- Phase 4: thread-reply detection + classification (batch classifier call)

Re-investigation runs first, then thread-reply detection sees the post-reinvestigation state. They don't conflict because re-investigation handles a single comment without thread context.

A parent comment with a `pending_review_comments` entry awaiting decision is **excluded** from thread-reply detection until the operator decides — this preserves the "single classification at a time" invariant.

## Edge Cases

- **Thread reply arrives between cycle 1 escalation and operator decision.** Cycle 2 skips it (parent is in `pending_review_comments` awaiting decision). Cycle 3 (after decision) picks it up.
- **Agent crashes during thread classification.** Same retry-once policy as `_reinvestigate_pending`: first failure leaves thread unprocessed (next cycle retries), second failure surfaces to TG with `"Thread classification failed for <file>:<line> — go check on GitHub"` and adds the new reply IDs to `thread_ids` anyway (so we don't re-attempt forever on a poison reply).
- **Reviewer resolves a thread mid-conversation.** Step A skips. We don't post "Acknowledged" — reviewer already did our job.
- **Bot posts "Will fix" but dev-agent never lands the fix.** Existing fail-count flow handles this — orthogonal.
- **Deeply nested or out-of-order threads.** GitHub's API returns replies sorted by creation time within each thread; we trust that order. The `thread_ids` chain matches API order.
- **Operator triggers `/unanswered` mid-thread-classification.** `/unanswered` only iterates `pending_review_comments` (operator-decision queue), not in-flight thread classifications. No interaction.
- **GraphQL rate limit hit.** `is_thread_resolved` defaults to `False` → behaves as if no thread is resolved → process everything. Worst case is an extra escalation that the operator already resolved on GitHub. Acceptable degradation.

## Tests

### New: `tests/unit/test_thread_replies.py`

- `_group_comments_into_threads`:
  - Parent without replies → `{parent_id: [parent]}`
  - Parent with N replies → `{parent_id: [parent, r1, r2, ...]}` in posted-at order
  - Orphan reply (parent missing) → discarded with warning logged
- `_build_thread_context`:
  - Produces `[{author, body, posted_at}, ...]` for every entry
  - Includes bot replies (so agent sees prior commitment)
- New parent → classified as today, `thread_ids = [parent_id]` written
- Existing parent + new reply → thread classification path called with `thread_context`
- Acknowledgment short-circuit:
  - Agent returns `Reviewer acknowledged` reason
  - Bot posts `"Acknowledged."` (NOT `"Won't fix:"`)
  - Decision recorded as `ACK_RESOLVED`
  - Thread resolved
- isResolved=True thread → skipped entirely (no classify call)
- `is_thread_resolved=True` does NOT write a new resolution-report entry

### New: `tests/unit/test_pr_review_second_cycle.py` (integration test)

Fake VCS with two-cycle flow:

- **Setup**: parent comment 100 on PR #42, no replies yet.
- **Cycle 1**:
  - Pipeline classifies 100 as ESCALATE
  - Operator decides FIX via TG
  - Bot posts `"Will fix: <reason>"` reply (id=101) → appended to thread_ids
  - Dev-agent (mocked) commits, push happens
- **Cycle 2** setup: VCS now returns parent 100, our reply 101, NEW reply 200 (reviewer follow-up "What about null?"), AND new top-level comment 300.
- **Cycle 2 run**:
  - Phase 1 verifies the FIX landed (fix from gap-2 ensures this works)
  - Phase 4 Step A: nothing isResolved
  - Phase 4 Step B: thread 100 has new ID 200 (in thread, not in stored chain). Comment 300 is a new parent.
  - Phase 4 Step D: thread 100 classified with thread_context = [parent, our reply, reply 200]. Result: ESCALATE → operator queue.
  - Phase 4 Step C: comment 300 classified as new parent.
- **Assertions**:
  - `thread_ids` for parent 100: `[100, 101, 200]`
  - `thread_ids` for parent 300: `[300]`
  - Two pending operator decisions (one for thread 100's ESCALATE, one for 300's ESCALATE)
  - The classifier was called with `thread_context` for parent 100, without it for 300

### Extensions to existing tests

- `tests/unit/test_comment_classifier.py` gets a `TestThreadContext` class:
  - `thread_context` threaded into `extra_context` correctly
  - Empty `thread_context` → `thread_context_block` substitutes to empty string
  - Multi-comment input with thread_context for only some → renders block only for those comments

- `tests/unit/test_github_reply_at_decision.py` semantics unchanged — but verify the test fixtures still work after `PRComment` gains `is_thread_resolved`.

- `tests/unit/test_review_decisions_freetext.py` gets a `test_acknowledgment_routes_to_ack_resolved` case asserting the new ACK_RESOLVED decision value reaches the resolution report.

## Migration

- **Existing resolution reports** without `Thread Ids:` lines: `read_entries` defaults to `[]`. The next cycle will populate the chain with whatever's currently in the thread (parent only if no replies yet, parent + replies if replies exist). No data loss; one cycle of "mild over-processing" possible if a reply was already manually handled — minimal impact.
- **Existing pending entries** without `reinvestigation_stale_ticks`: treated as `0`. No reset needed.
- **`PRComment.is_thread_resolved=False` default** keeps every existing test fixture working unchanged.

## Out of Scope (Explicitly)

- Branching threads / nested replies (GitHub review threads are flat — no support needed).
- Operator manually marking a thread as "ignore" via TG (could be added later via a new button on escalation messages).
- Thread classifier remembering state across PRs (each PR is independent).
- Polling for thread changes between explicit `reviewed` signals — operator still drives cycle starts.

## Success Criteria

- A reviewer adds a thread reply with a follow-up question. Next cycle classifies it (with full thread context) and routes through the standard pipeline; operator sees an escalation message that references the new question.
- A reviewer says "lgtm" in a thread. Next cycle, bot posts `"Acknowledged."` and resolves the thread on GitHub. Operator sees no TG message.
- A reviewer manually resolves a thread on GitHub. Next cycle skips the thread entirely. No spurious escalation.
- Bot's own `"Will fix:"` reply is recognized as a bot-reply by Filter B (defense-in-depth even when resolution report is unavailable).
- A workspace stuck with `pending_reinvestigation: True` for two no-progress ticks self-clears with an operator-facing TG message.
- The integration test in `test_pr_review_second_cycle.py` exercises the full two-cycle flow and passes.

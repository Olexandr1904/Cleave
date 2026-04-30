# Design: PR Review Flow Improvements

**Status:** Design
**Created:** 2026-04-30
**Author:** Oleksandr Brazhenko

## Problem

The current PR-review flow ([orchestrator/orchestrator.py:1456-1823](../../../orchestrator/orchestrator.py), [integrations/telegram/command_handler.py:449-516](../../../integrations/telegram/command_handler.py)) has three operator-facing gaps that surfaced during real use:

1. **Unanswered comments are hard to track.** When the pipeline escalates 5+ comments to Telegram, the messages scroll out of view as more pipeline activity arrives. There is no way to ask "what's still waiting on me?" without scrolling.
2. **Agent assessment is verbose without a quick verdict.** Each escalated TG message renders the agent's reasoning as a free-form paragraph. When scanning many comments, the operator has to read each paragraph to figure out whether the agent thinks the reviewer is right or wrong.
3. **Free-text replies are silently swallowed as SKIP.** A reply that isn't `fix` / `won't fix` falls through to a "SKIP (free-text reply, dropped)" label. The operator's prose — typically a hint for the dev-agent — is discarded. This bug surfaced when the operator typed an explanation like "check other repositories, we use the same pattern across the codebase" and the system recorded SKIP, even though the Skip button itself was already removed in [e1239ba](../../../).

## Goals

- Operator can recall pending comments on demand (slash command + inline button).
- Each escalated comment carries a one-word verdict (`Valid` / `Not valid`) ahead of the reasoning.
- Free-text replies trigger re-investigation by the `pr-comment-responder` agent with the operator's hint as context, capped at 3 rounds per comment.
- Reply-matching works whether the operator replies to the original escalation or any later "still pending" recall.
- Recognition feedback in every confirmation message ("matched: 'don't fix'") so it's never ambiguous how the system parsed a reply.
- **Every PR comment receives a reply on GitHub from the agent** stating fix-or-don't-fix and the reason/details, posted at the moment the decision is known. Reviewers should never see a comment go silent.

## Non-Goals

- Changes to AUTO_FIX / AUTO_REJECT pathways.
- Dashboard UI changes (no current dashboard surface for PR comment state).
- GitHub/GitLab-side changes (replies and resolutions still go through the existing VCS adapter).
- Multi-operator coordination.

## Flow Overview

```
Comment escalated to TG
    ├─ Renders verdict line: "Valid — <reason>" or "Not valid — <reason>"
    │
Operator replies or taps a button
    ├─ Reply matches `fix` synonym set → FIX (matched: 'yes')
    ├─ Reply matches `won't fix` synonym set → WON'T FIX (matched: 'don't fix')
    ├─ Anything else → re-investigation
    │
Re-investigation
    ├─ Stage hint on entry, send "🔍 Re-checking… (round N/3)" ack, wake orchestrator
    ├─ Orchestrator next tick: re-classify single comment with operator_hint
    ├─ Update entry verdict / reason / classification, increment hint_rounds
    ├─ Re-escalate as fresh TG message (always — never act silently after a hint)
    │
Operator wants to see what's still open
    ├─ /unanswered [TICKET]  OR  taps "Show N unanswered" button
    ├─ Each undecided comment is re-sent as a fresh TG message
    ├─ msg_ids list grows so reply-matching works for original OR recall
```

## Data Model Changes

### `pending_review_comments` entry (workspace state)

```python
# Before:
{
    "comment_id": 12345, "msg_id": 678, "decision": None,
    "author": "Copilot", "file": "...", "line": 13,
    "body": "...", "reason": "..."
}

# After:
{
    "comment_id": 12345,
    "msg_ids": [678],          # NEW — list, in order: original then each re-send
    "decision": None,
    "author": "Copilot", "file": "...", "line": 13,
    "body": "...", "reason": "...",
    "verdict": "Valid",        # NEW — "Valid" or "Not valid"
    "hint_rounds": 0,          # NEW — count of completed re-investigations
    "last_hint": None,         # NEW — most recent operator hint (audit/debug)
    "pending_reinvestigation": False,  # NEW — flag for orchestrator
}
```

**Migration:** on first read, if `msg_id` is present and `msg_ids` is not, set `msg_ids = [msg_id]` and drop `msg_id`. Done in [command_handler.py](../../../integrations/telegram/command_handler.py) and [orchestrator.py](../../../orchestrator/orchestrator.py) at the entry-points that touch the field, ~6 lines.

### Classifier output — `verdict` field

`pr-comment-responder-agent` returns a required `verdict` per comment:

```json
{
  "comment_id": 12345,
  "classification": "ESCALATE",
  "verdict": "Valid",
  "reason": "The @Inject annotation is missing — repo's other DI files use it consistently.",
  "suggested_fix": ""
}
```

`comment_classifier.ClassifiedComment` dataclass gains `verdict: str = "Unsure"`. The `"Unsure"` default is a defensive fallback for malformed agent output (logged as a warning); the agent prompt requires the agent to commit to `Valid` or `Not valid` for every comment.

Verdict is independent of classification:
- AUTO_FIX is almost always `Valid` (issue exists, fix is trivial)
- AUTO_REJECT can be `Valid` (issue exists but is out of scope) or `Not valid` (reviewer mistaken)
- ESCALATE can be either — the human decides what to do, the agent commits to its lean

### Resolution report (`reports/pr-review-resolution.md`)

Entries gain three fields, written by the existing `add_entry`/`update_entry` helpers — pure additive, since `read_entries` already preserves unknown fields:

```
Verdict: Valid
Hint round: 2
Hint: check other repositories, we use the same pattern...
```

## Component-by-Component Changes

### 1. `agents/pr-comment-responder-agent.md`

- Add a `## Verdict` section: every comment gets `verdict: "Valid" | "Not valid"`. Verdict is a judgment on whether the reviewer is correct, independent of whether we'll act on the comment.
- Add a `## Operator Hint` section: when `{operator_hint}` is non-empty, the agent must weight the hint as a strong human signal that the previous classification was wrong, but treat it as evidence to investigate, not a command to obey. The agent can still return `Not valid` if the hint is itself wrong.
- Update the example JSON output to include `verdict`.

### 2. `orchestrator/comment_classifier.py`

- Read `verdict` from JSON. Default to `"Unsure"` (logged warning) if missing/invalid.
- Add `verdict: str = "Unsure"` to `ClassifiedComment` dataclass.
- `classify_comments` accepts optional `operator_hint: str = ""` kwarg, threaded through `extra_context`.

### 3. `orchestrator/escalation_view.py` (new file, ~40 lines)

Pure function `build_escalated_comment_message(state, cc_dict, pr_number, *, recall: bool = False) -> tuple[str, list[Button]]`. Used by both the orchestrator (initial escalation) and the command_handler (recall via `/unanswered` and "Show unanswered" button). When `recall=True`, prefixes the message with `🔁 (still pending) `.

Renders the verdict line:

```
Agent assessment:
  Valid — the @Inject annotation is missing, repo's other DI files use it consistently.
```

### 4. `orchestrator/orchestrator.py`

- `_action_fetch_pr_comments`:
  - Before the existing "all decisions in" check, add a re-investigation phase: for each entry with `pending_reinvestigation: True`, call `classify_comments` with a single-comment list and `operator_hint=entry["last_hint"]`. Update entry (verdict, reason, classification), increment `hint_rounds`, clear flag, re-escalate via `build_escalated_comment_message` and append new `msg_id` to `msg_ids`. Always re-escalate, even if the new classification is AUTO_FIX or AUTO_REJECT.
  - On agent error during re-investigation: leave flag set, retry once on next tick, then surface to TG with an error message and clear the flag.
- `_send_escalated_comment_tg`: refactored to delegate rendering to `build_escalated_comment_message`. Also sets the `verdict` field on the new pending entry.
- Auto-handled summary at [orchestrator.py:1619-1633](../../../orchestrator/orchestrator.py): append `Button(label=f"Show {len(escalated)} unanswered", action=f"unanswered:{state.ticket_id}")` when `escalated` is non-empty.
- **AUTO_FIX path:** post `"Will fix: <reason>"` reply on GitHub at classification time (do NOT resolve — resolve happens after the fix is verified in the diff, where the existing flow posts `"Fixed in commit <sha>"` and resolves). Resolution report gains `github_reply: "Posted (will fix)"` so the audit trail shows the announce.
- **ESCALATE → operator FIX:** in `_execute_review_decisions`, when a comment's decision is `fix`, post `"Will fix: <reason>"` reply on GitHub before transitioning to DEV. The reason is the agent's most recent `reason` (which may have been updated by re-investigation with the operator's hint). Existing post-verification `"Fixed in commit"` reply still fires after the dev-agent lands the change.

### Reply policy: every comment is answered

The complete reply matrix on GitHub (per comment):

| Path | Initial reply (at decision time) | Final reply (after action) |
|---|---|---|
| AUTO_FIX | `Will fix: <agent reason>` | `Fixed in commit <sha>` + resolve |
| AUTO_REJECT | `Won't fix: <agent reason>` + resolve | (none — already resolved) |
| ESCALATE → operator FIX | `Will fix: <agent reason>` | `Fixed in commit <sha>` + resolve |
| ESCALATE → operator WON'T FIX | `Won't fix: <reason>` + resolve | (none) |
| ESCALATE → re-investigation in flight | (none — decision pending) | (becomes one of the above when settled) |

Re-investigation rounds do NOT post intermediate replies on GitHub — only the final settled decision speaks on the PR. This keeps the reviewer's notification feed clean.

### 5. `integrations/telegram/command_handler.py`

- New `_classify_reply(text) -> tuple[str, str]` helper, returning `(decision, matched_token)`:

  | Decision | Tokens (case-insensitive, optional `:` or whitespace + reason after) |
  |---|---|
  | `fix` | `fix`, `fxi`, `fifx`, `fixx`, `fx`, `fi`, `yes`, `fix it` |
  | `wont_fix` | `won't fix`, `wont fix`, `don't fix`, `dont fix`, `do not fix`, `not fix`, `no fix` |
  | `reinvestigate` | anything else |

  The `skip` literal is removed entirely.

- `handle_reply`:
  - Replace the linear lookup at [line 461](../../../integrations/telegram/command_handler.py#L461) with `if reply_to_msg_id in (c.get("msg_ids") or []):` so reply-matching works against original or any recall.
  - Replace the SKIP fallthrough at [line 489](../../../integrations/telegram/command_handler.py#L489) with `_stage_reinvestigation`.
  - Echo recognition in every confirmation: `✓ Recognized as FIX (matched: 'yes'). …`.

- New `_stage_reinvestigation(c, ws, hint_text, chat_id)`:
  - If `hint_rounds >= 3`: send "Hint loop exceeded" message, emit `pr_comment_hint_exhausted`, return.
  - Else: set `c["last_hint"] = hint_text`, `c["pending_reinvestigation"] = True`, save state, send "🔍 Re-checking… (round N/3)" ack, emit `pr_comment_reinvestigation_staged`, wake orchestrator.

- New `_handle_unanswered(intent, chat_id, processing_msg_id)`:
  - Find PR_REVIEW workspaces with non-empty `pending_review_comments`. If `intent.params.ticket_id` set, filter to that ticket.
  - For each undecided comment, call `build_escalated_comment_message(..., recall=True)`, send via notifier, append new `msg_id` to `c["msg_ids"]`.
  - Echo total count back to operator. Emit `pr_comments_unanswered_recalled`.

- `handle_callback`:
  - New `unanswered:<ticket>` action — invokes the same `_handle_unanswered` loop scoped to that ticket.
  - Existing `pr_fix` / `pr_wontfix` handlers gain `matched_token: "button:fix"` / `"button:wontfix"` in the event payload, and the echo string says "Recognized as FIX (matched: 'Fix' button)".
  - The unused `pr_skip` branch is removed (button is gone, action is dead).

- "N remaining" confirmation message at [line 507](../../../integrations/telegram/command_handler.py#L507) and [line 751](../../../integrations/telegram/command_handler.py#L751): append the "Show N unanswered" button when `len(undecided) > 0`.

### 6. `integrations/telegram/intent_parser.py`

- Add `unanswered` to the intent enum.
- Add prompt rule: "If the user is asking what's still waiting on them in PR review (e.g., `/unanswered`, `/repeat`, `what's pending`, `which comments are open`), classify as `unanswered`. Extract `ticket_id` if mentioned, else leave empty for all-tickets recall."
- Returns `params={"ticket_id": str | ""}`.

### 7. Event bus

Three new events plus one payload extension:

| Event | Emitter | Payload |
|---|---|---|
| `pr_comment_reinvestigation_staged` | command_handler | `{ticket_id, comment_id, hint_round, hint_excerpt}` |
| `pr_comment_reinvestigation_completed` | orchestrator | `{ticket_id, comment_id, hint_round, old_verdict, new_verdict, old_classification, new_classification}` |
| `pr_comment_hint_exhausted` | command_handler | `{ticket_id, comment_id, attempted_hint_excerpt}` |
| `pr_comments_unanswered_recalled` | command_handler | `{ticket_id, count, via: "command" \| "button"}` |
| `pr_comment_decision_recorded` (existing) | command_handler | adds `matched_token`, `via` ∈ `{button, reply, hint_followup}` |

### 8. Tests

- [tests/unit/test_pr_comment_decision_echo.py](../../../tests/unit/test_pr_comment_decision_echo.py): extend with one case per `fix` synonym, one per `wont_fix` synonym, and assertions on the "matched: '<token>'" echo string. Add a test for the button echo path naming "matched: 'Fix' button".
- [tests/unit/test_review_decisions_skip.py](../../../tests/unit/test_review_decisions_skip.py): rename to `test_review_decisions_freetext.py`. Replace SKIP-flow assertions with re-investigation staging assertions.
- New `tests/unit/test_reinvestigation.py`:
  - Free-text reply stages re-investigation.
  - Orchestrator tick processes the staged entry, calls `classify_comments` with `operator_hint`, updates verdict/reason, re-escalates, appends `msg_id`.
  - 3-round cap rejects the 4th hint with "Hint loop exceeded".
  - Re-classified AUTO_FIX still goes through escalation, never silent action.
- New `tests/unit/test_unanswered_recall.py`:
  - `/unanswered` recalls only undecided comments across all PR_REVIEW tickets.
  - `/unanswered TICKET-1` filters to one ticket.
  - "Show unanswered" button action does the same.
  - Reply to recall message resolves the same comment as reply to original (asserts `msg_ids` list grows).
  - The intent_parser test covers `/unanswered`, `unanswered`, `what's pending` → `unanswered` intent.
- [tests/unit/test_command_handler.py](../../../tests/unit/test_command_handler.py): extend with the `unanswered` intent dispatch.
- [tests/unit/test_comment_classifier.py](../../../tests/unit/test_comment_classifier.py): extend with `verdict` parsing — present, missing (defaults to `"Unsure"` with warning), invalid value.

### 9. Documentation

- Cross-link this spec from [docs/superpowers/specs/2026-04-21-pr-review-comment-resolution-design.md](2026-04-21-pr-review-comment-resolution-design.md) ("see also: 2026-04-30 flow improvements").
- Brief paragraph in [docs/features/agent-system.md](../../../docs/features/agent-system.md) under PR review describing the verdict line, hint loop, and recall command.

## Edge Cases

- **Hint arrives mid-tick:** staging only flips a flag; orchestrator picks it up on the next poll. No race because state is single-writer per workspace.
- **Two free-text hints in a row:** second overwrites `last_hint`, no second flag is set if one is already pending. Idempotent staging.
- **Re-investigation agent failure:** flag stays set, orchestrator retries once on next tick, then surfaces to TG: "Re-investigation failed for @<author> on <file>:<line>. Reply Fix or Won't Fix to close." Flag cleared.
- **Operator decides via button while re-investigation is in flight:** the button click sets `decision`, which short-circuits the re-investigation phase on the next tick (we check `decision is not None` before re-classifying).
- **Old workspace state with `msg_id` (singular):** lazy migration on first access converts to `msg_ids: [msg_id]`. No standalone migration script.
- **Operator hint includes profanity / nonsense:** agent treats it as evidence; if the hint is itself wrong, agent can return `Not valid` and the operator sees the same verdict back. After 3 rounds, hard cap kicks in.
- **`/unanswered` when no PR_REVIEW workspaces:** echo "No tickets have unanswered PR comments." — no error.

## Out of Scope (Explicitly)

- Changing how AUTO_FIX queues fixes or how AUTO_REJECT replies on GitHub.
- Verdict on AUTO-classified comments displayed in TG (those don't escalate to TG today).
- Persisting hint history across pipeline restarts beyond what's already in workspace state JSON.
- Rate-limiting `/unanswered` recall (operator can call it freely; cost is negligible).

## Success Criteria

- Operator types "don't fix this is intentional" → echoed as `WON'T FIX (matched: 'don't fix')` and posted on GitHub. No SKIP.
- Operator types free-text hint → "🔍 Re-checking…" within 2s, new assessment within ~30-60s, re-escalated message has updated verdict + reason.
- Operator types `/unanswered` → every undecided comment from active PR_REVIEW workspaces re-sent with fresh buttons.
- Reply to either original or recall message resolves the same comment.
- 4th hint on the same comment rejected with "Hint loop exceeded" message.
- **Every comment receives a GitHub reply at decision time:** AUTO_FIX gets a `"Will fix: ..."` announce, ESCALATE→FIX gets a `"Will fix: ..."` announce, AUTO_REJECT and ESCALATE→WON'T FIX continue to post `"Won't fix: ..."` and resolve. Post-fix `"Fixed in commit ..."` replies still fire after dev-agent lands the change.
- All new tests pass; renamed test file's old SKIP assertions are gone.

# Design: PR Review Comment Resolution

**Status:** Design
**Created:** 2026-04-21
**Author:** Oleksandr Brazhenko

**See also:** [2026-04-30 PR Review Flow Improvements](2026-04-30-pr-review-flow-improvements-design.md) — adds verdict, re-investigation hint loop, and unanswered recall on top of this base flow.

## Problem

After the pipeline pushes code and opens a PR, human reviewers leave comments. Today the pipeline has no way to:
- Wait for review completion
- Fetch and classify PR comments
- Auto-fix trivial issues
- Reply on GitHub/GitLab explaining why something won't be fixed
- Resolve comments after handling them
- Loop back for another review cycle if fixes were pushed

The operator must manually review every comment, fix code, reply, and resolve — defeating the purpose of an autonomous pipeline.

## Goals

- Fully autonomous handling of clear-cut comments (trivial fixes, obvious scope creep)
- Human-in-the-loop via Telegram for ambiguous comments
- Reply on GitHub/GitLab with reasons for won't-fix decisions
- Structured resolution report for audit trail
- Review cycle loop: fixes pushed → wait for re-review → fetch new comments → repeat until clean

## Non-Goals

- Auto-merging PRs (human always merges)
- Handling PR approval/rejection (only review comments)
- Supporting review tools beyond GitHub and GitLab

## Reference

Existing proven workflow at `/opt/sickle-helpers/f/pr_comments/`:
- `fetch_pr_comments.py` — GitHub API + GraphQL comment fetcher
- `400-pr-review-workflow.mdc` — extreme skepticism classification rules
- `resolve_pr_comments.py` — GraphQL comment resolution
- Real reports in `PR_*/` directories

## Flow

```
PR_REVIEW (waiting for 'reviewed' signal)
    │
    ├─ User replies 'reviewed' via TG (reply-to or free text)
    │
    ├─ Fetch unresolved PR comments via VCS adapter
    │   └─ 0 comments → DONE
    │
    ├─ Send comments to pr-comment-responder agent for classification
    │   Agent investigates each comment with extreme skepticism (x100)
    │   Returns structured JSON per comment:
    │     classification: AUTO_FIX | AUTO_REJECT | ESCALATE
    │     reason: why
    │     suggested_fix: what to change (for AUTO_FIX)
    │
    ├─ Execute auto-decisions immediately:
    │   AUTO_FIX → agent edits code
    │   AUTO_REJECT → reply on GitHub with reason, mark resolved
    │
    ├─ Send auto-handled summary to TG (one message)
    │
    ├─ Escalate ambiguous comments to TG (one message per comment)
    │   Store in state.pending_review_comments with msg_id
    │   Return skipped=True (non-blocking wait)
    │
    ├─ On each poll: check if all escalated decisions are filled
    │   User replies: fix / skip / won't fix [reason]
    │
    ├─ All decisions in → execute:
    │   fix → agent edits code
    │   won't fix → reply on GitHub with reason, mark resolved
    │   skip → do nothing
    │
    ├─ Write reports/pr-review-resolution.md
    │
    ├─ Fixes made? → git commit, push → PR_REVIEW (wait for 'reviewed' again)
    └─ No fixes → DONE
```

Each review cycle fetches only unresolved comments — previously resolved comments from earlier cycles don't reappear.

## Component: Comment Classification

**File:** `orchestrator/comment_classifier.py`

No rule-based string matching — all classification is done by the pr-comment-responder agent (Claude) with extreme skepticism from the workflow rules. The classifier module is a thin wrapper: sends comments + codebase context to the agent, parses the structured JSON response.

```python
@dataclass
class ClassifiedComment:
    comment_id: str
    author: str
    file: str
    line: int | None
    body: str
    code_context: str
    classification: str  # AUTO_FIX | AUTO_REJECT | ESCALATE
    reason: str
    suggested_fix: str   # empty for ESCALATE/AUTO_REJECT

async def classify_comments(
    comments: list[PRComment],
    workspace: Workspace,
    agent_runtime: AgentRuntime,
) -> list[ClassifiedComment]: ...
```

**Classification criteria (agent judgment, not rules):**

| Classification | When | Examples |
|---|---|---|
| AUTO_FIX | Clearly valid, trivial, within scope | Typo, missing import, naming convention violation matching project rules |
| AUTO_REJECT | Clearly out of scope or wrong | Request to change unrelated files, feature request in review comment |
| ESCALATE | Anything the agent isn't very confident about | Style disagreements, non-trivial refactoring suggestions, ambiguous feedback |

Default is ESCALATE — the agent must be very confident to auto-handle.

## Component: VCS Adapter Additions

**Files:** `integrations/base/vcs.py`, `integrations/github/github_adapter.py`, `integrations/gitlab/gitlab_adapter.py`

New interface methods:

```python
async def reply_to_comment(self, pr_number: int, comment_id: str, body: str) -> None:
    """Post a reply to a PR review comment."""

async def resolve_comment(self, pr_number: int, comment_id: str) -> None:
    """Mark a PR review comment thread as resolved."""
```

**GitHub implementation:**
- `reply_to_comment` — REST API: `POST /repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies`
- `resolve_comment` — GraphQL mutation `minimizeComment` or `resolveReviewThread` (same approach as `resolve_pr_comments.py`)

**GitLab implementation:**
- `reply_to_comment` — REST API: `POST /projects/{id}/merge_requests/{mr}/discussions/{discussion_id}/notes`
- `resolve_comment` — REST API: `PUT /projects/{id}/merge_requests/{mr}/discussions/{discussion_id}` with `resolved: true`

## Component: Reworked `_action_fetch_pr_comments`

**File:** `orchestrator/orchestrator.py`

Current flow: fetch → run agent → return DEV or DONE.

New flow:

```python
async def _action_fetch_pr_comments(self, workspace, stage_def) -> ActionResult:
    state = workspace.state

    # 1. Wait for 'reviewed' signal
    reply = (state.human_input_reply or "").lower()
    if "reviewed" not in reply and "proceed" not in reply:
        # Check if we're waiting for escalated comment decisions
        pending = state.pending_review_comments or []
        undecided = [c for c in pending if c.get("decision") is None]
        if undecided:
            return ActionResult(skipped=True, ...)  # still waiting
        if not pending:
            return ActionResult(skipped=True, ...)  # waiting for 'reviewed'

    # 2. If we have pending decisions that are all filled → execute them
    pending = state.pending_review_comments or []
    if pending and all(c.get("decision") for c in pending):
        return await self._execute_review_decisions(workspace)

    # 3. Fresh review cycle — fetch + classify
    state.human_input_reply = None
    state.pending_review_comments = []
    state.stage_iterations["pr_review"] = 0
    workspace.save_state()

    comments = await vcs.get_pr_comments(pr_number)
    if not comments:
        return ActionResult(success=True, next_state="DONE", ...)

    classified = await classify_comments(comments, workspace, agent_runtime)

    # 4. Auto-handle
    auto_fixed, auto_rejected = [], []
    for c in classified:
        if c.classification == "AUTO_FIX":
            # Agent already made the fix during classification
            auto_fixed.append(c)
            await vcs.resolve_comment(pr_number, c.comment_id)
        elif c.classification == "AUTO_REJECT":
            await vcs.reply_to_comment(pr_number, c.comment_id, c.reason)
            await vcs.resolve_comment(pr_number, c.comment_id)
            auto_rejected.append(c)

    # 5. Send auto-handled summary to TG
    # ... (one message with all auto actions)

    # 6. Escalate remaining
    escalated = [c for c in classified if c.classification == "ESCALATE"]
    if not escalated:
        # All auto-handled
        self._write_resolution_report(workspace, classified, cycle=...)
        if auto_fixed:
            # commit + push, loop back
            return ActionResult(success=True, next_state="PR_REVIEW", ...)
        return ActionResult(success=True, next_state="DONE", ...)

    # Store escalated with TG msg_ids, return skipped
    pending = []
    for c in escalated:
        msg_id = await self._send_escalated_comment_tg(workspace, c)
        pending.append({"comment_id": c.comment_id, "msg_id": msg_id, "decision": None, ...})
    state.pending_review_comments = pending
    workspace.save_state()
    return ActionResult(skipped=True, ...)
```

## Component: Reworked pr-comment-responder Agent

**File:** `agents/pr-comment-responder-agent.md`

Prompt includes:
- Your 400-workflow rules (extreme skepticism x100)
- The PR diff context
- All unresolved comments
- Project architecture rules
- The BA plan (to know what's in scope)

Agent returns structured JSON:

```json
[
  {
    "comment_id": "12345",
    "classification": "AUTO_FIX",
    "reason": "Project uses @PreviewAcme, this file has bare @Preview",
    "suggested_fix": "Replace @Preview with @PreviewAcme on line 10"
  },
  {
    "comment_id": "67890",
    "classification": "ESCALATE",
    "reason": "Reviewer suggests using dimen resource — valid convention but adds scope",
    "suggested_fix": ""
  }
]
```

Classification and fixing are two separate agent calls:
1. **Classify call** — agent reads all comments + codebase, returns JSON classifications. No code changes.
2. **Fix call** — for each AUTO_FIX + user-approved fix, agent runs again with specific instructions to edit the file. This is a targeted dev-agent-style call with write tools enabled.

## Component: TG Integration

**File:** `integrations/telegram/command_handler.py`

Handle replies to escalated comment messages:

```python
# In handle_reply, match escalated comment msg_ids
for c in state.pending_review_comments:
    if c["msg_id"] == reply_to_msg_id:
        c["decision"] = text  # "fix" / "skip" / "won't fix: reason"
        workspace.save_state()
        # Wake orchestrator
```

Also handle free-text: `fix TICKET-ID #1` / `won't fix TICKET-ID #1: out of scope`

**TG message formats:**

Escalated comment:
```
💬 [project/repo] TICKET-ID — PR #1234
Comment by @reviewer on File.kt:42
──────────────────────────────
Code:
  recyclerView.setSpacing(4.toPx)

Suggestion:
  Use dimen resource instead of hardcoded value

Agent assessment:
  VALID — project convention uses R.dimen references
  for spacing, but this is a one-off in HomeFragment.
  Fixing would mean adding a new dimen just for this.
──────────────────────────────
↩️ Reply: fix / skip / won't fix [reason]
```

Auto-handled summary:
```
🤖 [project/repo] TICKET-ID — PR #1234
Auto-processed 2 comments:
──────────────────────────────
✅ FIX: @Preview → @PreviewAcme (EmptyBlock.kt:10)
❌ REJECT: "Add analytics tracking" — out of scope
   → replied on GitHub: "Out of scope for TICKET-ID"
──────────────────────────────
Waiting for your decisions on 1 escalated comment above.
```

## Component: Resolution Report

**File:** Written to `reports/pr-review-resolution.md`

Each review cycle appends to the same file.

```markdown
# PR Review Resolution — {ticket_id}

PR: #{pr_number}
Review cycle: {n}
Comments this cycle: {count}
Comments total (all cycles): {total}

## Comment #1 — @author on file.kt:line
Suggestion: {body}
Classification: {AUTO_FIX|AUTO_REJECT|ESCALATE}
Status: {FIXED|WON'T_FIX|SKIPPED}
Action: {what was done}
GitHub reply: {posted|N/A}
Mark as Resolved: {YES|NO}

## 🎯 Resolution Summary
Fixed: N | Won't Fix: N | Commented: N | Skipped: N
Commits: {sha list}
```

## Component: State Changes

**File:** `workspace/workspace.py`

Add to `WorkspaceState`:

```python
pending_review_comments: list[dict] | None = None
# Each entry: {comment_id, msg_id, decision, author, file, line, body, reason}
review_cycle: int = 0
```

## Testing

**Unit tests:**
- `test_comment_classifier.py` — agent returns structured JSON, parser handles edge cases (malformed JSON, missing fields)
- `test_vcs_reply.py` — `reply_to_comment` and `resolve_comment` call correct API endpoints (mocked HTTP)
- `test_pr_review_action.py` — 0 comments → DONE; all auto-handled → commit + PR_REVIEW; escalated → skipped; all decisions in → execute + report

**Integration test:**
- Seed workspace at PR_REVIEW with mock comments → run action → verify report, VCS calls, TG messages

## Acceptance Criteria

- [ ] VCS adapter: `reply_to_comment` + `resolve_comment` for GitHub and GitLab
- [ ] Comment classifier calls pr-comment-responder agent, returns structured classifications
- [ ] pr-comment-responder agent prompt includes extreme skepticism rules + project context
- [ ] Auto-fix: agent makes code change + resolves comment on GitHub
- [ ] Auto-reject: reply on GitHub with reason + resolve comment
- [ ] Escalate: TG message with code context + suggestion + agent assessment + reply instructions
- [ ] TG replies (fix/skip/won't fix) stored in `pending_review_comments`
- [ ] All decisions filled → execute, write resolution report
- [ ] Fixes made → commit, push → PR_REVIEW (loop for re-review)
- [ ] No fixes → DONE
- [ ] Resolution report tracks cycles, per-comment status, summary with Commented count
- [ ] Unit tests for classifier, VCS methods, action flow

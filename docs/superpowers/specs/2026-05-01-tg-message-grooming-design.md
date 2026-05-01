# TG Message Grooming Design

Date: 2026-05-01

## Overview

Three scoped changes:
1. Fix three documentation gaps + remove one dead callback handler
2. Standardize the ticket message header format and extract it to a shared module
3. Fix message quality: strip all markdown from TG output, add headers to all ticket messages, fix terse/misleading copy

---

## Part 1: Docs + Dead Code

### docs/telegram.md additions

**"Replies and unblock flow" section — add `skip` keyword:**

> If you reply with `skip`, the workspace is advanced to the next stage (e.g. ANALYSIS -> DEV) without re-running the blocked stage. Useful when you want to move past a stuck agent rather than give it another shot. If you reply with `retry`, the workspace re-enters its previous_state without adding context.

**"Replies and unblock flow" section — add PR_REVIEW reply path:**

> The PR_REVIEW notification message (the one with the Review Complete button) is also a reply anchor. Replying to it with any text signals that you have finished reviewing and the pipeline should fetch PR comments. This is equivalent to tapping the Review Complete button.

**Buttons table — no change.** The `skip` callback handler is being removed (see below); it was never wired to a button.

### command_handler.py — remove dead `skip` callback

Remove the `elif action == "skip":` block (~lines 809-827). No button ever sends this action. The reply-based `skip` keyword in `handle_reply` stays and is now documented.

---

## Part 2: Header Format

### New format

Every message about a specific ticket starts with:

```
{emoji} [project_id] ticket_id
ticket_title
______________________________
```

Rules:
- 30 underscores exactly, no more, no less
- Title line omitted when title is empty string
- Emoji from the assignment table below; omit the emoji prefix entirely (no leading space) when no emoji applies

### New module: orchestrator/tg_format.py

Two public functions:

```python
def read_ticket_title(workspace) -> str:
    """Read meta/ticket.json summary field. Returns empty string on any failure."""

def tg_header(emoji: str, project_id: str, ticket_id: str, title: str = "") -> str:
    """Return formatted header string.
    
    Example output (with title):
        ❌ [acme] T-123
        Fix login crash
        ______________________________
    
    Example output (no title):
        ❌ [acme] T-123
        ______________________________
    """
```

Also add:

```python
def strip_markdown(text: str) -> str:
    """Remove markdown formatting that Telegram renders as literal syntax.
    
    Handles: **bold**, _italic_, `code`, ```blocks```,
    | table | rows |, |---|---| separator rows, # headings.
    Does not touch Unicode characters (bullets, box-drawing, emojis).
    """
```

### Changes to orchestrator.py

- Remove `_tg_header` static method — replaced by `tg_format.tg_header`
- Remove `_get_ticket_title` static method — replaced by `tg_format.read_ticket_title`
- All 7 existing `_tg_header(...)` call sites: update to `tg_format.tg_header(...)`
- Messages with hardcoded `[company/repo]` format (lines 1909, 1767-1772, 2130-2136, 2094): replace with `tg_format.tg_header`
- `_build_blocked_reason` output: wrap with `tg_format.strip_markdown()` before embedding in message
- `_build_gate_summary` BA summary line: wrap with `strip_markdown()`

### Changes to escalation_view.py

- Import `tg_format`
- Replace hardcoded header line with `tg_format.tg_header(...)`
- Remove backtick formatting from option descriptions (see Part 3)

### Changes to command_handler.py

- Import `tg_format`
- Every `send_message` that references a `ticket_id` and has a `ws` workspace in scope:
  prepend `tg_format.tg_header("", ws.state.company_id, ws.state.ticket_id, tg_format.read_ticket_title(ws))` to the message body
- Emoji assignments for command-handler confirmations:

| Message | Emoji |
|---|---|
| Approved | ✅ |
| Rejected | ❌ |
| Retry (any) | 🔄 |
| Skip | ⏭ |
| Reviewed / PR fetch | 🔍 |
| Resume with input | ▶ |
| Clear gradle success | 🧹 |
| Clear gradle error | ⚠ |
| Hint staged | 🔍 |
| Hint exhausted | ⚠ |
| Re-investigation failed | ⚠ |

---

## Part 3: Message Quality

### Hard rules (apply to every message)

1. No markdown: no `**bold**`, no `_italic_`, no backticks, no `# headings`, no `| tables |`
2. No reply hint (`↩️ Reply...`) on non-blocking messages
3. Every ticket message has the standard header (Part 2)

### Message audit table

Status codes:
- OK — content already meets reason + options + suggestion standard
- FIX — one or more issues listed in the Problems column
- N/A — not ticket-specific, out of scope for header/quality rules

| # | Message | File | Status | Problems | Required change |
|---|---|---|---|---|---|
| 1 | QA warnings | orchestrator.py:971 | OK | None | Header format update only (Part 2) |
| 2 | Gate: ANALYSIS done | orchestrator.py:2460 | OK | None | Header format update only |
| 3 | Gate: QA passed | orchestrator.py:2476 | OK | None | Header format update only |
| 4 | Gate: PR_REVIEW done | orchestrator.py:2432 | OK | None | Header format update only |
| 5 | Deferred — quota | orchestrator.py:1059 | OK | None | Header format update only |
| 6 | Deferred — max-turns | orchestrator.py:1065 | OK | None | Header format update only |
| 7 | Deferred — transient | orchestrator.py:1072 | OK | None | Header format update only |
| 8 | Failed — generic | orchestrator.py:1122 | FIX | Terse — no options shown | Add: "Options: tap Retry to re-run from the last stage, or send 'retry TICKET from STAGE' to pick a different starting point." |
| 9 | Failed — arch mismatch | orchestrator.py:1103 | OK | None | Header format update only |
| 10 | Failed — Gradle cache | orchestrator.py:1112 | OK | None | Header format update only |
| 11 | Rerun from dashboard | orchestrator.py:1141 | OK | None | Header format update only |
| 12 | PR created | orchestrator.py:1336 | FIX | Terse — no context, no options | Rewrite: explain what PR_REVIEW means, what to do (review the diff, reply to any comment escalations, tap Review Complete when done), mention reply anchor |
| 13 | Blocked — agent question | orchestrator.py:2289 | FIX | Backtick markdown in max_iterations hint (lines 2298-2299); agent output embedded raw (via _build_blocked_reason) may contain markdown | Strip markdown from embedded reason; replace backtick commands with plain text |
| 14 | Blocked — max iterations | orchestrator.py:2285 | FIX | Same as #13 | Same fix |
| 15 | Verification blocked | orchestrator.py:2243 | FIX | Agent output embedded raw (may contain markdown tables/bold from scope-guard) | Strip markdown from _build_blocked_reason and _notify_verification_blocked reason |
| 16 | Done | orchestrator.py:2130 | FIX | No header (hardcoded); no next-step instruction | Add header; rewrite to say PR is ready to merge and that Jira is updated |
| 17 | PR auto-processed summary | orchestrator.py:1909 | FIX | No header (hardcoded [company/repo] format); no title | Add header via tg_header |
| 18 | Dev-agent fix failed x2 | orchestrator.py:1767 | FIX | No header; terse — no options | Add header; add options: "Reply fix to try again, or won't fix: <reason> to close the comment." |
| 19 | Re-investigation failed | orchestrator.py:1661 | FIX | No header; backtick markdown; terse | Add header; remove backticks; add context explaining what re-investigation is |
| 20 | PR review pause | orchestrator.py:2094 | FIX | No header (hardcoded) | Add header via tg_header; content is already clear |
| 21 | Escalated PR comment | escalation_view.py:38 | FIX | No standard header; backtick markdown on fix/won't-fix instructions | Replace header with tg_header; remove backticks from option descriptions |
| 22 | Processing indicator | command_handler.py:160 | N/A | — | — |
| 23 | FIX decision confirm | command_handler.py:546 | FIX | No header | Add header |
| 24 | WON'T FIX decision confirm | command_handler.py:546 | FIX | No header | Add header |
| 25 | PR review signal (reply) | command_handler.py:590 | FIX | No header | Add header |
| 26 | Skip (BLOCKED reply) | command_handler.py:616 | FIX | No header | Add header |
| 27 | Retry (BLOCKED reply) | command_handler.py:630 | FIX | No header | Add header |
| 28 | Resume with input | command_handler.py:646 | FIX | No header | Add header |
| 29 | Hint staged | command_handler.py:739 | FIX | No header | Add header |
| 30 | Hint exhausted | command_handler.py:711 | FIX | No header; backtick markdown | Add header; remove backticks |
| 31 | Unanswered recalled | command_handler.py:699 | N/A | Response to /unanswered, no single ticket | No change |
| 32 | Approve confirm | command_handler.py:773 | FIX | No header | Add header |
| 33 | Reject confirm | command_handler.py:782 | FIX | No header | Add header |
| 34 | Reviewed confirm | command_handler.py:791 | FIX | No header | Add header |
| 35 | Retry button confirm | command_handler.py:805 | FIX | No header | Add header |
| 36 | Clear gradle success | command_handler.py:861 | FIX | No header | Add header |
| 37 | Clear gradle error | command_handler.py:843 | FIX | No header | Add header |
| 38 | No workspace / error fallbacks | command_handler.py:769,778,787,798,812,834 | N/A | No ticket context available | No change |

### Rewrite targets (FIX rows where body copy must change, not just header)

**#8 — Failed (generic)**
Current: `FAILED at {stage}. Error: {first_line}.`
New:
```
FAILED at {stage}.

Reason: {first_line}

Options:
- Tap Retry to re-run from {stage}
- Send "retry {ticket_id} from dev" to restart from an earlier stage
```

**#12 — PR created**
Current: `PR created: {url}\n\nPlease review the code.`
New:
```
PR opened: {url}

Review the diff and merge when ready. The pipeline will wait.

If there are review comments, Sickle will escalate them one by one for your decision (Fix or Won't Fix). Reply to any escalation message to provide context.

When done: tap Review Complete or reply to this message.
```

**#16 — Done**
Current: `Pipeline complete. PR ready for merge:\n{url}`
New:
```
Pipeline complete.

PR ready for merge: {url}

Jira ticket moved to review status.
```

**#18 — Dev-agent fix failed twice**
Current: `Dev-agent failed to fix comment #{cid} twice ({file}:{line})`
New:
```
Dev-agent failed to apply the fix for comment #{cid} twice.

File: {file}:{line}

The dev-agent committed changes but the target file was not modified as expected. Options:
- Reply "fix" to this comment to retry once more
- Reply "won't fix: <reason>" to close the comment without fixing
```

**#19 — Re-investigation failed**
Current: `⚠ Re-investigation failed for @{author} on {file}:{line}. Reply fix or won't fix to close.`
New:
```
Re-investigation failed for @{author}'s comment on {file}:{line}.

The agent was unable to re-classify this comment after your hint. Options:
- Reply "fix" to send the dev-agent in anyway
- Reply "won't fix: <reason>" to close the comment on GitHub
```

**#21 — Escalated PR comment (escalation_view.py)**
Current option list uses backtick formatting.
New (plain text):
```
Reply to this message with:
  - fix — re-engage dev-agent
  - won't fix: <reason> — post the reason on GitHub and resolve
  - free text — re-investigate with your hint
```

---

## Files changed

| File | Change type |
|---|---|
| docs/telegram.md | Add skip keyword + PR_REVIEW reply docs; remove skip button note |
| orchestrator/tg_format.py | New module: tg_header, read_ticket_title, strip_markdown |
| orchestrator/orchestrator.py | Replace _tg_header/_get_ticket_title; apply strip_markdown to agent output; body copy rewrites for rows #8, #12, #16, #17, #18, #19, #20 |
| orchestrator/escalation_view.py | Use tg_header; remove backtick formatting |
| orchestrator/gradle_remediation.py | No change (ARCH_MISMATCH_HELP is already plain text) |
| integrations/telegram/command_handler.py | Remove skip callback; add tg_header to all ticket confirmations; remove backticks |

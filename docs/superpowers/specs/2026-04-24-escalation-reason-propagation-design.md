# Escalation Reason Propagation — Design Spec

**Date:** 2026-04-24
**Status:** Approved

## Problem

When the pipeline transitions a workspace to BLOCKED, the human-facing surfaces (Telegram notification, `/status` drill-down) do not carry the reason for the transition. Operators receive notifications they cannot act on without opening the dashboard or the workspace reports.

Concrete symptoms observed on 2026-04-24:

- **ACME-12043 (BLOCKED via escalation)** — TG message body was three boilerplate lines from the latest agent output (`---`, `**Attempt: 2026-04-24 13:57 UTC**`, `## Decision: Escalate — \`waiting_for_human\``). The BA agent's actual questions, written to `reports/ba-questions.md`, were never surfaced. `/status` reported `Last error: none`.
- **Verification-failure BLOCKEDs** — no TG notification is sent at all. The operator finds out only by checking the dashboard.
- Escalation messages carry `[Proceed] [Retry]` inline buttons, contradicting the original [inline buttons spec](./2026-04-21-telegram-inline-buttons-design.md) which defines escalations as text-reply-only.

Root cause: three call sites each handle the reason differently, and none of them extracts it reliably.

## Decisions

- **One helper, three call sites.** A single `_build_blocked_reason(workspace, stage_id) -> str` helper owns the extraction logic. The three call sites consume its output.
- **Escalation messages stay text-reply only.** Remove `[Proceed] [Retry]` buttons from `_handle_escalate`. Operators reply with `skip`, `retry`, or free text — the existing reply flow in `command_handler.handle_reply()` already recognizes these.
- **Verification-failure BLOCKED now notifies.** Same TG + state-field semantics as an escalation, so the existing reply flow unblocks it unchanged.
- **`human_input_question` stores the reason, not the full TG message.** Today it stores the entire formatted message. That confuses downstream consumers (status drill-down, future dashboard use) that want to re-wrap the reason in their own UI.
- **No new workspace fields.** Existing `human_input_question`, `escalation_msg_id`, `escalation_chat_id` are sufficient.

## Helper

```python
def _build_blocked_reason(self, workspace: Workspace, stage_id: str) -> str:
    """Extract a human-readable reason for why a workspace is blocked.

    For analysis: prefer reports/ba-questions.md (the BA agent's numbered questions).
    For other stages: extract the first meaningful content from the latest
    agent output, skipping header boilerplate.
    Falls back to a generic message if nothing useful is found.
    """
```

**Precedence:**

1. `stage_id == "analysis"` and `reports/ba-questions.md` exists → return its contents (truncated to ~800 chars).
2. Else read the latest `reports/*-output.md` by mtime. Strip leading lines matching any of:
   - `^---+$` / `^===+$` (markdown separators)
   - `^\*\*Attempt.*\*\*$` (timestamp header emitted by the agent runtime)
   - `^## Decision:` (routing decision line)
   - blank lines
   Then return up to ~800 chars of what remains, appending `…` if truncated.
3. Fall back to `"Pipeline stuck at {stage_id}. Check reports/ for details."`

## Call Sites

### 1. `orchestrator._handle_escalate` (orchestrator.py:1646)

Replace the inline summary extraction (lines 1670-1692) with:

```python
reason = self._build_blocked_reason(workspace, stage)
hint = f"\n{sep}\n↩️ Reply with your answer or additional context."
message = f"{header}\n{reason}{hint}"
msg_id = await self._notifier.send_message(chat_id, message)  # no buttons
workspace.update_state(human_input_question=reason)  # store reason, not full message
```

Drop the `buttons=[Proceed, Retry]` argument. Drop "or use buttons below" from the hint string.

### 2. `orchestrator._handle_agent_stage` verification-fail path (orchestrator.py:794-807)

Currently sets `error`, transitions to BLOCKED, emits `stage_verification_failed`. Adds: send a TG notification with the same shape as `_handle_escalate` but with a different header.

```python
if not verify_result.ok:
    agent_snippet = (result.output or "")[:200].replace("\n", " ")
    error_msg = f"{stage_id}: {verify_result.reason} (agent said: {agent_snippet})"
    workspace.transition(Stage.BLOCKED)
    workspace.update_state(error=error_msg)
    self._log_pipeline(...)
    self._emit("stage_verification_failed", ...)
    await self._notify_verification_blocked(workspace, stage_id, verify_result.reason)
    return
```

New method `_notify_verification_blocked(workspace, stage_id, verify_reason)`:

- Builds a combined body: `f"Verification failed: {verify_reason}\n\n{self._build_blocked_reason(workspace, stage_id)}"`
- Header uses `⚠️` instead of `🔔` and stage line `f"Stage: {stage_id} — verification failed"`
- Sends message, captures `msg_id`
- Sets `human_input_question=<combined body>`, `escalation_msg_id=msg_id`, `escalation_chat_id=chat_id` on workspace state. Storing the combined body means `/status` drill-down shows both the verifier's complaint and the agent's snippet under `Blocked on:`.

The existing reply handler in `command_handler.handle_reply()` matches on `escalation_msg_id` and already handles BLOCKED workspaces, so unblock via reply works without changes.

### 3. `handlers/status.py::format_drill_down` (status.py:117-120)

```python
if s.current_state == "BLOCKED" and s.human_input_question:
    reason = s.human_input_question.strip()
    if len(reason) > 500:
        reason = reason[:500] + "…"
    lines.append(f"\nBlocked on: {reason}")
elif s.error:
    lines.append(f"\nLast error: {s.error}")
else:
    lines.append(f"\nLast error: none")
```

## Message Templates

### Escalation (unclear requirements, max iterations, notify_human action)

```
🔔 [project/repo] TICKET-ID
Stage: ANALYSIS
──────────────────────────────
<reason — numbered questions from ba-questions.md, or extracted output>
──────────────────────────────
↩️ Reply with your answer or additional context.
```

No buttons.

### Verification-failure BLOCKED (new)

```
⚠️ [project/repo] TICKET-ID
Stage: QA — verification failed
──────────────────────────────
Verification failed: <verify_result.reason>

<reason from agent output>
──────────────────────────────
↩️ Reply with your answer or additional context.
```

No buttons.

### `/status` drill-down (BLOCKED)

```
ACME-12043

Stage: BLOCKED
Branch: feature/ACME-12043-…
Jira: …

Iterations:
  analysis: 1

Blocked on: 1. [AC2] The acceptance criterion "handles errors" is vague…
```

## Not Changed

- `_notify_failed` — current format (stage name + first line of error) is adequate for the FAILED case. "Timed out" is the complete reason.
- Dashboard UI — `actions.py` and the cards already read `state.error`; BLOCKED still shows up with the pulse styling. The dashboard surfacing of `human_input_question` is a separate change if needed.
- BA agent prompt — the agent already writes `ba-questions.md` on unclear; we just start reading it.
- Workspace state fields — all reused.
- The inline buttons spec — no amendment needed; we are bringing the code back in line with it.

## File Changes

| File | Change |
|---|---|
| `orchestrator/orchestrator.py` | Add `_build_blocked_reason`. Rewrite summary/button logic in `_handle_escalate`. Add `_notify_verification_blocked` and call it from the verification-fail branch in `_handle_agent_stage`. Change `human_input_question` to store just the reason. |
| `integrations/telegram/handlers/status.py` | Show `Blocked on: <reason>` for BLOCKED workspaces in `format_drill_down`. |
| `tests/unit/test_orchestrator.py` (or new) | Unit tests for `_build_blocked_reason`: ba-questions.md path, boilerplate-only output, empty reports. |
| `tests/unit/test_handlers_status.py` (or similar) | Assert BLOCKED drill-down renders `Blocked on:` line when `human_input_question` is set. |

## Test Plan

### Unit
- `_build_blocked_reason` with `ba-questions.md` present → returns its content.
- `_build_blocked_reason` on non-analysis stage with output starting in boilerplate → boilerplate stripped, meaningful body returned.
- `_build_blocked_reason` with empty reports dir → generic fallback string.
- `_handle_escalate` called on a workspace with `ba-questions.md` → notifier receives message containing questions, `buttons` arg is `None`, `human_input_question` stores only the reason.

### Integration
- Force analysis escalation on a test workspace; assert TG mock received questions (not boilerplate).
- Force verification failure; assert TG mock received a message and `escalation_msg_id` is set.
- Reply "skip" to a verification-failure message; workspace advances to next stage.

### Manual
- Reproduce on ACME-12043-style ticket locally: confirm escalation shows questions, `/status <ticket>` shows `Blocked on: …`.

## Change Log

| Date | Description |
|---|---|
| 2026-04-24 | Initial draft — brainstormed from TG observation on ACME-12043 and ACME-12051. |

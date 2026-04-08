# Telegram Command Layer: Status, Modes & Interactive Control

**Date:** 2026-04-08
**Status:** Draft

## Overview

Extend Sickle's Telegram integration from outbound-only notifications to a full interactive command interface. Three capabilities:

1. **Status / health check** — on-demand pipeline status with drill-down per ticket
2. **Auto / manual mode** — operator chooses between fully autonomous and approval-gated pipeline execution
3. **Free-text command interface** — Claude CLI-powered intent parser for conversational interaction

## 1. Free-Text Command Interface

### Intent Parser

Every incoming Telegram message (that isn't a reply to an escalation) is routed through an `IntentParser`. The parser calls Claude Code CLI via a new `quick_query` method on `claude_code_adapter` — a lightweight prompt-to-response call with no tools, short max tokens, and a 5-second timeout.

**System prompt structure:**

```
You are the command parser for Sickle, an autonomous dev pipeline.
Current state:
- Mode: {mode}
- Awaiting approval: {list of ticket IDs and their gate}
- Active workspaces: {list of ticket IDs and stages}

Classify the user message into one of these intents:
status, analyze, approve, reject, set_mode, unknown

Return JSON: { "intent": "...", "params": { ... }, "reply": "..." }
```

The `reply` field allows the parser to generate a natural confirmation message rather than requiring hardcoded response templates.

Pipeline state is injected into the system prompt on every call so the parser can resolve ambiguity:
- "yes" resolves to `approve` for the only workspace in `AWAITING_APPROVAL`
- "yes to the search ticket" matches against active workspace titles
- "how's 123 doing" resolves to `status` with drill-down for ACME-123

### Supported Intents

| Intent | Example Messages | Params |
|--------|-----------------|--------|
| `status` | "what's going on", "status", "how are things" | optional: `ticket_id` for drill-down |
| `analyze` | "start analyzing ACME-123", "work on ACME-123, ACME-456" | `ticket_ids` (required) |
| `approve` | "yes go ahead", "approved", "move forward with ACME-123" | `ticket_id` (optional, inferred from context) |
| `reject` | "no stop", "don't proceed", "reject ACME-123" | `ticket_id` (optional) |
| `set_mode` | "switch to manual", "go auto", "manual mode" | `mode`: auto or manual |
| `unknown` | anything unrecognizable | `raw_text` |

For `unknown` intent, the bot replies with a friendly message: "I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, switch modes."

For `analyze` intent: the handler fetches the ticket from Jira before creating a workspace. If the ticket doesn't exist or is inaccessible, the bot replies with an error message and does not create a workspace.

### Error Handling

- **Claude CLI unavailable:** Bot replies "I'm having trouble understanding right now. Try again in a moment." Pipeline continues unaffected — command interface is decoupled from core pipeline.
- **Malformed JSON response:** Treat as `unknown`, ask user to rephrase.
- **Timeout:** 5-second limit on the CLI call.

### Cost

Negligible. Even at 100 messages/day, intent parsing costs ~$0.10/day.

## 2. Status / Health Check

Triggered by the `status` intent. Two levels of detail.

### Summary View

```
Sickle Status

Mode: manual
Uptime: 3d 14h
Last Jira poll: 2 min ago

Active (3):
  ACME-123 — DEV (iteration 1/2)
  ACME-456 — QA (iteration 1/2)
  ACME-789 — ANALYSIS, awaiting approval

Recent (24h):
  ACME-100 — merged
  ACME-101 — failed at QA

Queue: 0 pending
```

### Drill-Down View

Triggered by mentioning a specific ticket ID (e.g., "tell me about ACME-123").

```
ACME-123 — Implement user search

Stage: DEV (iteration 1/2)
Agent: dev-agent (James)
Started: 2h ago
Branch: feature/ACME-123

Jira: https://acme.atlassian.net/browse/ACME-123
PR: https://github.com/acme/acme-mobile/pull/42

History:
  ANALYSIS — completed (45min)
  DEV — in progress

Last error: none
```

- **Jira URL** is always shown (constructed from `jira.url` + ticket key).
- **PR URL** is only shown when a PR exists (post-PUSHED stage), read from workspace state.

### Data Sources

All read from existing state — no new storage:
- **Mode / uptime:** orchestrator runtime state
- **Active workspaces:** `workspace_manager.list_active()` + each workspace's `state.json`
- **Recent completions/failures:** terminal workspaces (DONE, FAILED) with timestamps within 24h
- **Last Jira poll:** timestamp from orchestrator poll loop
- **Drill-down details:** individual workspace `state.json` (full stage history, iterations, errors)

## 3. Auto / Manual Mode

### Behavior Differences

| Aspect | Auto Mode | Manual Mode |
|--------|-----------|-------------|
| Ticket intake | Polls Jira, auto-creates workspaces | No polling. Waits for `analyze` command with explicit ticket IDs |
| Pipeline flow | Fully autonomous, no pauses | Pauses at 3 approval gates |
| Escalation | Only on errors / unclear requirements | Same, plus approval gates |
| Telegram | Receives notifications only | Interactive — commands + approvals |

### Approval Gates (Manual Mode Only)

Three gates at key milestones:

1. **Post-ANALYSIS** — "Here's the plan for ACME-123: [summary from ba.md]. Proceed to development?"
2. **Post-QA** — "Tests pass for ACME-123. [test/lint/build summary]. Push and open PR?"
3. **Post-PR_REVIEW** — "PR review complete for ACME-123. [review summary]. Finalize and merge?"

When a workspace hits a gate, it transitions to `AWAITING_APPROVAL`. The bot sends the summary. The workspace stays blocked until the operator approves or rejects.

- **Approve:** workspace resumes to the next stage.
- **Reject:** workspace moves to FAILED with rejection reason.

### Mode Switching

- **Default mode** set in config (`global.yaml` or per-project `project.yaml`).
- **Runtime override** via Telegram command (e.g., "switch to manual"). Persisted to daemon runtime state, survives restarts.
- **Auto → Manual mid-flight:** workspaces already past a gate continue. Only future gates apply.
- **Manual → Auto mid-flight:** any workspaces in `AWAITING_APPROVAL` auto-approve and resume.

### New State: AWAITING_APPROVAL

Added to the workspace state machine alongside existing states.

```json
{
  "state": "AWAITING_APPROVAL",
  "approval_gate": "post_analysis",
  "previous_state": "ANALYSIS",
  "next_state": "DEV",
  "approval_message_id": "tg_msg_12345"
}
```

Valid transitions:
- `ANALYSIS` → `AWAITING_APPROVAL` (post_analysis gate)
- `QA` → `AWAITING_APPROVAL` (post_qa gate)
- `PR_REVIEW` → `AWAITING_APPROVAL` (post_pr_review gate)
- `AWAITING_APPROVAL` → next stage (on approve)
- `AWAITING_APPROVAL` → `FAILED` (on reject)

## 4. Configuration

### global.yaml Additions

```yaml
pipeline:
  mode: auto  # auto | manual

intent_parser:
  max_tokens: 200
  timeout_seconds: 5
```

### project.yaml Override (Optional)

```yaml
pipeline:
  mode: manual  # override global default for this project
```

### Runtime State Persistence

Mode is persisted in the daemon-level state file:

```json
{
  "mode": "manual",
  "mode_changed_at": "2026-04-08T14:30:00Z",
  "active_workspaces": [...],
  "started_at": "2026-04-05T00:00:00Z"
}
```

On startup: load mode from runtime state if a runtime override exists, otherwise fall back to config default.

## 5. Component Architecture

### New Files

| File | Purpose |
|------|---------|
| `integrations/telegram/command_handler.py` | Receives all incoming messages, routes to intent parser, dispatches to handlers |
| `integrations/telegram/intent_parser.py` | Builds prompt with pipeline context, calls Claude CLI, returns structured intent |
| `integrations/telegram/handlers/status.py` | Collects workspace data, formats summary and drill-down |
| `integrations/telegram/handlers/mode.py` | Switches auto/manual, persists to runtime state |
| `integrations/telegram/handlers/analyze.py` | Creates workspaces for specified ticket IDs in manual mode |
| `integrations/telegram/handlers/approval.py` | Approves/rejects workspaces in AWAITING_APPROVAL state |

### Modified Files

| File | Change |
|------|--------|
| `integrations/telegram/telegram_adapter.py` | Hook CommandHandler into polling loop — incoming messages route to command handler first, reply-matching second |
| `orchestrator/orchestrator.py` | Check mode before polling Jira. Insert approval gate checks in manual mode. Persist/load mode from runtime state |
| `orchestrator/workflow_router.py` | Add AWAITING_APPROVAL state and transitions |
| `workspace/workspace.py` | Add AWAITING_APPROVAL to valid states |
| `workflows/default-workflow.yaml` | Add approval gate transitions |
| `config/schemas.py` | Add `pipeline.mode` and `intent_parser` config sections |
| `integrations/llm/claude_code_adapter.py` | Add `quick_query` method for lightweight prompt-to-response calls (no tools, short max tokens) |

### Message Flow (Manual Mode)

```
Operator: "analyze ACME-123"
  -> TelegramAdapter polling picks up message
  -> CommandHandler -> IntentParser (Claude CLI quick_query)
  -> Intent: { analyze, tickets: [ACME-123] }
  -> analyze handler: creates workspace, starts ANALYSIS
  -> ANALYSIS completes
  -> Orchestrator sees manual mode + post_analysis gate
  -> Workspace -> AWAITING_APPROVAL
  -> Bot sends: "Plan ready for ACME-123: [summary]. Proceed?"
Operator: "yes"
  -> IntentParser resolves to { approve, ticket: ACME-123 }
  -> approval handler: resumes workspace -> DEV
  -> DEV -> SCOPE_CHECK -> QA (all run automatically)
  -> Post-QA gate -> AWAITING_APPROVAL
  -> Bot sends: "Tests pass. Push and open PR?"
Operator: "go ahead"
  -> approve -> PUSHED -> PR_REVIEW
  -> Post-PR_REVIEW gate -> AWAITING_APPROVAL
  -> Bot sends: "PR reviewed. Finalize?"
Operator: "yes"
  -> approve -> DONE
```

# Severity-Tiered Notifications — Design

**Date:** 2026-04-15
**Status:** Proposed
**Part of:** [Feature Plan](2026-04-15-feature-plan.md)

## Problem

Sickle's Telegram integration treats every outbound alert the same. A ticket waiting for a trivial clarification ("should I use `snake_case` or `camelCase`?") pings the operator with the same urgency as a ticket that has locked the repo, left a dirty branch, and blocked three other tickets behind it. Over weeks this produces one of two failure modes:

1. The operator treats every ping as urgent, loses focus constantly, and productivity collapses.
2. The operator treats every ping as trivial, ignores them, and genuine blockers sit unaddressed for hours.

Both failure modes are already happening to some degree.

## Goal

Every outbound Telegram escalation gets tagged with a severity at the moment it's raised. Each severity has a distinct **delivery policy**, not merely different wording. Critical alerts interrupt; normal alerts respect working hours; low alerts are batched into a digest.

## Non-goals

- Replacing `BLOCKED` or the state machine — severity tags the *notification*, not the ticket
- Per-project or per-repo severity policies (one policy for all projects; revisit only if a second operator appears)
- Alternate channels (email, SMS, PagerDuty) — Telegram only
- Inbound message routing — the existing `IntentParser` handles inbound; this spec is outbound only

## Design — three tiers, three delivery policies

Three tiers is the minimum viable set. Five-tier severity systems exist for logs, not humans. In practice operators act on three buckets: *"drop everything," "look at this today,"* and *"read when convenient."* More tiers just move the decision from *"is this urgent?"* to *"is this severity 3 or severity 4?"* — the same question wearing a worse costume.

### `critical`

Something is actively broken or blocking other work. A human must act now.

**Examples:**
- Ticket Progress Watchdog killed a running ticket
- A git push left the branch in a dirty state that the daemon can't recover from
- A protected file (`arch-rules.md`, lint config, CI config) was touched by an agent
- The daemon itself can't reach Jira, GitHub, or Telegram
- Quota deferred for longer than `max_wall_clock`

**Delivery policy:**
- Immediate push to Telegram
- **Ignores quiet hours** — fires at 3 AM if it fires
- Never batched, never suppressed
- Includes a direct link to the ticket in the dashboard

### `normal`

A ticket needs a human decision to proceed, but nothing is broken.

**Examples:**
- BA agent is unsure between two valid interpretations
- Scope Guard flagged a diff that might be intentional
- PR Comment Responder received a reviewer question it can't answer confidently
- Ticket entered `BLOCKED` through a non-failure path (agent explicitly requested human input)

**Delivery policy:**
- Immediate push during working hours (configured; default `08:00–20:00` local time)
- Queued for the next digest outside working hours
- Never double-sent (if queued and working hours start, it's promoted to an immediate push once and removed from the queue)

### `low`

Informational. "You probably want to know eventually."

**Examples:**
- Stall Detector noticed a stall but the Timeout Enforcer didn't kill it
- Quota deferred for more than 1 hour (but less than `max_wall_clock`)
- Repo config drifted from template (detected by periodic check)
- Daily summary of completed tickets

**Delivery policy:**
- **Digest only.** Never sent individually.
- Batched and flushed at configured times (default `09:00` and `17:00` local time)
- Each digest is a single Telegram message with bullet points, one per queued event
- If the digest would be empty, no message is sent

## Why this shape

### Why three tiers and not five

Five-tier systems assume the severity assignment is where the hard thinking happens. But for outbound alerts, the hard thinking is *"what do I do about it?"* — and there are only three answers. More tiers force the writer at the call site to agonize over severity 2 vs 3, which wastes thinking on a decision with no behavioral difference.

### Why delivery policy, not just labels

Labels without policy differences are decoration. The whole value is that `critical` interrupts quiet hours while `low` waits until morning. If all three tiers sent immediate pushes, this would be prettier logging, not a feature.

### Why digest and not threads

Telegram supports reply threads and separate chats. Either could route different severities. Both were considered and rejected:

- **Separate chats** fragment the operator's attention — they now have to check two places. Worse, not better.
- **Reply threads** clutter the main chat with empty parent messages and don't change delivery semantics. The problem isn't presentation; it's interruption.

A batched digest solves interruption at the cost of freshness — which is the correct trade for `low` events by definition.

## Configuration

New section in `global.yaml`:

```yaml
telegram:
  severity:
    critical:
      mode: immediate
      respect_quiet_hours: false
    normal:
      mode: immediate
      respect_quiet_hours: true
    low:
      mode: digest
      digest_times: ["09:00", "17:00"]
  working_hours:
    start: "08:00"
    end: "20:00"
    timezone: "Europe/Kyiv"
```

Operators can tune `working_hours`, `digest_times`, and timezone but cannot add new severity tiers. The three are fixed by design.

## Call-site changes

The existing Telegram escalation function gains a required `severity` parameter. Every call site (estimated ~8-12 locations across `integrations/telegram/`, `orchestrator/`, agent result handlers, and the new watchdog) is updated to pass an explicit value. No default — a missing severity should be a type error caught at import time, not a silent fall-through to `normal`.

This is the only invasive part of the feature. Once the parameter is in place, the routing logic lives entirely inside the Telegram integration.

## Digest implementation

A small SQLite table in the existing `data/events.db`:

```sql
CREATE TABLE telegram_digest_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  severity TEXT NOT NULL,
  queued_at TIMESTAMP NOT NULL,
  payload TEXT NOT NULL,
  flushed INTEGER DEFAULT 0
);
```

A scheduled job (running on the orchestrator tick, guarded by a "next flush time" timestamp stored in a small state file) wakes up at each configured digest time, reads all unflushed entries, composes a single Telegram message, sends it, and marks them flushed. No cron needed — the orchestrator already ticks.

Working-hours promotion (normal alerts queued outside working hours) uses the same table with a flag distinguishing "always-digest" entries from "queued until working hours" entries.

## Dashboard integration

Every dashboard event gains a `severity` column (nullable for historical events). The event log view gains:

- A severity filter (`critical` / `normal` / `low` / all)
- Color coding on the event row (red / yellow / grey)
- A "digest queue" panel showing pending `low` events and the next scheduled flush time

## Failure modes

| Failure | Handling |
|---------|----------|
| Telegram API down when sending critical | Retry with exponential backoff up to 1 minute; after that, write to a local fallback file and log-error. Critical alerts must never be silently dropped. |
| Telegram API down when sending digest | Retry on next orchestrator tick; digest entries remain unflushed and will be included in the next successful send. |
| Clock jumps during quiet hours | Use monotonic clock for retry backoff, wall clock for working-hours comparison. Document the trade-off in the code. |
| Operator is on vacation (no one reading) | Out of scope — this is an operator-process concern, not a daemon concern. |
| Severity parameter accidentally omitted at a new call site | Type error at import / lint time. No runtime fallback. |

## Testing strategy

- **Unit:** severity → policy mapping table; verify each tier routes to the correct delivery path
- **Unit:** working-hours promotion logic for `normal` alerts (queued outside hours → promoted when working hours start)
- **Unit:** digest composition with mixed severities in the queue
- **Integration:** mock Telegram client; fire synthetic events across all three tiers; assert correct messages sent at correct times
- **Regression:** existing Telegram tests updated to include explicit severity at every call site

## Success criteria

- Critical alerts arrive within seconds of being raised, regardless of time of day
- Normal alerts respect working hours; queued ones flush correctly at start-of-day
- Low alerts arrive only in digests, never individually
- No alert is lost; no alert is sent twice
- Operator reports (qualitative) that alert fatigue has dropped and response time on real blockers has improved

## Open questions

- Should `normal` alerts queued outside working hours flush as a batch at start-of-day, or as individual messages? Recommendation: individual — queued normals are still "look at this today" and shouldn't be buried in a bullet list.
- Should the digest include completed-ticket summaries, or is that a separate daily-report feature? Recommendation: include in `low` digest for now; split only if the digest becomes too long.
- Configurable per-operator severity overrides (e.g., "treat `normal` as `low` for me")? Recommendation: no — single operator today; revisit if needed.

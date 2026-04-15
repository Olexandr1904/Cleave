# Feature Plan — Reliability, Signal, and Memory

**Date:** 2026-04-15
**Status:** Proposed
**Author:** Oleksandr Brazhenko

## Purpose

Three features that close gaps in Sickle's current pipeline without changing its core character (Jira-driven, human-merged, ticket-isolated, subscription-based). Each feature addresses a distinct class of problem: one reliability, one signal, one memory.

## The three features

1. **Ticket Progress Watchdog** — per-ticket stall detection that notices when an agent has silently hung and kills it before it burns hours
2. **Severity-Tiered Notifications** — three-level routing for outbound Telegram alerts so critical blockers interrupt and trivia goes to digest
3. **Repository Decision Ledger** — a committed, per-repo markdown file where agents record rulings that future tickets on the same repo must respect

## Problem each feature solves

| Feature | Problem today | Cost today |
|---------|---------------|------------|
| Ticket Progress Watchdog | `safeguards.py` checks *what* agents write, never *whether they're making progress*. A stalled Claude CLI call or hung subprocess is invisible until a human notices. | Wasted slot time, delayed tickets, no clean recovery path |
| Severity-Tiered Notifications | All Telegram escalations arrive on one channel with equal urgency. Trivial clarifications and broken-daemon alerts are indistinguishable. | Alert fatigue; either everything interrupts or everything is ignored |
| Repository Decision Ledger | Every ticket starts with amnesia about previous tickets on the same repo. Agents re-propose already-vetoed approaches, re-learn the same repo quirks. | Repeated human round-trips, token waste, scope creep from forgotten rulings |

## Why these three, not others

Each was chosen against three hard filters:

- **Does it preserve ticket isolation?** Sickle's strength is that tickets can't corrupt each other. Anything reintroducing shared mutable state between tickets is rejected.
- **Does it preserve human-merged PRs?** Sickle explicitly rejects auto-merge. Anything implying a merge queue, auto-approval, or reduced review is rejected.
- **Is it additive, not restructuring?** Sickle's orchestrator, state machine, config cascade, and agent model all work. Features that require ripping out existing components to install are rejected.

All three features pass all three filters. The watchdog sits alongside the orchestrator loop without changing it. The severity tiers extend the existing Telegram path without rewriting it. The decision ledger lives in the *target repo*, not Sickle, so it can't contaminate Sickle's ticket isolation.

## Sequencing recommendation

**Build order: 1 → 3 → 2.**

1. **Watchdog first** because it's the biggest reliability gap and the other features assume the daemon is reliably making progress. Until stalled tickets get killed cleanly, nothing else matters.
2. **Decision Ledger second** because it has the longest feedback loop — value compounds over months as entries accumulate. Starting earlier gives the ledger more time to populate.
3. **Severity Tiers third** because it's the smallest and depends on both of the above: the watchdog emits `critical` events that the new routing needs, and the ledger emits `low` events during normal operation.

Each feature is independently shippable and reversible. If feature 3 (Decision Ledger) proves to be ignored by agents in practice, it can be turned off by removing one line from each agent prompt — no code rollback needed.

## Scope boundaries

**In scope:**
- Per-ticket liveness and stall handling
- Tiered Telegram alerting with working-hours and digest behavior
- Per-repo markdown ledger read/written by specific agents at specific moments

**Out of scope (explicitly):**
- Cross-repo or cross-project memory (would break isolation)
- Semantic search, embeddings, or vector stores (premature; a file suffices)
- Auto-merge, merge queues, or CI gating of Sickle's own pushes
- Web UI for configuration (YAML cascade is deliberate)
- Multi-agent hierarchies, "CEO" layers, or goal decomposition (Jira already decomposes)
- PagerDuty / email / SMS fallbacks (Telegram only)

## Shared implementation constraints

- **No new external dependencies.** All three features use the existing stack: Python, SQLite, filesystem, Telegram, Git.
- **Configuration lives in `global.yaml`** under dedicated sections. No per-project overrides unless a concrete need appears.
- **Dashboard integration is mandatory.** Every new event must flow through `dashboard/events.py` so the web view remains the single pane of glass.
- **State transitions use the existing state machine.** No new terminal states; the watchdog transitions stalled tickets to the existing `BLOCKED` state.

## Success criteria

| Feature | Measurable outcome |
|---------|-------------------|
| Watchdog | Zero tickets remain in a running state for more than their configured stage budget. Killed tickets land in `BLOCKED` with a clear reason. |
| Severity Tiers | Critical alerts arrive within seconds; low alerts arrive batched at configured times; no alert is lost or double-sent. |
| Decision Ledger | Agents cite ledger entries in at least one BA brief or Dev plan per week on active repos. Ledger file grows monotonically and never gets rewritten by agents. |

## Individual specs

- [Ticket Progress Watchdog](2026-04-15-ticket-progress-watchdog-design.md)
- [Severity-Tiered Notifications](2026-04-15-severity-tiered-notifications-design.md)
- [Repository Decision Ledger](2026-04-15-repository-decision-ledger-design.md)

## Open questions

- Watchdog stage thresholds: start with defaults (LLM 5 min, test 15 min, kill 30 min) or measure current ticket stage durations first and derive thresholds from data?
- Ledger writers: should Fix agent write entries, or is that too noisy? Start with Scope Guard + PR Comment Responder only and add Fix later if warranted?
- Severity digest times: fixed `09:00`/`17:00` local time, or configurable per operator? Start fixed; make configurable only if a second operator appears.

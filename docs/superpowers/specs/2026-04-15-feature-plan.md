# Feature Plan — Reliability, Signal, Memory, and Control (+Collaboration)

**Date:** 2026-04-15
**Status:** Proposed
**Author:** Oleksandr Brazhenko

## Purpose

Five features that close gaps in Sickle's current pipeline without changing its core character (Jira-driven, human-merged, ticket-isolated, subscription-based). Each feature addresses a distinct class of problem: reliability, signal, memory, control, and collaboration.

## The five features

1. **Ticket Progress Watchdog** — per-ticket stall detection that notices when an agent has silently hung and kills it before it burns hours
2. **Severity-Tiered Notifications** — three-level routing for outbound Telegram alerts so critical blockers interrupt and trivia goes to digest
3. **Repository Decision Ledger** — a committed, per-repo markdown file where agents record rulings that future tickets on the same repo must respect
4. **Project Lifecycle Management** — pause, resume, and delete operations for projects so operators can control which projects are active without editing config files or restarting the daemon
5. **Jira Question Escalation** — when the pipeline blocks on a question (unclear requirements, missing context, BA/PM input needed), post that question as a Jira comment on the ticket so stakeholders see it where they already work instead of only in Telegram

## Problem each feature solves

| Feature | Problem today | Cost today |
|---------|---------------|------------|
| Ticket Progress Watchdog | `safeguards.py` checks *what* agents write, never *whether they're making progress*. A stalled Claude CLI call or hung subprocess is invisible until a human notices. | Wasted slot time, delayed tickets, no clean recovery path |
| Severity-Tiered Notifications | All Telegram escalations arrive on one channel with equal urgency. Trivial clarifications and broken-daemon alerts are indistinguishable. | Alert fatigue; either everything interrupts or everything is ignored |
| Repository Decision Ledger | Every ticket starts with amnesia about previous tickets on the same repo. Agents re-propose already-vetoed approaches, re-learn the same repo quirks. | Repeated human round-trips, token waste, scope creep from forgotten rulings |
| Project Lifecycle Management | No way to pause a project without removing it from config and restarting. No way to delete a project and its data cleanly. Operators resort to manual file edits. | Risk of orphaned workspaces, partial config states, and accidental ticket processing on projects that should be idle |
| Jira Question Escalation | When the pipeline blocks with a question, it only goes to Telegram. BA/PMs who need to answer don't monitor Telegram — the question sits unanswered and the ticket stalls. | Tickets blocked for hours/days waiting for answers that could have been provided quickly if the question was visible on the Jira ticket itself |

## Why these five, not others

Each was chosen against three hard filters:

- **Does it preserve ticket isolation?** Sickle's strength is that tickets can't corrupt each other. Anything reintroducing shared mutable state between tickets is rejected.
- **Does it preserve human-merged PRs?** Sickle explicitly rejects auto-merge. Anything implying a merge queue, auto-approval, or reduced review is rejected.
- **Is it additive, not restructuring?** Sickle's orchestrator, state machine, config cascade, and agent model all work. Features that require ripping out existing components to install are rejected.

All five features pass all three filters. The watchdog sits alongside the orchestrator loop without changing it. The severity tiers extend the existing Telegram path without rewriting it. The decision ledger lives in the *target repo*, not Sickle, so it can't contaminate Sickle's ticket isolation. Project lifecycle management operates at the project level, not the ticket level — paused projects simply stop polling for new tickets while existing in-flight tickets complete naturally. Jira question escalation uses the existing `add_comment()` Jira adapter method and `BLOCKED` state — it adds a comment to the ticket, not a new state or external dependency.

## Sequencing recommendation

**Build order: 1 → 3 → 2 → 4.**

1. **Watchdog first** because it's the biggest reliability gap and the other features assume the daemon is reliably making progress. Until stalled tickets get killed cleanly, nothing else matters.
2. **Decision Ledger second** because it has the longest feedback loop — value compounds over months as entries accumulate. Starting earlier gives the ledger more time to populate.
3. **Severity Tiers third** because it's the smallest and depends on both of the above: the watchdog emits `critical` events that the new routing needs, and the ledger emits `low` events during normal operation.
4. **Project Lifecycle Management fourth** because the current workflow (add/remove via config + restart) works, just inconveniently. This is a quality-of-life improvement that becomes more valuable as the number of managed projects grows.

Each feature is independently shippable and reversible. If feature 3 (Decision Ledger) proves to be ignored by agents in practice, it can be turned off by removing one line from each agent prompt — no code rollback needed.

## Scope boundaries

**In scope:**
- Per-ticket liveness and stall handling
- Tiered Telegram alerting with working-hours and digest behavior
- Per-repo markdown ledger read/written by specific agents at specific moments
- Pause/resume projects via dashboard and API (stops new ticket polling, lets in-flight tickets finish)
- Delete projects with clean removal of workspaces, state, and config references
- Project status reflected in dashboard UI
- Post pipeline questions as Jira comments on blocked tickets so BA/PMs can answer in-context

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
| Project Lifecycle | Paused projects process zero new tickets. Resumed projects resume polling within one daemon cycle. Deleted projects leave no orphaned workspaces or state entries. All operations available via dashboard and API. |
| Jira Question Escalation | Every pipeline-generated question appears as a Jira comment on the blocked ticket. BA/PMs can see and respond without leaving Jira. |

## Individual specs

- [Ticket Progress Watchdog](2026-04-15-ticket-progress-watchdog-design.md)
- [Severity-Tiered Notifications](2026-04-15-severity-tiered-notifications-design.md)
- [Repository Decision Ledger](2026-04-15-repository-decision-ledger-design.md)

## Open questions

- Watchdog stage thresholds: start with defaults (LLM 5 min, test 15 min, kill 30 min) or measure current ticket stage durations first and derive thresholds from data?
- Ledger writers: should Fix agent write entries, or is that too noisy? Start with Scope Guard + PR Comment Responder only and add Fix later if warranted?
- Severity digest times: fixed `09:00`/`17:00` local time, or configurable per operator? Start fixed; make configurable only if a second operator appears.
- Project pause behavior: should in-flight tickets be force-killed on pause, or allowed to complete naturally? Recommend: complete naturally, but block new ticket pickup.
- Project delete: soft-delete (mark inactive, keep data) or hard-delete (remove workspaces and state)? Recommend: hard-delete with a confirmation prompt; data can be recovered from git history if needed.
- Jira Question Escalation: post to Jira in addition to Telegram, or instead of? Should the pipeline also poll Jira comments for answers to auto-unblock, or keep the reply path via Telegram only?

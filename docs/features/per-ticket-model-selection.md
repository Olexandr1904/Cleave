# Feature: Per-Ticket Model Selection via Jira Label

**Status:** In Progress
**Created:** 2026-04-30
**Updated:** 2026-04-30
**Author:** Oleksandr Brazhenko

## Description

Allows operators to override the Claude model used for a single ticket by adding a Jira label: `model-haiku`, `model-opus`, or `model-sonnet`. The label is read once at workspace creation, snapshotted into `WorkspaceState.model`, and used by every agent dispatched against that ticket.

## Requirements

- FR1: Recognize labels of the form `model-<short_name>` where short_name is one of `haiku`, `opus`, `sonnet` (case-insensitive on the short name; prefix must be lowercase).
- FR2: When a ticket has exactly one valid model label, every agent dispatched against that ticket runs with the corresponding Claude model.
- FR3: When a ticket has no model label, fall back to the existing chain (per-agent frontmatter → global default).
- FR4: When a ticket has multiple model labels or an unknown short name, fall back to the global default and post a Jira comment explaining which label was ignored.
- FR5: Resolved model is persisted in `state.json` so it survives server restarts and mid-pipeline retries.
- FR6: Mid-flight label changes are intentionally ignored — the snapshot is set once at workspace creation.

## Technical Approach

- `orchestrator/model_resolver.py` — pure function `resolve_ticket_model(labels)` returns `ResolutionResult(model, warning)`.
- `WorkspaceState.model: str = ""` — new dataclass field, persisted via the existing `asdict` serializer.
- `Orchestrator._create_workspace_for_ticket` — calls the resolver, persists the model on success, posts the warning comment via `JiraAdapter.add_comment` on conflict / unknown.
- `AgentRuntime.execute` — prefers `workspace.state.model` over the agent frontmatter pin.

## Dependencies

- Jira Integration (`integrations/jira/jira_adapter.py`) — for `add_comment`.
- Workspace Isolation (`workspace/workspace.py`) — for `WorkspaceState` and `state.json` persistence.
- Orchestrator — for the dispatch and workspace-creation flow.

## Acceptance Criteria

- AC1: Ticket with `model-opus` → all agents on that ticket call the LLM with `claude-opus-4-7`.
- AC2: Ticket with `model-opus` + `model-haiku` → agents fall back to global default; one Jira comment posted listing both labels.
- AC3: Ticket with `model-llama` → agents fall back to global default; one Jira comment posted listing the supported labels.
- AC4: Ticket with no `model-*` label → existing behavior (per-agent → global default).
- AC5: After server restart, the resolved model is still applied to dispatches against the same workspace.
- AC6: `add_comment` failure does not abort workspace creation.

## Implementation Notes

- Resolver lives at `orchestrator/model_resolver.py` and is a pure function — no I/O, no side effects.
- `WorkspaceState.model` defaults to `""`, which preserves the prior fallback behavior. Old `state.json` files load with `""` automatically (the existing `_load_state` filter tolerates added fields).
- `agent_runtime.execute()` checks `workspace.state.model` first; if empty, falls through to the agent frontmatter pin.
- Workspace creation in `_create_workspace_for_ticket` calls the resolver, persists the model, and posts a Jira comment when labels are ambiguous or unknown.

## References

- Spec: [`docs/superpowers/specs/2026-04-30-per-ticket-model-label-design.md`](../superpowers/specs/2026-04-30-per-ticket-model-label-design.md)
- Plan: [`docs/superpowers/plans/2026-04-30-per-ticket-model-label.md`](../superpowers/plans/2026-04-30-per-ticket-model-label.md)

# Feature: Per-Ticket Model Selection via Jira Label

**Status:** Implemented
**Created:** 2026-04-30
**Updated:** 2026-04-30
**Author:** Oleksandr Brazhenko

## Description

Every ticket workspace stores the Claude model it should use on `WorkspaceState.model` — the single source of truth for that workspace. The model is resolved once at workspace creation: a Jira label (`model-haiku`, `model-opus`, or `model-sonnet`) overrides the global default; absent a label, the global default is snapshotted. Every agent dispatched against the ticket uses this snapshot.

## How to use

1. Add one of these labels to the Jira ticket **before** the pipeline picks it up:
   - `model-haiku`
   - `model-opus`
   - `model-sonnet`
2. The label is consumed at workspace creation. Once the workspace exists, the model is locked in — relabeling the ticket won't affect the running workspace.
3. To switch the model for an existing workspace, delete it and let the pipeline re-create it on the next poll.
4. The chosen model is shown as a colored pill on every ticket card in the dashboard, regardless of whether it came from a label or the global default.

The label prefix is lowercase; the short name is matched case-insensitively (`model-OPUS` works, `MODEL-opus` does not).

## Requirements

- FR1: Recognize labels of the form `model-<short_name>` where short_name is one of `haiku`, `opus`, `sonnet` (case-insensitive on the short name; prefix must be lowercase).
- FR2: At workspace creation, resolve the model and store it on `WorkspaceState.model`. The field is always non-empty after creation.
- FR3: Every agent dispatched against the workspace uses `state.model` directly. No per-agent override; no fallback chain.
- FR4: When a ticket has multiple model labels or an unknown short name, the global default is snapshotted and a Jira comment is posted explaining which label was ignored.
- FR5: The snapshot survives server restarts and mid-pipeline retries via `state.json`.
- FR6: Mid-flight label changes are intentionally ignored — the snapshot is set once at workspace creation.
- FR7: The dashboard renders the snapshotted model as a pill on each ticket card.

## Technical Approach

- `orchestrator/model_resolver.py` — pure function `resolve_ticket_model(labels)` returns `ResolutionResult(model, warning)`. `model` is `None` when the labels don't yield a single valid choice; the caller substitutes the global default.
- `WorkspaceState.model: str` — new dataclass field, persisted via the existing `asdict` serializer.
- `Orchestrator._create_workspace_for_ticket` — calls the resolver, sets `state.model = resolution.model or default_model_provider()`, and posts the warning comment via `JiraAdapter.add_comment` on conflict / unknown.
- `Orchestrator.__init__` takes a `default_model_provider: Callable[[], str]` — the same SQLite-backed reader the LLM adapters use, so the snapshotted default matches what the operator currently has set in the dashboard.
- `AgentRuntime.execute` — reads `workspace.state.model` directly. The agent frontmatter no longer has a `model:` field.
- `dashboard/web.py` — `_scan_all_workspaces` exposes `state.model` so the frontend can render the pill.

## Dependencies

- Jira Integration (`integrations/jira/jira_adapter.py`) — for `add_comment`.
- Workspace Isolation (`workspace/workspace.py`) — for `WorkspaceState` and `state.json` persistence.
- Orchestrator — for the dispatch and workspace-creation flow.
- Dashboard (`dashboard/`) — settings store provides the global default; web layer exposes the snapshot to the UI.

## Acceptance Criteria

- AC1: Ticket with `model-opus` → `state.model == "claude-opus-4-7"` and every agent on that ticket dispatches with that model.
- AC2: Ticket with `model-opus` + `model-haiku` → `state.model` is the global default; one Jira comment posted listing both labels.
- AC3: Ticket with `model-llama` → `state.model` is the global default; one Jira comment posted listing the supported labels.
- AC4: Ticket with no `model-*` label → `state.model` is the global default snapshot at creation time.
- AC5: After server restart or mid-pipeline retry, the same model is applied — `state.json` is the durable record.
- AC6: `add_comment` failure does not abort workspace creation.
- AC7: The dashboard ticket card shows a colored pill with the short name (`opus` / `sonnet` / `haiku`) of `state.model`.

## Behavior notes

- Changing the dashboard's global default does **not** affect in-flight tickets. Each workspace keeps the model that was current when it was created. Only new workspaces pick up the new default.
- Per-agent model pinning is no longer supported. The agent frontmatter `model:` field has been removed; the workspace-level snapshot is the single source of truth.
- LLM calls that aren't tied to a workspace (e.g. the Telegram intent parser) continue to read the global default at call time — they're outside this feature's scope.

## References

- Spec: [`docs/superpowers/specs/2026-04-30-per-ticket-model-label-design.md`](../superpowers/specs/2026-04-30-per-ticket-model-label-design.md)
- Plan: [`docs/superpowers/plans/2026-04-30-per-ticket-model-label.md`](../superpowers/plans/2026-04-30-per-ticket-model-label.md)

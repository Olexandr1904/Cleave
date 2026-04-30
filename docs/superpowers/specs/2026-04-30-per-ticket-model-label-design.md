# Per-Ticket Model Selection via Jira Label

**Date:** 2026-04-30
**Status:** Design — pending review

## Summary

Allow a Jira ticket to specify which Claude model the pipeline should use for that ticket via a single Jira label: `model-haiku`, `model-opus`, or `model-sonnet`. The label is human-set input. The pipeline reads it once at workspace creation, snapshots the resolved model into `workspace.state`, and uses it as the override for every agent dispatched against that ticket.

## Motivation

Today, model selection has two layers:
- **Per-agent override** — `agent.model` in agent frontmatter (currently empty everywhere)
- **Global default** — SQLite `settings` table, edited via Dashboard

Operators have no way to say "this ticket needs opus" or "this is a trivial fix, use haiku" without changing the global default for *all* tickets. A per-ticket label closes that gap and gives operators per-ticket control without code changes.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Direction is human → pipeline | Label *instructs* the pipeline. It's input, not audit output. |
| 2 | Per-ticket label overrides everything for that ticket | Predictable. Today all agent pins are empty, so the override boundary only matters when someone starts pinning. |
| 3 | No label → fall back to global default | Lightest touch. Label is opt-in, never gatekeeping. |
| 4 | Label format: `model-<short>` (dash, lowercase) | Matches existing convention (`ai-pipeline`). No special chars. |
| 5 | Short names: `haiku`, `opus`, `sonnet` (no version) | Stable across model upgrades. Mapping to full ID lives in code. |
| 6 | Multiple/unknown labels → fall back to default + post Jira comment | Graceful degradation; operator gets actionable feedback. |
| 7 | Resolution snapshotted at workspace creation, persisted to `state.json` | Survives restart and mid-pipeline retry. Mid-flight label changes are ignored (predictability). |

## Architecture

Resolution chain, in priority order:

1. **Per-ticket snapshot** (NEW) — `workspace.state.model`, set once at workspace creation
2. **Per-agent override** — `agent.model` in `agents/*.md` frontmatter (unchanged)
3. **Global default** — SQLite `settings` table (unchanged)
4. **Hard-coded fallback** — `DEFAULT_MODEL = "claude-sonnet-4-6"` (unchanged)

The new layer slots in *above* the existing chain. If `workspace.state.model` is non-empty, it wins.

### Data flow

```
Jira ticket has label "model-opus"
        ↓
Orchestrator polls → TicketData.labels = [..., "model-opus", ...]
        ↓
_create_workspace_for_ticket():
    result = resolve_ticket_model(ticket.labels)
    if result.model:
        workspace.state.model = result.model        # → state.json
    if result.warning:
        await tracker.add_comment(ticket.id, result.warning)
        ↓
(at any later dispatch, including after restart)
        ↓
agent_runtime.execute():
    model = workspace.state.model or agent_meta.model
    → adapter receives "claude-opus-4-7"
```

## Components

### New: `orchestrator/model_resolver.py`

Pure function module. ~40 lines.

```python
SHORT_NAME_TO_MODEL: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
}
LABEL_PREFIX = "model-"

@dataclass
class ResolutionResult:
    model: str | None        # full Claude model id, or None to fall back
    warning: str | None      # human-readable text for Jira comment if label was ignored

def resolve_ticket_model(labels: list[str]) -> ResolutionResult:
    """Inspect a ticket's labels and resolve to a Claude model id.

    Returns (None, None) when no model-* label is present.
    Returns (model_id, None) when exactly one valid label is present.
    Returns (None, warning_text) when labels are ambiguous or unknown.
    """
```

Comment: this mapping must be kept in sync with `dashboard/settings_store.ALLOWED_MODELS`. When a model version is bumped, both lists move together.

### Modified: `workspace/workspace.py` — `WorkspaceState`

Add field `model: str = ""` to the `WorkspaceState` dataclass at [workspace/workspace.py:57](../../workspace/workspace.py#L57). Empty string = "no per-ticket override; use the existing fallback chain". The existing JSON serializer picks it up automatically. Old `state.json` files load with `""` (backwards compatible — no migration needed).

### Modified: `orchestrator/orchestrator.py` — `_create_workspace_for_ticket`

After workspace + ticket data are set up:

```python
result = resolve_ticket_model(pt.ticket.labels)
if result.model:
    ws.state.model = result.model
    ws.save_state()
if result.warning and self._tracker:
    try:
        await self._tracker.add_comment(pt.ticket.id, result.warning)
    except Exception as e:
        logger.warning("Failed to post model-label warning to %s: %s", pt.ticket.id, e)
```

Failure to post the comment is non-fatal — log and continue. The pipeline still falls back to the default model.

### Modified: `orchestrator/agent_runtime.py` — `execute`

Replace lines 213-217:

```python
# Determine model — ticket snapshot wins over agent frontmatter
model = workspace.state.model or ""
if not model:
    agent_meta = agent.metadata.get("agent", {})
    if isinstance(agent_meta, dict):
        model = agent_meta.get("model", "")
```

No other change to `agent_runtime`. Adapters and dispatch paths are untouched.

## Edge cases

| Input labels | Resolved model | Comment posted |
|---|---|---|
| no `model-*` label | `None` (use fallback) | none |
| `model-opus` only | `"claude-opus-4-7"` | none |
| `model-opus` + unrelated labels | `"claude-opus-4-7"` | none |
| `model-opus` + `model-haiku` | `None` | "Multiple model labels found (`model-opus`, `model-haiku`). Falling back to global default. Please remove all but one." |
| `model-llama` | `None` | "Unknown model label `model-llama`. Supported: `model-haiku`, `model-opus`, `model-sonnet`. Falling back to global default." |
| `model-opus` + `model-llama` | `None` | Same as multiple-labels case (treat ambiguous as ambiguous; do not silently pick the valid one). |

**Case handling:** the resolver matches short names case-insensitively (`model-OPUS` works), but only after stripping `model-`. We assume Jira labels are already lowercase per project convention. No transformation of input — bad casing on the prefix (`MODEL-opus`) does not match.

**Comment idempotency:** `_create_workspace_for_ticket` is only called when a workspace doesn't yet exist on disk, so the warning is posted at most once per workspace lifetime. No dedup logic needed.

**Mid-flight label change:** intentionally ignored. The resolved model is part of the workspace's identity. To pick up a new label, the workspace must be deleted and recreated. (Out of scope: a "reseed" command.)

**Comment-classifier dispatch path** ([orchestrator.py:1579](../../orchestrator/orchestrator.py#L1579)): this dispatch reuses an existing workspace, so it picks up the snapshotted `state.model` automatically. No special handling needed.

## Testing

### Unit tests — `tests/unit/test_model_resolver.py` (new)

- empty labels → `(None, None)`
- `["model-opus"]` → `("claude-opus-4-7", None)`
- `["ai-pipeline", "model-haiku", "frontend"]` → `("claude-haiku-4-5-20251001", None)`
- `["model-llama"]` → `(None, warning)`, warning mentions all three valid short names
- `["model-opus", "model-haiku"]` → `(None, warning)`, warning lists both labels
- `["model-opus", "model-llama"]` → `(None, warning)` (ambiguous path)
- `["model-OPUS"]` → `("claude-opus-4-7", None)` (case-insensitive on short name)
- `["MODEL-opus"]` → `(None, None)` (prefix must be lowercase; treated as no match)

### Integration tests — orchestrator + agent_runtime

- `test_orchestrator.py`: ticket with `model-opus` → after `_create_workspace_for_ticket`, `state.model == "claude-opus-4-7"`, value persists in `state.json`.
- `test_orchestrator.py`: ticket with `model-opus` + `model-haiku` → `state.model == ""`, mock `tracker.add_comment` called once with the warning text.
- `test_orchestrator.py`: comment-post failure → workspace creation still succeeds; warning is logged.
- `test_agent_runtime.py`: workspace with `state.model = "claude-opus-4-7"` and agent frontmatter pinning a different model → adapter receives `"claude-opus-4-7"` (snapshot wins).
- `test_agent_runtime.py`: workspace with `state.model = ""` and agent frontmatter pinning a model → adapter receives the agent's pinned model (existing behavior preserved).

## Out of scope

- Dashboard UI to display or edit `state.model` per ticket. Could be added later by surfacing the field in the existing ticket detail view.
- Mid-flight re-evaluation of ticket labels.
- Per-agent or per-stage model overrides beyond what already exists.
- Migration of existing workspaces — `state.model` defaults to `""`, which preserves current behavior.

## File-change summary

| File | Change |
|---|---|
| `orchestrator/model_resolver.py` | NEW — resolver function + mapping |
| `workspace/workspace.py` | Add `model: str = ""` field to `WorkspaceState` dataclass (line 57) |
| `orchestrator/orchestrator.py` | Call resolver in `_create_workspace_for_ticket`, persist to state, post warning comment |
| `orchestrator/agent_runtime.py` | In `execute()`, prefer `workspace.state.model` over agent frontmatter |
| `tests/unit/test_model_resolver.py` | NEW — unit tests |
| `tests/unit/test_orchestrator.py` | Add cases for snapshot persistence + warning comment |
| `tests/unit/test_agent_runtime.py` | Add cases for snapshot-wins-over-frontmatter |

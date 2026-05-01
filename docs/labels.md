# Jira Labels Reference

Every Jira label Cleave reads, what it does, where it's configured, and how it behaves on conflicts.

This doc is a user-facing reference. For the implementation/spec, see [docs/features/jira-integration.md](features/jira-integration.md) and [docs/features/per-ticket-model-selection.md](features/per-ticket-model-selection.md).

---

## Trigger labels

Tickets are picked up only if **all** trigger labels are set. The list is per-project.

- **Configured at:** `project.yaml` → `jira.trigger_labels` (list of strings)
- **Default:** `["ai-pipeline"]`
- **Semantics:** AND. A ticket needs every label in the list. If you set `trigger_labels: [ai-pipeline, ready]`, only tickets carrying both labels enter the pipeline.
- **Why:** lets a project use one Jira instance for both AI-driven and human-only tickets without accidentally sweeping in everything.

```yaml
# config-live/projects/<project-id>/project.yaml
jira:
  trigger_labels:
    - ai-pipeline
```

## Ignore labels

If any of these labels is set on a ticket, it's skipped — even if it has all the trigger labels.

- **Configured at:** `project.yaml` → `jira.ignore_labels` (list of strings)
- **Default:** `[]` (empty)
- **Semantics:** OR. Any single match excludes the ticket.
- **Use cases:** `wip`, `human-only`, `do-not-automate`, …

```yaml
jira:
  ignore_labels:
    - human-only
    - do-not-automate
```

---

## Model selection labels

Override the Claude model for a single ticket.

| Label | Model |
|---|---|
| `model-haiku` | Claude Haiku |
| `model-sonnet` | Claude Sonnet |
| `model-opus` | Claude Opus |

- **Where:** add the label on the Jira ticket itself, **before** the pipeline picks it up.
- **Snapshot timing:** the model is resolved once at workspace creation and frozen on `WorkspaceState.model`. Re-labeling later does not affect a running workspace; delete the workspace and let the pipeline recreate it if you need to switch.
- **Case rules:** the prefix `model-` must be lowercase. The short name (`opus`/`sonnet`/`haiku`) is matched case-insensitively, so `model-OPUS` works.
- **Conflicts:** if a ticket has multiple model labels (e.g. `model-opus` + `model-haiku`), or an unknown short name (`model-llama`), the global default is used **and** Cleave posts a Jira comment explaining which labels were ignored.
- **Default model:** set in the dashboard's **Settings** view. Stored in the dashboard SQLite, hot-reloaded on the next agent dispatch.

See [docs/features/per-ticket-model-selection.md](features/per-ticket-model-selection.md) for the full spec.

---

## Repo-routing label (multi-repo projects)

When a project has more than one repo (`config-live/projects/<id>/repos/*.yaml`), every repo can declare a unique label that routes a ticket to it.

- **Configured at:** `repos/<repo-id>.yaml` → `jira_repo_label` (single string)
- **Default:** `""` (no routing label)
- **Semantics:** if the ticket carries this label, the workspace is created against that repo. If the project has multiple repos and the ticket has no matching `jira_repo_label`, the orchestrator falls back to project defaults.
- **Use case:** a project with `frontend.yaml` and `backend.yaml` repos can label tickets `repo-frontend` or `repo-backend` to route them.

```yaml
# config-live/projects/<id>/repos/frontend.yaml
repo:
  id: frontend
  name: ...
jira_repo_label: repo-frontend
```

---

## Quick reference table

| Label pattern | Where set | Where read | Effect |
|---|---|---|---|
| Custom (e.g. `ai-pipeline`) | Jira ticket | `project.yaml: jira.trigger_labels` | Ticket eligible for pipeline (AND across list) |
| Custom (e.g. `human-only`) | Jira ticket | `project.yaml: jira.ignore_labels` | Skip ticket (OR across list) |
| `model-haiku` / `model-sonnet` / `model-opus` | Jira ticket | Hard-coded in `orchestrator/model_resolver.py` | Override Claude model for this ticket |
| Custom (e.g. `repo-frontend`) | Jira ticket | `repos/<repo-id>.yaml: jira_repo_label` | Route ticket to a specific repo |

## Anti-patterns to avoid

- **Don't put `model-*` in `trigger_labels`.** Trigger labels gate pipeline entry; model labels select a model. Mixing them means tickets without a model preference don't enter the pipeline.
- **Don't expect mid-flight relabeling to switch the model.** It won't. Workspace creation is the snapshot point.
- **Don't reuse the same label as both `trigger_labels` and `jira_repo_label`.** It works mechanically, but the intent is muddled — keep them in different namespaces (e.g. `ai-pipeline` for trigger, `repo-frontend` for routing).

# Zero-Project Boot and Hot-Reload After Wizard

**Date:** 2026-04-16
**Status:** Design (rev2)
**Author:** Oleksandr Brazhenko

## Problem

The project-create wizard landed in `d284841`, but two issues block its primary use case — onboarding the first project from an empty config:

1. **Daemon exits when `projects == {}`.** [main.py:124-126](../../../main.py#L124) has an unconditional `return 0` in that case, so the dashboard never starts. The wizard is unreachable from a clean install.
2. **Wizard-created projects require a daemon restart.** `/api/projects/create` writes `config-live/projects/<id>/project.yaml` and `repos/<rid>.yaml` via Atlas, but the daemon's in-memory `projects` dict is captured once at startup. New projects don't get polled, don't get adapters, and don't appear on the dashboard until the user runs `./run.sh` again.

Both break the "zero-to-first-project, no terminal" onboarding the wizard was built for.

## Goals

- Daemon boots cleanly with zero configured projects; dashboard and wizard are reachable.
- When Atlas completes successfully in the wizard flow, the newly-created project becomes live in-process within seconds: orchestrator polls its Jira, dashboard shows it, health strip runs its validators.
- Hand-edited YAML under `config-live/projects/` is also picked up — no restart required.
- No disruption to any in-flight workspace when a new project is added.

## Non-goals

- Hot-reload on project *deletion* or *disable* (out of scope for this pass — orchestrator keeps polling removed projects until restart; low-risk, noisy logs at worst).
- Hot-reload of `global.yaml` changes.
- Filesystem watcher (`watchfiles`, inotify). Polling is sufficient.

## Architecture

Two independent changes:

### Change 1 — Boot with zero projects

Drop the early return in [main.py:124-126](../../../main.py#L124). Replace the "Nothing to do" message with an informational log that the dashboard is starting so the wizard is reachable. Everything downstream already tolerates `projects == {}`:

- `first_project = next(iter(projects.values()), None)` → `None`; Jira adapter init is gated on `if first_project and jira.url:`, so `tracker` stays `None`.
- GitHub adapter loop `for proj_id, proj in projects.items():` → empty, `vcs` stays `None`.
- Telegram adapter is built from `global_config.telegram` (independent of projects).
- Orchestrator accepts `tracker=None`; its [poll_cycle](../../../orchestrator/orchestrator.py#L266) already gates work on `if self._tracker and not is_manual:`.
- `projects_health` endpoint already guards `if not projects: return {"projects": []}`.

### Change 2 — Poll-driven config rescan, with wizard kick

**Discovery at poll time.** At the top of each `poll_cycle()`, orchestrator calls a new `_rescan_projects_from_disk()`:
1. Re-runs `load_config(self._config_dir)` (catch `ConfigError` → log at `WARNING`, skip rescan for this cycle).
2. Diffs the loaded keys against `self._projects`.
3. For each **new** project id, stores the `LoadedProject` in `self._projects` and fires a new-project hook (see below).
4. Existing projects are left untouched — this pass intentionally does not handle config edits for already-loaded projects (out of scope).

**Immediate kick from wizard.** After Atlas success in the wizard flow, the create route calls `orchestrator.rescan_projects()` (a public wrapper for `_rescan_projects_from_disk`). Latency from "Atlas done" to "project live" drops from up to `poll_interval_seconds` (default 300s) to effectively zero — no waiting for the next poll tick.

**New-project hook.** Building adapters is main.py's job (that's where the startup path builds them today). Orchestrator does not own adapter construction. At construction time, `main.py` passes an `on_project_added: Callable[[str, LoadedProject], None] | None = None` kwarg to `Orchestrator`. When rescan finds a new project, orchestrator invokes the hook for each.

The hook, defined in `main.py`, does:
1. For each repo in the new project, build its VCS adapter (GitHub or GitLab) and call `orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)` (existing method).
2. If `orchestrator._tracker is None` *and* the new project has a Jira URL, build a `JiraAdapter` and call a new `orchestrator.set_tracker(adapter)`. If `command_handler` exists (only when a Telegram notifier was configured at startup), backfill its tracker via a new setter.
3. If `command_handler` exists and the new project has a Telegram `default_chat_id`, call a new `command_handler.add_allowed_chat_id(chat_id)`. If the startup allowlist was `None` ("admit all"), this call is a no-op.
4. Emit `project_loaded` events for each `(project_id, repo_id)` so the dashboard board view picks it up.
5. Log at `INFO`: `project <id> added and live (no restart required)`.

### New orchestrator API

Three additions:

```python
def __init__(self, ..., config_dir: str, on_project_added: Callable[[str, LoadedProject], None] | None = None) -> None:
    # config_dir and the hook are stored on self

def set_tracker(self, tracker: TrackerInterface) -> None:
    """Attach a tracker after startup. Idempotent; last writer wins."""
    self._tracker = tracker

async def rescan_projects(self) -> list[str]:
    """Re-read config from disk and add any new projects.
    Returns the list of newly-added project ids.
    Public wrapper — called from the wizard create route for instant kick.
    """
    return await self._rescan_projects_from_disk()
```

Plus a private `_rescan_projects_from_disk()` called at the top of `poll_cycle()`.

### New command_handler API

```python
def set_tracker(self, tracker) -> None: ...
def add_allowed_chat_id(self, chat_id: str) -> None: ...  # no-op if _allowed_chat_ids is None
```

Both are trivial mutators on the existing `_tracker` and `_allowed_chat_ids` attributes.

### Wizard route change

`dashboard/project_create.py`'s route handler gains one new kwarg, `orchestrator` (the same Orchestrator already passed to `create_app`). Inside the existing `on_complete` callback (which fires in both success and failure paths), we conditionally schedule `await orchestrator.rescan_projects()` *only* on success.

But: the current `on_complete` is called from `atlas_runner.run_supervised`'s `finally` block — it doesn't know success vs failure. Simplest fix: check for the presence of `project_config_dir / "project.yaml"` on disk. If it exists (atlas wrote it) → success → kick rescan. If not → failure → no rescan needed. This keeps `atlas_runner` untouched.

Alternative: pass a success-only callback into `atlas_runner.schedule()`. Rejected — adds 30 lines of plumbing for the sake of one `if path.exists():` check.

## Data flow — wizard → live project

```
Browser                 Dashboard                 Atlas                 Orchestrator
   │                       │                        │                        │
   │ POST /api/projects/   │                        │                        │
   │ create  ──────────────▶                        │                        │
   │                       │ validate payload       │                        │
   │                       │ write .env secrets     │                        │
   │                       │ create setup workspace │                        │
   │                       │ dispatch agent  ───────▶                        │
   │ 202 Accepted          │                        │ validate creds         │
   │◀──────────────────────│                        │ write project.yaml     │
   │                       │                        │ write repos/*.yaml     │
   │                       │ on_complete ◀──────────│                        │
   │                       │   clear_busy           │                        │
   │                       │   if project.yaml      │                        │
   │                       │   exists:              │                        │
   │                       │     rescan_projects() ────────────────────────▶│
   │                       │                        │  load_config(dir)      │
   │                       │                        │  diff vs _projects     │
   │                       │                        │  add new project       │
   │                       │                        │  invoke hook(id, proj) │
   │                       │                        │     ↓ callback in main.py
   │                       │                        │     register VCS       │
   │                       │                        │     set_tracker if None│
   │                       │                        │     emit events        │
   │ poll /api/workspaces  │                        │                        │
   │ (existing)  ──────────▶                        │                        │
   │ new workspace visible │                        │                        │
```

Same flow works when a human edits YAML directly — on the next `poll_cycle` tick (≤300s), orchestrator picks it up.

## Error handling

- `load_config` raises `ConfigError` during rescan (e.g., YAML saved mid-edit): catch, log at `WARNING` with the project_id if derivable, skip this rescan. Next poll tick retries. No state mutated.
- Hook raises during adapter construction: catch per-project, log at `ERROR`, emit `project_added_failed` event, leave the project in `_projects` but unregistered. On next rescan tick, the project is already in `_projects` so it's NOT re-added (the hook doesn't re-fire). Operator must restart to retry. This is an acceptable degradation for an unexpected adapter build failure.
- `rescan_projects()` called from the wizard route when `config_dir / "projects" / project_id / "project.yaml"` is missing (atlas failed): not called at all — the existence check guards.

## Testing

### Unit

- `tests/unit/test_orchestrator_hot_reload.py` — **new file**
  - `test_set_tracker_attaches_after_init`
  - `test_set_tracker_replaces_existing`
  - `test_rescan_adds_new_project_and_calls_hook` (monkeypatch `load_config` to return a new project; assert hook was called)
  - `test_rescan_does_not_recall_hook_for_existing_project`
  - `test_rescan_handles_config_error_gracefully` (load_config raises; rescan returns empty list; no state change)
  - `test_rescan_called_at_top_of_poll_cycle` (mock `_rescan_projects_from_disk`, run one poll_cycle, assert it was awaited)

- `tests/unit/test_command_handler.py` — **extend**
  - `test_set_tracker_attaches_after_init`
  - `test_add_allowed_chat_id_admits_new_chat`
  - `test_add_allowed_chat_id_noop_when_allowlist_is_none`

- `tests/unit/test_main_startup.py` — **new file**
  - `test_main_runs_dashboard_with_zero_projects` — asserts `main()` proceeds past the zero-project check rather than early-exiting.

### Integration

- `tests/integration/test_project_create_flow.py` — **extend**
  - `test_wizard_creates_project_and_rescan_makes_it_live`:
    1. Build an Orchestrator + app with empty `projects` dict.
    2. POST `/api/projects/create` with a stub Atlas fn that writes valid YAML.
    3. After Atlas completes, assert `orchestrator._projects` contains the new id and the hook fired.

### Manual

1. `rm -rf config-live/projects/*` (ensure empty).
2. `./run.sh` — expect daemon stays up, dashboard binds `:8080`.
3. Open browser, walk the wizard to completion.
4. Without restarting, within a few seconds hit `/api/projects` — new project present.
5. Within `poll_interval_seconds` of editing a YAML by hand, observe new project appears.

## File changes

| File | Change | Est LOC |
|------|--------|---------|
| `main.py` | drop early return; add `on_project_added` hook closure; pass to Orchestrator; pre-declare `command_handler = None` | +40 −3 |
| `orchestrator/orchestrator.py` | accept `config_dir` + `on_project_added`; add `set_tracker`, `rescan_projects`, `_rescan_projects_from_disk`; call rescan at top of `poll_cycle` | +40 |
| `integrations/telegram/command_handler.py` | add `set_tracker` and `add_allowed_chat_id` | +8 |
| `dashboard/project_create.py` | accept `orchestrator` kwarg; after on_complete, if project.yaml exists schedule `rescan_projects()` | +15 |
| `dashboard/web.py` | thread `orchestrator` into `build_create_route` (already has orchestrator in scope) | +2 |
| `tests/unit/test_orchestrator_hot_reload.py` | new | +80 |
| `tests/unit/test_main_startup.py` | new | +30 |
| `tests/unit/test_command_handler.py` | extend | +20 |
| `tests/integration/test_project_create_flow.py` | extend | +35 |

Target: ≤110 LOC production + ≤165 LOC tests.

## Open questions

None at design time.

# Zero-Project Boot and Hot-Reload After Wizard

**Date:** 2026-04-16
**Status:** Design
**Author:** Oleksandr Brazhenko

## Problem

The project-create wizard landed in `d284841`, but two issues block its primary use case — onboarding the first project from an empty config:

1. **Daemon exits when `projects == {}`.** [main.py:124-126](../../../main.py#L124) has an unconditional `return 0` in that case, so the dashboard never starts. The wizard is unreachable from a clean install.
2. **Wizard-created projects require a daemon restart.** `/api/projects/create` writes `config-live/projects/<id>/project.yaml` and `repos/<rid>.yaml` via Atlas, but the daemon's in-memory `projects` dict is captured once at startup. New projects don't get polled, don't get adapters, and don't appear on the dashboard until the user runs `./run.sh` again.

Both break the "zero-to-first-project, no terminal" onboarding the wizard was built for.

## Goals

- Daemon boots cleanly with zero configured projects; dashboard and wizard are reachable.
- When Atlas completes successfully in the wizard flow, the newly-created project becomes live in-process: orchestrator polls its Jira, dashboard shows it in the project list, health strip runs its validators.
- No disruption to any in-flight workspace when a new project is added.

## Non-goals

- Hot-reload for hand-edited YAML files (still requires restart — accepted by user).
- Filesystem watcher on `config-live/` (deferred).
- Hot-reload on project *deletion* or *disable* (out of scope for this pass).
- Hot-reload of `global.yaml` changes.

## Architecture

Two independent changes with one shared callback plumbing:

### Change 1 — Boot with zero projects

Drop the early return in [main.py:124-126](../../../main.py#L124). Replace the "Nothing to do" message with an informational log that the dashboard is starting so the wizard is reachable. Everything downstream already tolerates `projects == {}`:

- `first_project = next(iter(projects.values()), None)` → `None`; Jira adapter init is gated on `if first_project and jira.url:`, so `tracker` stays `None`.
- GitHub adapter loop `for proj_id, proj in projects.items():` → empty, `vcs` stays `None`, `github_adapters` stays `{}`.
- Telegram adapter is built from `global_config.telegram` (independent of projects).
- Orchestrator accepts `tracker=None`; its [poll_cycle](../../../orchestrator/orchestrator.py#L266) already gates work on `if self._tracker and not is_manual:`.
- `projects_health` endpoint already guards `if not projects: return {"projects": []}`.

No code in `main.py` downstream of the early return fails on empty projects today — it just never runs.

### Change 2 — Hot-reload on Atlas success

A single callback, `on_project_added(project_id: str)`, threaded through:

```
main.py
  └─ builds on_project_added closure (captures projects dict, orchestrator,
     agent_runtime, command_handler, event_bus, global_config, args.config)
  └─ create_app(..., on_project_added=callback)
      └─ project_create.create_route_handler(..., on_project_added=callback)
          └─ called from the existing Atlas on_complete hook (same place
             that clears the _busy flag today)
```

The closure performs, in order:
1. Re-run `load_config(args.config, project_filter=project_id)` to get the single new `LoadedProject`.
2. Mutate the shared `projects` dict in place: `projects[project_id] = new_project`. Mutation (not rebind) is load-bearing — `create_app` and `Orchestrator.__init__` both captured this dict by reference.
3. For each repo in the new project, build its VCS adapter (GitHub or GitLab) and call `orchestrator.register_repo_vcs(repo_id, adapter, repo_cfg)` (existing method).
4. If `orchestrator._tracker is None` *and* the new project has a Jira URL, build a `JiraAdapter` from it and call a new `orchestrator.set_tracker(adapter)`. If `command_handler` exists (it only does when a Telegram notifier was configured at startup), backfill its tracker via a new setter so `/status` works against the new tracker.
5. If `command_handler` exists and the new project has a Telegram `default_chat_id`, call a new `command_handler.add_allowed_chat_id(chat_id)` so its operators can drive the bot. If the startup allowlist was `None` ("admit all"), this call is a no-op to preserve that semantic.
6. Emit `project_loaded` events for each `(project_id, repo_id)` so the dashboard board view picks them up.
7. Log at `INFO`: `project <id> added and live (no restart required)`.

Step order matters: register VCS before setting tracker, because the orchestrator's next poll cycle will want both.

### New orchestrator API

Two new methods on `Orchestrator`:

```python
def set_tracker(self, tracker: TrackerProtocol) -> None:
    """Attach a tracker after startup. Idempotent; last writer wins."""
    self._tracker = tracker

def add_project(self, project_id: str, project: LoadedProject) -> None:
    """Merge a newly-loaded project into the in-memory map.

    Called from main.py's on_project_added callback. Does NOT own VCS adapter
    registration — caller handles that via register_repo_vcs() for parity
    with startup wiring.
    """
    self._projects[project_id] = project
```

Rationale for two methods vs one: `register_repo_vcs` is already the startup path's own method; reusing it from the callback keeps adapter-construction logic in `main.py` (where it lives today) rather than pushing adapter knowledge into the orchestrator.

### New command_handler API

```python
def set_tracker(self, tracker: TrackerProtocol) -> None: ...
def add_allowed_chat_id(self, chat_id: str) -> None: ...  # no-op if _allowed_chat_ids is None
```

Both are trivial mutators on the existing `_tracker` and `_allowed_chat_ids` attributes.

## Data flow — wizard → live project

```
Browser                 Dashboard                 Atlas                 Daemon core
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
   │                       │   clear _busy          │                        │
   │                       │   on_project_added(id) ────────────────────────▶│
   │                       │                        │   load_config(filter=id)
   │                       │                        │   projects[id] = proj
   │                       │                        │   register VCS adapters
   │                       │                        │   set_tracker if None
   │                       │                        │   emit project_loaded
   │ poll /api/workspaces  │                        │                        │
   │ (existing)  ──────────▶                        │                        │
   │ new workspace visible │                        │                        │
```

## Error handling

- `load_config(project_filter=...)` raises `ConfigError` — catch, log at `ERROR`, emit `event: project_added_failed` with the exception string, do *not* mutate state. The on-disk YAML exists but the project is not loaded; the user can retry or restart. (Expected to be rare: Atlas validates before writing.)
- Adapter construction (`JiraAdapter`, `GitHubAdapter`, `GitLabAdapter`) is pure I/O-free — unlikely to raise. If it does, catch per-adapter, log, emit event, continue with the rest.
- `register_repo_vcs` is idempotent (`dict[repo_id] = ...`), safe to call even if the repo_id collided with an earlier one.
- If `on_project_added` raises unexpectedly, the wizard success response still goes back to the browser (it's called after the POST returns). The user sees their project on disk but it's not live; log + event make this visible. On next restart it'll be picked up normally.

## Testing

### Unit

- `tests/unit/test_orchestrator_hot_reload.py` — **new file**
  - `test_set_tracker_attaches_after_init`
  - `test_add_project_merges_into_projects_dict`
  - `test_register_repo_vcs_after_add_project_is_polled_next_cycle` (uses existing orchestrator test fixtures)

- `tests/unit/test_command_handler.py` — **extend**
  - `test_add_allowed_chat_id_admits_new_chat`
  - `test_set_tracker_replaces_existing`

- `tests/unit/test_main_startup.py` — **new file** (no `test_main.py` currently covers startup branching at this level; will be tiny)
  - `test_main_runs_dashboard_with_zero_projects` — assert `main()` returns 0 via normal shutdown (SIGTERM) rather than the old "nothing to do" early exit. Uses the existing `global.yaml` fixture + empty projects dir.

### Integration

- `tests/integration/test_project_create_flow.py` — **extend** existing file
  - `test_wizard_creates_project_and_daemon_picks_it_up_without_restart`:
    1. Start the daemon with zero projects (using pytest fixture).
    2. POST `/api/projects/create` with a stub Atlas function that writes valid YAML into `config-live/projects/<id>/`.
    3. Await the `on_complete` promise.
    4. Assert `GET /api/projects` now includes the new project.
    5. Assert `orchestrator._projects` has the new entry and `orchestrator._tracker is not None`.

### Manual

1. `rm -rf config-live/projects/*` (ensure empty)
2. `./run.sh` — expect daemon stays up, dashboard binds `:8080`, prints "No projects configured — dashboard started for wizard use".
3. Open browser, run wizard to completion against real Jira + GitHub.
4. Without restarting, hit `/api/projects` — new project present.
5. Within `poll_interval_seconds`, observe orchestrator poll log entry for the new Jira project.

## File changes

| File | Change | Est LOC |
|------|--------|---------|
| `main.py` | drop early return; build `on_project_added` closure; pass to `create_app` | +50 −3 |
| `dashboard/web.py` | add `on_project_added` kwarg; plumb into `create_project_create_handler` | +4 |
| `dashboard/project_create.py` | accept callback; call inside existing `on_complete` hook after `clear_busy` | +6 |
| `orchestrator/orchestrator.py` | add `set_tracker` and `add_project` methods | +12 |
| `integrations/telegram/command_handler.py` | add `set_tracker` and `add_allowed_chat_id` | +8 |
| `tests/unit/test_orchestrator_hot_reload.py` | new | +40 |
| `tests/unit/test_main_startup.py` | new | +25 |
| `tests/unit/test_command_handler.py` | extend | +15 |
| `tests/integration/test_project_create_flow.py` | extend | +30 |

Target: ≤200 LOC production + ≤110 LOC tests.

## Open questions

None at design time. Any surprises during implementation get surfaced before code changes.

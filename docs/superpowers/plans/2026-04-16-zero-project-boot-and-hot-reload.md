# Zero-Project Boot and Wizard Hot-Reload Implementation Plan (rev2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daemon boots with zero projects; wizard-created projects become live in-process without restart via poll-based rescan + immediate kick.

**Architecture:** (1) Drop the `return 0` at [main.py:124](../../../main.py#L124) so downstream init runs with empty `projects`. (2) At the top of each `poll_cycle`, orchestrator re-reads `config-live/projects/` via `load_config` and invokes an `on_project_added` hook for any new project ids. (3) After Atlas success in the wizard flow, the create route calls `orchestrator.rescan_projects()` so the kick is instant. Adapter construction stays in `main.py` (via the hook closure), matching the startup wiring.

**Tech Stack:** Python 3.12, Starlette, asyncio, pytest, pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-04-16-zero-project-boot-and-hot-reload-design.md](../specs/2026-04-16-zero-project-boot-and-hot-reload-design.md)

---

## Task 1: Orchestrator — set_tracker + rescan_projects + hook wiring

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Test: `tests/unit/test_orchestrator_hot_reload.py` (new)

This is the biggest task. Five additions to Orchestrator, all in one file:
1. `__init__` gains `config_dir: str` and `on_project_added: Callable[[str, LoadedProject], None] | None = None` kwargs.
2. `set_tracker(tracker)` method.
3. `_rescan_projects_from_disk()` async method — re-reads config, diffs, fires hook for new ids.
4. `rescan_projects()` public async wrapper (for the wizard route).
5. `poll_cycle()` calls `await self._rescan_projects_from_disk()` at its top.

### Step 1: Write the failing tests

Create `tests/unit/test_orchestrator_hot_reload.py`:

```python
"""Tests for Orchestrator hot-reload helpers: set_tracker, rescan_projects, poll_cycle wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_orch(
    projects=None,
    tracker=None,
    on_project_added=None,
    config_dir="/tmp/cfg",
):
    return Orchestrator(
        global_config=MagicMock(defaults=MagicMock(poll_interval_seconds=1)),
        projects=projects if projects is not None else {},
        registry=MagicMock(),
        workflow=MagicMock(),
        workspace_manager=MagicMock(discover_workspaces=lambda: []),
        agent_runtime=MagicMock(),
        tracker=tracker,
        vcs=None,
        notifier=None,
        dry_run=False,
        event_bus=None,
        config_dir=config_dir,
        on_project_added=on_project_added,
    )


def test_set_tracker_attaches_after_init():
    orch = _make_orch(tracker=None)
    assert orch._tracker is None
    new_tracker = MagicMock()
    orch.set_tracker(new_tracker)
    assert orch._tracker is new_tracker


def test_set_tracker_replaces_existing():
    old = MagicMock(name="old")
    orch = _make_orch(tracker=old)
    new = MagicMock(name="new")
    orch.set_tracker(new)
    assert orch._tracker is new


@pytest.mark.asyncio
async def test_rescan_adds_new_project_and_calls_hook(monkeypatch):
    hook = MagicMock()
    orch = _make_orch(projects={}, on_project_added=hook)

    new_proj = MagicMock(name="loaded_project")

    def fake_load_config(path, **kwargs):
        assert path == "/tmp/cfg"
        return (MagicMock(), {"demo": new_proj})

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == ["demo"]
    assert orch._projects["demo"] is new_proj
    hook.assert_called_once_with("demo", new_proj)


@pytest.mark.asyncio
async def test_rescan_does_not_recall_hook_for_existing_project(monkeypatch):
    hook = MagicMock()
    existing = MagicMock(name="existing_project")
    orch = _make_orch(projects={"demo": existing}, on_project_added=hook)

    def fake_load_config(path, **kwargs):
        return (MagicMock(), {"demo": existing})

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == []
    hook.assert_not_called()


@pytest.mark.asyncio
async def test_rescan_handles_config_error_gracefully(monkeypatch, caplog):
    from config.config_loader import ConfigError
    hook = MagicMock()
    orch = _make_orch(projects={}, on_project_added=hook)

    def fake_load_config(path, **kwargs):
        raise ConfigError("bad yaml")

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == []
    assert orch._projects == {}
    hook.assert_not_called()


@pytest.mark.asyncio
async def test_rescan_with_no_hook_still_merges_projects(monkeypatch):
    """on_project_added=None is valid — orchestrator still tracks the new project."""
    orch = _make_orch(projects={}, on_project_added=None)

    new_proj = MagicMock(name="loaded_project")

    def fake_load_config(path, **kwargs):
        return (MagicMock(), {"demo": new_proj})

    monkeypatch.setattr("orchestrator.orchestrator.load_config", fake_load_config)

    added = await orch.rescan_projects()

    assert added == ["demo"]
    assert orch._projects["demo"] is new_proj


@pytest.mark.asyncio
async def test_poll_cycle_calls_rescan(monkeypatch):
    orch = _make_orch()
    orch._rescan_projects_from_disk = AsyncMock(return_value=[])

    await orch.poll_cycle()

    orch._rescan_projects_from_disk.assert_awaited_once()
```

### Step 2: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_hot_reload.py -v`
Expected: all FAIL — `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'config_dir'` (or similar), plus missing `set_tracker` / `rescan_projects`.

### Step 3: Extend Orchestrator

Modify `orchestrator/orchestrator.py`:

**Edit A — imports** (add near top, alongside existing imports):
```python
from collections.abc import Callable
from config.config_loader import ConfigError, load_config
```

**Edit B — __init__ signature and body.** Add two new kwargs at the end of the existing `__init__` parameter list (after `event_bus`):

```python
    def __init__(
        self,
        global_config: GlobalConfig,
        projects: dict[str, LoadedProject],
        registry: ResourceRegistry,
        workflow: WorkflowDefinition,
        workspace_manager: WorkspaceManager,
        agent_runtime: AgentRuntime,
        tracker: TrackerInterface | None = None,
        vcs: VCSInterface | None = None,
        notifier: NotifierInterface | None = None,
        dry_run: bool = False,
        event_bus: Any | None = None,
        config_dir: str | None = None,
        on_project_added: Callable[[str, LoadedProject], None] | None = None,
    ) -> None:
```

Then in the body (after existing assignments), add:
```python
        self._config_dir = config_dir
        self._on_project_added = on_project_added
```

**Edit C — new methods.** Insert after the existing `register_repo_vcs` method (around line 98):

```python
    def set_tracker(self, tracker: TrackerInterface) -> None:
        """Attach a tracker after startup (used by wizard hot-reload)."""
        self._tracker = tracker

    async def rescan_projects(self) -> list[str]:
        """Re-read config from disk; add new projects; invoke hook for each.

        Returns the list of newly-added project ids. Public entry point called
        from the wizard route for instant kick.
        """
        return await self._rescan_projects_from_disk()

    async def _rescan_projects_from_disk(self) -> list[str]:
        """Internal: re-read config and merge new projects into _projects.

        Does NOT touch already-loaded projects (hot-reload of edits is out of
        scope). Swallows ConfigError (e.g., mid-edit YAML) and logs at WARNING.
        """
        if not self._config_dir:
            return []
        try:
            _, loaded = load_config(self._config_dir)
        except ConfigError as exc:
            logger.warning("Rescan: load_config failed: %s", exc)
            return []

        added: list[str] = []
        for pid, proj in loaded.items():
            if pid in self._projects:
                continue
            self._projects[pid] = proj
            added.append(pid)
            if self._on_project_added is not None:
                try:
                    self._on_project_added(pid, proj)
                except Exception:
                    logger.exception("on_project_added hook failed for %s", pid)
        if added:
            logger.info("Rescan added projects: %s", added)
        return added
```

**Edit D — poll_cycle call.** At the top of `poll_cycle()` (currently line 266), insert before the existing `self._emit("poll_cycle", "Poll cycle started")`:

```python
    async def poll_cycle(self) -> None:
        """Single poll + advance cycle."""
        # Pick up any projects added to config-live/ since last cycle (wizard or hand-edit).
        await self._rescan_projects_from_disk()
        self._emit("poll_cycle", "Poll cycle started")
```

### Step 4: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_hot_reload.py -v`
Expected: 6 PASS.

### Step 5: Check existing orchestrator tests still pass

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_deferred.py tests/unit/test_orchestrator_manual_control.py tests/unit/test_orchestrator_modes.py tests/unit/test_orchestrator_stage_verify.py -v`
Expected: all PASS. The new `config_dir` and `on_project_added` kwargs default to `None`, so no existing callsite breaks.

### Step 6: Commit

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_hot_reload.py
git commit -m "feat(orchestrator): rescan projects on poll cycle with on_project_added hook"
```

---

## Task 2: CommandHandler — set_tracker + add_allowed_chat_id

**Files:**
- Modify: `integrations/telegram/command_handler.py`
- Test: `tests/unit/test_command_handler.py` (extend)

### Step 1: Inspect existing test file for fixture patterns

Run: `grep -n "^def\|^class\|fixture\|make_command_handler\|CommandHandler(" tests/unit/test_command_handler.py | head -40`

Reuse existing fixtures if present. If no factory fixture exists, define one at the top of the new tests (see Step 2).

### Step 2: Write the failing tests

Append to `tests/unit/test_command_handler.py`. Use the existing project import style (copy from the top of that file):

```python
def test_set_tracker_attaches_after_init():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        jira_base_url="",
        started_at="2026-04-16T00:00:00Z",
        tracker=None,
        analyze_callback=MagicMock(),
        recent_completions_fn=lambda: [],
        allowed_chat_ids=None,
        event_bus=None,
    )
    assert handler._tracker is None

    new_tracker = MagicMock()
    handler.set_tracker(new_tracker)
    assert handler._tracker is new_tracker


def test_add_allowed_chat_id_admits_new_chat():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        jira_base_url="",
        started_at="2026-04-16T00:00:00Z",
        tracker=MagicMock(),
        analyze_callback=MagicMock(),
        recent_completions_fn=lambda: [],
        allowed_chat_ids={"1001"},
        event_bus=None,
    )
    handler.add_allowed_chat_id("2002")
    assert handler._allowed_chat_ids == {"1001", "2002"}


def test_add_allowed_chat_id_noop_when_allowlist_is_none():
    from unittest.mock import MagicMock
    from integrations.telegram.command_handler import CommandHandler

    handler = CommandHandler(
        intent_parser=MagicMock(),
        notifier=MagicMock(),
        mode_handler=MagicMock(),
        active_workspaces_fn=lambda: [],
        jira_base_url="",
        started_at="2026-04-16T00:00:00Z",
        tracker=MagicMock(),
        analyze_callback=MagicMock(),
        recent_completions_fn=lambda: [],
        allowed_chat_ids=None,
        event_bus=None,
    )
    handler.add_allowed_chat_id("2002")
    assert handler._allowed_chat_ids is None
```

**Before writing these tests:** verify the kwargs above match `CommandHandler.__init__`. Run `grep -A 20 "def __init__" integrations/telegram/command_handler.py`. If the signature differs, adapt the test calls — do not invent kwargs.

### Step 3: Run tests to verify they fail

Run: `.venv/bin/python -m pytest tests/unit/test_command_handler.py -v -k "set_tracker or add_allowed_chat_id"`
Expected: 3 FAILs with `AttributeError`.

### Step 4: Add the two methods

In `integrations/telegram/command_handler.py`, add next to the existing `__init__`:

```python
    def set_tracker(self, tracker) -> None:
        """Attach a tracker after init (used by wizard hot-reload)."""
        self._tracker = tracker

    def add_allowed_chat_id(self, chat_id: str) -> None:
        """Extend the chat allowlist with a new id.

        No-op if the startup allowlist was None ('admit all' semantic preserved).
        """
        if self._allowed_chat_ids is None:
            return
        self._allowed_chat_ids.add(chat_id)
```

### Step 5: Run tests to verify they pass

Run: `.venv/bin/python -m pytest tests/unit/test_command_handler.py -v -k "set_tracker or add_allowed_chat_id"`
Expected: 3 PASS.

### Step 6: Commit

```bash
git add integrations/telegram/command_handler.py tests/unit/test_command_handler.py
git commit -m "feat(telegram): add set_tracker and add_allowed_chat_id for hot-reload"
```

---

## Task 3: Drop zero-project early return

**Files:**
- Modify: `main.py:124-126`
- Test: `tests/unit/test_main_startup.py` (new)

### Step 1: Write the failing test

Create `tests/unit/test_main_startup.py`:

```python
"""Tests for main.py startup branching with zero projects."""

from __future__ import annotations

from unittest.mock import patch

import main


def test_main_runs_dashboard_with_zero_projects(tmp_path):
    """With zero projects, main() must NOT early-exit before the dashboard.

    Before the fix, main.py returned 0 immediately when projects was empty.
    After: asyncio.run(_run_all()) must be reached.
    """
    cfg = tmp_path / "config-live"
    (cfg / "projects").mkdir(parents=True)
    global_yaml = _MINIMAL_GLOBAL_YAML.format(
        ws=tmp_path / "ws",
        log=tmp_path / "log",
        db=tmp_path / "events.db",
    )
    (cfg / "global.yaml").write_text(global_yaml, encoding="utf-8")

    reached = {"run_all": False}

    def fake_asyncio_run(coro):
        reached["run_all"] = True
        coro.close()

    with patch("asyncio.run", side_effect=fake_asyncio_run):
        rc = main.main(["--config", str(cfg)])

    assert reached["run_all"], (
        "main() must reach asyncio.run(_run_all()) even with zero projects"
    )
    assert rc == 0


_MINIMAL_GLOBAL_YAML = """\
telegram:
  bot_token: ''
  default_chat_id: ''
claude:
  api_key: ''
  model: claude-sonnet-4-5
workspaces:
  base_dir: {ws}
  max_age_days: 14
  min_free_disk_gb: 0
  max_workspace_size_gb: 2
defaults:
  poll_interval_seconds: 300
  max_iterations:
    scope_guard: 1
    fix: 1
    qa: 1
    dev: 1
  max_parallel_tickets: 1
  pr_comment_fetch_delay_minutes: 30
logging:
  level: WARNING
  dir: {log}
heartbeat:
  enabled: false
  interval_hours: 24
  send_at: '09:00'
operator:
  role: ''
  stack: []
  preferences:
    code_style: ''
    commit_format: ''
  rules: []
dashboard:
  enabled: false
  host: '127.0.0.1'
  port: 0
  db_path: {db}
pipeline:
  mode: auto
intent_parser:
  max_history: 5
  confidence_threshold: 0.7
"""
```

**Before committing the test:** run it once and adjust `_MINIMAL_GLOBAL_YAML` if `load_config` complains about a missing required field. Check `config-live.example/global.yaml` for the canonical schema. Do not invent fields.

### Step 2: Run test to verify it fails

Run: `.venv/bin/python -m pytest tests/unit/test_main_startup.py -v`
Expected: FAIL — `reached["run_all"]` is False because `main()` returns early.

### Step 3: Remove the early return

Modify [main.py:124-126](../../../main.py#L124-L126):

```python
    if not projects:
        print("  No active projects configured — dashboard starting so you can create one via the wizard.")
```

(Remove the `return 0` line. Keep the print so the operator sees why nothing is polling.)

### Step 4: Run test to verify it passes

Run: `.venv/bin/python -m pytest tests/unit/test_main_startup.py -v`
Expected: PASS.

### Step 5: Commit

```bash
git add main.py tests/unit/test_main_startup.py
git commit -m "feat(main): start dashboard even with zero projects so wizard is reachable"
```

---

## Task 4: on_project_added closure + threading into Orchestrator

**Files:**
- Modify: `main.py`

### Step 1: Pre-declare `command_handler = None` before its conditional block

At [main.py:273](../../../main.py#L273), before `if notifier is not None:`, add:

```python
    # Initialize command handler for Telegram free-text control
    command_handler = None
    if notifier is not None:
```

(Keep the existing conditional; just lift the name to the outer scope so the closure can reference it unconditionally.)

### Step 2: Build a nested helper `_build_repo_adapters` inside `_run_all`

This helper registers VCS adapters for a single loaded project with the orchestrator. Place it inside `_run_all()`, just before the `on_project_added` closure (Step 3). It exists only for hot-reload — startup wiring is unchanged.

Inside `_run_all()`, after `orchestrator` has been constructed and before `create_app(...)` is called:

```python
        def _build_repo_adapters(project, logger_):
            """Build + register VCS adapters for each repo in a single project."""
            for repo_id, repo_cfg in project.repos.items():
                provider = repo_cfg.vcs.provider
                if provider == "github" and repo_cfg.vcs.github.token:
                    from integrations.github.github_adapter import GitHubAdapter
                    gh = GitHubAdapter(
                        token=repo_cfg.vcs.github.token,
                        owner=repo_cfg.vcs.github.owner,
                        repo=repo_cfg.vcs.github.repo,
                    )
                    orchestrator.register_repo_vcs(repo_id, gh, repo_cfg)
                    logger_.info(
                        "Hot-reload: registered GitHub adapter for %s: %s/%s",
                        repo_id, repo_cfg.vcs.github.owner, repo_cfg.vcs.github.repo,
                    )
```

(Only GitHub today, matching the startup path at [main.py:218-232](../../../main.py#L218-L232). When GitLab lands in startup, mirror it here.)

### Step 3: Build the on_project_added closure

Immediately after `_build_repo_adapters`, still inside `_run_all()`:

```python
        def on_project_added(project_id, new_project):
            log = logging.getLogger(__name__)
            _build_repo_adapters(new_project, log)

            jira_cfg = new_project.config.jira
            if orchestrator._tracker is None and jira_cfg.url:
                from integrations.jira.jira_adapter import JiraAdapter
                new_tracker = JiraAdapter(
                    url=jira_cfg.url,
                    email=jira_cfg.email,
                    token=jira_cfg.token,
                    project_key=jira_cfg.project_key,
                    trigger_labels=jira_cfg.trigger_labels,
                    ignore_labels=jira_cfg.ignore_labels,
                    statuses={
                        "todo": jira_cfg.statuses.todo,
                        "in_progress": jira_cfg.statuses.in_progress,
                        "in_review": jira_cfg.statuses.in_review,
                        "done": jira_cfg.statuses.done,
                    },
                )
                orchestrator.set_tracker(new_tracker)
                log.info("Jira tracker attached from project %s", project_id)

            if command_handler is not None:
                pcid = new_project.config.telegram.default_chat_id
                if pcid:
                    command_handler.add_allowed_chat_id(pcid)
                if orchestrator._tracker is not None:
                    command_handler.set_tracker(orchestrator._tracker)

            for rid, repo in new_project.repos.items():
                event_bus.emit(
                    "project_loaded",
                    f"Project {project_id}/{rid}: {repo.repo.name}",
                    project_id=project_id,
                    data={"repo_id": rid, "repo_name": repo.repo.name},
                )

            log.info("Project %s added and live (no restart required)", project_id)
```

### Step 4: Pass the closure + config_dir to Orchestrator construction

**Problem:** Orchestrator is built at [main.py:243-255](../../../main.py#L243-L255), BEFORE `_run_all()` is entered. The closure lives inside `_run_all()`. Two options:

**Option A — move closure earlier.** Define `_build_repo_adapters` and `on_project_added` at the outer `main()` scope, after `orchestrator` is built at line 255. Pass to Orchestrator via setters (but there's no `set_on_project_added` method; we'd need to add one).

**Option B — pass via constructor, move closure definition.** Define the closure before `Orchestrator(...)`. Requires `event_bus`, `logging`, and `command_handler` (which is still `None` at this point — that's fine, the closure captures the *name*; by the time it's invoked, the outer variable has been assigned).

Go with **Option B**. Move the closure + helper definitions to BEFORE the `Orchestrator(...)` construction. They reference `orchestrator` — but orchestrator is constructed on the very next lines, so the closure captures the name and resolves it at call time (not definition time). This works in Python.

Concretely, the order inside `main()`:
1. Load config, init event bus, etc. (existing)
2. `command_handler = None` (from Task 4 Step 1)
3. Build `github_adapters` from `projects` (existing startup loop at 218-232) — unchanged.
4. Define `_build_repo_adapters(project, logger_)` at function scope.
5. Define `on_project_added(project_id, new_project)` — references `orchestrator` and `command_handler`, both resolved at call time.
6. Construct `Orchestrator(..., config_dir=args.config, on_project_added=on_project_added)`.
7. Register per-repo VCS adapters (existing loop at 258-259) — unchanged.
8. Build `command_handler` (existing conditional) — unchanged.
9. Enter `_run_all()`.

**Wait — there's a simpler placement.** The closure only needs: `orchestrator` (name), `command_handler` (name), `event_bus`, `logging`. `event_bus` and `logging` are already available above. So yes, define after orchestrator and before `_run_all()`. But `orchestrator.__init__` needs `on_project_added` at construction time — so we need the closure BEFORE orchestrator.

**Resolution:** orchestrator captures the closure reference. Inside the closure, `orchestrator` is a name lookup that resolves when the closure runs (which is always AFTER orchestrator is assigned). Forward references in Python work this way. Same for `command_handler`.

Place both `_build_repo_adapters` and `on_project_added` immediately before the `orchestrator = Orchestrator(...)` line at [main.py:243](../../../main.py#L243). Pass both new kwargs to `Orchestrator`:

```python
    orchestrator = Orchestrator(
        global_config=global_config,
        projects=projects,
        registry=registry,
        workflow=workflow,
        workspace_manager=workspace_manager,
        agent_runtime=agent_runtime,
        tracker=tracker,
        vcs=vcs,
        notifier=notifier,
        dry_run=args.dry_run,
        event_bus=event_bus,
        config_dir=args.config,
        on_project_added=on_project_added,
    )
```

### Step 5: Import what the closure needs

Add at the top of main.py (or in main() if imports are already local-scoped there):
```python
import logging
```

Check if it's already imported — [main.py:130](../../../main.py#L130) imports it inside `main()` — that's AFTER the closure would use it. Hoist `import logging` to module top-level (next to `import argparse`, `import sys`).

### Step 6: Verify

Run: `.venv/bin/python -c "import main"`
Expected: no import errors.

Run: `.venv/bin/python -m pytest tests/unit/ -q`
Expected: all PASS. No existing test constructs Orchestrator with positional args past `event_bus`, so the two new kwargs (with defaults) don't break anything. If a test does break, fix it to use the new `config_dir=None, on_project_added=None` defaults explicitly.

### Step 7: Commit

```bash
git add main.py
git commit -m "feat(main): wire on_project_added closure into Orchestrator for hot-reload"
```

---

## Task 5: Wizard route kicks rescan on success

**Files:**
- Modify: `dashboard/project_create.py`
- Modify: `dashboard/web.py`

### Step 1: Thread `orchestrator` into build_create_route

In [dashboard/web.py:255-261](../../../dashboard/web.py#L255-L261), update the existing `build_create_route` call:

```python
    create_route_handler = build_create_route(
        workspace_base_dir=Path(workspace_base_dir),
        config_dir=Path(config_dir) if config_dir else Path("config-live"),
        env_path=Path(".env"),
        atlas_fn=atlas_fn or _default_atlas_fn,
        orchestrator=orchestrator,
    )
```

`orchestrator` is already in scope in `create_app` — no new plumbing needed above this layer.

### Step 2: Accept orchestrator kwarg in build_create_route and kick rescan

In `dashboard/project_create.py`, modify `build_create_route`:

```python
def build_create_route(
    *,
    workspace_base_dir: Path,
    config_dir: Path,
    env_path: Path,
    atlas_fn: AtlasFn,
    orchestrator=None,
):
```

Wrap the existing `clear_busy` callback so it also schedules the rescan when atlas succeeded. Atlas success is detected by checking that `project_config_dir / "project.yaml"` exists after completion. Replace the existing `clear_busy` definition at [dashboard/project_create.py:102-106](../../../dashboard/project_create.py#L102-L106):

```python
        import asyncio as _asyncio

        def clear_busy() -> None:
            global _busy, _active_workspace
            _busy = False
            _active_workspace = None

            # Atlas success <=> project.yaml exists on disk.
            if orchestrator is not None and (project_config_dir / "project.yaml").exists():
                try:
                    _asyncio.ensure_future(orchestrator.rescan_projects())
                except Exception:
                    logger.exception(
                        "Failed to schedule orchestrator rescan for %s", project_id,
                    )
```

The `_asyncio.ensure_future` dispatches the coroutine onto the running event loop (the same one uvicorn + orchestrator are using). The coroutine completes in the background; we don't await it from `clear_busy` because the atlas_runner's supervisor is inside its `finally` block and should not block.

### Step 3: No unit test here

The existing integration test in Task 6 covers this wiring end-to-end. A unit test at this layer would be mostly mocking.

### Step 4: Verify no regression

Run: `.venv/bin/python -m pytest tests/unit/test_project_create_payload.py tests/unit/test_dashboard_web.py tests/integration/test_project_create_flow.py -v`
Expected: all PASS. The new kwarg is optional; default `None` preserves prior behavior.

### Step 5: Commit

```bash
git add dashboard/project_create.py dashboard/web.py
git commit -m "feat(dashboard): kick orchestrator.rescan_projects after Atlas success"
```

---

## Task 6: Integration test — wizard hot-reload end-to-end

**Files:**
- Extend: `tests/integration/test_project_create_flow.py`

### Step 1: Skim the existing test file for fixtures

Run: `grep -n "^def\|^async def\|^class\|fixture\|create_app\|build_create_route" tests/integration/test_project_create_flow.py | head -30`

Reuse the existing app/client fixture if present.

### Step 2: Add the end-to-end test

Append to `tests/integration/test_project_create_flow.py`:

```python
@pytest.mark.asyncio
async def test_wizard_creates_project_and_rescan_makes_it_live(tmp_path):
    """End-to-end: wizard POST → Atlas stub writes YAML → rescan adds project."""
    from unittest.mock import MagicMock, AsyncMock
    from dashboard.events import EventBus
    from dashboard.event_store import EventStore
    from dashboard.web import create_app

    cfg_dir = tmp_path / "config-live"
    (cfg_dir / "projects").mkdir(parents=True)
    (cfg_dir / "global.yaml").write_text(
        _MINIMAL_GLOBAL_YAML_INT.format(
            ws=tmp_path / "ws", log=tmp_path / "log", db=tmp_path / "events.db",
        ),
        encoding="utf-8",
    )
    ws_base = tmp_path / "ws"
    ws_base.mkdir()

    projects_dict: dict = {}

    # Build a real-ish orchestrator with mock_rescan instrumented.
    rescan_calls: list = []

    async def fake_rescan():
        rescan_calls.append(True)
        # Mimic what the real rescan would do — merge any newly-written yaml.
        from config.config_loader import load_config
        try:
            _, loaded = load_config(str(cfg_dir))
        except Exception:
            return []
        added = []
        for pid, proj in loaded.items():
            if pid not in projects_dict:
                projects_dict[pid] = proj
                added.append(pid)
        return added

    orchestrator = MagicMock()
    orchestrator.rescan_projects = fake_rescan

    # Stub atlas_fn: write valid project.yaml + repos/<rid>.yaml.
    async def stub_atlas(setup_ws, cfg):
        pid = setup_ws.project_id
        rid = setup_ws.repo_id
        pdir = cfg / "projects" / pid
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "project.yaml").write_text(
            _STUB_PROJECT_YAML.format(pid=pid), encoding="utf-8",
        )
        (pdir / "repos").mkdir(exist_ok=True)
        (pdir / "repos" / f"{rid}.yaml").write_text(
            _STUB_REPO_YAML.format(rid=rid), encoding="utf-8",
        )

    bus = EventBus()
    store = EventStore(str(tmp_path / "events.db"))
    await store.initialize()

    app = create_app(
        bus, store,
        workspace_base_dir=str(ws_base),
        orchestrator=orchestrator,
        projects=projects_dict,
        config_dir=str(cfg_dir),
        atlas_fn=stub_atlas,
    )

    from starlette.testclient import TestClient
    with TestClient(app) as client:
        resp = client.post("/api/projects/create", json=_make_valid_payload("demo", "main"))
    assert resp.status_code == 202, resp.text

    # Give scheduled tasks time to run (atlas_fn → clear_busy → rescan).
    for _ in range(40):
        if rescan_calls and "demo" in projects_dict:
            break
        await asyncio.sleep(0.05)

    assert rescan_calls, "orchestrator.rescan_projects was not called"
    assert "demo" in projects_dict, "demo project not merged into projects dict"


_MINIMAL_GLOBAL_YAML_INT = """\
telegram:
  bot_token: ''
  default_chat_id: ''
claude:
  api_key: ''
  model: claude-sonnet-4-5
workspaces:
  base_dir: {ws}
  max_age_days: 14
  min_free_disk_gb: 0
  max_workspace_size_gb: 2
defaults:
  poll_interval_seconds: 300
  max_iterations: {{scope_guard: 1, fix: 1, qa: 1, dev: 1}}
  max_parallel_tickets: 1
  pr_comment_fetch_delay_minutes: 30
logging:
  level: WARNING
  dir: {log}
heartbeat:
  enabled: false
  interval_hours: 24
  send_at: '09:00'
operator:
  role: ''
  stack: []
  preferences:
    code_style: ''
    commit_format: ''
  rules: []
dashboard:
  enabled: false
  host: '127.0.0.1'
  port: 0
  db_path: {db}
pipeline:
  mode: auto
intent_parser:
  max_history: 5
  confidence_threshold: 0.7
"""

_STUB_PROJECT_YAML = """\
project:
  id: "{pid}"
  name: "Demo"
  enabled: true
jira:
  url: "https://example.atlassian.net"
  token: "stub"
  email: "t@t"
  project_key: "DEMO"
  trigger_labels: ["ai-pipeline"]
  ignore_labels: []
  statuses:
    todo: "To Do"
    in_progress: "In Progress"
    in_review: "In Review"
    done: "Done"
telegram:
  bot_token: ""
  default_chat_id: ""
parallelism:
  max_concurrent_tickets: 1
defaults:
  poll_interval_seconds: 300
  max_iterations:
    scope_guard: 1
    fix: 1
    qa: 1
    dev: 1
  pr_comment_fetch_delay_minutes: 30
"""

_STUB_REPO_YAML = """\
repo:
  id: "{rid}"
  name: "Demo Repo"
vcs:
  provider: "github"
  github:
    token: "stub"
    owner: "demo-org"
    repo: "demo-repo"
quality:
  linters: []
  test_commands: []
"""


def _make_valid_payload(project_id: str, repo_id: str) -> dict:
    return {
        "identity": {
            "project_id": project_id,
            "display_name": "Demo",
            "repo_id": repo_id,
            "repo_display_name": "Demo Repo",
        },
        "jira": {
            "url": "https://example.atlassian.net",
            "project_key": "DEMO",
            "email": "t@t",
            "token": "stub",
            "trigger_labels": ["ai-pipeline"],
        },
        "vcs": {
            "provider": "github",
            "github": {"token": "stub", "owner": "demo-org", "repo": "demo-repo"},
        },
    }
```

**Before committing:**
- Run `grep -n "def validate_payload\|required_fields\|missing_fields" dashboard/project_create_payload.py` and adjust `_make_valid_payload` if the schema has evolved.
- Run `grep -n "class SetupWorkspace" dashboard/setup_workspace.py` to confirm the attributes used by the stub (`project_id`, `repo_id`) exist.

### Step 3: Run the test

Run: `.venv/bin/python -m pytest tests/integration/test_project_create_flow.py::test_wizard_creates_project_and_rescan_makes_it_live -v`
Expected: PASS.

Debug tips if it fails:
- `print(resp.json())` after the POST to see validation errors.
- Increase the sleep loop iterations if the background task is slow.
- Check `tmp_path / "config-live" / "projects" / "demo" / "project.yaml"` exists after the sleep loop — if not, stub_atlas didn't run or the env isolation broke.

### Step 4: Commit

```bash
git add tests/integration/test_project_create_flow.py
git commit -m "test(integration): wizard triggers orchestrator.rescan_projects on success"
```

---

## Task 7: Full suite green + manual smoke test

**Files:** none (verification only).

### Step 1: Full unit suite

Run: `.venv/bin/python -m pytest tests/unit/ -q`
Expected: 632+ PASS (626 before + ~6 new in this feature), zero FAIL.

### Step 2: Integration suite

Run: `.venv/bin/python -m pytest tests/integration/ -q`
Expected: all PASS.

### Step 3: Health e2e

Run: `.venv/bin/python -m pytest tests/e2e/test_dashboard_health.py -q`
Expected: 2 PASS.

(Never mix unit + e2e in one invocation — fixtures conflict per Feature 2's brief.)

### Step 4: Manual smoke test — zero-project boot

```bash
# Stop the running daemon if any
pkill -f "python main.py" 2>/dev/null; sleep 1
# Ensure no projects
ls config-live/projects/     # expect empty
# Start
./run.sh
```

Expected output:
```
Sickle v0.1.0 starting with config: config-live
  Config loaded: logging=INFO
  Projects discovered: 0
  Resources discovered: …
  No active projects configured — dashboard starting so you can create one via the wizard.
  Dashboard: http://127.0.0.1:8080
  Orchestrator initialized. Starting main loop...
```

Verify with:
```bash
curl -s http://localhost:8080/api/health
curl -s http://localhost:8080/api/projects
```

### Step 5: Manual smoke test — wizard end-to-end

With the daemon from Step 4 running:
1. Open http://localhost:8080 in a browser.
2. Click **+ New Project**.
3. Fill all 6 wizard steps with real Jira + GitHub credentials.
4. Submit.
5. Wait for the wizard to reach SETUP_DONE.
6. Within a few seconds, check the daemon stdout for:
   ```
   Project <id> added and live (no restart required)
   ```
7. Run `curl -s http://localhost:8080/api/projects` — the new project should appear.
8. Within `poll_interval_seconds`, expect orchestrator poll logs for the new Jira project.

### Step 6: Done

Update [docs/superpowers/plans/2026-04-07-feature-tracker.md](../plans/2026-04-07-feature-tracker.md) with a one-line entry (match existing style), then:

```bash
git add docs/superpowers/plans/2026-04-07-feature-tracker.md
git commit -m "docs: log zero-project boot + wizard hot-reload in feature tracker"
```

---

## Self-review

**Spec coverage:**
- ✅ Goal 1 (zero-project boot) — Task 3
- ✅ Goal 2 (wizard → live without restart) — Tasks 1 + 4 + 5
- ✅ Goal 3 (hand-edited YAML picked up) — Task 1 (rescan at top of poll_cycle)
- ✅ Goal 4 (no disruption to in-flight workspaces) — rescan is additive, doesn't touch `self._active_workspaces`
- ✅ Orchestrator API (set_tracker, rescan_projects, _rescan_projects_from_disk, on_project_added, config_dir) — Task 1
- ✅ CommandHandler API (set_tracker, add_allowed_chat_id) — Task 2
- ✅ Error handling (ConfigError → log + skip; hook exception → log + mark added) — Task 1
- ✅ Wizard kick (instant rescan after Atlas success) — Task 5
- ✅ Integration end-to-end — Task 6

**Type/name consistency:**
- `on_project_added(project_id: str, project: LoadedProject) -> None` — consistent Task 1 + 4.
- `set_tracker(tracker)` — consistent Orchestrator + CommandHandler, Tasks 1 + 2.
- `rescan_projects()` / `_rescan_projects_from_disk()` — public/private pair in Task 1.
- `add_allowed_chat_id(chat_id: str)` — Task 2 + Task 4.

**Placeholder scan:** none.

**Scope check:** 7 tasks (was 9 in rev1), cleaner wiring, ~110 LOC production + ~165 LOC tests.

# Zero-Project Boot and Wizard Hot-Reload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daemon boots with zero projects (dashboard up so the wizard is reachable); wizard-created projects become live in-process without restart.

**Architecture:** Two independent changes sharing a callback. (1) Drop the `return 0` at [main.py:124](../../../main.py#L124) so downstream init runs with an empty `projects` dict. (2) Build an `on_project_added(project_id)` closure in `main.py` that re-loads that one project, mutates the shared `projects` dict, registers VCS adapters, and (on the first project) attaches a Jira tracker. Thread the closure through `create_app` and into `atlas_runner.schedule` as a new `on_success` callback that fires only on the Atlas-success path.

**Tech Stack:** Python 3.12, Starlette, asyncio, pytest, pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-04-16-zero-project-boot-and-hot-reload-design.md](../specs/2026-04-16-zero-project-boot-and-hot-reload-design.md)

---

## Task 1: Orchestrator.set_tracker and add_project

**Files:**
- Modify: `orchestrator/orchestrator.py` (add 2 methods after `register_repo_vcs`)
- Test: `tests/unit/test_orchestrator_hot_reload.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_orchestrator_hot_reload.py`:

```python
"""Tests for hot-reload helpers added to Orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _make_orchestrator(projects=None, tracker=None):
    """Build an Orchestrator with all collaborators mocked."""
    return Orchestrator(
        global_config=MagicMock(),
        projects=projects if projects is not None else {},
        registry=MagicMock(),
        workflow=MagicMock(),
        workspace_manager=MagicMock(),
        agent_runtime=MagicMock(),
        tracker=tracker,
        vcs=None,
        notifier=None,
        dry_run=False,
        event_bus=None,
    )


def test_set_tracker_attaches_after_init():
    orch = _make_orchestrator(tracker=None)
    assert orch._tracker is None

    new_tracker = MagicMock()
    orch.set_tracker(new_tracker)

    assert orch._tracker is new_tracker


def test_set_tracker_replaces_existing():
    old = MagicMock(name="old")
    orch = _make_orchestrator(tracker=old)

    new = MagicMock(name="new")
    orch.set_tracker(new)

    assert orch._tracker is new


def test_add_project_merges_into_projects_dict():
    projects: dict = {}
    orch = _make_orchestrator(projects=projects)

    new_project = MagicMock(name="loaded_project")
    orch.add_project("acme", new_project)

    assert orch._projects["acme"] is new_project
    # Same dict object — callers who captured the reference see the update.
    assert projects["acme"] is new_project


def test_add_project_is_idempotent():
    projects = {"acme": MagicMock(name="old")}
    orch = _make_orchestrator(projects=projects)

    new = MagicMock(name="new")
    orch.add_project("acme", new)

    assert orch._projects["acme"] is new
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_hot_reload.py -v`
Expected: 4 FAILs with `AttributeError: 'Orchestrator' object has no attribute 'set_tracker'` / `add_project`.

- [ ] **Step 3: Add the two methods**

Modify [orchestrator/orchestrator.py](../../../orchestrator/orchestrator.py) — insert after the existing `register_repo_vcs` method (currently ends at line 98):

```python
    def set_tracker(self, tracker: TrackerInterface) -> None:
        """Attach a tracker after startup. Idempotent; last writer wins.

        Used by the wizard hot-reload path: when the first project is added
        in a zero-project boot, main.py builds a Jira adapter and calls this.
        """
        self._tracker = tracker

    def add_project(self, project_id: str, project: LoadedProject) -> None:
        """Merge a newly-loaded project into the in-memory projects map.

        Mutates the same dict passed to __init__, so other components that
        captured the reference (e.g. dashboard create_app) see the update.
        """
        self._projects[project_id] = project
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_hot_reload.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/unit/test_orchestrator_hot_reload.py
git commit -m "feat(orchestrator): add set_tracker and add_project for hot-reload"
```

---

## Task 2: CommandHandler.set_tracker and add_allowed_chat_id

**Files:**
- Modify: `integrations/telegram/command_handler.py`
- Test: `tests/unit/test_command_handler.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_command_handler.py` (check existing fixture names with `grep -n "^def \|^class \|fixture" tests/unit/test_command_handler.py | head -40` first — reuse them):

```python
def test_set_tracker_attaches_after_init(make_command_handler):
    """Attaching a tracker after init makes /status and /analyze functional."""
    handler = make_command_handler(tracker=None)
    assert handler._tracker is None

    tracker = MagicMock()
    handler.set_tracker(tracker)

    assert handler._tracker is tracker


def test_set_tracker_replaces_existing(make_command_handler):
    old = MagicMock(name="old")
    handler = make_command_handler(tracker=old)

    new = MagicMock(name="new")
    handler.set_tracker(new)

    assert handler._tracker is new


def test_add_allowed_chat_id_admits_new_chat(make_command_handler):
    """A chat id added after init is accepted by the allowlist check."""
    handler = make_command_handler(allowed_chat_ids={"1001"})

    handler.add_allowed_chat_id("2002")

    assert handler._allowed_chat_ids == {"1001", "2002"}


def test_add_allowed_chat_id_noop_when_allowlist_is_none(make_command_handler):
    """When startup allowlist is None ('admit all'), adding is a no-op."""
    handler = make_command_handler(allowed_chat_ids=None)

    handler.add_allowed_chat_id("2002")

    assert handler._allowed_chat_ids is None
```

If `make_command_handler` fixture does not exist, add one at the top of the test file:

```python
@pytest.fixture
def make_command_handler():
    """Factory fixture — returns a CommandHandler with sane mocked defaults."""
    from integrations.telegram.command_handler import CommandHandler
    from unittest.mock import MagicMock

    def _make(tracker=MagicMock(), allowed_chat_ids=None):
        return CommandHandler(
            intent_parser=MagicMock(),
            notifier=MagicMock(),
            mode_handler=MagicMock(),
            active_workspaces_fn=lambda: [],
            jira_base_url="",
            started_at="2026-04-16T00:00:00Z",
            tracker=tracker,
            analyze_callback=MagicMock(),
            recent_completions_fn=lambda: [],
            allowed_chat_ids=allowed_chat_ids,
            event_bus=None,
        )

    return _make
```

(Verify required kwargs with `grep -n "def __init__" integrations/telegram/command_handler.py` and adjust if a kwarg is missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_command_handler.py -v -k "set_tracker or add_allowed_chat_id"`
Expected: 4 FAILs with `AttributeError`.

- [ ] **Step 3: Add the two methods**

Modify [integrations/telegram/command_handler.py](../../../integrations/telegram/command_handler.py) — add these methods to the `CommandHandler` class, next to the existing `__init__`:

```python
    def set_tracker(self, tracker) -> None:
        """Attach a tracker after init; used by wizard hot-reload."""
        self._tracker = tracker

    def add_allowed_chat_id(self, chat_id: str) -> None:
        """Extend the chat allowlist with a new id.

        No-op if the startup allowlist was None ('admit all' semantic preserved).
        """
        if self._allowed_chat_ids is None:
            return
        self._allowed_chat_ids.add(chat_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_command_handler.py -v -k "set_tracker or add_allowed_chat_id"`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/telegram/command_handler.py tests/unit/test_command_handler.py
git commit -m "feat(telegram): add set_tracker and add_allowed_chat_id for hot-reload"
```

---

## Task 3: atlas_runner on_success hook

**Files:**
- Modify: `dashboard/atlas_runner.py`
- Test: `tests/unit/test_atlas_runner.py` (create if missing; otherwise extend)

**Why a new hook and not reuse on_complete:** `on_complete` runs in a `finally` block, so it fires on BOTH success and failure. The hot-reload must only fire when Atlas actually wrote the YAML — otherwise we'd try to load a project that doesn't exist on disk.

- [ ] **Step 1: Check whether `tests/unit/test_atlas_runner.py` exists**

Run: `ls tests/unit/test_atlas_runner.py 2>&1`
- If it exists, append tests to it.
- If not, create it fresh.

- [ ] **Step 2: Write the failing tests**

Add / create at `tests/unit/test_atlas_runner.py`:

```python
"""Tests for atlas_runner success/failure hook semantics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dashboard.atlas_runner import run_supervised
from dashboard.setup_workspace import SetupWorkspace


@pytest.fixture
def setup_ws(tmp_path):
    ws_root = tmp_path / "p" / "r" / "setup"
    (ws_root / "reports").mkdir(parents=True)
    (ws_root / "meta").mkdir(parents=True)
    # Minimal state.json so write_state works.
    (ws_root / "state.json").write_text(
        '{"ticket_id":"p","company_id":"p","repo_id":"r","current_state":"SETUP_PENDING"}',
        encoding="utf-8",
    )
    return SetupWorkspace(project_id="p", repo_id="r", setup_dir=ws_root)


@pytest.mark.asyncio
async def test_on_success_fires_after_atlas_success(setup_ws, tmp_path):
    async def ok(_ws, _cfg):
        return None

    on_success = MagicMock()
    on_failure = MagicMock()
    on_complete = MagicMock()

    await run_supervised(
        setup_ws, tmp_path, ok, on_failure, on_complete, on_success=on_success,
    )

    on_success.assert_called_once()
    on_failure.assert_not_called()
    on_complete.assert_called_once()


@pytest.mark.asyncio
async def test_on_success_not_called_on_atlas_failure(setup_ws, tmp_path):
    async def boom(_ws, _cfg):
        raise RuntimeError("atlas blew up")

    on_success = MagicMock()
    on_failure = MagicMock()
    on_complete = MagicMock()

    await run_supervised(
        setup_ws, tmp_path, boom, on_failure, on_complete, on_success=on_success,
    )

    on_success.assert_not_called()
    on_failure.assert_called_once()
    on_complete.assert_called_once()


@pytest.mark.asyncio
async def test_on_success_exception_is_logged_but_does_not_break_complete(
    setup_ws, tmp_path, caplog,
):
    async def ok(_ws, _cfg):
        return None

    def bad_success():
        raise RuntimeError("reload failed")

    on_complete = MagicMock()

    await run_supervised(
        setup_ws, tmp_path, ok,
        on_failure=MagicMock(),
        on_complete=on_complete,
        on_success=bad_success,
    )

    # Failure inside on_success must not prevent on_complete from firing.
    on_complete.assert_called_once()
    assert "on_success callback failed" in caplog.text


@pytest.mark.asyncio
async def test_on_success_default_is_noop(setup_ws, tmp_path):
    """Omitting on_success keeps previous behavior — nothing extra fires."""
    async def ok(_ws, _cfg):
        return None

    # Should not raise; no keyword required.
    await run_supervised(
        setup_ws, tmp_path, ok,
        on_failure=MagicMock(),
        on_complete=MagicMock(),
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_atlas_runner.py -v`
Expected: 3 of 4 FAIL because `run_supervised` doesn't accept `on_success`. (`test_on_success_default_is_noop` may pass already.)

- [ ] **Step 4: Add the on_success hook**

Modify [dashboard/atlas_runner.py](../../../dashboard/atlas_runner.py) — update `run_supervised` signature and body, and `schedule`:

```python
async def run_supervised(
    workspace: SetupWorkspace,
    config_dir: Path,
    atlas_fn: AtlasFn,
    on_failure: Callable[[], None],
    on_complete: Callable[[], None],
    on_success: Callable[[], None] | None = None,
) -> None:
    """Run Atlas in VALIDATING → WRITING → SETUP_DONE.

    Hook order on success: atlas_fn → SETUP_DONE → on_success → on_complete.
    Hook order on failure: atlas_fn raises → SETUP_FAILED → on_failure → on_complete.
    on_success NEVER fires on failure — it signals 'config on disk is ready'.
    """
    try:
        try:
            write_state(workspace, "VALIDATING")
            await atlas_fn(workspace, config_dir)
            write_state(workspace, "SETUP_DONE")
            if on_success is not None:
                try:
                    on_success()
                except Exception:
                    logger.exception(
                        "on_success callback failed for %s/%s",
                        workspace.project_id, workspace.repo_id,
                    )
        except Exception as exc:
            logger.exception("Atlas run failed for %s/%s",
                             workspace.project_id, workspace.repo_id)
            report = workspace.setup_dir / "reports" / "project-setup-output.md"
            existing = report.read_text(encoding="utf-8") if report.exists() else ""
            report.write_text(
                existing
                + "\n\n## Failure\n\n"
                + f"```\n{traceback.format_exc()}\n```\n",
                encoding="utf-8",
            )
            write_state(workspace, "SETUP_FAILED", error=str(exc))
            try:
                on_failure()
            except Exception:
                logger.exception("Rollback failed for %s/%s",
                                 workspace.project_id, workspace.repo_id)
    finally:
        try:
            on_complete()
        except Exception:
            logger.exception("on_complete callback failed for %s/%s",
                             workspace.project_id, workspace.repo_id)


def schedule(
    workspace: SetupWorkspace,
    config_dir: Path,
    atlas_fn: AtlasFn,
    on_failure: Callable[[], None],
    on_complete: Callable[[], None],
    on_success: Callable[[], None] | None = None,
) -> asyncio.Task:
    return asyncio.create_task(
        run_supervised(
            workspace, config_dir, atlas_fn, on_failure, on_complete, on_success,
        )
    )
```

**Important:** A raising `on_success` is caught and logged. This means a broken reload does NOT leave `_busy = True` — `on_complete` still fires in the `finally`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_atlas_runner.py -v`
Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/atlas_runner.py tests/unit/test_atlas_runner.py
git commit -m "feat(atlas_runner): add on_success hook that fires only on setup success"
```

---

## Task 4: Thread on_project_added through project_create

**Files:**
- Modify: `dashboard/project_create.py`
- Test: `tests/unit/test_project_create_payload.py` or new `tests/unit/test_project_create_route.py`

- [ ] **Step 1: Check whether a route-level test file exists**

Run: `ls tests/unit/test_project_create_route.py tests/integration/test_project_create_flow.py 2>&1`

Prefer extending `tests/integration/test_project_create_flow.py` for the callback wiring — it already builds an app and runs Atlas stubs end-to-end. A unit test here would reduce to asserting `schedule()` was called with the right kwarg, which is a lot of mocking for little value. **Skip the unit test at this layer.** The integration test in Task 9 covers this wiring.

- [ ] **Step 2: Add the kwarg to build_create_route**

Modify [dashboard/project_create.py](../../../dashboard/project_create.py) — two edits:

**Edit A:** add the kwarg to `build_create_route`:

```python
def build_create_route(
    *,
    workspace_base_dir: Path,
    config_dir: Path,
    env_path: Path,
    atlas_fn: AtlasFn,
    on_project_added: Callable[[str], None] | None = None,
):
```

Add the import at the top of the file:

```python
from collections.abc import Callable
```

**Edit B:** replace the `schedule(...)` call inside `create_project` (currently at line 107) with one that passes `on_success`. The closure below captures `project_id` so main.py's callback receives it:

```python
        on_success = None
        if on_project_added is not None:
            def on_success() -> None:
                on_project_added(project_id)

        schedule(
            workspace, config_dir, atlas_fn,
            on_failure=rollback,
            on_complete=clear_busy,
            on_success=on_success,
        )
```

Note: the existing `schedule(workspace, config_dir, atlas_fn, rollback, clear_busy)` call at [dashboard/project_create.py:107](../../../dashboard/project_create.py#L107) uses positional args. Switch to keyword args as shown for readability and to make the new `on_success` unambiguous.

- [ ] **Step 3: Verify no tests broke**

Run: `.venv/bin/python -m pytest tests/unit/test_project_create_payload.py tests/integration/test_project_create_flow.py -v`
Expected: all PASS (existing behavior unchanged when `on_project_added=None`).

- [ ] **Step 4: Commit**

```bash
git add dashboard/project_create.py
git commit -m "feat(dashboard): thread on_project_added through project_create route"
```

---

## Task 5: Add on_project_added kwarg to create_app

**Files:**
- Modify: `dashboard/web.py`

- [ ] **Step 1: Add the kwarg and pass it through**

Modify [dashboard/web.py](../../../dashboard/web.py) — two edits:

**Edit A:** Add `on_project_added` to `create_app`'s signature (around line 120). Current signature:

```python
def create_app(
    event_bus: EventBus,
    store: EventStore,
    *,
    workspace_base_dir: str,
    orchestrator=None,
    mode_handler=None,
    global_config=None,
    projects: dict[str, Any] | None = None,
    config_dir: str | None = None,
    atlas_fn: Any | None = None,
):
```

Add `on_project_added: Callable[[str], None] | None = None,` as the last kwarg. Add import at the top: `from collections.abc import Callable`.

**Edit B:** Pass it to `build_create_route` (around line 255):

```python
    create_route_handler = build_create_route(
        workspace_base_dir=Path(workspace_base_dir),
        config_dir=Path(config_dir) if config_dir else Path("config-live"),
        env_path=Path(".env"),
        atlas_fn=atlas_fn or _default_atlas_fn,
        on_project_added=on_project_added,
    )
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `.venv/bin/python -m pytest tests/unit/test_dashboard_web.py tests/integration/test_project_create_flow.py -v`
Expected: all PASS (kwarg is optional; default None is equivalent to prior behavior).

- [ ] **Step 3: Commit**

```bash
git add dashboard/web.py
git commit -m "feat(dashboard): add on_project_added kwarg to create_app"
```

---

## Task 6: Build the on_project_added closure in main.py

**Files:**
- Modify: `main.py`

**Context:** The closure is the heart of the feature. It captures everything it needs from the enclosing scope.

- [ ] **Step 1: Extract repo-adapter construction into a helper**

Currently [main.py:218-232](../../../main.py#L218-L232) builds a `GitHubAdapter` for each repo inline. Hot-reload needs the same logic for *just* the new project's repos. Extract it into a module-level helper.

Add this helper function near the top of `main.py` (after the imports, before `get_version`):

```python
def _build_repo_adapters(project, orchestrator, logger=None):
    """Build VCS adapters for every repo in a single loaded project.

    Registers each with the orchestrator via register_repo_vcs.
    Returns the first adapter built (None if the project has no GitHub repos),
    so callers can use it as a fallback default VCS if they don't have one yet.
    """
    first_adapter = None
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
            if first_adapter is None:
                first_adapter = gh
            if logger is not None:
                logger.info(
                    "VCS: registered GitHub adapter for %s: %s/%s",
                    repo_id, repo_cfg.vcs.github.owner, repo_cfg.vcs.github.repo,
                )
        # GitLab branch: mirror the github branch once GitLabAdapter lands;
        # current codebase only registers GitHub adapters in the startup path
        # (main.py:218-232), so we match that scope here.
    return first_adapter
```

**Do not yet** refactor the existing inline block in main.py's startup sequence — that's Step 4 below. This keeps the git diff reviewable (helper added first, call-sites updated next).

- [ ] **Step 2: Build the on_project_added closure just before `create_app` is called**

Insert this block inside `_run_all()`, before the `app = create_app(...)` call (currently around [main.py:366](../../../main.py#L366)):

```python
            def on_project_added(project_id: str) -> None:
                """Load a newly-created project and wire it into the running daemon.

                Called by atlas_runner.on_success after the wizard writes
                project.yaml + repos/*.yaml. Does not require a restart.
                """
                try:
                    _, new_projects = load_config(
                        args.config, project_filter=project_id,
                    )
                except ConfigError as exc:
                    logging.getLogger(__name__).error(
                        "Hot-reload failed: load_config(%s) raised ConfigError: %s",
                        project_id, exc,
                    )
                    event_bus.emit(
                        "project_added_failed",
                        f"Project {project_id} YAML written but could not be loaded",
                        project_id=project_id,
                        data={"error": str(exc)},
                    )
                    return

                if project_id not in new_projects:
                    logging.getLogger(__name__).error(
                        "Hot-reload: load_config returned no entry for %s",
                        project_id,
                    )
                    return

                new_project = new_projects[project_id]
                orchestrator.add_project(project_id, new_project)

                _build_repo_adapters(
                    new_project, orchestrator,
                    logger=logging.getLogger(__name__),
                )

                # First-project case: build a Jira tracker if we don't have one.
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
                    logging.getLogger(__name__).info(
                        "Jira tracker attached from newly-added project %s",
                        project_id,
                    )

                # Telegram: extend allowlist and backfill tracker if configured.
                if 'command_handler' in dir() or locals().get('command_handler'):
                    ch = locals().get('command_handler')
                    if ch is not None:
                        pcid = new_project.config.telegram.default_chat_id
                        if pcid:
                            ch.add_allowed_chat_id(pcid)
                        if orchestrator._tracker is not None:
                            ch.set_tracker(orchestrator._tracker)

                # Emit dashboard events so the board view picks up the project.
                for rid, repo in new_project.repos.items():
                    event_bus.emit(
                        "project_loaded",
                        f"Project {project_id}/{rid}: {repo.repo.name}",
                        project_id=project_id,
                        data={"repo_id": rid, "repo_name": repo.repo.name},
                    )

                logging.getLogger(__name__).info(
                    "Project %s added and live (no restart required)",
                    project_id,
                )
```

**Known footgun** — the `locals().get('command_handler')` pattern above is ugly because `command_handler` is defined conditionally earlier (`if notifier is not None:` / `if isinstance(notifier, TelegramAdapter):`). Replace with a simpler approach: initialize `command_handler = None` before the conditional block (line 273 area), then the closure can reference it directly without `locals()` gymnastics.

Concretely, change [main.py:273](../../../main.py#L273):

```python
    # Initialize command handler for Telegram free-text control
    command_handler = None
    if notifier is not None:
```

Then inside the conditional block, `command_handler = CommandHandler(...)` assigns to the outer name. This lets the closure use `command_handler` as a normal captured variable. Revise the closure's Telegram block to:

```python
                if command_handler is not None:
                    pcid = new_project.config.telegram.default_chat_id
                    if pcid:
                        command_handler.add_allowed_chat_id(pcid)
                    if orchestrator._tracker is not None:
                        command_handler.set_tracker(orchestrator._tracker)
```

- [ ] **Step 3: Pass the closure to create_app**

Update the `create_app(...)` call (around [main.py:366](../../../main.py#L366)) to include:

```python
            app = create_app(
                event_bus, event_store,
                workspace_base_dir=global_config.workspaces.base_dir,
                orchestrator=orchestrator,
                mode_handler=mode_handler,
                global_config=global_config,
                projects=projects,
                config_dir=args.config,
                atlas_fn=_production_atlas_fn,
                on_project_added=on_project_added,
            )
```

- [ ] **Step 4: Swap the inline repo-adapter loop for _build_repo_adapters**

Replace the inline block at [main.py:218-232](../../../main.py#L218-L232) with:

```python
    # Per-repo VCS adapters (extracted to helper so hot-reload can reuse it).
    github_adapters_first: Any = None  # kept for parity with previous code path
    for proj_id, proj in projects.items():
        first = _build_repo_adapters(proj, orchestrator=None, logger=None)
        # Startup path: orchestrator isn't constructed yet — we register later.
```

**Actually that doesn't work** — the existing code registers VCS adapters via `orchestrator.register_repo_vcs` AFTER orchestrator is constructed ([main.py:258-259](../../../main.py#L258-L259)). The helper registers eagerly, which requires orchestrator to exist first.

**Correct approach:** don't refactor the startup path. Just call `_build_repo_adapters` from the hot-reload closure only. Leave the startup block alone. Undo this step — keep the existing block at lines 218-259 as-is.

Remove the `_build_repo_adapters` helper at module scope and instead define it as a nested helper inside `_run_all()`, defined after `orchestrator` is built and before `on_project_added`. Update Step 1 accordingly: the helper lives inside `_run_all()`, not at module scope. This keeps the startup path untouched and makes the closure's dependencies obvious.

Concretely, move the helper from module scope to inside `_run_all()`, just before the `on_project_added` closure. Remove the "Do not yet" / "extract it into a module-level helper" phrasing — it's a nested function, defined once near its one call site.

- [ ] **Step 5: Manual smoke test**

Run: `.venv/bin/python -c "import main"`
Expected: no import errors.

Run: `.venv/bin/python -m pytest tests/unit/ tests/integration/test_project_create_flow.py -q`
Expected: all PASS (closure is unreferenced unless the wizard is used).

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat(main): add on_project_added closure for wizard hot-reload"
```

---

## Task 7: Drop the zero-project early return

**Files:**
- Modify: `main.py:124-126`
- Test: extend `tests/integration/test_project_create_flow.py` or add `tests/unit/test_main_startup.py`

- [ ] **Step 1: Write the failing test**

Add `tests/unit/test_main_startup.py`:

```python
"""Tests for main.py's startup branching behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import main


def test_main_runs_dashboard_with_zero_projects(tmp_path, monkeypatch):
    """With zero projects, main() must NOT early-exit before the dashboard.

    Before this change, main.py at line 124-126 returned 0 as soon as it saw
    an empty projects dict — making the wizard unreachable from a clean install.
    After this change, _run_all() must be reached so uvicorn binds.

    We stub asyncio.run to intercept the call and avoid actually starting
    the dashboard; reaching it is enough to prove the early return is gone.
    """
    # Minimal config-live
    cfg = tmp_path / "config-live"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "global.yaml").write_text(
        "telegram:\n  bot_token: ''\n  default_chat_id: ''\n"
        "claude:\n  api_key: ''\n  model: claude-sonnet-4-5\n"
        "workspaces:\n  base_dir: " + str(tmp_path / "ws") + "\n"
        "  max_age_days: 14\n  min_free_disk_gb: 0\n  max_workspace_size_gb: 2\n"
        "defaults:\n  poll_interval_seconds: 300\n"
        "  max_iterations: {scope_guard: 1, fix: 1, qa: 1, dev: 1}\n"
        "  max_parallel_tickets: 1\n  pr_comment_fetch_delay_minutes: 30\n"
        "logging:\n  level: WARNING\n  dir: " + str(tmp_path / "log") + "\n"
        "heartbeat:\n  enabled: false\n  interval_hours: 24\n  send_at: '09:00'\n"
        "operator:\n  role: ''\n  stack: []\n  preferences:\n    code_style: ''\n    commit_format: ''\n  rules: []\n"
        "dashboard:\n  enabled: true\n  host: '127.0.0.1'\n  port: 0\n  db_path: " + str(tmp_path / "events.db") + "\n"
        "pipeline:\n  mode: auto\n"
        "intent_parser:\n  max_history: 5\n  confidence_threshold: 0.7\n",
        encoding="utf-8",
    )

    reached_run_all = {"flag": False}

    def fake_asyncio_run(coro):
        reached_run_all["flag"] = True
        # Do not actually run _run_all(); we just need to observe that main()
        # tried to. Close the coroutine to avoid "never awaited" warnings.
        coro.close()

    with patch("asyncio.run", side_effect=fake_asyncio_run):
        rc = main.main(["--config", str(cfg)])

    assert reached_run_all["flag"], (
        "main() must reach asyncio.run(_run_all()) even with zero projects; "
        "the early 'Nothing to do' return was not removed."
    )
    assert rc == 0
```

**Guardrail:** the global.yaml fixture above must match your real config schema. If `ConfigLoader` complains about a missing field, copy the minimal fields from `config-live.example/global.yaml` rather than inventing fields.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_main_startup.py -v`
Expected: FAIL — `reached_run_all` stays False because main.py returns early.

- [ ] **Step 3: Remove the early return**

Modify [main.py:124-126](../../../main.py#L124-L126):

```python
    if not projects:
        print("  No active projects configured — dashboard starting so you can create one via the wizard.")
```

(Remove the `return 0` line. The informational print stays so the operator knows why nothing is being polled.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_main_startup.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full unit suite to catch regressions**

Run: `.venv/bin/python -m pytest tests/unit/ -q`
Expected: 627+ PASS, zero FAIL.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/unit/test_main_startup.py
git commit -m "feat(main): start dashboard even with zero projects so wizard is reachable"
```

---

## Task 8: Integration test — wizard hot-reload end-to-end

**Files:**
- Modify / extend: `tests/integration/test_project_create_flow.py`

- [ ] **Step 1: Inspect the existing integration test for reusable helpers**

Run: `.venv/bin/python -m pytest tests/integration/test_project_create_flow.py --collect-only -q`

Look for a fixture that builds the full Starlette app with a stub `atlas_fn`. Reuse it if present.

- [ ] **Step 2: Write the failing test**

Append to `tests/integration/test_project_create_flow.py`:

```python
@pytest.mark.asyncio
async def test_wizard_creates_project_and_daemon_picks_it_up_without_restart(
    tmp_path, monkeypatch,
):
    """The wizard must make a new project live in-process without a restart.

    Flow:
    1. Build an app with an empty projects dict and an on_project_added callback
       that records which project id it was called for.
    2. POST a valid payload.
    3. Wait for the scheduled Atlas task to finish (stub writes YAML).
    4. Assert the callback fired with the new project_id AND that reloading
       the projects dict via the callback put the project into the shared dict.
    """
    # ---- Build fixture environment ----
    cfg_dir = tmp_path / "config-live"
    (cfg_dir / "projects").mkdir(parents=True)
    # Minimal global.yaml — copy schema from config-live.example/global.yaml
    (cfg_dir / "global.yaml").write_text(
        _MINIMAL_GLOBAL_YAML.format(tmp=tmp_path),
        encoding="utf-8",
    )
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    # Shared projects dict — starts empty.
    projects: dict[str, Any] = {}
    added_ids: list[str] = []

    # Stub Atlas: write a valid project.yaml + repos/main.yaml to disk.
    async def stub_atlas_fn(setup_ws, cfg):
        pid = setup_ws.project_id
        rid = setup_ws.repo_id
        (cfg / "projects" / pid).mkdir(parents=True, exist_ok=True)
        (cfg / "projects" / pid / "project.yaml").write_text(
            _STUB_PROJECT_YAML.format(pid=pid), encoding="utf-8",
        )
        (cfg / "projects" / pid / "repos").mkdir(exist_ok=True)
        (cfg / "projects" / pid / "repos" / f"{rid}.yaml").write_text(
            _STUB_REPO_YAML.format(rid=rid), encoding="utf-8",
        )

    def on_project_added(project_id: str) -> None:
        added_ids.append(project_id)
        # Minimal mirror of main.py's closure: just re-load and merge.
        from config.config_loader import load_config
        _, loaded = load_config(str(cfg_dir), project_filter=project_id)
        projects.update(loaded)

    # Point .env to our tmp path so env_writer does not clobber the real file.
    monkeypatch.setenv("SICKLE_ENV_PATH_OVERRIDE", str(env_path))
    # If your env_writer does not honor that env var, pass env_path directly
    # to build_create_route via a thin create_app call — adapt as needed.

    # ---- Build the Starlette app ----
    from starlette.testclient import TestClient
    from dashboard.web import create_app
    from dashboard.events import EventBus
    from dashboard.event_store import EventStore

    bus = EventBus()
    store = EventStore(str(tmp_path / "events.db"))
    await store.initialize()

    app = create_app(
        bus, store,
        workspace_base_dir=str(ws_base),
        projects=projects,
        config_dir=str(cfg_dir),
        atlas_fn=stub_atlas_fn,
        on_project_added=on_project_added,
    )

    # ---- POST a valid payload ----
    payload = _make_valid_payload(project_id="demo", repo_id="main")
    with TestClient(app) as client:
        resp = client.post("/api/projects/create", json=payload)
    assert resp.status_code == 202, resp.text

    # Wait for the scheduled Atlas task to finish.
    # The schedule() call returns a Task; in TestClient context, we need to
    # pump the event loop briefly. A small sleep is adequate for the stub.
    for _ in range(30):
        if added_ids:
            break
        await asyncio.sleep(0.05)

    assert added_ids == ["demo"], f"expected callback fired once, got {added_ids}"
    assert "demo" in projects, "projects dict was not mutated by callback"
```

**Define the YAML fixtures** at the top of the test file (below imports):

```python
_MINIMAL_GLOBAL_YAML = """\
telegram:
  bot_token: ''
  default_chat_id: ''
claude:
  api_key: ''
  model: claude-sonnet-4-5
workspaces:
  base_dir: {tmp}/ws
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
  dir: {tmp}/log
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
  db_path: {tmp}/events.db
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
  max_iterations: {{scope_guard: 1, fix: 1, qa: 1, dev: 1}}
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
```

And `_make_valid_payload`:

```python
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

**Check the schema:** run `grep -n "def validate_payload" dashboard/project_create_payload.py` to confirm required fields match `_make_valid_payload`. If anything differs, use the actual schema — do not invent fields.

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_project_create_flow.py::test_wizard_creates_project_and_daemon_picks_it_up_without_restart -v`
Expected: FAIL — either `added_ids == []` (callback didn't fire, route didn't pass it through) or import error on the new fixtures.

- [ ] **Step 4: Confirm the test now passes**

Given Tasks 1-7 are already committed, the plumbing exists. Run:

Run: `.venv/bin/python -m pytest tests/integration/test_project_create_flow.py::test_wizard_creates_project_and_daemon_picks_it_up_without_restart -v`
Expected: PASS.

If it fails, check:
- Did the route actually pass `on_success` to `schedule()`? (Task 4)
- Did `create_app` forward `on_project_added`? (Task 5)
- Is `load_config(project_filter=...)` returning the new project? Verify with a `print(loaded)` inside `on_project_added`.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_project_create_flow.py
git commit -m "test(integration): wizard hot-reload fires on_project_added on success"
```

---

## Task 9: Full suite green + manual smoke test

**Files:** none (verification only).

- [ ] **Step 1: Run the full unit + integration + e2e suites**

Run: `.venv/bin/python -m pytest tests/unit/ tests/integration/ -q`
Expected: all PASS.

Run: `.venv/bin/python -m pytest tests/e2e/test_dashboard_health.py tests/e2e/test_daemon_mode.py -q`
Expected: all PASS.

(Per CLAUDE.md and Feature 2's prompt, never mix unit and e2e in one invocation.)

- [ ] **Step 2: Manual smoke test — zero-project boot**

```bash
rm -rf config-live/projects/*   # ensure empty
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

Then from another shell:
```bash
curl -s http://localhost:8080/api/health | head
curl -s http://localhost:8080/api/projects
```

Expected: 200 responses; `/api/projects` returns `{"projects":[]}`.

- [ ] **Step 3: Manual smoke test — wizard hot-reload**

With the daemon from Step 2 still running:
1. Open http://localhost:8080 in a browser.
2. Click **+ New Project**.
3. Fill all 6 wizard steps with real Jira + GitHub credentials.
4. Submit.
5. Watch the wizard transition `SETUP_PENDING → VALIDATING → WRITING → SETUP_DONE`.
6. After SETUP_DONE, within a few seconds check the daemon stdout:
   ```
   Project <id> added and live (no restart required)
   ```
7. Verify: `curl -s http://localhost:8080/api/projects` now shows the new project.
8. Verify (within `poll_interval_seconds`, default 300): the orchestrator logs a Jira poll for the new project.

- [ ] **Step 4: If everything looks green, the feature is done.**

Update [docs/superpowers/plans/2026-04-07-feature-tracker.md](../plans/2026-04-07-feature-tracker.md) with a one-line entry for this feature (match existing style).

Commit the feature-tracker update:

```bash
git add docs/superpowers/plans/2026-04-07-feature-tracker.md
git commit -m "docs: log zero-project boot + wizard hot-reload in feature tracker"
```

---

## Self-review

**Spec coverage:**
- ✅ Change 1 (zero-project boot) — Task 7
- ✅ Change 2.1 (re-run load_config with project_filter) — Task 6 closure
- ✅ Change 2.2 (mutate shared projects dict) — Task 1 + Task 6
- ✅ Change 2.3 (register VCS adapters) — Task 6 `_build_repo_adapters` helper
- ✅ Change 2.4 (set_tracker + command_handler backfill) — Tasks 1, 2, 6
- ✅ Change 2.5 (Telegram chat allowlist) — Tasks 2, 6
- ✅ Change 2.6 (emit project_loaded events) — Task 6
- ✅ New orchestrator API — Task 1
- ✅ New command_handler API — Task 2
- ✅ Error handling: load_config failure logged + event emitted, state unchanged — Task 6
- ✅ on_success never fires on atlas failure — Task 3
- ✅ Unit tests for orchestrator + command_handler + atlas_runner — Tasks 1, 2, 3
- ✅ Startup zero-project test — Task 7
- ✅ Integration end-to-end test — Task 8

**No gaps detected.**

**Type/name consistency check:**
- `set_tracker(tracker)` / `add_project(id, project)` — consistent between spec, Task 1 impl, Task 6 closure.
- `add_allowed_chat_id(chat_id)` / `set_tracker(tracker)` on CommandHandler — consistent Task 2 / Task 6.
- `on_project_added(project_id: str) -> None` — consistent Task 4 / 5 / 6.
- `on_success` Callable on atlas_runner — consistent Task 3 / 4.

**Placeholder scan:** none found.

**Scope check:** single plan, bounded at ~200 LOC production + ~120 LOC tests, 9 tasks with clear commits. Good for subagent-driven execution.

# Dashboard Model Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace duplicated `claude.model` config defaults with a SQLite-backed runtime store, exposed in a new dashboard Settings page (Android-`Spinner`-style dropdown, three-model fixed list, hot-reload on next dispatch).

**Architecture:** A new `dashboard/settings_store.py` owns a `settings(key, value)` table inside the existing `data/events.db`. Two new HTTP routes (`GET /api/settings/model`, `PUT /api/settings/model`) read/write it. Both LLM adapters (`ClaudeAdapter`, `ClaudeCodeAdapter`) take a `model_provider: Callable[[], str]` instead of a fixed `model` string and call it on every dispatch. `main.py` constructs the provider as a closure over the SQLite path. The `claude.model` field is removed from YAML, the `ClaudeConfig` dataclass, and the special-case logic in `main.py`. A new sidebar entry "Settings" renders a single-section view with the dropdown + Save button + status pill.

**Tech Stack:** Python 3 (asyncio + Starlette + aiosqlite + sqlite3), Pydantic-style dataclasses, ES module JS (no framework), pytest + pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-04-27-dashboard-model-settings-design.md](../specs/2026-04-27-dashboard-model-settings-design.md)

---

## Task 1: Settings store module

**Files:**
- Create: `dashboard/settings_store.py`
- Test: `tests/unit/test_settings_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_settings_store.py`:

```python
"""Tests for dashboard.settings_store — SQLite-backed runtime model setting."""

from __future__ import annotations

import aiosqlite
import pytest

from dashboard.settings_store import (
    ALLOWED_MODELS,
    DEFAULT_MODEL,
    get_model,
    init_settings,
    set_model,
)


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    try:
        yield conn
    finally:
        await conn.close()


class TestInitSettings:
    async def test_creates_table_when_missing(self, db):
        await init_settings(db)
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        )
        assert len(rows) == 1

    async def test_seeds_default_model_on_empty(self, db):
        await init_settings(db)
        assert await get_model(db) == DEFAULT_MODEL

    async def test_idempotent_does_not_overwrite_existing_value(self, db):
        await init_settings(db)
        await set_model(db, "claude-opus-4-7")
        await init_settings(db)  # Second call should not seed
        assert await get_model(db) == "claude-opus-4-7"


class TestGetSetModel:
    async def test_set_and_get_each_allowed_model(self, db):
        await init_settings(db)
        for model in ALLOWED_MODELS:
            await set_model(db, model)
            assert await get_model(db) == model

    async def test_set_model_rejects_unknown(self, db):
        await init_settings(db)
        with pytest.raises(ValueError, match="not allowed"):
            await set_model(db, "claude-sonnet-3.5")

    async def test_default_model_is_in_allowed_list(self):
        assert DEFAULT_MODEL in ALLOWED_MODELS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_settings_store.py -v`
Expected: FAIL with `ImportError: cannot import name 'ALLOWED_MODELS' from 'dashboard.settings_store'` (module does not exist).

- [ ] **Step 3: Implement the settings_store module**

Create `dashboard/settings_store.py`:

```python
"""Runtime settings store backed by the dashboard's SQLite database.

Single source of truth for the active Claude model. Lives in the same
SQLite file as the event store (data/events.db) — see DashboardConfig.db_path.
"""

from __future__ import annotations

import aiosqlite

ALLOWED_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)
DEFAULT_MODEL = "claude-sonnet-4-6"

_MODEL_KEY = "model"


async def init_settings(db: aiosqlite.Connection) -> None:
    """Create the settings table if missing and seed the model row if empty."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        (_MODEL_KEY, DEFAULT_MODEL),
    )
    await db.commit()


async def get_model(db: aiosqlite.Connection) -> str:
    """Return the active model. Caller must have run init_settings first."""
    rows = await db.execute_fetchall(
        "SELECT value FROM settings WHERE key = ?",
        (_MODEL_KEY,),
    )
    if not rows:
        raise RuntimeError("settings.model row missing — init_settings not called?")
    return rows[0][0]


async def set_model(db: aiosqlite.Connection, model: str) -> None:
    """Persist a new model value. Raises ValueError if not in ALLOWED_MODELS."""
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Model {model!r} not allowed. Allowed: {ALLOWED_MODELS}")
    await db.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_MODEL_KEY, model),
    )
    await db.commit()
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `pytest tests/unit/test_settings_store.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/settings_store.py tests/unit/test_settings_store.py
git commit -m "feat(dashboard): add SQLite-backed runtime settings store for model"
```

---

## Task 2: Settings HTTP API routes

**Files:**
- Modify: `dashboard/web.py` (add 2 routes near line 238)
- Modify: `main.py` (call `init_settings` after `event_store.initialize` near line 389)
- Test: `tests/integration/test_settings_api.py`

- [ ] **Step 1: Write the failing integration tests**

Create `tests/integration/test_settings_api.py`:

```python
"""Integration tests for /api/settings/model routes."""

from __future__ import annotations

import aiosqlite
import pytest
from starlette.testclient import TestClient

from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.settings_store import ALLOWED_MODELS, DEFAULT_MODEL, init_settings
from dashboard.web import create_app


@pytest.fixture
async def app_and_db(tmp_path):
    db_path = tmp_path / "events.db"
    bus = EventBus()
    store = EventStore(str(db_path))
    await store.initialize()

    # Init settings table on the same DB (mirrors main.py startup).
    async with aiosqlite.connect(str(db_path)) as conn:
        await init_settings(conn)

    app = create_app(bus, store, workspace_base_dir=str(tmp_path))
    yield app, str(db_path)
    await store.close()


class TestGetModel:
    async def test_returns_default_and_options(self, app_and_db):
        app, _ = app_and_db
        client = TestClient(app)
        r = client.get("/api/settings/model")
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == DEFAULT_MODEL
        assert body["options"] == list(ALLOWED_MODELS)


class TestPutModel:
    async def test_persists_valid_model(self, app_and_db):
        app, _ = app_and_db
        client = TestClient(app)
        r = client.put(
            "/api/settings/model",
            json={"model": "claude-opus-4-7"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "claude-opus-4-7"

        # Subsequent GET reflects the change.
        r2 = client.get("/api/settings/model")
        assert r2.json()["model"] == "claude-opus-4-7"

    async def test_rejects_unknown_model(self, app_and_db):
        app, _ = app_and_db
        client = TestClient(app)
        r = client.put(
            "/api/settings/model",
            json={"model": "claude-sonnet-3.5"},
        )
        assert r.status_code == 400
        # Original default remains.
        r2 = client.get("/api/settings/model")
        assert r2.json()["model"] == DEFAULT_MODEL

    async def test_rejects_missing_model_key(self, app_and_db):
        app, _ = app_and_db
        client = TestClient(app)
        r = client.put("/api/settings/model", json={})
        assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_settings_api.py -v`
Expected: FAIL with 404 on the routes (they don't exist yet).

- [ ] **Step 3: Add routes to dashboard/web.py**

In [dashboard/web.py](../../dashboard/web.py), add these two route handlers inside `create_app`, immediately above the `routes = [` list assembly (currently around line 224, just before `async def index`):

```python
    async def get_settings_model(request: Request) -> JSONResponse:
        from dashboard.settings_store import ALLOWED_MODELS, get_model
        async with aiosqlite.connect(store._db_path) as conn:
            current = await get_model(conn)
        return JSONResponse({
            "model": current,
            "options": list(ALLOWED_MODELS),
        })

    async def put_settings_model(request: Request) -> JSONResponse:
        from dashboard.settings_store import set_model
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)
        try:
            async with aiosqlite.connect(store._db_path) as conn:
                await set_model(conn, model)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"model": model})
```

Add `import aiosqlite` at the top of `dashboard/web.py` (after the existing `from starlette.*` imports).

In the `routes = [...]` list (currently at line 228), add two new `Route(...)` entries:

```python
        Route("/api/settings/model", get_settings_model, methods=["GET"]),
        Route("/api/settings/model", put_settings_model, methods=["PUT"]),
```

- [ ] **Step 4: Wire init_settings into main.py startup**

In [main.py](../../main.py), find the line `await event_store.initialize()` (currently line 389). Immediately after it, add:

```python
        # Init runtime settings table (model picker).
        import aiosqlite as _aiosqlite
        from dashboard.settings_store import init_settings
        async with _aiosqlite.connect(db_path) as _conn:
            await init_settings(_conn)
```

- [ ] **Step 5: Run integration tests**

Run: `pytest tests/integration/test_settings_api.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 6: Run full test suite to ensure no regressions**

Run: `pytest tests/ -x --ignore=tests/e2e -q`
Expected: All tests still pass (or only pre-existing failures unrelated to this change).

- [ ] **Step 7: Commit**

```bash
git add dashboard/web.py main.py tests/integration/test_settings_api.py
git commit -m "feat(dashboard): add GET/PUT /api/settings/model routes"
```

---

## Task 3: Replace adapter `model` parameter with `model_provider`

This is the destructive single-source-of-truth change. We update the adapters, both their tests, the callers (`main.py`), and the test fixtures that injected the model in one go. After this task, the literal `"claude-sonnet-4-5"` no longer exists in production code or in YAML — only in tests where a specific model is asserted.

**Files:**
- Modify: `integrations/llm/claude_adapter.py:22-24,143,150`
- Modify: `integrations/llm/claude_code_adapter.py:91-100,197,205,254,272`
- Modify: `config/schemas.py:14-17`
- Modify: `config-live/global.yaml:8-10`
- Modify: `config-live.example/global.yaml:8-10`
- Modify: `tests/fixtures/config/global.yaml:5-7`
- Modify: `tests/unit/test_main_startup.py:46`
- Modify: `tests/integration/test_project_create_flow.py:218`
- Modify: `main.py:159-171`
- Modify: `tests/unit/test_config_loader.py:53`
- Modify: `tests/unit/test_claude_adapter.py:25,44`
- Modify: `tests/unit/test_claude_code_adapter.py:18,95`
- Modify: `tests/unit/test_agent_runtime.py:30,157,167,212,246,256,302,312`
- Modify: `tests/integration/test_e2e_dry_run.py:30`

- [ ] **Step 1: Update ClaudeCodeAdapter tests to use model_provider**

In [tests/unit/test_claude_code_adapter.py](../../tests/unit/test_claude_code_adapter.py), change the two fixtures:

```python
# Line 17-18 — currently:
#     @pytest.fixture
#     def adapter(self):
#         return ClaudeCodeAdapter(model="claude-haiku-4-5-20251001")
# Change to:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-haiku-4-5-20251001")
```

```python
# Line 94-95 — currently:
#     @pytest.fixture
#     def adapter(self):
#         return ClaudeCodeAdapter(model="claude-sonnet-4-5")
# Change to:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")
```

- [ ] **Step 2: Update ClaudeAdapter tests to use default_model_provider**

In [tests/unit/test_claude_adapter.py:25](../../tests/unit/test_claude_adapter.py#L25):

```python
# Currently:
#         a = ClaudeAdapter(api_key="fake-key", default_model="claude-sonnet-4-5")
# Change to:
        a = ClaudeAdapter(
            api_key="fake-key",
            default_model_provider=lambda: "claude-sonnet-4-5",
        )
```

Line 44 already asserts `kwargs["model"] == "claude-sonnet-4-5"` — leave as-is; the provider must produce that value.

- [ ] **Step 3: Update agent_runtime tests to use model_provider**

In [tests/unit/test_agent_runtime.py](../../tests/unit/test_agent_runtime.py), every line currently `model="claude-sonnet-4-5",` (lines 30, 157, 167, 212, 246, 256, 302, 312) becomes `model_provider=lambda: "claude-sonnet-4-5",`. Use `sed` for the bulk change:

```bash
sed -i 's/model="claude-sonnet-4-5"/model_provider=lambda: "claude-sonnet-4-5"/g' tests/unit/test_agent_runtime.py
```

Verify: `grep -n 'model=\|model_provider=' tests/unit/test_agent_runtime.py | head -20` — should show only `model_provider=lambda: ...` lines (not `model=...`).

- [ ] **Step 4: Update e2e dry-run test**

In [tests/integration/test_e2e_dry_run.py:30](../../tests/integration/test_e2e_dry_run.py#L30):

```python
# Currently:
#         model="claude-sonnet-4-5",
# Change to:
        model_provider=lambda: "claude-sonnet-4-5",
```

- [ ] **Step 5: Run the updated tests to verify they fail (signature mismatch)**

Run: `pytest tests/unit/test_claude_code_adapter.py tests/unit/test_claude_adapter.py tests/unit/test_agent_runtime.py tests/integration/test_e2e_dry_run.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'model_provider'` (or `'default_model_provider'`).

- [ ] **Step 6: Modify ClaudeCodeAdapter to accept model_provider**

In [integrations/llm/claude_code_adapter.py](../../integrations/llm/claude_code_adapter.py):

Replace the `__init__` block (currently lines 91-100):

```python
    def __init__(
        self,
        model_provider: Callable[[], str] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._claude_bin = shutil.which("claude") or "claude"
        self._model_provider = model_provider or (lambda: "")
        self._max_turns = max_turns
        self._timeout = timeout
```

Add `from typing import Callable` to the top imports (if not already present alongside `Any`).

Replace the body that reads `self._model` in `quick_query` (line 197):

```python
# Currently:
#         use_model = self._model
# Change to:
        use_model = self._model_provider()
```

Replace the same pattern in `_run_cli` (line 254):

```python
# Currently:
#         use_model = model or self._model
# Change to:
        use_model = model or self._model_provider()
```

- [ ] **Step 7: Modify ClaudeAdapter to accept default_model_provider**

In [integrations/llm/claude_adapter.py](../../integrations/llm/claude_adapter.py), replace lines 22-24:

```python
    def __init__(
        self,
        api_key: str,
        default_model_provider: Callable[[], str],
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model_provider = default_model_provider
```

Add `from typing import Any, Callable` (replace existing `from typing import Any`).

Replace `self._default_model` reads. Line 58 in `_call_api`:

```python
# Currently:
#         use_model = model or self._default_model
# Change to:
        use_model = model or self._default_model_provider()
```

In `quick_query` (lines 142-150), replace the two reads of `self._default_model`:

```python
# Currently:
#         kwargs: dict[str, Any] = {
#             "model": self._default_model,
#             ...
#         }
#         ...
#         logger.info("Claude API quick_query: model=%s", self._default_model)
# Change to:
        model = self._default_model_provider()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        logger.info("Claude API quick_query: model=%s", model)
```

- [ ] **Step 8: Drop the `claude.model` field from ClaudeConfig**

In [config/schemas.py:14-17](../../config/schemas.py#L14-L17), replace:

```python
@dataclass
class ClaudeConfig:
    api_key: str = ""
    model: str = "claude-sonnet-4-5"
```

with:

```python
@dataclass
class ClaudeConfig:
    api_key: str = ""
```

- [ ] **Step 9: Update test_config_loader assertion**

In [tests/unit/test_config_loader.py:53](../../tests/unit/test_config_loader.py#L53):

```python
# Currently:
#         assert config.claude.model == "claude-sonnet-4-5"
# Change to:
        assert not hasattr(config.claude, "model")
        assert config.claude.api_key == ""  # default empty string
```

(Adjust the second assertion to whatever value the fixture loads — read the surrounding test for context if needed.)

- [ ] **Step 10: Drop `model:` from YAML files**

In [config-live/global.yaml:8-10](../../config-live/global.yaml#L8-L10), [config-live.example/global.yaml:8-10](../../config-live.example/global.yaml#L8-L10), and [tests/fixtures/config/global.yaml:5-7](../../tests/fixtures/config/global.yaml#L5-L7), delete the line:

```yaml
  model: "claude-sonnet-4-5"
```

The `claude:` block then has only `api_key:`. Verify with: `grep -n "claude\|model" config-live/global.yaml config-live.example/global.yaml tests/fixtures/config/global.yaml`.

- [ ] **Step 11: Drop `model:` from inline YAML in two test files**

In [tests/unit/test_main_startup.py:46](../../tests/unit/test_main_startup.py#L46), find the `model: claude-sonnet-4-5` line inside the inline YAML literal and delete it (keep the surrounding `claude:` and `api_key:` lines).

In [tests/integration/test_project_create_flow.py:218](../../tests/integration/test_project_create_flow.py#L218), do the same — delete the `model: claude-sonnet-4-5` line in the inline YAML.

- [ ] **Step 12: Wire model_provider into main.py**

In [main.py](../../main.py), replace the LLM-init block (lines 159-171):

```python
    # Initialize LLM adapter
    if global_config.claude.api_key:
        llm = ClaudeAdapter(
            api_key=global_config.claude.api_key,
            default_model=global_config.claude.model,
        )
        print("  LLM: Anthropic API adapter")
    else:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        llm = ClaudeCodeAdapter(
            model=global_config.claude.model if global_config.claude.model != "claude-sonnet-4-5" else "",
        )
        print("  LLM: Claude Code CLI adapter (using existing auth)")
```

with:

```python
    # Initialize LLM adapter (model read fresh from runtime store on every dispatch).
    import sqlite3 as _sqlite3

    def _read_model() -> str:
        with _sqlite3.connect(db_path) as _conn:
            row = _conn.execute(
                "SELECT value FROM settings WHERE key='model'"
            ).fetchone()
        if row is None:
            from dashboard.settings_store import DEFAULT_MODEL
            return DEFAULT_MODEL
        return row[0]

    if global_config.claude.api_key:
        llm = ClaudeAdapter(
            api_key=global_config.claude.api_key,
            default_model_provider=_read_model,
        )
        print("  LLM: Anthropic API adapter")
    else:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        llm = ClaudeCodeAdapter(model_provider=_read_model)
        print("  LLM: Claude Code CLI adapter (using existing auth)")
```

Note: `db_path` is the absolute path computed earlier in `main.py` (currently around line 149). The closure captures it.

The fallback to `DEFAULT_MODEL` inside `_read_model` covers the case where the LLM adapter is constructed before `init_settings` runs. The cleaner alternative — calling `init_settings` synchronously here — would force `aiosqlite` async or duplicate the seed logic; the fallback is the simpler choice.

- [ ] **Step 13: Run all updated unit + integration tests**

Run: `pytest tests/unit/test_claude_code_adapter.py tests/unit/test_claude_adapter.py tests/unit/test_agent_runtime.py tests/unit/test_config_loader.py tests/unit/test_main_startup.py tests/integration/test_project_create_flow.py tests/integration/test_e2e_dry_run.py tests/integration/test_settings_api.py tests/unit/test_settings_store.py -v`
Expected: ALL PASS.

- [ ] **Step 14: Run full test suite for regressions**

Run: `pytest tests/ -x --ignore=tests/e2e -q`
Expected: ALL PASS. If a test fails because it grep'd for `"claude-sonnet-4-5"` in a YAML file, fix that test (it's now stale).

- [ ] **Step 15: Verify no stale references remain**

Run: `grep -rn 'claude-sonnet-4-5' --include='*.py' --include='*.yaml' --include='*.yml' .`
Expected: Only test fixtures and adapter test code that explicitly assert that string remain. `config-live/global.yaml` and `main.py` should NOT contain it.

- [ ] **Step 16: Commit**

```bash
git add integrations/llm/claude_adapter.py integrations/llm/claude_code_adapter.py \
        config/schemas.py main.py \
        config-live/global.yaml config-live.example/global.yaml \
        tests/fixtures/config/global.yaml \
        tests/unit/test_config_loader.py tests/unit/test_main_startup.py \
        tests/unit/test_claude_adapter.py tests/unit/test_claude_code_adapter.py \
        tests/unit/test_agent_runtime.py \
        tests/integration/test_project_create_flow.py tests/integration/test_e2e_dry_run.py
git commit -m "refactor: adapters take model_provider; drop YAML model field

Single source of truth becomes the dashboard SQLite settings table.
Removes hardcoded 'claude-sonnet-4-5' defaults from ClaudeConfig
schema, ClaudeAdapter/ClaudeCodeAdapter constructors, and the
special-case logic in main.py. Adapters now call a
Callable[[], str] on every dispatch."
```

---

## Task 4: Settings page UI

Add a sidebar entry "Settings", a content section with the dropdown + Save button + status pill, and the JS module that talks to the API.

**Files:**
- Modify: `dashboard/static/index.html` (sidebar nav + new section markup)
- Modify: `dashboard/static/js/app.js` (route + nav binding)
- Modify: `dashboard/static/style.css` (settings layout)
- Create: `dashboard/static/js/settings.js`

- [ ] **Step 1: Add the sidebar nav link**

In [dashboard/static/index.html](../../dashboard/static/index.html), inside the existing `<div class="sidebar-section">` that already holds `nav-board` and `nav-eventlog` (lines 13-16), add the new link after `nav-eventlog`:

```html
  <div class="sidebar-section">
    <a class="nav-link" id="nav-board">Board</a>
    <a class="nav-link" id="nav-eventlog">Event Log</a>
    <a class="nav-link" id="nav-settings">Settings</a>
  </div>
```

- [ ] **Step 2: Create the settings.js module**

Create `dashboard/static/js/settings.js`:

```javascript
// settings.js — Settings view (model picker)

import { esc } from './helpers.js';

let baseline = null;

export async function renderSettings() {
  const content = document.getElementById('content');
  content.innerHTML = '<div class="state-msg">Loading…</div>';

  try {
    const r = await fetch('/api/settings/model');
    if (!r.ok) throw new Error(`GET failed: ${r.status}`);
    const { model, options } = await r.json();
    baseline = model;

    const opts = options.map(m =>
      `<option value="${esc(m)}"${m === model ? ' selected' : ''}>${esc(m)}</option>`
    ).join('');

    content.innerHTML = `
      <section class="settings-view">
        <h2 class="settings-section-title">Claude Model</h2>
        <p class="settings-help">Used by the agent dispatcher. Changes apply to the next agent dispatch — running tickets keep their starting model for the current step.</p>
        <div class="settings-row">
          <label class="settings-label" for="settings-model">Model</label>
          <select id="settings-model" class="settings-spinner">${opts}</select>
          <button id="settings-save" class="btn-primary" disabled>Save</button>
          <span id="settings-status" class="settings-pill pill-idle"></span>
        </div>
      </section>
    `;

    const select = document.getElementById('settings-model');
    const save = document.getElementById('settings-save');
    const pill = document.getElementById('settings-status');

    select.addEventListener('change', () => {
      save.disabled = select.value === baseline;
      setPill(pill, '', 'pill-idle');
    });

    save.addEventListener('click', async () => {
      save.disabled = true;
      setPill(pill, 'Saving…', 'pill-saving');
      try {
        const resp = await fetch('/api/settings/model', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: select.value }),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || `HTTP ${resp.status}`);
        }
        const { model: saved } = await resp.json();
        baseline = saved;
        setPill(pill, 'Saved ✓', 'pill-saved');
        setTimeout(() => setPill(pill, '', 'pill-idle'), 1500);
      } catch (e) {
        setPill(pill, `Error: ${e.message}`, 'pill-error');
        save.disabled = select.value === baseline;
      }
    });
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
  }
}

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = `settings-pill ${cls}`;
}
```

- [ ] **Step 3: Hook the settings view into app.js**

In [dashboard/static/js/app.js](../../dashboard/static/js/app.js):

a) Add a new view function after `showEventLog` (line 47), before `showDetail`:

```javascript
function showSettings(fromHash) {
  state.view = 'settings';
  state.ticketId = null;
  document.getElementById('view-title').textContent = 'Settings';
  document.getElementById('toolbar-eventlog-controls').style.display = 'none';
  updateActiveNav('nav-settings');
  stopAutoRefresh();
  import('./settings.js').then(({ renderSettings }) => renderSettings());
  if (!fromHash) setHash('#/settings');
}
```

b) Update the hash router (line 60-67):

```javascript
function routeFromHash() {
  const h = location.hash || '#/board';
  const m = h.match(/^#\/(board|eventlog|settings|ticket)(?:\/(.+))?$/);
  if (!m) { showBoard(null, true); return; }
  const [, view, arg] = m;
  if (view === 'board') showBoard(arg || null, true);
  else if (view === 'eventlog') showEventLog(true);
  else if (view === 'settings') showSettings(true);
  else if (view === 'ticket' && arg) showDetail(arg, true);
  else showBoard(null, true);
}
```

c) Bind the new nav link in `init()` (after the existing `nav-eventlog` binding at line 224):

```javascript
  document.getElementById('nav-settings').addEventListener('click', () => showSettings());
```

- [ ] **Step 4: Add settings styles**

Append to [dashboard/static/style.css](../../dashboard/static/style.css):

```css
/* Settings view */
.settings-view {
  max-width: 760px;
  padding: 24px;
}
.settings-section-title {
  margin: 0 0 4px;
  font-size: 18px;
  color: #c9d1d9;
}
.settings-help {
  margin: 0 0 16px;
  font-size: 13px;
  color: #8b949e;
}
.settings-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.settings-label {
  min-width: 60px;
  color: #c9d1d9;
  font-size: 14px;
}
.settings-spinner {
  background: #0d1117;
  color: #c9d1d9;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 14px;
  min-width: 280px;
}
.settings-spinner:focus {
  outline: none;
  border-color: #58a6ff;
}
.settings-pill {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  min-height: 18px;
  min-width: 72px;
  text-align: center;
}
.pill-idle    { background: transparent; color: transparent; }
.pill-saving  { background: #1f6feb33; color: #58a6ff; }
.pill-saved   { background: #2ea04333; color: #3fb950; }
.pill-error   { background: #f8514922; color: #f85149; }
```

- [ ] **Step 5: Manual smoke test in the browser**

Run the daemon locally:

```bash
python -m main --config config-live
```

In a browser, visit `http://localhost:8080` (or whatever `dashboard.port` is). Then:

1. Click the new "Settings" link in the sidebar.
2. Verify the dropdown shows three options: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`. The current value (`claude-sonnet-4-6` on first run, since the YAML field is gone and the DB is freshly seeded) is preselected.
3. Save button is disabled.
4. Pick a different model — Save button enables.
5. Click Save — pill flashes "Saving…" then "Saved ✓" (~1.5s) then disappears.
6. Reload the page, navigate back to Settings — the new value is preselected.
7. Open `data/events.db` with `sqlite3 data/events.db "SELECT * FROM settings"` — confirm row matches the UI.
8. Hit `curl -X PUT http://localhost:8080/api/settings/model -H 'Content-Type: application/json' -d '{"model":"bogus"}'` — expect HTTP 400 with an error body.

- [ ] **Step 6: Commit**

```bash
git add dashboard/static/index.html dashboard/static/js/app.js \
        dashboard/static/js/settings.js dashboard/static/style.css
git commit -m "feat(dashboard): add Settings page with Claude model picker"
```

---

## Verification & cleanup

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -q --ignore=tests/e2e`
Expected: ALL PASS.

- [ ] **Step 2: Confirm git status is clean**

Run: `git status`
Expected: Working tree clean (or only the local `data/events.db` file modified, which is fine — it's in `.gitignore` if configured, otherwise leave untracked).

- [ ] **Step 3: Confirm spec coverage**

Re-read [docs/superpowers/specs/2026-04-27-dashboard-model-settings-design.md](../specs/2026-04-27-dashboard-model-settings-design.md). Each "Decisions" row and each "Removals" bullet should be reflected in the diffs of these four commits. If any are missing, add a follow-up task.

---

## Self-review — done

Plan vs. spec coverage:

- ✓ SQLite `settings` key/value table in `data/events.db` — Task 1
- ✓ `init_settings`, `get_model`, `set_model` API + ALLOWED_MODELS constant — Task 1
- ✓ `GET /api/settings/model`, `PUT /api/settings/model` — Task 2
- ✓ `init_settings` called on dashboard startup — Task 2
- ✓ `model_provider: Callable[[], str]` for both adapters — Task 3
- ✓ Removal of YAML `claude.model` and `ClaudeConfig.model` — Task 3
- ✓ Removal of `main.py:169` special-case — Task 3
- ✓ Sidebar entry, dropdown view, Save button, status pill, hot-reload semantics — Task 4
- ✓ Test impact bullets — Tasks 1-4 (each test file mentioned in spec is touched in a numbered step)

Open considerations confirmed:

- The `_read_model` closure in main.py uses `sqlite3` (sync) rather than `aiosqlite` because it's called from the adapter's per-dispatch path — synchronous reads of a tiny table are microseconds and won't measurably block the async loop. This matches what the spec described as a "short-lived sqlite3 connection per call."
- The `init_settings` fallback inside `_read_model` (returns DEFAULT_MODEL if the row is missing) is a defensive guard for the brief window before `init_settings` runs in the dashboard startup path — it ensures the daemon never crashes if someone constructs an adapter before init.

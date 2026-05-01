# Dashboard Model Settings — Design

**Date:** 2026-04-27
**Status:** Approved (design phase)

## Problem

The Claude model identifier used by the Cleave pipeline is duplicated across at least four locations:

- `config-live/global.yaml` — runtime config (`claude.model: "claude-sonnet-4-5"`)
- `config/schemas.py:17` — Pydantic field default (`model: str = "claude-sonnet-4-5"`)
- `integrations/llm/claude_adapter.py:22` — `__init__(default_model="claude-sonnet-4-5")`
- `main.py:169` — special-case logic comparing the configured value to the literal `"claude-sonnet-4-5"` to decide whether to pass an override flag

These can drift independently. Changing the model today means editing YAML and restarting the daemon. There is no UI surface for it.

## Goal

1. **Single source of truth** for the active model — readable and writable from one place.
2. **Dashboard UI** — a Settings page with a dropdown picker (Android-`Spinner`-style) and explicit Save.
3. **Hot-reload** — saving from the UI applies to the next agent dispatch with no daemon restart. A `claude -p` call already in flight cannot be retargeted, but the next dispatch (including the next stage of an already-running ticket) reads the new value.

## Non-goals

- A general settings page covering all of `global.yaml`. Only the model picker is in scope; the page is structured so a future setting can be added later, but no other settings ship in this work.
- Per-project / per-agent model overrides.
- Model A/B testing or experiment frameworks.
- Migration tooling for users with existing custom model values in YAML — the field is dropped; first-launch seeds `claude-sonnet-4-6`. Users who had a non-default value will need to re-select it once after upgrade.

## Decisions

| Decision                  | Choice                                                                                                                              |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Source of truth location  | SQLite `settings` table inside the existing `data/events.db`                                                                        |
| Allowed models            | Fixed list: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`                                                     |
| Default seed (empty DB)   | `claude-sonnet-4-6`                                                                                                                 |
| Hot-reload semantics      | Read fresh on every dispatch. The currently-executing `claude -p` call cannot be retargeted, but the next stage of the same ticket picks up the new value. |
| Settings page scope       | Model picker only. Single sidebar entry, single section.                                                                            |
| Save UX                   | Explicit Save button. Disabled until value differs. Status pill: idle → Saving… → Saved ✓ (1.5s) → idle.                            |
| Default removal           | Hardcoded `"claude-sonnet-4-5"` defaults stripped from `ClaudeAdapter`, `ClaudeConfig` schema, and `main.py:169` special-case logic |
| YAML field                | `claude.model` removed from `config-live/global.yaml`, `config-live.example/global.yaml`, and test fixture YAML                     |

## Architecture

The dashboard's existing `data/events.db` (managed via `aiosqlite` in `dashboard/event_store.py`) gains one new table:

```sql
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

A new `dashboard/settings_store.py` module owns all reads and writes against this table. It exposes:

- `init_settings(db) -> None` — create table if missing; insert `('model', 'claude-sonnet-4-6')` if no row exists.
- `get_model(db) -> str` — return the current model. Raises if missing (init must have been called).
- `set_model(db, model: str) -> None` — validate against `ALLOWED_MODELS`; raise `ValueError` on mismatch; otherwise UPSERT.
- `ALLOWED_MODELS: tuple[str, ...]` — module-level constant.

The agent dispatch path receives a `model_provider: Callable[[], str]` instead of a fixed `model: str`. The provider is a closure over the SQLite path constructed once at daemon startup, and called by the adapter on every dispatch:

```
adapter = ClaudeCodeAdapter(model_provider=make_model_provider(db_path))
```

The adapter calls `self._model_provider()` inside `quick_query()` and `run()` to obtain the current model string. Per-call `model=` overrides on those methods continue to win when supplied (used by tests and any explicit override callsites).

```
┌──────────────────────┐        ┌────────────────────────────┐
│ Dashboard UI         │  PUT   │ web.py /api/settings/model │
│ Settings → Spinner   │ ─────► │     validate + write       │
└──────────────────────┘        └────────────┬───────────────┘
                                              │
                                              ▼
                                ┌────────────────────────────┐
                                │ settings_store.py          │
                                │   data/events.db           │
                                │   table: settings(key,val) │
                                └────────────┬───────────────┘
                                              │  read on every dispatch
                                              ▼
                                ┌────────────────────────────┐
                                │ ClaudeCodeAdapter / Claude │
                                │ Adapter — model_provider() │
                                └────────────────────────────┘
```

## Components

### 1. `dashboard/settings_store.py` (new)

Thin module. ~50 lines. Owns the `settings` table, the allowed list, and the seed default. No business logic beyond validation.

```python
ALLOWED_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)
DEFAULT_MODEL = "claude-sonnet-4-6"

async def init_settings(db: aiosqlite.Connection) -> None: ...
async def get_model(db: aiosqlite.Connection) -> str: ...
async def set_model(db: aiosqlite.Connection, model: str) -> None: ...
```

### 2. `dashboard/web.py` (modified)

Two new routes, registered alongside existing `/api/*` routes:

- `GET /api/settings/model` → `{"model": "claude-sonnet-4-6", "options": ["claude-opus-4-7", ...]}`
- `PUT /api/settings/model` accepts `{"model": "<one of options>"}`. Returns `200 {"model": "..."}` on success, `400` on unknown model.

`init_settings` is called once during dashboard startup, after the existing `EventStore` connect.

### 3. Synchronous read path for adapters

Adapters run inside the daemon process and need a synchronous read (the dispatch sites are not all async). The provider closure opens a short-lived `sqlite3` connection per call:

```python
def make_model_provider(db_path: str) -> Callable[[], str]:
    def _read() -> str:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='model'").fetchone()
        return row[0]  # init guarantees presence
    return _read
```

Cost is one cheap SQLite read per dispatch (microseconds; far smaller than the LLM call itself).

### 4. `dashboard/static/index.html` (modified)

Add a sidebar link in the existing nav block:

```html
<a class="nav-link" id="nav-settings">Settings</a>
```

Add a hidden settings view section that mirrors the structure of the existing Board / Event Log sections so the existing view-switching logic in `app.js` can show/hide it.

### 5. `dashboard/static/js/settings.js` (new)

~40 lines. On settings-view show:

1. `GET /api/settings/model` → populate `<select>` with `options`, set `value` to current `model`, store baseline.
2. `change` listener enables Save when value differs from baseline.
3. Save click → `PUT /api/settings/model` with selected value → on success update baseline, disable Save, flash `Saved ✓` pill for 1.5s. On failure: error pill with response message.

### 6. `dashboard/static/js/app.js` (modified)

Register the `nav-settings` click handler and add a case for the settings view in the existing view router. No structural refactor — follow the pattern used by `nav-board` and `nav-eventlog`.

### 7. `dashboard/static/style.css` (modified)

Minimal additions for the settings layout: a `.settings-view` container, a row laying out label + select + save button + status pill, and pill states `.pill-idle`, `.pill-saving`, `.pill-saved`, `.pill-error`.

## Removals

To enforce single source of truth, the following are deleted:

- `config/schemas.py` — `model: str = "claude-sonnet-4-5"` field on `ClaudeConfig`. The class keeps only `api_key`.
- `config-live/global.yaml`, `config-live.example/global.yaml`, `tests/fixtures/config/global.yaml` — the `model:` line under `claude:`.
- `integrations/llm/claude_adapter.py` — the `default_model: str = "claude-sonnet-4-5"` parameter on `ClaudeAdapter.__init__`. Replaced by `default_model_provider: Callable[[], str]`.
- `integrations/llm/claude_code_adapter.py` — the `model: str = ""` parameter on `ClaudeCodeAdapter.__init__`. Replaced by `model_provider: Callable[[], str]`. Per-call `model=` overrides on public methods are kept.
- `main.py:169` — `model=global_config.claude.model if global_config.claude.model != "claude-sonnet-4-5" else ""` becomes `model_provider=make_model_provider(db_path)`.

## Test impact

### Updated tests

- `tests/unit/test_config_loader.py:53` — assertion changes from `config.claude.model == "claude-sonnet-4-5"` to verifying `ClaudeConfig` no longer exposes `.model`.
- `tests/unit/test_main_startup.py:46`, `tests/integration/test_project_create_flow.py:218` — drop the `model: claude-sonnet-4-5` line from inline YAML fixtures.
- `tests/unit/test_claude_code_adapter.py`, `tests/unit/test_claude_adapter.py`, `tests/unit/test_agent_runtime.py`, `tests/integration/test_e2e_dry_run.py` — switch from `model="claude-sonnet-4-5"` to `model_provider=lambda: "claude-sonnet-4-5"` (or the relevant test value). No DB required in unit tests.

### New tests

- `tests/unit/test_settings_store.py`
  - `init_settings` creates table when missing.
  - `init_settings` seeds `claude-sonnet-4-6` when empty.
  - `init_settings` is idempotent — does not overwrite existing value.
  - `get_model` returns current value.
  - `set_model` accepts each allowed model.
  - `set_model` raises `ValueError` on unknown model.
- `tests/integration/test_settings_api.py`
  - `GET /api/settings/model` returns current value and full options list.
  - `PUT /api/settings/model` with valid model persists and is reflected on subsequent GET.
  - `PUT /api/settings/model` with unknown model returns 400 and does not persist.

## Migration

First daemon startup after this change:

1. `init_settings` runs against `data/events.db`.
2. `settings` table is created.
3. Empty → row `('model', 'claude-sonnet-4-6')` inserted.

Existing YAML files lose the `claude.model` line on upgrade. Operators who had customised that value will see the daemon switch to `claude-sonnet-4-6` until they pick their preferred model in the dashboard. This is acceptable because the Settings page exists specifically to make that re-selection trivial.

## Open issues

None at design time. Implementation plan to be produced via the `writing-plans` skill.

# Telegram Quiet Hours Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bot messages sent inside a configurable nightly window (default 22:00–09:00, host local time) go out silent; the window is set on the dashboard's Settings page.

**Architecture:** A `QuietHours` value object (`integrations/telegram/quiet_hours.py`) decides whether a local time is quiet. It is stored as JSON in the dashboard's `settings` table, loaded once at startup into one shared instance that `TelegramAdapter.send_message` reads on every send and the dashboard's `PUT` changes in place.

**Tech Stack:** Python 3.12, python-telegram-bot (`disable_notification`), aiosqlite, Starlette, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-26-telegram-quiet-hours-design.md`

## Global Constraints

- Default: enabled, `start="22:00"`, `end="09:00"`; host local time (`datetime.now()`), no time zone setting.
- Times are `"HH:MM"` strings, 00:00–23:59. Start inclusive, end exclusive. Start later than end crosses midnight. Start equal to end is refused.
- Settings key `quiet_hours`, value JSON `{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`; no row means the default.
- Only `send_message` changes; edits, button changes, deletes, typing are untouched.
- Committed text is public: synthetic names only (`acme/app`, `ACME-123`), no `/Users/...` paths.
- Tests: the main checkout's `.venv/bin/python -m pytest …`, run from the worktree root.

## Review Focus

- A window crossing midnight: 23:30 and 08:59 are quiet, 09:00 and 21:59 are not (Task 1).
- A window within one day (13:00–15:00): 14:00 quiet, 16:00 not (Task 1).
- Disabled: no message is silent at any hour (Tasks 1, 2).
- A save reaches the running bot without a restart: `PUT` changes the very object the adapter holds (Task 3).
- Bad input from the page or curl (`"25:00"`, `"9:00"`, start == end, `enabled: "yes"`, a missing key) is a 400 and changes nothing (Tasks 1, 3).

---

### Task 1: QuietHours and its storage

**Files:**
- Create: `integrations/telegram/quiet_hours.py`
- Modify: `dashboard/settings_store.py` (add get/set beside the model's)
- Test: `tests/unit/test_quiet_hours.py`

**Interfaces:**
- Produces: `@dataclass class QuietHours: enabled: bool = True; start: str = "22:00"; end: str = "09:00"`
  - `QuietHours.active(self, now: datetime.time) -> bool`
  - `QuietHours.from_dict(data: object) -> QuietHours` (classmethod; raises `ValueError` with a readable message on any invalid input)
  - `QuietHours.to_dict(self) -> dict` (`dataclasses.asdict`)
  - `QuietHours.update(self, other: QuietHours) -> None` (copy fields in place)
- Produces in `dashboard/settings_store.py`: `async get_quiet_hours(db) -> QuietHours`, `async set_quiet_hours(db, quiet: QuietHours) -> None` (needs `init_settings` run; upsert like `set_model`).

- [ ] **Step 1: Write the failing tests** — `test_default_is_on_22_to_9`; `test_crossing_midnight` (`active` True at 22:00, 23:30, 00:00, 08:59; False at 09:00, 12:00, 21:59); `test_within_one_day` (13:00–15:00: 13:00 and 14:00 True, 15:00 and 16:00 False); `test_disabled_is_never_active` (enabled False: 23:30 False); `test_from_dict_refuses` parametrized over `{"enabled": True, "start": "25:00", "end": "09:00"}`, `"9:00"`, `"22:60"`, start == end, `enabled: "yes"`, a missing key, a non-dict → `pytest.raises(ValueError)`; `test_storage_default_without_row` (`init_settings` then `get_quiet_hours` == `QuietHours()`); `test_storage_round_trip` (`set_quiet_hours(QuietHours(False, "23:00", "07:30"))` then get returns it).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_quiet_hours.py -v` — FAIL (module missing).
- [ ] **Step 3: Implement.** Parse with `re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s)`; `enabled` must be `bool` exactly. `active` compares `datetime.time` objects: `start <= now < end` when start < end, else `now >= start or now < end`.
- [ ] **Step 4: Run** the same command — PASS.
- [ ] **Step 5: Commit** `feat(telegram): quiet hours setting and its storage`.

### Task 2: Silent sends in the window

**Files:**
- Modify: `integrations/telegram/telegram_adapter.py` (`__init__`, `send_message`)
- Test: `tests/unit/test_telegram_adapter.py`

**Interfaces:**
- Consumes: `QuietHours` from Task 1.
- Produces: `TelegramAdapter.quiet_hours: QuietHours` (public attribute, default `QuietHours()`); module function `_local_time() -> datetime.time` (`datetime.now().time()`), patched by tests.

- [ ] **Step 1: Write the failing tests** (mock the Bot as `test_send_message_passes_reply_markup` does; patch `integrations.telegram.telegram_adapter._local_time`): `test_send_in_quiet_hours_is_silent` (23:30 → `send_message` kwargs have `disable_notification is True`, also with buttons and `reply_to_message_id`); `test_send_outside_quiet_hours_notifies` (12:00 → no `disable_notification` key); `test_send_with_quiet_hours_off_notifies` (`adapter.quiet_hours = QuietHours(enabled=False)`, 23:30 → no key); `test_silent_send_is_logged` (caplog: the `TG out send` JSON has `"silent": true`; outside the window no `silent` field).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_telegram_adapter.py -v` — the new tests FAIL.
- [ ] **Step 3: Implement.** In `send_message`, when `self.quiet_hours.active(_local_time())` set `kwargs["disable_notification"] = True` and pass `silent=True` to `_call` (None otherwise, so `_traffic` leaves it out). One line in the docstring.
- [ ] **Step 4: Run** the same command — PASS (all of the file).
- [ ] **Step 5: Commit** `feat(telegram): messages in quiet hours go out silent`.

### Task 3: Dashboard setting, wiring, docs

**Files:**
- Modify: `dashboard/web.py` (`create_app` param `quiet_hours: Any | None = None`; two routes beside `/api/settings/model`)
- Modify: `main.py` (after `init_settings`, ~line 1131; `create_app(...)` call, ~line 1220)
- Modify: `dashboard/static/js/settings.js` (a `renderQuietHours(el)` section after the language one)
- Modify: `docs/dashboard.md` (Settings view bullet; REST API line), `docs/telegram.md` (a `## Quiet hours` section after `## Language`; `silent` in the traffic-log table row for `TG out send`)
- Test: `tests/unit/test_settings_api.py`

**Interfaces:**
- Consumes: `QuietHours`, `get_quiet_hours`, `set_quiet_hours` (Task 1); `TelegramAdapter.quiet_hours` (Task 2).
- Produces: `GET /api/settings/quiet-hours` → `{"enabled", "start", "end"}` (from the DB); `PUT` with the same body → 200 with the saved value, 400 `{"error": <ValueError text>}` on invalid input or invalid JSON. On success it stores and, when `create_app` got a `quiet_hours` object, calls `.update()` on it.

- [ ] **Step 1: Write the failing tests** in `test_settings_api.py`: `test_get_quiet_hours_default` (`{"enabled": True, "start": "22:00", "end": "09:00"}`); `test_put_quiet_hours_persists_and_applies` (client built with `create_app(bus, store, quiet_hours=live)`; PUT `{"enabled": True, "start": "23:00", "end": "07:30"}` → 200, a GET returns it, and `live == QuietHours(True, "23:00", "07:30")`); `test_put_quiet_hours_refuses_bad_input` (PUT start == end → 400, `"error"` in the body, a GET still returns the default and `live` unchanged).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_settings_api.py -v` — new tests FAIL (404).
- [ ] **Step 3: Implement the routes.** Same shape as `get_settings_model` / `put_settings_model` (`aiosqlite.connect(store._db_path)`).
- [ ] **Step 4: Run** the same command — PASS.
- [ ] **Step 5: Wire main.py.** In the `async with _aiosqlite.connect(db_path)` block after `init_settings`: `quiet_hours = await get_quiet_hours(_conn)`; then `if isinstance(notifier, TelegramAdapter): notifier.quiet_hours = quiet_hours`; pass `quiet_hours=quiet_hours` to `create_app`. (Messages sent in the seconds before this load use the default; accepted.)
- [ ] **Step 6: The page.** Section title `Quiet hours`; help: `Inside this window the bot's messages arrive without sound or vibration (Telegram's silent messages). Host local time; a window that starts later than it ends runs past midnight. Applies to the next message.`; a row with a checkbox `On`, `<input type="time">` `From` and `To`, `Save` (disabled until something changes) and the status pill, as `renderLanguage` does.
- [ ] **Step 7: Docs** as listed under Files: the Settings bullet (what it does, the default, stored in the `settings` table, no restart), the REST line (shape, 400), the Telegram section (silent, not held; default; no exceptions; set in Settings), and `silent` in the traffic-log row.
- [ ] **Step 8: Full suite** `timeout 300 .venv/bin/python -m pytest tests --ignore=tests/e2e -q > <scratch>/pytest.log 2>&1`; the summary shows only the 9 known environment failures.
- [ ] **Step 9: Look at the page** in a headless browser against a throwaway server on a copy of the DB: section renders, Save stores, reload shows the stored value.
- [ ] **Step 10: Commit** `feat(dashboard): quiet hours in Settings` with the spec and this plan.

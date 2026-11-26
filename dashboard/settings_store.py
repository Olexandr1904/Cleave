"""Runtime settings store backed by the dashboard's SQLite database.

Single source of truth for the active Claude model and the Telegram quiet hours. Lives in the same
SQLite file as the event store (data/events.db) — see DashboardConfig.db_path.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from integrations.telegram.quiet_hours import QuietHours

logger = logging.getLogger(__name__)

ALLOWED_MODELS: tuple[str, ...] = (
    "claude-fable-5-2",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
)
DEFAULT_MODEL = "claude-sonnet-5-5"

# Models the list offered before, each with the model that took its place. A stored value moves on at startup.
_SUCCESSORS = {
    "claude-opus-4-7": "claude-opus-5-5",
    "claude-sonnet-4-6": "claude-sonnet-5-5",
    "claude-haiku-4-5-20251001": "claude-haiku-5-5",
}

_MODEL_KEY = "model"
_QUIET_HOURS_KEY = "quiet_hours"


async def init_settings(db: aiosqlite.Connection) -> None:
    """Create the settings table if missing, seed the model row if empty, and move a stored model that is
    no longer offered to its successor (or the default)."""
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
    stored = await get_model(db)
    if stored not in ALLOWED_MODELS:
        await set_model(db, _SUCCESSORS.get(stored, DEFAULT_MODEL))


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


async def get_quiet_hours(db: aiosqlite.Connection) -> QuietHours:
    """The stored quiet hours, the default (22:00–09:00, on) when none were saved or the stored value does not read
    (the daemon must start whatever the row holds; a save in Settings replaces it). Caller must have run
    init_settings first."""
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (_QUIET_HOURS_KEY,))
    if not rows:
        return QuietHours()
    try:
        return QuietHours.from_dict(json.loads(rows[0][0]))
    except ValueError as e:  # JSONDecodeError too
        logger.warning("settings.quiet_hours %r does not read (%s): using the default", rows[0][0], e)
        return QuietHours()


async def set_quiet_hours(db: aiosqlite.Connection, quiet: QuietHours) -> None:
    """Persist the quiet hours."""
    await db.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_QUIET_HOURS_KEY, json.dumps(quiet.to_dict())),
    )
    await db.commit()

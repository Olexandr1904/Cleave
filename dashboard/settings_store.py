"""Runtime settings store backed by the dashboard's SQLite database.

Single source of truth for the active Claude model. Lives in the same
SQLite file as the event store (data/events.db) — see DashboardConfig.db_path.
"""

from __future__ import annotations

import aiosqlite

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

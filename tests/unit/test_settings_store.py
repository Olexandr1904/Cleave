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


def test_default_model_is_in_allowed_list():
    assert DEFAULT_MODEL in ALLOWED_MODELS

"""Tests for integrations/telegram/quiet_hours.py and its storage in dashboard/settings_store.py."""

from __future__ import annotations

from datetime import time

import aiosqlite
import pytest

from dashboard.settings_store import get_quiet_hours, init_settings, set_quiet_hours
from integrations.telegram.quiet_hours import QuietHours


def test_default_is_on_22_to_9():
    assert QuietHours() == QuietHours(enabled=True, start="22:00", end="09:00")


@pytest.mark.parametrize(("now", "quiet"), [
    (time(22, 0), True), (time(23, 30), True), (time(0, 0), True), (time(8, 59, 59), True),
    (time(9, 0), False), (time(12, 0), False), (time(21, 59, 59), False),
])
def test_crossing_midnight(now, quiet):
    assert QuietHours().active(now) is quiet


@pytest.mark.parametrize(("now", "quiet"), [
    (time(13, 0), True), (time(14, 0), True), (time(15, 0), False), (time(16, 0), False), (time(12, 59), False),
])
def test_within_one_day(now, quiet):
    assert QuietHours(start="13:00", end="15:00").active(now) is quiet


def test_disabled_is_never_active():
    assert QuietHours(enabled=False).active(time(23, 30)) is False


def test_from_dict_reads_a_valid_window():
    assert QuietHours.from_dict({"enabled": False, "start": "23:00", "end": "07:30"}) == QuietHours(
        enabled=False, start="23:00", end="07:30")


@pytest.mark.parametrize("data", [
    {"enabled": True, "start": "25:00", "end": "09:00"},
    {"enabled": True, "start": "9:00", "end": "22:00"},
    {"enabled": True, "start": "22:60", "end": "09:00"},
    {"enabled": True, "start": "22:00", "end": "22:00"},
    {"enabled": "yes", "start": "22:00", "end": "09:00"},
    {"enabled": True, "start": "22:00"},
    {"enabled": True, "start": 22, "end": 9},
    ["22:00", "09:00"],
])
def test_from_dict_refuses(data):
    with pytest.raises(ValueError):
        QuietHours.from_dict(data)


def test_update_copies_in_place():
    live = QuietHours()
    live.update(QuietHours(enabled=False, start="23:00", end="07:30"))
    assert live == QuietHours(enabled=False, start="23:00", end="07:30")


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "test.db"))
    await init_settings(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_storage_default_without_row(db):
    assert await get_quiet_hours(db) == QuietHours()


async def test_storage_round_trip(db):
    await set_quiet_hours(db, QuietHours(enabled=False, start="23:00", end="07:30"))
    assert await get_quiet_hours(db) == QuietHours(enabled=False, start="23:00", end="07:30")

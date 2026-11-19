"""A wake that arrives while a cycle runs starts the next cycle at once.

An operator's reply wakes the loop. Two replies came 13 s apart: the first started a cycle, the second arrived
during it, and its wake was cleared when that cycle ended — the second ticket waited out the whole poll interval.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from orchestrator.runtime import Runtime


async def test_a_wake_during_a_cycle_is_not_lost():
    wm = MagicMock()
    wm.discover_workspaces.return_value = []
    rt = Runtime(
        SimpleNamespace(
            defaults=SimpleNamespace(max_parallel_tickets=3, max_parallel_builds=1, poll_interval_seconds=60),
            workspaces=SimpleNamespace(max_age_days=7),
        ),
        wm, poll_callback=AsyncMock(), advance_callback=AsyncMock(),
    )
    cycles = 0

    async def poll_cycle():
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            rt.wake()       # a reply lands while this cycle is running
        else:
            rt.shutdown()

    rt.poll_cycle = poll_cycle

    await asyncio.wait_for(rt.run(), timeout=5)   # the poll interval is 60 s

    assert cycles == 2

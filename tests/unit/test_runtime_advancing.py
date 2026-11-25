"""A stage that runs for hours does not hold up the other tickets.

The cycle awaited every active ticket's stage. While one ticket's QA agent ran (up to four hours), a Retry pressed on
another ticket waited for it, as did the next stage of every ticket whose stage had ended, new tickets from the
tracker and deferred tickets due to resume.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from orchestrator.runtime import Runtime
from workspace.workspace import Stage


def _ws(ticket: str, state: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(company_id="acme", repo_id="r", current_state=state, ticket_id=ticket))


def _runtime(advance, workspaces=()) -> Runtime:
    wm = MagicMock()
    wm.cleanup_old_workspaces.return_value = []
    wm.discover_workspaces.return_value = list(workspaces)
    rt = Runtime(
        SimpleNamespace(
            defaults=SimpleNamespace(max_parallel_tickets=3, max_parallel_builds=1, poll_interval_seconds=60),
            workspaces=SimpleNamespace(max_age_days=7),
        ),
        wm, poll_callback=AsyncMock(), advance_callback=advance,
    )
    rt.reconcile_disk_workspaces = lambda: None
    rt._active_workspaces = list(workspaces)
    return rt


async def test_a_cycle_does_not_wait_for_a_running_stage():
    release, retried = asyncio.Event(), asyncio.Event()

    async def advance(ws):
        if ws.state.ticket_id == "LONG":
            await release.wait()
        else:
            retried.set()

    rt = _runtime(advance, [_ws("LONG", Stage.QA)])

    await asyncio.wait_for(rt.poll_cycle(), timeout=1)
    rt._active_workspaces.append(_ws("RETRIED", Stage.PUSHED))   # the operator pressed Retry meanwhile
    await asyncio.wait_for(rt.poll_cycle(), timeout=1)
    await asyncio.wait_for(retried.wait(), timeout=1)

    release.set()
    await rt.wait_for_advances()


async def test_a_ticket_whose_stage_still_runs_is_not_started_again():
    release = asyncio.Event()
    started: list[str] = []

    async def advance(ws):
        started.append(ws.state.ticket_id)
        await release.wait()

    rt = _runtime(advance, [_ws("LONG", Stage.QA)])

    await asyncio.wait_for(rt.poll_cycle(), timeout=1)
    await asyncio.sleep(0)
    await asyncio.wait_for(rt.poll_cycle(), timeout=1)
    await asyncio.sleep(0)

    assert started == ["LONG"]
    release.set()
    await rt.wait_for_advances()


async def test_a_ticket_starts_again_once_its_stage_has_ended():
    started: list[str] = []

    async def advance(ws):
        started.append(ws.state.ticket_id)

    rt = _runtime(advance, [_ws("T-1", Stage.DEV)])

    await rt.poll_cycle()
    await rt.wait_for_advances()
    await rt.poll_cycle()
    await rt.wait_for_advances()

    assert started == ["T-1", "T-1"]


async def test_shutdown_waits_for_the_stages_still_running():
    release = asyncio.Event()
    finished: list[str] = []

    async def advance(ws):
        await release.wait()
        finished.append(ws.state.ticket_id)

    rt = _runtime(advance, [_ws("LONG", Stage.QA)])
    run = asyncio.create_task(rt.run())
    await asyncio.sleep(0.05)       # the first cycle has started LONG's stage

    rt.shutdown()
    await asyncio.sleep(0.05)
    assert not run.done()

    release.set()
    await asyncio.wait_for(run, timeout=1)
    assert finished == ["LONG"]


async def test_a_ticket_a_cycle_found_busy_wakes_the_loop_when_its_stage_ends():
    """A Retry pressed while the ticket's last stage still finished (a Telegram send) found it busy; nothing else
    would start it before the poll interval."""
    release = asyncio.Event()

    async def advance(ws):
        await release.wait()

    rt = _runtime(advance, [_ws("T-1", Stage.PUSHED)])
    await rt.poll_cycle()
    await asyncio.sleep(0)
    await rt.poll_cycle()             # the operator's Retry woke a cycle; the ticket was busy
    rt._wake_event.clear()

    release.set()
    await rt.wait_for_advances()

    assert rt._wake_event.is_set()


async def test_a_stage_no_cycle_found_busy_does_not_wake_the_loop():
    """Waiting states (PR review, an approval) end at once every cycle: a wake from each would spin the loop."""
    async def advance(ws):
        pass

    rt = _runtime(advance, [_ws("T-1", Stage.PR_REVIEW)])
    await rt.poll_cycle()
    await rt.wait_for_advances()

    assert not rt._wake_event.is_set()

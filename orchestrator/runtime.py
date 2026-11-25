"""Daemon runtime: signals, poll loop, semaphore, shutdown.

Owns the long-running state of the daemon process — the active-workspace
list, completion ring buffer, asyncio events, agent semaphore — and runs
the poll loop. The Orchestrator class wraps a Runtime instance.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from config.schemas import GlobalConfig
from workspace.workspace import Stage, Workspace
from workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

# Stages whose agent runs the build toolchain (gradle + Kotlin compiler daemon).
# These are throttled by the build semaphore on top of the general one.
_BUILD_STAGES = {Stage.DEV, Stage.QA}


def is_build_stage(state: Any) -> bool:
    """True if `state` is a build-heavy stage gated by the build semaphore."""
    return state in _BUILD_STAGES


class Runtime:
    """Daemon shell. Owns the event loop, semaphore, signal handling, and
    workspace-list lifecycle (active + recent_completions).

    The Orchestrator class instantiates Runtime and delegates run() and
    poll_cycle() to it. Callbacks plumbed in for per-cycle work that lives
    on the Orchestrator (poll_callback, advance_callback).
    """

    def __init__(
        self,
        global_config: GlobalConfig,
        workspace_manager: WorkspaceManager,
        *,
        poll_callback: Callable,
        advance_callback: Callable,
        rescan_callback: Callable | None = None,
        cleanup_callback: Callable | None = None,
        sweep_quota_window_callback: Callable | None = None,
        get_trackers: Callable | None = None,
        get_mode_handler: Callable | None = None,
        ticket_gate: Callable[[str, str], bool] | None = None,
        event_bus: Any | None = None,
        dry_run: bool = False,
    ) -> None:
        self._global_config = global_config
        self._workspace_manager = workspace_manager
        self._poll_callback = poll_callback
        self._advance_callback = advance_callback
        self._rescan_callback = rescan_callback
        self._cleanup_callback = cleanup_callback
        self._sweep_quota_window_callback = sweep_quota_window_callback
        self._get_trackers = get_trackers
        self._get_mode_handler = get_mode_handler
        self._ticket_gate = ticket_gate
        self._events = event_bus
        self._dry_run = dry_run

        self._active_workspaces: list[Workspace] = []
        # The task advancing each ticket, by ticket id, while its stage runs.
        self._advancing: dict[str, asyncio.Task] = {}
        # Tickets a cycle wanted to start while their stage still ran.
        self._found_busy: set[str] = set()
        self._recent_completions: deque[tuple[str, str, float]] = deque(maxlen=20)
        self._shutdown_event = asyncio.Event()
        self._wake_event = asyncio.Event()

        try:
            max_parallel = int(global_config.defaults.max_parallel_tickets)
        except (TypeError, ValueError, AttributeError):
            max_parallel = 3
        self._agent_semaphore = asyncio.Semaphore(max_parallel)

        # Build-heavy stages (dev, qa) run gradle + the Kotlin compiler daemon and
        # are gated by a stricter semaphore so concurrent builds don't OOM the
        # host. Light stages (BA, scope-guard) only take the general semaphore.
        try:
            max_builds = int(global_config.defaults.max_parallel_builds)
        except (TypeError, ValueError, AttributeError):
            max_builds = 1
        self._build_semaphore = asyncio.Semaphore(max(1, max_builds))

    @property
    def active_workspaces(self) -> list[Workspace]:
        return self._active_workspaces

    @property
    def recent_completions(self) -> list[tuple[str, str, float]]:
        return list(self._recent_completions)

    def wake(self) -> None:
        self._wake_event.set()

    def shutdown(self) -> None:
        self._shutdown_event.set()

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self.shutdown()

    def emit(self, event_type: str, message: str, **kwargs: Any) -> None:
        if self._events is not None:
            self._events.emit(event_type, message, **kwargs)

    def reconcile_disk_workspaces(self) -> None:
        """Sync in-memory workspace state with disk.

        - Re-adopt workspaces on disk that fell out of the active list
        - Refresh state for active workspaces (picks up dashboard retries,
          manual edits, TG replies that wrote to state.json)
        """
        disk_workspaces = {ws.state.ticket_id: ws for ws in self._workspace_manager.discover_workspaces()}
        active_ids = {ws.state.ticket_id for ws in self._active_workspaces}

        # Re-adopt orphans
        for tid, ws in disk_workspaces.items():
            if tid not in active_ids:
                self._active_workspaces.append(ws)
                logger.warning(
                    "Re-adopted orphaned workspace: %s (state=%s)",
                    tid, ws.state.current_state,
                )

        # Refresh state from disk for all active workspaces
        for i, ws in enumerate(self._active_workspaces):
            disk_ws = disk_workspaces.get(ws.state.ticket_id)
            if disk_ws and disk_ws.state.current_state != ws.state.current_state:
                logger.info(
                    "Refreshed %s state from disk: %s -> %s",
                    ws.state.ticket_id, ws.state.current_state, disk_ws.state.current_state,
                )
                self._active_workspaces[i] = disk_ws

    async def sweep_deferred(self) -> None:
        """Resume DEFERRED workspaces whose retry_at has passed.

        Called at the top of each poll cycle. Also clears the in-memory
        quota debounce window once its retry_at has passed.
        """
        now = datetime.now(timezone.utc)

        if self._sweep_quota_window_callback is not None:
            self._sweep_quota_window_callback(now)

        for ws in list(self._active_workspaces):
            if ws.state.current_state != Stage.DEFERRED:
                continue
            retry_at_str = ws.state.retry_at
            if not retry_at_str:
                continue
            try:
                retry_at = datetime.fromisoformat(retry_at_str)
            except ValueError:
                logger.warning(
                    "Workspace %s has malformed retry_at: %s",
                    ws.state.ticket_id, retry_at_str,
                )
                continue
            if retry_at <= now:
                target = ws.state.previous_state or Stage.ANALYSIS
                ws.transition(target)
                self.emit(
                    "deferred_resumed",
                    f"Resumed {ws.state.ticket_id} from DEFERRED to {target}",
                    project_id=ws.state.company_id,
                    ticket_id=ws.state.ticket_id,
                    data={"target_state": target},
                )

    async def _advance_one(self, ws: Workspace) -> None:
        """Advance a single workspace under the concurrency semaphores.

        All advances take the general semaphore; build-heavy stages (dev, qa)
        additionally take the stricter build semaphore so concurrent gradle /
        Kotlin-daemon builds don't exhaust host memory.
        """
        async with self._agent_semaphore:
            try:
                if is_build_stage(ws.state.current_state):
                    async with self._build_semaphore:
                        await self._advance_callback(ws)
                else:
                    await self._advance_callback(ws)
            except Exception as e:
                logger.error(
                    "Workspace %s error: %s",
                    ws.state.ticket_id, e, exc_info=True,
                )
                try:
                    ws.transition(Stage.FAILED)
                    ws.update_state(error=str(e))
                except Exception:
                    pass

    def _start_advancing(self, ws: Workspace) -> None:
        """Advance `ws` in a task of its own, unless a stage of it is still running."""
        key = ws.state.ticket_id
        if key in self._advancing:
            self._found_busy.add(key)
            return
        task = asyncio.create_task(self._advance_one(ws))
        self._advancing[key] = task

        def _ended(t: asyncio.Task) -> None:
            if self._advancing.get(key) is t:
                del self._advancing[key]
            # A cycle wanted it while it ran (a Retry pressed during the stage's last Telegram send, or its next
            # stage due): start that now, not after the poll interval. A stage no cycle found busy wakes nothing,
            # or waiting states, which end at once every cycle, would spin the loop.
            if key in self._found_busy:
                self._found_busy.discard(key)
                self.wake()

        task.add_done_callback(_ended)

    async def wait_for_advances(self) -> None:
        """Wait until every stage started so far has ended."""
        while self._advancing:
            await asyncio.gather(*self._advancing.values(), return_exceptions=True)

    async def poll_cycle(self) -> None:
        """Single poll + advance cycle. Starts the stages; does not wait for them."""
        # Pick up any projects added to config-live/ since last cycle (wizard or hand-edit).
        if self._rescan_callback is not None:
            await self._rescan_callback()
        self.emit("poll_cycle", "Poll cycle started")
        # 0. Re-adopt workspaces that exist on disk but fell out of the active list
        self.reconcile_disk_workspaces()
        # 0b. Resume any DEFERRED workspaces whose retry_at has passed
        await self.sweep_deferred()
        # 1. Poll for new tickets and create workspaces
        trackers = self._get_trackers() if self._get_trackers is not None else None
        if trackers:
            await self._poll_callback()

        # 2. Advance active workspaces in parallel (bounded by semaphores)
        # Skip workspaces in terminal or clearly waiting states
        _SKIP = {Stage.DONE, Stage.ARCHIVED, Stage.BLOCKED,
                 Stage.MANUAL_CONTROL, Stage.DEFERRED, Stage.FAILED, Stage.PAUSED}
        active = [ws for ws in self._active_workspaces if ws.state.current_state not in _SKIP]
        # Tickets paused for a repo: its workspaces wait (a running stage already finished).
        if self._ticket_gate is not None:
            active = [ws for ws in active
                      if self._ticket_gate(ws.state.company_id, ws.state.repo_id)]
        # Each in a task of its own that the cycle does not wait for: one ticket's stage can run for hours, and a
        # Retry, another ticket's next stage, new tickets and deferred ones waited with it.
        for ws in active:
            self._start_advancing(ws)

        # 3. Cleanup terminal workspaces from active list and record them for
        # /status to show recent completions even after they leave the list.
        terminal = {Stage.DONE, Stage.ARCHIVED}
        still_active: list[Workspace] = []
        now = time.time()
        for ws in self._active_workspaces:
            if ws.state.current_state in terminal:
                self._recent_completions.append(
                    (ws.state.ticket_id, ws.state.current_state, now),
                )
            else:
                still_active.append(ws)
        self._active_workspaces = still_active

        # 4. Workspace cleanup
        max_age = self._global_config.workspaces.max_age_days
        deleted = self._workspace_manager.cleanup_old_workspaces(max_age)
        if deleted:
            logger.info("Cleaned up %d old workspace(s)", len(deleted))

    async def run(self) -> None:
        """Main async loop — poll and advance until shutdown."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # Discover existing workspaces on startup
        self._active_workspaces = self._workspace_manager.discover_workspaces()
        if self._active_workspaces:
            logger.info(
                "Resumed %d active workspace(s) from disk",
                len(self._active_workspaces),
            )

        poll_interval = self._global_config.defaults.poll_interval_seconds
        logger.info(
            "Orchestrator started (poll_interval=%ds, dry_run=%s)",
            poll_interval, self._dry_run,
        )
        mode_handler = self._get_mode_handler() if self._get_mode_handler else None
        mode = mode_handler.get_mode() if mode_handler else "auto"
        self.emit(
            "daemon_started",
            f"Orchestrator started (mode={mode}, dry_run={self._dry_run})",
        )

        while not self._shutdown_event.is_set():
            # Cleared before the cycle, not after it: a wake that arrives while
            # the cycle runs (an operator's reply about another ticket) must
            # start the next cycle, not wait out the poll interval.
            self._wake_event.clear()
            try:
                await self.poll_cycle()
            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)

            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(self._shutdown_event.wait()),
                        asyncio.create_task(self._wake_event.wait()),
                    ],
                    timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel and reap the loser(s) so they don't leak across cycles.
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                for t in done:
                    t.result()  # suppress unhandled-task warnings
            except asyncio.TimeoutError:
                pass

        # As when the cycle awaited them: a shutdown lets the running stages end.
        await self.wait_for_advances()
        logger.info("Orchestrator shutting down gracefully")

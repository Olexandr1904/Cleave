"""Running/paused switches for the tickets and reviews modes."""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable

from config.schemas import LoadedProject

logger = logging.getLogger(__name__)

MODES = ("tickets", "reviews")
STATES = ("running", "paused")
GLOBAL = "*"
FIRST_BOOT = {"tickets": "paused", "reviews": "running"}


def scope_of(project_id: str, repo_id: str) -> str:
    return f"{project_id}/{repo_id}"


def switch_scopes(projects: dict[str, LoadedProject]) -> dict[str, list[str]]:
    tickets: list[str] = []
    reviews: list[str] = []
    for pid, proj in projects.items():
        for rid, repo in proj.repos.items():
            tickets.append(scope_of(pid, rid))
            if repo.reviewer.enabled:
                reviews.append(scope_of(pid, rid))
    return {"tickets": sorted(tickets), "reviews": sorted(reviews)}


class Switches:
    """In-memory switch rows, written through to reviewer.db on set."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str, str, str, str], None]] = []
        self._rows: dict[tuple[str, str], str] = {
            (m, s): st for m, s, st in conn.execute("SELECT mode, scope, state FROM switches")
        }
        for mode, state in FIRST_BOOT.items():
            if (mode, GLOBAL) not in self._rows:
                self._write(mode, GLOBAL, state, "boot")

    def _write(self, mode: str, scope: str, state: str, by: str) -> None:
        with self._lock:
            self._conn.execute(  # autocommit: connect() uses isolation_level=None
                "INSERT OR REPLACE INTO switches (mode, scope, state, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (mode, scope, state, datetime.now(timezone.utc).isoformat(), by),
            )
            self._rows[(mode, scope)] = state

    def get(self, mode: str, scope: str = GLOBAL) -> str:
        return self._rows.get((mode, scope), "running")

    def set(self, mode: str, state: str, scope: str = GLOBAL, by: str = "dashboard") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        if state not in STATES:
            raise ValueError(f"unknown state: {state!r}")
        self._write(mode, scope, state, by)
        for fn in self._listeners:
            try:
                fn(mode, scope, state, by)
            except Exception:
                logger.exception("switch listener failed")

    def is_running(self, mode: str, project_id: str, repo_id: str) -> bool:
        return (
            self.get(mode) == "running"
            and self.get(mode, scope_of(project_id, repo_id)) == "running"
        )

    def add_listener(self, fn: Callable[[str, str, str, str], None]) -> None:
        self._listeners.append(fn)

    def snapshot(self, scopes: dict[str, list[str]]) -> dict:
        out = {}
        for mode in MODES:
            glob = self.get(mode)
            repos = []
            for scope in scopes.get(mode, []):
                state = self.get(mode, scope)
                effective = "running" if glob == "running" and state == "running" else "paused"
                repos.append({"scope": scope, "state": state, "effective": effective})
            out[mode] = {"global": glob, "repos": repos}
        return out

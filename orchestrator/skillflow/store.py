"""SkillStore: skill runs and their reply anchors in reviewer.db."""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from uuid import uuid4

from orchestrator.skillflow.models import RUN_STATES, SkillRun

logger = logging.getLogger(__name__)

_COLUMNS = ("id", "scope", "ticket_id", "state", "retry_at", "unsent", "created_at", "updated_at")
_SELECT = f"SELECT {', '.join(_COLUMNS)}, data FROM skill_runs"
_UPSERT = (
    f"INSERT INTO skill_runs ({', '.join(_COLUMNS)}, data) VALUES ({', '.join('?' * (len(_COLUMNS) + 1))}) "
    f"ON CONFLICT (id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in (*_COLUMNS[1:], 'data'))}"
)
_FIELDS = frozenset(f.name for f in dataclasses.fields(SkillRun))
_DATA_FIELDS = _FIELDS - set(_COLUMNS)


class SkillStore:
    """Skill-flow state on reviewer.db's autocommit connection (connect() uses isolation_level=None)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    def add_run(self, scope: str, ticket_id: str, now: str, **data) -> SkillRun:
        run = SkillRun(uuid4().hex[:12], scope, ticket_id, "queued", "", now, now, **data)
        self.save(run, now)
        return run

    def run(self, run_id: str) -> SkillRun | None:
        """The run; None when there is none, or its data cannot be read."""
        found = self._conn.execute(f"{_SELECT} WHERE id = ?", (run_id,)).fetchone()
        return None if found is None else _readable(found)

    def runs_for_ticket(self, scope: str, ticket_id: str) -> list[SkillRun]:
        return self._select("WHERE scope = ? AND ticket_id = ?", (scope, ticket_id))

    def in_states(self, states: tuple[str, ...]) -> list[SkillRun]:
        return self._select(f"WHERE state IN ({', '.join('?' * len(states))})", states)

    def unsent_runs(self) -> list[SkillRun]:
        return self._select("WHERE unsent > 0", ())

    def save(self, run: SkillRun, now: str) -> None:
        """Write every column and the data: the run's updated_at becomes `now`, its unsent its outbox's length."""
        with self._lock:
            self._write(run, now)

    def claim(
        self, run_id: str, from_states: tuple[str, ...], now: str, state: str, **changes
    ) -> SkillRun | None:
        """Move the run from one of `from_states` to `state` with `changes`: only the first claim gets it, later
        ones get None."""
        unknown = sorted(set(changes) - _FIELDS)
        if unknown:
            raise AttributeError(f"SkillRun has no field {', '.join(unknown)}")
        if state not in RUN_STATES:
            raise ValueError(f"unknown run state {state!r}")
        # The read and the write in one lock hold: a reply and the runner cannot both take the run.
        with self._lock:
            run = self.run(run_id)
            if run is None or run.state not in from_states:
                return None
            run.state = state
            for name, value in changes.items():
                setattr(run, name, value)
            self._write(run, now)
        return run

    def drop_sent(self, run_id: str, count: int, now: str, ask_message_ids: list[int] | None = None) -> None:
        """Remove the first `count` entries of the run's outbox, and set its ask_message_ids when given, on the
        row as it is now: a reply may have taken the run while its message was going out."""
        with self._lock:
            run = self.run(run_id)
            if run is None:
                return
            del run.outbox[:count]
            if ask_message_ids is not None:
                run.ask_message_ids = list(ask_message_ids)
            self._write(run, now)

    def mark_parts(self, run_id: str, ids: list[int], now: str) -> None:
        """Add `ids` to the parts of the outbox's first entry that went out (its `ids`), on the row as it is now:
        the next send of that entry starts after them."""
        with self._lock:
            run = self.run(run_id)
            if run is None or not run.outbox:
                return
            first = run.outbox[0]
            first["ids"] = [*first.get("ids", []), *ids]
            self._write(run, now)

    def keep_translation(self, run_id: str, text: str, translated: str, now: str) -> None:
        """Put `translated` in place of the outbox's first entry, on the row as it is now, while that entry is still
        `text` with no part out: a send again then sends the same parts."""
        with self._lock:
            run = self.run(run_id)
            if run is None or not run.outbox:
                return
            first = run.outbox[0]
            if first.get("text") != text or first.get("ids"):
                return
            first["text"] = translated
            self._write(run, now)

    def add_anchor(self, chat_id: str, message_id: int, run_id: str, now: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO skill_anchors (chat_id, message_id, run_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, message_id, run_id, now),
            )

    def anchor(self, chat_id: str, message_id: int) -> str | None:
        found = self._conn.execute(
            "SELECT run_id FROM skill_anchors WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return None if found is None else found[0]

    def has_anchors(self) -> bool:
        return self._conn.execute("SELECT 1 FROM skill_anchors LIMIT 1").fetchone() is not None

    def _select(self, where: str, args: tuple) -> list[SkillRun]:
        # Oldest first; rowid breaks ties between runs created at the same `now`.
        sql = f"{_SELECT} {where} ORDER BY created_at, rowid"
        return [run for run in map(_readable, self._conn.execute(sql, args)) if run is not None]

    def _write(self, run: SkillRun, now: str) -> None:
        # The caller holds the lock. An upsert rather than INSERT OR REPLACE: the row keeps its rowid, which
        # breaks created_at ties.
        run.updated_at, run.unsent = now, len(run.outbox)
        data = {k: v for k, v in dataclasses.asdict(run).items() if k in _DATA_FIELDS}
        self._conn.execute(_UPSERT, (*(getattr(run, c) for c in _COLUMNS), json.dumps(data)))


def _readable(row: tuple) -> SkillRun | None:
    # One damaged row must not stop every read that lists it, the runner's among them.
    try:
        return _load(row)
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("Skill run %s not read: %s", row[0], type(e).__name__)
        return None


def _load(row: tuple) -> SkillRun:
    # A field the stored JSON lacks (written by an older version) takes its default; a key no field has is
    # skipped.
    data = {k: v for k, v in json.loads(row[-1]).items() if k in _DATA_FIELDS}
    return SkillRun(**dict(zip(_COLUMNS, row[:-1])), **data)

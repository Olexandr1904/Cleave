"""ComparisonStore: comparisons of the staged and the skill flow on one ticket, in reviewer.db."""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from uuid import uuid4

from orchestrator.skillflow.compare_models import COMPARISON_STATES, ID_PREFIX, VERDICTS, Comparison

logger = logging.getLogger(__name__)

_COLUMNS = ("id", "scope", "ticket_id", "run_id", "state", "unsent", "created_at", "updated_at")
_SELECT = f"SELECT {', '.join(_COLUMNS)}, data FROM comparisons"
_UPSERT = (
    f"INSERT INTO comparisons ({', '.join(_COLUMNS)}, data) VALUES ({', '.join('?' * (len(_COLUMNS) + 1))}) "
    f"ON CONFLICT (id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in (*_COLUMNS[1:], 'data'))}"
)
_OLDEST_FIRST = "ORDER BY created_at, rowid"
_FIELDS = frozenset(f.name for f in dataclasses.fields(Comparison))
_DATA_FIELDS = _FIELDS - set(_COLUMNS)


class ComparisonStore:
    """Comparisons on reviewer.db's autocommit connection. There is no save of a Comparison object: every
    change reads and writes the row in one lock hold, so a tap and the keeper cannot overwrite each other."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    def add(self, scope: str, ticket_id: str, run_id: str, now: str, **data) -> Comparison:
        c = Comparison(ID_PREFIX + uuid4().hex[:12], scope, ticket_id, run_id, "open", now, now, **data)
        with self._lock:
            self._write(c, now)
        return c

    def get(self, cid: str) -> Comparison | None:
        """The comparison; None when there is none, or its data cannot be read."""
        found = self._conn.execute(f"{_SELECT} WHERE id = ?", (cid,)).fetchone()
        return None if found is None else _readable(found)

    def for_run(self, run_id: str) -> Comparison | None:
        return next(iter(self._select(f"WHERE run_id = ? {_OLDEST_FIRST}", (run_id,))), None)

    def for_workspace(self, scope: str, ticket_id: str, started_at: str) -> Comparison | None:
        found = self._select(f"WHERE scope = ? AND ticket_id = ? {_OLDEST_FIRST}", (scope, ticket_id))
        return next((c for c in found if c.workspace_started_at == started_at), None)

    def in_states(self, states: tuple[str, ...]) -> list[Comparison]:
        return self._select(f"WHERE state IN ({', '.join('?' * len(states))}) {_OLDEST_FIRST}", states)

    def open_tickets(self, scope: str) -> frozenset[str]:
        """The tickets of the scope's comparisons not closed yet: their PRs wait for the verdict."""
        rows = self._conn.execute("SELECT ticket_id FROM comparisons WHERE scope = ? AND state != 'closed'", (scope,))
        return frozenset(ticket for (ticket,) in rows)

    def unsent(self) -> list[Comparison]:
        return self._select(f"WHERE unsent > 0 {_OLDEST_FIRST}", ())

    def newest_first(self, limit: int = 200) -> list[Comparison]:
        return self._select("ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))

    def tally(self) -> dict[str, int]:
        """Every comparison counted once: for its verdict from the tap on, else as undecided. A row not open whose
        data cannot be read is skipped."""
        counts = dict.fromkeys((*VERDICTS, "undecided"), 0)
        for cid, state, data in self._conn.execute("SELECT id, state, data FROM comparisons"):
            verdict = ""
            if state != "open":
                try:
                    verdict = json.loads(data).get("verdict", "")
                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning("Comparison %s not read: %s", cid, type(e).__name__)
                    continue
            counts[verdict if verdict in VERDICTS else "undecided"] += 1
        return counts

    def claim(
        self, cid: str, from_states: tuple[str, ...], now: str, state: str, *,
        expect: dict | None = None, message: dict | None = None, **changes
    ) -> Comparison | None:
        """Move the comparison from one of `from_states` to `state` with `changes`, and append `message` to its
        outbox, when every `expect` field still has its value: only the first claim gets it, later ones get
        None."""
        unknown = sorted((set(changes) | set(expect or {})) - _FIELDS)
        if unknown:
            raise AttributeError(f"Comparison has no field {', '.join(unknown)}")
        if state not in COMPARISON_STATES:
            raise ValueError(f"unknown comparison state {state!r}")
        # The read and the write in one lock hold: a tap and the keeper cannot both take the comparison.
        with self._lock:
            c = self.get(cid)
            if c is None or c.state not in from_states:
                return None
            if any(getattr(c, name) != value for name, value in (expect or {}).items()):
                return None
            c.state = state
            for name, value in changes.items():
                setattr(c, name, value)
            if message is not None:
                c.outbox.append(message)
            self._write(c, now)
        return c

    def drop_sent(self, cid: str, count: int, now: str, ask_message_ids: list[int] | None = None) -> None:
        """Remove the first `count` entries of the outbox, and set ask_message_ids when given, on the row as it
        is now: a tap may have taken the comparison while its message was going out."""
        with self._lock:
            c = self.get(cid)
            if c is None:
                return
            del c.outbox[:count]
            if ask_message_ids is not None:
                c.ask_message_ids = list(ask_message_ids)
            self._write(c, now)

    def mark_parts(self, cid: str, ids: list[int], now: str) -> None:
        """Add `ids` to the parts of the outbox's first entry that went out (its `ids`), on the row as it is now:
        the next send of that entry starts after them."""
        with self._lock:
            c = self.get(cid)
            if c is None or not c.outbox:
                return
            first = c.outbox[0]
            first["ids"] = [*first.get("ids", []), *ids]
            self._write(c, now)

    def add_note(self, cid: str, text: str, now: str) -> Comparison | None:
        """Append `text` to the note, in any state: a note changes nothing else."""
        with self._lock:
            c = self.get(cid)
            if c is None:
                return None
            c.note = f"{c.note}\n\n{text}" if c.note else text
            self._write(c, now)
        return c

    def _select(self, rest: str, args: tuple) -> list[Comparison]:
        # rowid breaks ties between comparisons created at the same `now`.
        rows = self._conn.execute(f"{_SELECT} {rest}", args)
        return [c for c in map(_readable, rows) if c is not None]

    def _write(self, c: Comparison, now: str) -> None:
        # The caller holds the lock. An upsert rather than INSERT OR REPLACE: the row keeps its rowid, which
        # breaks created_at ties.
        c.updated_at, c.unsent = now, len(c.outbox)
        data = {k: v for k, v in dataclasses.asdict(c).items() if k in _DATA_FIELDS}
        self._conn.execute(_UPSERT, (*(getattr(c, col) for col in _COLUMNS), json.dumps(data)))


def _readable(row: tuple) -> Comparison | None:
    # One damaged row must not stop every read that lists it, the keeper's among them.
    try:
        return _load(row)
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("Comparison %s not read: %s", row[0], type(e).__name__)
        return None


def _load(row: tuple) -> Comparison:
    # A field the stored JSON lacks (written by an older version) takes its default; a key no field has is
    # skipped.
    data = {k: v for k, v in json.loads(row[-1]).items() if k in _DATA_FIELDS}
    return Comparison(**dict(zip(_COLUMNS, row[:-1])), **data)

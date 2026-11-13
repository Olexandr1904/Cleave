"""ReviewerStore: board rows, repo state, jobs, skips and notices in reviewer.db."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from uuid import uuid4

from orchestrator.reviewer.models import JOB_STATES, BoardRow, Job, RepoState

_JOB_COLUMNS = "id, scope, number, kind, head_sha, key, state, reason, created_at, updated_at"


class ReviewerStore:
    """Reviewer state on an autocommit connection (connect() uses isolation_level=None)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    def board(self, scope: str) -> dict[int, BoardRow]:
        rows = self._conn.execute(
            "SELECT number, data FROM board_prs WHERE scope = ? ORDER BY number", (scope,)
        )
        return {number: BoardRow.from_json(data) for number, data in rows}

    def replace_board(self, scope: str, rows: dict[int, BoardRow]) -> None:
        # Explicit BEGIN on the autocommit connection; `with conn` commits, or rolls back on error.
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM board_prs WHERE scope = ?", (scope,))
            self._conn.executemany(
                "INSERT INTO board_prs (scope, number, data) VALUES (?, ?, ?)",
                [(scope, number, row.to_json()) for number, row in rows.items()],
            )

    def repo_state(self, scope: str) -> RepoState:
        found = self._conn.execute(
            "SELECT data FROM repo_state WHERE scope = ?", (scope,)
        ).fetchone()
        if found is None:
            return RepoState(scope)
        known = {f.name for f in dataclasses.fields(RepoState)}
        return RepoState(**{k: v for k, v in json.loads(found[0]).items() if k in known})

    def save_repo_state(self, state: RepoState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO repo_state (scope, data) VALUES (?, ?)",
                (state.scope, json.dumps(dataclasses.asdict(state))),
            )

    def add_job(self, scope: str, number: int, kind: str, head_sha: str, key: str, now: str) -> Job:
        job = Job(uuid4().hex[:12], scope, number, kind, head_sha, key, "queued", "", now, now)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO jobs ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                dataclasses.astuple(job),
            )
        return job

    def jobs(self, scope: str, number: int | None = None) -> list[Job]:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE scope = ?"
        args: tuple = (scope,)
        if number is not None:
            sql += " AND number = ?"
            args += (number,)
        # Newest first; rowid breaks ties between jobs created at the same `now`.
        sql += " ORDER BY created_at DESC, rowid DESC"
        return [Job(*r) for r in self._conn.execute(sql, args)]

    def set_job_state(self, job_id: str, state: str, reason: str, now: str) -> None:
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state = ?, reason = ?, updated_at = ? WHERE id = ?",
                (state, reason, now, job_id),
            )

    def skip(self, scope: str, number: int, head_sha: str, now: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO skips (scope, number, head_sha, created_at) VALUES (?, ?, ?, ?)",
                (scope, number, head_sha, now),
            )

    def is_skipped(self, scope: str, number: int, head_sha: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM skips WHERE scope = ? AND number = ? AND head_sha = ?",
            (scope, number, head_sha),
        ).fetchone() is not None

    def notice_sent(self, scope: str, number: int, kind: str, key: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM notices WHERE scope = ? AND number = ? AND kind = ? AND key = ?",
            (scope, number, kind, key),
        ).fetchone() is not None

    def mark_notice(self, scope: str, number: int, kind: str, key: str, now: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO notices (scope, number, kind, key, sent_at) VALUES (?, ?, ?, ?, ?)",
                (scope, number, kind, key, now),
            )

    def clear_notice(self, scope: str, number: int, kind: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM notices WHERE scope = ? AND number = ? AND kind = ?",
                (scope, number, kind),
            )

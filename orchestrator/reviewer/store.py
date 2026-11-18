"""ReviewerStore: board rows, repo state, jobs, job runs, skips, notices, anchors, preparation
hashes, posts and confirmations in reviewer.db."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from uuid import uuid4

from orchestrator.reviewer.models import (
    JOB_STATES,
    Anchor,
    BoardRow,
    Confirmation,
    Job,
    JobRun,
    Post,
    RepoState,
)
from orchestrator.reviewer.report import Report

_JOB_COLUMNS = (
    "id, scope, number, kind, head_sha, key, state, reason, created_at, updated_at, retry_at"
)
_CONFIRMATION_COLUMNS = "id, job_id, chat_id, state, created_at, data"
_LANDED_STATES = ("posted", "partial")  # a post that landed, in full or in part
# A replies Post records only the steps GitHub showed, so its landed items count in these final states too;
# an interrupted one records none.
_REPLY_LANDED_STATES = ("posted", "partial", "failed", "unverified")


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
                f"INSERT INTO jobs ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    def job(self, job_id: str) -> Job | None:
        found = self._conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return None if found is None else Job(*found)

    def jobs_in_state(self, state: str, kinds: tuple[str, ...] | None = None) -> list[Job]:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state = ?"
        args: tuple = (state,)
        if kinds is not None:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY created_at, rowid"
        return [Job(*r) for r in self._conn.execute(sql, args)]

    def small_jobs(self) -> list[Job]:
        sql = (
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE kind IN ('edit', 'draft') AND state = 'queued' "
            "ORDER BY created_at, rowid"
        )
        return [Job(*r) for r in self._conn.execute(sql)]

    def runnable_jobs(self, now: str) -> list[Job]:
        # A queued job has no retry_at ("" sorts first) unless it waits for GitHub to be available again.
        sql = (
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE kind IN ('review', 'recheck', 'address') "
            "AND state IN ('queued', 'deferred') AND retry_at <= ? "
            "ORDER BY created_at, rowid"
        )
        return [Job(*r) for r in self._conn.execute(sql, (now,))]

    def set_job_state(
        self, job_id: str, state: str, reason: str, now: str, retry_at: str = ""
    ) -> None:
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state = ?, reason = ?, updated_at = ?, retry_at = ? WHERE id = ?",
                (state, reason, now, retry_at, job_id),
            )

    def rehead_job(self, job_id: str, head_sha: str, key: str, now: str) -> None:
        """Move a job to another head and key: an address job whose fix Cleave pushed."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET head_sha = ?, key = ?, updated_at = ? WHERE id = ?",
                (head_sha, key, now, job_id),
            )

    def save_run(self, run: JobRun) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO job_runs (job_id, data) VALUES (?, ?)",
                (run.job_id, run.to_json()),
            )

    def run(self, job_id: str) -> JobRun | None:
        found = self._conn.execute(
            "SELECT data FROM job_runs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if found is None else JobRun.from_json(found[0])

    def done_unsent(self) -> list[tuple[Job, JobRun]]:
        # No column of job_runs (job_id, data) shares a name with one of jobs. A job posted from the
        # first part of its message is still unsent: the other parts go out too.
        sql = (
            f"SELECT {_JOB_COLUMNS}, data FROM jobs "
            "JOIN job_runs ON job_runs.job_id = jobs.id "
            "WHERE kind IN ('review', 'recheck', 'address') AND state IN ('done', 'posted') "
            "AND json_extract(data, '$.message_sent') = 0 "
            "ORDER BY created_at, jobs.rowid"
        )
        return [(Job(*r[:-1]), JobRun.from_json(r[-1])) for r in self._conn.execute(sql)]

    def skip(self, scope: str, number: int, head_sha: str, now: str) -> None:
        """Skip the head; skipped again, it keeps the later time (see skipped_at)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO skips (scope, number, head_sha, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (scope, number, head_sha) DO UPDATE SET created_at = excluded.created_at",
                (scope, number, head_sha, now),
            )

    def is_skipped(self, scope: str, number: int, head_sha: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM skips WHERE scope = ? AND number = ? AND head_sha = ?",
            (scope, number, head_sha),
        ).fetchone() is not None

    def skipped_at(self, scope: str, number: int, head_sha: str) -> str:
        """When the head was last skipped; "" when it is not."""
        found = self._conn.execute(
            "SELECT created_at FROM skips WHERE scope = ? AND number = ? AND head_sha = ?",
            (scope, number, head_sha),
        ).fetchone()
        return "" if found is None else found[0]

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

    def add_anchor(
        self, chat_id: str, message_id: int, job_id: str, now: str, scope: str = "", number: int = 0
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO anchors (chat_id, message_id, job_id, created_at, scope, number) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, job_id, now, scope, number),
            )

    def anchor(self, chat_id: str, message_id: int) -> Anchor | None:
        found = self._conn.execute(
            "SELECT job_id, scope, number FROM anchors WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return None if found is None else Anchor(*found)

    def prepare_hash(self, scope: str) -> str:
        found = self._conn.execute(
            "SELECT hash FROM prepare_hashes WHERE scope = ?", (scope,)
        ).fetchone()
        return "" if found is None else found[0]

    def set_prepare_hash(self, scope: str, value: str, now: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO prepare_hashes (scope, hash, updated_at) VALUES (?, ?, ?)",
                (scope, value, now),
            )

    def save_post(self, post: Post) -> None:
        # An upsert rather than INSERT OR REPLACE: the row keeps its rowid, which breaks created_at ties.
        with self._lock:
            self._conn.execute(
                "INSERT INTO posts (id, scope, number, head_sha, state, data) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET scope = excluded.scope, number = excluded.number, "
                "head_sha = excluded.head_sha, state = excluded.state, data = excluded.data",
                (post.id, post.scope, post.number, post.head_sha, post.state, post.to_json()),
            )

    def post(self, post_id: str) -> Post | None:
        found = self._conn.execute("SELECT data FROM posts WHERE id = ?", (post_id,)).fetchone()
        return None if found is None else Post.from_json(found[0])

    def posts(self, scope: str, number: int) -> list[Post]:
        # Newest first; created_at is only in the JSON, rowid breaks ties.
        sql = (
            "SELECT data FROM posts WHERE scope = ? AND number = ? "
            "ORDER BY json_extract(data, '$.created_at') DESC, rowid DESC"
        )
        return [Post.from_json(data) for (data,) in self._conn.execute(sql, (scope, number))]

    def posts_in_state(self, state: str) -> list[Post]:
        sql = "SELECT data FROM posts WHERE state = ? ORDER BY json_extract(data, '$.created_at'), rowid"
        return [Post.from_json(data) for (data,) in self._conn.execute(sql, (state,))]

    def _landed_at(self, scope: str, number: int, head: str, posts: list[Post] | None = None) -> list[Post]:
        posts = self.posts(scope, number) if posts is None else posts
        return [p for p in posts if p.head_sha == head and p.state in _LANDED_STATES]

    def posted_keys(self, scope: str, number: int, head: str,
                    posts: list[Post] | None = None) -> set[tuple[str, int | None, str]]:
        """(path, line, title) of the report items that landed at the head, in a review. `posts`: the PR's posts,
        when the caller has read them (posts(scope, number))."""
        return {
            (item["path"], item["line"], item["title"])
            for p in self._landed_at(scope, number, head, posts)
            for item in p.landed
            if item["where"] in ("inline", "moved", "body")
        }

    def posted_count(self, scope: str, number: int, head: str) -> tuple[int, bool]:
        """The review comments that landed at the head, and whether a summary did: a re-check's replies are
        not counted."""
        landed = [p for p in self._landed_at(scope, number, head) if p.kind == "review"]
        return sum(p.comments for p in landed), any(p.summary_posted for p in landed)

    def cleave_review_ids(self, scope: str, number: int) -> set[int]:
        return {review_id for p in self.posts(scope, number) for review_id in p.review_ids}

    def posted_replies(self, job: Job, posts: list[Post] | None = None) -> set[int]:
        """The numbers of the job's replies and comments that landed, as GitHub showed them: from its PR's posts
        (`posts`, when the caller has read them)."""
        posts = self.posts(job.scope, job.number) if posts is None else posts
        return {
            item["n"]
            for p in posts
            if p.job_id == job.id and p.kind == "replies" and p.state in _REPLY_LANDED_STATES
            for item in p.landed
            if item["where"] in ("reply", "comment")
        }

    def push_of(self, job: Job) -> Post | None:
        """The newest push of the job's fix that landed; None when there is none."""
        return next((p for p in self.posts(job.scope, job.number)
                     if p.job_id == job.id and p.kind == "push" and p.state == "posted"), None)

    def posted_numbers(self, job: Job, report: Report | None, posts: list[Post] | None = None) -> set[int]:
        """The numbers of the job that landed: a re-check's or an address job's replies, or the items of a
        review's `report` that landed at its head (by path, line and title). The one test of "already posted".
        `posts`: the job's PR's posts, when the caller has read them."""
        if job.kind in ("recheck", "address"):
            return self.posted_replies(job, posts)
        keys = self.posted_keys(job.scope, job.number, job.head_sha, posts)
        items = report.items if report else ()
        return {item.number for item in items if (item.path, item.line, item.title) in keys}

    def add_confirmation(self, job_id: str, chat_id: str, command: dict, now: str) -> Confirmation:
        conf = Confirmation(uuid4().hex[:12], job_id, chat_id, "pending", now, command)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO confirmations ({_CONFIRMATION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                (conf.id, job_id, chat_id, conf.state, now, json.dumps(command)),
            )
        return conf

    def confirmation(self, conf_id: str) -> Confirmation | None:
        found = self._conn.execute(
            f"SELECT {_CONFIRMATION_COLUMNS} FROM confirmations WHERE id = ?", (conf_id,)
        ).fetchone()
        return None if found is None else Confirmation(*found[:-1], json.loads(found[-1]))

    def claim_confirmation(self, conf_id: str, state: str) -> Confirmation | None:
        """Move a pending confirmation to `state`: only the first claim gets it, later ones get None."""
        with self._lock:
            claimed = self._conn.execute(
                "UPDATE confirmations SET state = ? WHERE id = ? AND state = 'pending'", (state, conf_id)
            ).rowcount
            if not claimed:
                return None
            found = self._conn.execute(
                f"SELECT {_CONFIRMATION_COLUMNS} FROM confirmations WHERE id = ?", (conf_id,)
            ).fetchone()
        return Confirmation(*found[:-1], json.loads(found[-1]))

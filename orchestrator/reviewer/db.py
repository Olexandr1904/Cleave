"""reviewer.db: the reviewer-mode SQLite store and its migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config.schemas import ReviewerGlobalConfig

# Entry i brings PRAGMA user_version to i+1.
MIGRATIONS: list[str] = [
    "CREATE TABLE switches (mode TEXT NOT NULL, scope TEXT NOT NULL, "
    "state TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL, "
    "PRIMARY KEY (mode, scope))",
    "CREATE TABLE board_prs (scope TEXT NOT NULL, number INTEGER NOT NULL, data TEXT NOT NULL, "
    "PRIMARY KEY (scope, number)); "
    "CREATE TABLE repo_state (scope TEXT PRIMARY KEY, data TEXT NOT NULL); "
    "CREATE TABLE jobs (id TEXT PRIMARY KEY, scope TEXT NOT NULL, number INTEGER NOT NULL, "
    "kind TEXT NOT NULL, head_sha TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL, "
    "reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL); "
    "CREATE INDEX jobs_pr ON jobs (scope, number); "
    "CREATE TABLE skips (scope TEXT NOT NULL, number INTEGER NOT NULL, head_sha TEXT NOT NULL, "
    "created_at TEXT NOT NULL, PRIMARY KEY (scope, number, head_sha)); "
    "CREATE TABLE notices (scope TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL, "
    "key TEXT NOT NULL, sent_at TEXT NOT NULL, PRIMARY KEY (scope, number, kind, key))",
    "ALTER TABLE jobs ADD COLUMN retry_at TEXT NOT NULL DEFAULT ''; "
    "CREATE TABLE job_runs (job_id TEXT PRIMARY KEY, data TEXT NOT NULL); "
    "CREATE TABLE anchors (chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, "
    "job_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (chat_id, message_id)); "
    "CREATE TABLE prepare_hashes (scope TEXT PRIMARY KEY, hash TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)",
    "ALTER TABLE anchors ADD COLUMN scope TEXT NOT NULL DEFAULT ''; "
    "ALTER TABLE anchors ADD COLUMN number INTEGER NOT NULL DEFAULT 0; "
    "CREATE TABLE posts (id TEXT PRIMARY KEY, scope TEXT NOT NULL, number INTEGER NOT NULL, "
    "head_sha TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL); "
    "CREATE INDEX posts_pr ON posts (scope, number); "
    "CREATE TABLE confirmations (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, chat_id TEXT NOT NULL, "
    "state TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE skill_runs (id TEXT PRIMARY KEY, scope TEXT NOT NULL, ticket_id TEXT NOT NULL, "
    "state TEXT NOT NULL, retry_at TEXT NOT NULL DEFAULT '', unsent INTEGER NOT NULL DEFAULT 0, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL); "
    "CREATE INDEX skill_runs_ticket ON skill_runs (scope, ticket_id); "
    "CREATE TABLE skill_anchors (chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, run_id TEXT NOT NULL, "
    "created_at TEXT NOT NULL, PRIMARY KEY (chat_id, message_id))",
    "CREATE TABLE comparisons (id TEXT PRIMARY KEY, scope TEXT NOT NULL, ticket_id TEXT NOT NULL, "
    "run_id TEXT NOT NULL, state TEXT NOT NULL, unsent INTEGER NOT NULL DEFAULT 0, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL); "
    "CREATE INDEX comparisons_ticket ON comparisons (scope, ticket_id); "
    "CREATE INDEX comparisons_run ON comparisons (run_id)",
]


def reviewer_db_path(cfg: ReviewerGlobalConfig) -> Path:
    if cfg.db_path:
        return Path(cfg.db_path)
    return Path.home() / "CleaveData" / "reviewer" / "reviewer.db"


def connect(path: Path) -> sqlite3.Connection:
    """Open reviewer.db, creating its directory, and apply pending migrations."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # isolation_level=None: no implicit transactions, each migration BEGINs explicitly.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(MIGRATIONS)):
        try:
            # One transaction per migration: the DDL and the version bump commit together.
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[i]};\nPRAGMA user_version = {i + 1};\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    return conn

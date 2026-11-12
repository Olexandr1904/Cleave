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

"""
db/database.py — Minimal SQLite connection layer (MVP)

Will be replaced by SQLAlchemy Core in db/engine.py for V1 (PG portability),
but this lightweight wrapper is sufficient for MVP and lets us iterate fast.

Pattern repris d'icd11pycode (db/database.py) avec adaptations transcomonitor.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

_db_path: Optional[str] = None
_schema_path = str(Path(__file__).parent / "schema_sqlite.sql")


def set_db_path(path: str) -> None:
    """Set the database file path (call once at app startup)."""
    global _db_path
    _db_path = path


def get_db_path() -> str:
    """Resolve the DB path. Priority: explicit set > env var > local default."""
    if _db_path is not None:
        return _db_path
    env_path = os.environ.get("TRANSCOMONITOR_DB_PATH")
    if env_path:
        return env_path
    return str(Path(__file__).parent.parent / "transcomonitor.sqlite")


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, FK enforcement and dict rows.

    `check_same_thread=False` is required because Shiny runs handlers in a
    thread pool. A single connection can be shared across threads as long as
    each query is committed/closed properly.
    """
    con = sqlite3.connect(get_db_path(), check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(con: Optional[sqlite3.Connection] = None) -> None:
    """Initialize the database schema if not yet present.

    Also applies incremental migrations for schema changes introduced after
    the initial release. New columns added via ALTER TABLE on existing DBs
    so we don't break in-place upgrades.
    """
    close = False
    if con is None:
        con = get_connection()
        close = True
    with open(_schema_path, encoding="utf-8") as f:
        con.executescript(f.read())
    _migrate(con)
    con.commit()
    if close:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    """Apply incremental schema migrations to existing DBs.

    New columns added after first release :
      - mappings.target_label (TEXT) — denormalized target label fallback (plan §16.1)
    """
    # mappings.target_label
    cols = {r[1] for r in con.execute("PRAGMA table_info(mappings)").fetchall()}
    if "target_label" not in cols:
        con.execute("ALTER TABLE mappings ADD COLUMN target_label TEXT")
        con.commit()


def is_db_initialized(con: Optional[sqlite3.Connection] = None) -> bool:
    """Return True if the core tables are present."""
    close = False
    if con is None:
        con = get_connection()
        close = True
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mappings'"
        ).fetchone()
        return row is not None
    finally:
        if close:
            con.close()

"""
db/engine.py — SQLAlchemy Core engine factory for transcomonitor.

Provides a thin SQLAlchemy abstraction over the existing sqlite3 layer
(db/database.py) so the codebase can migrate to PostgreSQL (V1) without
rewriting every CRUD function.

Strategy :
  - sqlite3 (db/database.py) remains the canonical layer for the MVP : it's
    battle-tested, supports WAL+FK, and the raw-SQL queries in db/models.py
    are clear and fast.
  - SQLAlchemy Core is added for :
      * portability of SQL (sqlite3 → psycopg via DSN switch)
      * reflection-based access to all tables (no boilerplate Column declarations)
      * connection pooling (for PG)
      * complex queries (joins, CTEs) where raw SQL gets cluttered
  - Both layers coexist : raw sqlite3 for simple CRUD, SQLAlchemy for complex queries.

DSN resolution (priority order) :
  1. Explicit parameter to get_engine(dsn=...)
  2. DATABASE_URL env var (postgresql+psycopg://… for V1)
  3. TRANSCOMONITOR_DB_PATH env var → sqlite:///path
  4. Default → sqlite:///<repo>/transcomonitor.sqlite

Reflection :
  load_metadata(engine) reads the schema from the live DB so we get
  Table objects for every table declared in schema_sqlite.sql, without
  having to redeclare them. Useful for ad-hoc queries.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.pool import StaticPool

from db.database import get_db_path

_engine: Optional[Engine] = None
_engine_lock = threading.Lock()
_metadata: Optional[MetaData] = None


def resolve_dsn(dsn: Optional[str] = None) -> str:
    """Resolve the DSN to use. Priority : explicit > DATABASE_URL > sqlite path."""
    if dsn:
        return dsn
    env_dsn = os.environ.get("DATABASE_URL")
    if env_dsn:
        return env_dsn
    # SQLite default — derives from db.database.get_db_path()
    sqlite_path = get_db_path()
    return f"sqlite:///{sqlite_path}"


def _on_connect_sqlite(dbapi_connection, connection_record):
    """Enforce SQLite PRAGMAs on every new raw connection.

    Crucial : WAL mode and FK enforcement are PER-CONNECTION in SQLite,
    so even with connection pooling we need to set them every time."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def get_engine(dsn: Optional[str] = None, *, force_new: bool = False) -> Engine:
    """Return a singleton SQLAlchemy engine. Pass force_new=True to recreate
    (useful for tests that switch DBs)."""
    global _engine, _metadata
    with _engine_lock:
        if _engine is not None and not force_new:
            return _engine

        resolved = resolve_dsn(dsn)

        if resolved.startswith("sqlite"):
            # SQLite : check_same_thread=False to allow Shiny's thread pool
            # to share the connection. StaticPool ensures we keep ONE connection
            # (mandatory for SQLite in-memory ; recommended on file-based too
            # to avoid concurrent-write issues — but we don't use memory:// for
            # the MVP, file mode is fine with a small pool).
            _engine = create_engine(
                resolved,
                connect_args={"check_same_thread": False, "timeout": 30.0},
                pool_pre_ping=True,
                future=True,
            )
            event.listen(_engine, "connect", _on_connect_sqlite)
        else:
            # PostgreSQL (or any non-sqlite) : standard pooling
            _engine = create_engine(
                resolved,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_recycle=3600,
                future=True,
            )

        # Reset metadata cache when engine changes
        _metadata = None

        return _engine


def load_metadata(engine: Optional[Engine] = None) -> MetaData:
    """Reflect the schema from the live DB into a SQLAlchemy MetaData.
    Cached per-engine. Call after init_db() so all tables exist.

    NOTE : the expression-based UNIQUE index `uq_mappings_active`
    (COALESCE(current_version_id, 0)) cannot be reflected by SQLAlchemy
    and will emit a SAWarning — that's expected and harmless, the index
    is still enforced at DB level for both INSERT and UPDATE.
    """
    global _metadata
    if _metadata is not None:
        return _metadata
    eng = engine or get_engine()
    md = MetaData()
    # Suppress the expression-index warning (we know about it and document it)
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.filterwarnings("ignore", message=".*expression-based index.*")
        md.reflect(bind=eng)
    _metadata = md
    return _metadata


def reset_for_testing() -> None:
    """Reset the singleton — for tests that switch DBs."""
    global _engine, _metadata
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _metadata = None


def is_sqlite(engine: Optional[Engine] = None) -> bool:
    eng = engine or get_engine()
    return eng.dialect.name == "sqlite"


def is_postgresql(engine: Optional[Engine] = None) -> bool:
    eng = engine or get_engine()
    return eng.dialect.name in ("postgresql", "postgres")

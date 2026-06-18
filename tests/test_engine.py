"""tests/test_engine.py — Tests for db/engine.py (SQLAlchemy Core layer)"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import database, engine as engine_mod


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Each test gets a fresh DB + reset singletons."""
    db_file = tmp_path / "test.sqlite"
    database.set_db_path(str(db_file))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TRANSCOMONITOR_DB_PATH", raising=False)
    engine_mod.reset_for_testing()
    database.init_db()
    yield
    engine_mod.reset_for_testing()
    database.set_db_path(None)


def test_resolve_dsn_explicit():
    assert engine_mod.resolve_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"


def test_resolve_dsn_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@host:5432/db")
    assert engine_mod.resolve_dsn() == "postgresql+psycopg://x:y@host:5432/db"


def test_resolve_dsn_fallback_sqlite():
    # Without DATABASE_URL → uses sqlite path
    dsn = engine_mod.resolve_dsn()
    assert dsn.startswith("sqlite:///")
    assert dsn.endswith(".sqlite")


def test_get_engine_is_singleton():
    e1 = engine_mod.get_engine()
    e2 = engine_mod.get_engine()
    assert e1 is e2


def test_get_engine_force_new():
    e1 = engine_mod.get_engine()
    e2 = engine_mod.get_engine(force_new=True)
    assert e1 is not e2


def test_engine_dialect_sqlite():
    e = engine_mod.get_engine()
    assert engine_mod.is_sqlite(e)
    assert not engine_mod.is_postgresql(e)


def test_engine_sqlite_pragmas_applied():
    """FK + WAL must be enabled per connection (per-connection PRAGMAs)."""
    e = engine_mod.get_engine()
    with e.connect() as con:
        fk = con.execute(text("PRAGMA foreign_keys")).scalar()
        jm = con.execute(text("PRAGMA journal_mode")).scalar()
        bt = con.execute(text("PRAGMA busy_timeout")).scalar()
        assert fk == 1
        assert jm in ("wal", "memory")
        assert bt == 30000


def test_engine_can_query_seeded_catalogues():
    """Engine reads the same DB as raw sqlite3 (db/models.py)."""
    e = engine_mod.get_engine()
    with e.connect() as con:
        n = con.execute(text("SELECT COUNT(*) FROM relation_types")).scalar()
        assert n == 9
        n = con.execute(text("SELECT COUNT(*) FROM source_decisions")).scalar()
        assert n == 33


def test_load_metadata_reflects_all_tables():
    md = engine_mod.load_metadata()
    expected_tables = {
        "users", "cim10_codes", "cim11_foundation", "cim11_linearizations",
        "relation_types", "source_decisions", "nomenclature_versions",
        "mappings", "mapping_proposals", "justifications",
        "mapping_foundation_links",
        "assignment_lists", "assignments",
        "frozen_versions", "version_mappings_snapshot",
        "import_batches", "import_diffs",
        "audit_events", "rules", "rule_applications", "app_config",
    }
    assert expected_tables.issubset(set(md.tables.keys())), (
        f"Missing tables: {expected_tables - set(md.tables.keys())}"
    )


def test_load_metadata_cached():
    md1 = engine_mod.load_metadata()
    md2 = engine_mod.load_metadata()
    assert md1 is md2


def test_metadata_table_has_columns():
    md = engine_mod.load_metadata()
    users = md.tables["users"]
    col_names = {c.name for c in users.columns}
    assert {"id", "username", "password_hash", "role", "active",
            "email", "full_name", "created_at", "last_login"}.issubset(col_names)


def test_can_select_via_core():
    """Test SQLAlchemy Core select() against reflected table."""
    md = engine_mod.load_metadata()
    e = engine_mod.get_engine()
    with e.connect() as con:
        relation_types = md.tables["relation_types"]
        rows = con.execute(
            select(relation_types.c.code, relation_types.c.libelle)
            .order_by(relation_types.c.sort_order)
        ).all()
        assert len(rows) == 9
        # First row by sort_order = 'equivalent'
        assert rows[0].code == "equivalent"


def test_engine_works_with_models_in_parallel():
    """sqlite3 (db/models.py) and SQLAlchemy engine must coexist on same DB."""
    from db import models

    # Insert via raw sqlite3
    con = database.get_connection()
    uid = models.create_user(con, "alice", "p", role="admin")
    con.close()

    # Read via SQLAlchemy
    e = engine_mod.get_engine()
    with e.connect() as conn:
        row = conn.execute(
            text("SELECT username, role FROM users WHERE id = :id"),
            {"id": uid},
        ).first()
        assert row.username == "alice"
        assert row.role == "admin"


def test_engine_postgresql_dsn_creates_postgres_dialect(monkeypatch):
    """Smoke test : with a PG DSN, the engine is created with the PG dialect.
    Does NOT actually connect (no PG server in tests). Skipped if psycopg
    is not installed (V1 dependency)."""
    pytest.importorskip("psycopg", reason="psycopg is a V1 dependency")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/db")
    engine_mod.reset_for_testing()
    e = engine_mod.get_engine()
    assert engine_mod.is_postgresql(e)
    assert not engine_mod.is_sqlite(e)

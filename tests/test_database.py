"""tests/test_database.py — Tests for db/database.py"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db import database


@pytest.fixture(autouse=True)
def reset_db_path(monkeypatch, tmp_path):
    """Use an isolated tmp DB for each test."""
    db_file = tmp_path / "test.sqlite"
    database.set_db_path(str(db_file))
    monkeypatch.delenv("TRANSCOMONITOR_DB_PATH", raising=False)
    yield
    database.set_db_path(None)


def test_get_db_path_explicit(tmp_path):
    p = str(tmp_path / "explicit.sqlite")
    database.set_db_path(p)
    assert database.get_db_path() == p


def test_get_db_path_env(monkeypatch, tmp_path):
    database.set_db_path(None)
    p = str(tmp_path / "envvar.sqlite")
    monkeypatch.setenv("TRANSCOMONITOR_DB_PATH", p)
    assert database.get_db_path() == p


def test_get_connection_pragmas():
    database.init_db()
    con = database.get_connection()
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA journal_mode").fetchone()[0] in ("wal", "memory")
    con.close()


def test_init_db_idempotent():
    database.init_db()
    database.init_db()  # should not raise
    assert database.is_db_initialized()


def test_is_db_initialized_false_when_empty(tmp_path):
    database.set_db_path(str(tmp_path / "empty.sqlite"))
    assert not database.is_db_initialized()


def test_init_db_creates_seed_catalogues():
    database.init_db()
    con = database.get_connection()
    assert con.execute("SELECT COUNT(*) FROM relation_types").fetchone()[0] == 9
    assert con.execute("SELECT COUNT(*) FROM source_decisions").fetchone()[0] == 33
    con.close()


def test_init_db_triggers_append_only():
    """Verify the append-only triggers are installed."""
    database.init_db()
    con = database.get_connection()
    # Try to update audit_events → should fail
    con.execute("INSERT INTO audit_events (action, source) VALUES ('login', 'system')")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE audit_events SET note='hacked'")
        con.commit()
    con.close()

"""tests/test_models.py — Tests for db/models.py (users + app_config)"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db import models
from utils import crypto

SCHEMA = (ROOT / "db" / "schema_sqlite.sql").read_text(encoding="utf-8")


@pytest.fixture
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    c.commit()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def reset_crypto():
    crypto.reset_for_testing()
    yield
    crypto.reset_for_testing()


class TestUsers:
    def test_create_user(self, con):
        uid = models.create_user(con, "alice", "p@ss", role="admin")
        assert uid > 0
        u = models.get_user(con, uid)
        assert u["username"] == "alice"
        assert u["role"] == "admin"
        assert u["password_hash"] != "p@ss"  # bcrypt hashed

    def test_create_user_invalid_role(self, con):
        with pytest.raises(ValueError):
            models.create_user(con, "bad", "p", role="superuser")

    def test_authenticate_success(self, con):
        models.create_user(con, "alice", "secret", role="admin")
        result = models.authenticate(con, "alice", "secret")
        assert result is not None
        assert result.get("error") is None
        assert result["username"] == "alice"
        assert result["last_login"] is not None  # updated by authenticate

    def test_authenticate_wrong_password(self, con):
        models.create_user(con, "alice", "secret")
        assert models.authenticate(con, "alice", "WRONG") is None

    def test_authenticate_inactive(self, con):
        uid = models.create_user(con, "alice", "secret")
        models.update_user(con, uid, active=0)
        result = models.authenticate(con, "alice", "secret")
        assert result == {"error": "inactive"}

    def test_authenticate_unknown_user(self, con):
        assert models.authenticate(con, "nobody", "x") is None

    def test_list_users(self, con):
        models.create_user(con, "a", "p", role="admin")
        models.create_user(con, "b", "p", role="mainteneur")
        models.create_user(con, "c", "p", role="valideur")
        users = models.list_users(con)
        assert len(users) == 3
        assert sorted(u["username"] for u in users) == ["a", "b", "c"]

    def test_update_user_role(self, con):
        uid = models.create_user(con, "u", "p", role="mainteneur")
        models.update_user(con, uid, role="valideur")
        assert models.get_user(con, uid)["role"] == "valideur"
        with pytest.raises(ValueError):
            models.update_user(con, uid, role="god")

    def test_update_user_password(self, con):
        uid = models.create_user(con, "u", "old")
        models.update_user(con, uid, password="new")
        assert models.authenticate(con, "u", "old") is None
        assert models.authenticate(con, "u", "new") is not None

    def test_ensure_default_admin_creates(self, con, capsys):
        models.ensure_default_admin(con, username="root", password="defaultpw")
        u = models.get_user_by_username(con, "root")
        assert u is not None
        assert u["role"] == "admin"

    def test_ensure_default_admin_skips_if_admin_exists(self, con):
        models.create_user(con, "existing_admin", "p", role="admin")
        models.ensure_default_admin(con, username="root", password="x")
        # Should NOT create 'root' because 'existing_admin' already exists
        assert models.get_user_by_username(con, "root") is None

    def test_ensure_default_admin_generates_random_pw(self, con, capsys):
        models.ensure_default_admin(con, username="root")  # no password
        captured = capsys.readouterr()
        assert "generated random password" in captured.out
        u = models.get_user_by_username(con, "root")
        assert u is not None


class TestAppConfig:
    def test_set_get_value(self, con):
        models.set_config_value(con, "my_key", "my_value")
        assert models.get_config_value(con, "my_key") == "my_value"

    def test_get_default_when_missing(self, con):
        assert models.get_config_value(con, "absent", "default") == "default"

    def test_set_secret_encrypted(self, con, monkeypatch):
        monkeypatch.setenv("DB_ENCRYPTION_KEY", "test-key")
        crypto.reset_for_testing()
        models.set_config_value(con, "my_secret", "p@ssw0rd", is_secret=True)
        # Raw value in DB should be encrypted
        raw = con.execute("SELECT value FROM app_config WHERE key='my_secret'").fetchone()[0]
        assert raw.startswith("gAAAAA")
        # get_config_value decrypts
        assert models.get_config_value(con, "my_secret") == "p@ssw0rd"

    def test_sensitive_key_auto_encrypted(self, con, monkeypatch):
        monkeypatch.setenv("DB_ENCRYPTION_KEY", "test-key")
        crypto.reset_for_testing()
        # WHO_CLIENT_SECRET is in SENSITIVE_CONFIG_KEYS → auto-encrypted
        models.set_config_value(con, "WHO_CLIENT_SECRET", "the-secret")
        raw = con.execute("SELECT value, is_secret FROM app_config WHERE key='WHO_CLIENT_SECRET'").fetchone()
        assert raw[0].startswith("gAAAAA")
        assert raw[1] == 1
        assert models.get_config_value(con, "WHO_CLIENT_SECRET") == "the-secret"

    def test_update_overwrites(self, con):
        models.set_config_value(con, "k", "v1")
        models.set_config_value(con, "k", "v2")
        assert models.get_config_value(con, "k") == "v2"

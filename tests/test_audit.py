"""tests/test_audit.py — Tests for services/audit.py"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from services import audit

SCHEMA = (ROOT / "db" / "schema_sqlite.sql").read_text(encoding="utf-8")


@pytest.fixture
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    # Sample user for tests
    c.execute("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def user(con) -> dict:
    row = con.execute("SELECT id, username, role FROM users WHERE username='alice'").fetchone()
    return dict(row)


# ─────────────────────────────────────────────────────────────────────────
# write_audit
# ─────────────────────────────────────────────────────────────────────────

class TestWriteAudit:
    def test_basic_write(self, con):
        eid = audit.write_audit(con, action="login", actor_user_id=1,
                                actor_username="alice", source="ui")
        assert eid > 0
        row = con.execute("SELECT * FROM audit_events WHERE id=?", (eid,)).fetchone()
        assert row["action"] == "login"
        assert row["actor_username"] == "alice"
        assert row["source"] == "ui"
        assert row["old_value_json"] is None
        assert row["new_value_json"] is None
        assert row["ts"] is not None

    def test_invalid_action_rejected(self, con):
        with pytest.raises(ValueError, match="action must be"):
            audit.write_audit(con, action="hack_db", source="ui")

    def test_invalid_object_type_rejected(self, con):
        with pytest.raises(ValueError, match="object_type"):
            audit.write_audit(con, action="login", object_type="not_a_thing", source="ui")

    def test_invalid_source_rejected(self, con):
        with pytest.raises(ValueError, match="source must be"):
            audit.write_audit(con, action="login", source="email")

    def test_json_serialization_of_dict_values(self, con):
        eid = audit.write_audit(
            con, action="edit_mapping",
            actor_user_id=1, actor_username="alice",
            object_type="mapping", object_id=42,
            old_value={"target_mms_code": "BA00"},
            new_value={"target_mms_code": "BA01"},
            source="ui",
        )
        row = con.execute("SELECT * FROM audit_events WHERE id=?", (eid,)).fetchone()
        assert json.loads(row["old_value_json"]) == {"target_mms_code": "BA00"}
        assert json.loads(row["new_value_json"]) == {"target_mms_code": "BA01"}
        assert row["object_id"] == "42"  # stored as string

    def test_unicode_in_values(self, con):
        eid = audit.write_audit(
            con, action="edit_mapping", actor_user_id=1, actor_username="alice",
            object_type="mapping", object_id=1,
            new_value={"libelle": "Fièvre typhoïde — précision"},
            source="ui",
        )
        row = con.execute("SELECT new_value_json FROM audit_events WHERE id=?", (eid,)).fetchone()
        assert "Fièvre typhoïde" in row["new_value_json"]

    def test_non_serializable_raises(self, con):
        class Custom:
            pass
        # default=str fallback handles objects → won't raise
        eid = audit.write_audit(
            con, action="login", actor_user_id=1, actor_username="alice",
            new_value=Custom(), source="ui",
        )
        # Value gets str()-converted
        row = con.execute("SELECT new_value_json FROM audit_events WHERE id=?", (eid,)).fetchone()
        assert "Custom" in row["new_value_json"]

    def test_request_meta_captured(self, con):
        audit.write_audit(con, action="login", actor_user_id=1, actor_username="alice",
                          source="ui", request_ip="10.0.0.1", request_ua="Mozilla/5.0")
        row = con.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        assert row["request_ip"] == "10.0.0.1"
        assert row["request_ua"] == "Mozilla/5.0"


# ─────────────────────────────────────────────────────────────────────────
# Convenience wrappers
# ─────────────────────────────────────────────────────────────────────────

class TestConvenienceWrappers:
    def test_audit_user_action(self, con, user):
        audit.audit_user_action(
            con, user=user, action="create_mapping",
            object_type="mapping", object_id=99,
            new_value={"target_mms_code": "BA00"},
        )
        row = con.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor_user_id"] == user["id"]
        assert row["actor_username"] == user["username"]
        assert row["source"] == "ui"
        assert row["action"] == "create_mapping"

    def test_audit_system_action(self, con):
        audit.audit_system_action(
            con, action="cache_refresh",
            object_type="system", object_id="cim11_foundation",
            note="scheduled bootstrap",
        )
        row = con.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor_user_id"] is None
        assert row["source"] == "system"
        assert row["note"] == "scheduled bootstrap"

    def test_audit_system_action_with_import_source(self, con):
        audit.audit_system_action(
            con, action="import_apply", source="import",
            object_type="import_batch", object_id=1,
        )
        row = con.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        assert row["source"] == "import"


# ─────────────────────────────────────────────────────────────────────────
# Append-only enforcement (trigger-based — already tested in test_database
# but we test through the service interface here too)
# ─────────────────────────────────────────────────────────────────────────

class TestAppendOnly:
    def test_update_blocked(self, con):
        eid = audit.write_audit(con, action="login", actor_user_id=1,
                                actor_username="alice", source="ui")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE audit_events SET note='hacked' WHERE id=?", (eid,))
            con.commit()

    def test_delete_blocked(self, con):
        eid = audit.write_audit(con, action="login", actor_user_id=1,
                                actor_username="alice", source="ui")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM audit_events WHERE id=?", (eid,))
            con.commit()


# ─────────────────────────────────────────────────────────────────────────
# Consultation
# ─────────────────────────────────────────────────────────────────────────

class TestConsultation:
    def _populate(self, con, user):
        audit.audit_user_action(con, user=user, action="login")
        audit.audit_user_action(con, user=user, action="create_mapping",
                                object_type="mapping", object_id=1)
        audit.audit_user_action(con, user=user, action="edit_mapping",
                                object_type="mapping", object_id=1,
                                old_value={"x": 1}, new_value={"x": 2})
        audit.audit_user_action(con, user=user, action="validate_mapping",
                                object_type="mapping", object_id=1)
        audit.audit_system_action(con, action="cache_refresh",
                                  object_type="system", object_id="cim11")

    def test_get_recent_events_default(self, con, user):
        self._populate(con, user)
        rows = audit.get_recent_events(con)
        assert len(rows) == 5
        # Ordered DESC
        assert rows[0]["action"] == "cache_refresh"
        assert rows[-1]["action"] == "login"

    def test_get_recent_events_filtered_by_action(self, con, user):
        self._populate(con, user)
        rows = audit.get_recent_events(con, action="edit_mapping")
        assert len(rows) == 1
        assert rows[0]["action"] == "edit_mapping"

    def test_get_recent_events_filtered_by_object(self, con, user):
        self._populate(con, user)
        rows = audit.get_recent_events(con, object_type="mapping", object_id=1)
        assert len(rows) == 3
        actions = {r["action"] for r in rows}
        assert actions == {"create_mapping", "edit_mapping", "validate_mapping"}

    def test_get_recent_events_filtered_by_user(self, con, user):
        self._populate(con, user)
        rows = audit.get_recent_events(con, actor_user_id=user["id"])
        # All except the system one
        assert len(rows) == 4
        assert all(r["actor_user_id"] == user["id"] for r in rows)

    def test_get_recent_events_limit(self, con, user):
        for _ in range(10):
            audit.audit_user_action(con, user=user, action="login")
        rows = audit.get_recent_events(con, limit=3)
        assert len(rows) == 3

    def test_get_object_history(self, con, user):
        self._populate(con, user)
        history = audit.get_object_history(con, "mapping", 1)
        assert len(history) == 3
        # Ordered ASC (oldest first)
        actions = [r["action"] for r in history]
        assert actions == ["create_mapping", "edit_mapping", "validate_mapping"]

    def test_get_user_actions(self, con, user):
        self._populate(con, user)
        rows = audit.get_user_actions(con, user["id"])
        assert len(rows) == 4

    def test_count_events(self, con, user):
        assert audit.count_events(con) == 0
        self._populate(con, user)
        assert audit.count_events(con) == 5

    def test_count_events_since(self, con, user):
        # Insert one event and capture its timestamp directly (avoids
        # timezone mismatches between Python and SQLite's datetime('now')).
        audit.audit_user_action(con, user=user, action="login")
        first_ts = con.execute("SELECT ts FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()["ts"]

        # Sleep so the next event has a different (later) ts
        time.sleep(1.1)
        audit.audit_user_action(con, user=user, action="logout")
        second_ts = con.execute("SELECT ts FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()["ts"]
        assert second_ts > first_ts, "second event should be strictly later"

        # Cutoff between them → only the second event is counted
        assert audit.count_events(con, since_ts=second_ts) == 1
        # Cutoff at first event → both are counted (>= comparison)
        assert audit.count_events(con, since_ts=first_ts) == 2


# ─────────────────────────────────────────────────────────────────────────
# Diff helpers
# ─────────────────────────────────────────────────────────────────────────

class TestDiffDicts:
    def test_no_changes(self):
        d = audit.diff_dicts({"a": 1, "b": 2}, {"a": 1, "b": 2})
        assert d == {}

    def test_value_change(self):
        d = audit.diff_dicts({"a": 1}, {"a": 2})
        assert d == {"a": {"before": 1, "after": 2}}

    def test_key_added(self):
        d = audit.diff_dicts({"a": 1}, {"a": 1, "b": 2})
        assert d == {"b": {"before": None, "after": 2}}

    def test_key_removed(self):
        d = audit.diff_dicts({"a": 1, "b": 2}, {"a": 1})
        assert d == {"b": {"before": 2, "after": None}}

    def test_ignored_keys(self):
        d = audit.diff_dicts(
            {"a": 1, "updated_at": "t1"},
            {"a": 2, "updated_at": "t2"},
            ignored_keys=("updated_at",),
        )
        assert d == {"a": {"before": 1, "after": 2}}

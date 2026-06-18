"""tests/test_worklist.py — Tests for mod_worklist core functions."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db import models
from modules.mod_worklist import (
    assign_user_to_list, create_assignment_list, evaluate_assignment_list,
    fetch_all_assignments, fetch_assignments_for_user,
)
from services import authz

SCHEMA = (ROOT / "db" / "schema_sqlite.sql").read_text(encoding="utf-8")


@pytest.fixture
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    models.create_user(c, "alice_admin", "p", role="admin")
    models.create_user(c, "bob_maint", "p", role="mainteneur")
    models.create_user(c, "carla_valid", "p", role="valideur")
    # Sample data : a few cim10 codes + mappings to filter on
    c.execute("INSERT INTO cim10_codes (code, libelle_fr, chapitre, est_classant) VALUES ('A011', 'Typhoid', '01', 1)")
    c.execute("INSERT INTO cim10_codes (code, libelle_fr, chapitre, est_classant) VALUES ('I10', 'HTA', '09', 1)")
    c.execute("INSERT INTO cim10_codes (code, libelle_fr, chapitre, est_classant) VALUES ('Z00', 'Examen', '21', 0)")
    for code, status, fiab in [("A011", "propose", "HAUTE"), ("I10", "valide", "TRES_HAUTE"),
                                 ("Z00", "propose", "MOYENNE")]:
        c.execute("""INSERT INTO mappings (direction, source_code, source_kind, target_kind,
                                            target_mms_code, status, fiabilite)
                     VALUES ('forward', ?, 'cim10_code', 'mms_simple', ?, ?, ?)""",
                  (code, f"X{code}", status, fiab))
    c.commit()
    yield c
    c.close()


@pytest.fixture
def admin(con):
    return dict(con.execute("SELECT * FROM users WHERE username='alice_admin'").fetchone())


@pytest.fixture
def maint(con):
    return dict(con.execute("SELECT * FROM users WHERE username='bob_maint'").fetchone())


# ─────────────────────────────────────────────────────────────────────────
# create_assignment_list
# ─────────────────────────────────────────────────────────────────────────

class TestCreateList:
    def test_create_dynamic_list(self, con, admin):
        lid = create_assignment_list(
            con, user=admin,
            name="Forward classant",
            description="Tous les codes classant à valider",
            direction="forward",
            query_definition={"direction": "forward", "est_classant": True},
        )
        assert lid > 0
        row = con.execute("SELECT * FROM assignment_lists WHERE id=?", (lid,)).fetchone()
        assert row["name"] == "Forward classant"
        assert row["direction"] == "forward"
        assert json.loads(row["query_definition"]) == {"direction": "forward", "est_classant": True}

    def test_create_static_list(self, con, admin):
        lid = create_assignment_list(
            con, user=admin,
            name="Codes spéciaux",
            direction="forward",
            static_codes=["A011", "I10"],
        )
        row = con.execute("SELECT * FROM assignment_lists WHERE id=?", (lid,)).fetchone()
        assert json.loads(row["static_codes"]) == ["A011", "I10"]

    def test_requires_query_or_static(self, con, admin):
        with pytest.raises(ValueError, match="query_definition or static_codes"):
            create_assignment_list(con, user=admin, name="empty", direction="forward")

    def test_invalid_direction(self, con, admin):
        with pytest.raises(ValueError, match="invalid direction"):
            create_assignment_list(con, user=admin, name="x", direction="north",
                                    static_codes=["A011"])

    def test_mainteneur_can_create(self, con, maint):
        lid = create_assignment_list(con, user=maint, name="My list",
                                      direction="forward", static_codes=["A011"])
        assert lid > 0

    def test_valideur_cannot_create(self, con):
        valid = dict(con.execute("SELECT * FROM users WHERE username='carla_valid'").fetchone())
        with pytest.raises(authz.AuthzError):
            create_assignment_list(con, user=valid, name="x",
                                    direction="forward", static_codes=["A011"])

    def test_audit_event_recorded(self, con, admin):
        create_assignment_list(con, user=admin, name="X", direction="forward",
                                static_codes=["A011"])
        row = con.execute(
            "SELECT * FROM audit_events WHERE action='admin_list_create' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["actor_username"] == "alice_admin"


# ─────────────────────────────────────────────────────────────────────────
# assign_user_to_list
# ─────────────────────────────────────────────────────────────────────────

class TestAssignUser:
    def test_assign_mainteneur(self, con, admin, maint):
        lid = create_assignment_list(con, user=admin, name="L", direction="forward",
                                      static_codes=["A011"])
        aid = assign_user_to_list(con, user=admin, list_id=lid,
                                    target_user_id=maint["id"],
                                    expected_role="mainteneur")
        assert aid > 0
        row = con.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        assert row["user_id"] == maint["id"]
        assert row["expected_role"] == "mainteneur"
        assert row["status"] == "open"

    def test_invalid_role(self, con, admin, maint):
        lid = create_assignment_list(con, user=admin, name="L", direction="forward",
                                      static_codes=["A011"])
        with pytest.raises(ValueError, match="invalid expected_role"):
            assign_user_to_list(con, user=admin, list_id=lid,
                                  target_user_id=maint["id"], expected_role="god")

    def test_unique_constraint(self, con, admin, maint):
        lid = create_assignment_list(con, user=admin, name="L", direction="forward",
                                      static_codes=["A011"])
        assign_user_to_list(con, user=admin, list_id=lid,
                              target_user_id=maint["id"], expected_role="mainteneur")
        # Duplicate (list, user, role) blocked
        with pytest.raises(sqlite3.IntegrityError):
            assign_user_to_list(con, user=admin, list_id=lid,
                                  target_user_id=maint["id"], expected_role="mainteneur")


# ─────────────────────────────────────────────────────────────────────────
# evaluate_assignment_list
# ─────────────────────────────────────────────────────────────────────────

class TestEvaluate:
    def test_dynamic_list_counts(self, con, admin):
        lid = create_assignment_list(
            con, user=admin, name="Classant forward",
            direction="forward",
            query_definition={"direction": "forward", "est_classant": True},
        )
        result = evaluate_assignment_list(con, lid)
        # 2 codes are classant : A011 (propose), I10 (valide)
        assert result["total_codes"] == 2
        assert result["done"] == 1  # I10 has status=valide
        assert result["remaining"] == 1
        assert 49 < result["progress_pct"] < 51

    def test_filter_by_status(self, con, admin):
        lid = create_assignment_list(
            con, user=admin, name="proposés",
            direction="forward",
            query_definition={"direction": "forward", "status": ["propose"]},
        )
        result = evaluate_assignment_list(con, lid)
        assert result["total_codes"] == 2  # A011 + Z00

    def test_static_list(self, con, admin):
        lid = create_assignment_list(
            con, user=admin, name="ma liste",
            direction="forward",
            static_codes=["A011", "Z00", "X999"],
        )
        result = evaluate_assignment_list(con, lid)
        assert result["static"] is True
        assert result["total_codes"] == 3

    def test_missing_list_raises(self, con):
        with pytest.raises(KeyError):
            evaluate_assignment_list(con, 999)


# ─────────────────────────────────────────────────────────────────────────
# fetch_assignments_for_user
# ─────────────────────────────────────────────────────────────────────────

class TestFetchAssignments:
    def test_fetch_returns_progress(self, con, admin, maint):
        lid = create_assignment_list(
            con, user=admin, name="L1",
            direction="forward",
            query_definition={"direction": "forward", "est_classant": True},
        )
        assign_user_to_list(con, user=admin, list_id=lid,
                              target_user_id=maint["id"], expected_role="mainteneur")
        results = fetch_assignments_for_user(con, maint["id"])
        assert len(results) == 1
        a = results[0]
        assert a["list_name"] == "L1"
        assert a["expected_role"] == "mainteneur"
        assert a["progress"]["total_codes"] == 2

    def test_fetch_excludes_done_by_default(self, con, admin, maint):
        lid = create_assignment_list(con, user=admin, name="L",
                                      direction="forward", static_codes=["A011"])
        aid = assign_user_to_list(con, user=admin, list_id=lid,
                                    target_user_id=maint["id"],
                                    expected_role="mainteneur")
        con.execute("UPDATE assignments SET status='done' WHERE id=?", (aid,))
        con.commit()
        results = fetch_assignments_for_user(con, maint["id"])
        assert len(results) == 0
        results = fetch_assignments_for_user(con, maint["id"], include_done=True)
        assert len(results) == 1

    def test_fetch_empty_for_user_with_no_assignments(self, con, maint):
        results = fetch_assignments_for_user(con, maint["id"])
        assert results == []


# ─────────────────────────────────────────────────────────────────────────
# Admin view
# ─────────────────────────────────────────────────────────────────────────

class TestAdminView:
    def test_fetch_all_assignments(self, con, admin, maint):
        lid = create_assignment_list(con, user=admin, name="L",
                                      direction="forward", static_codes=["A011"])
        assign_user_to_list(con, user=admin, list_id=lid,
                              target_user_id=maint["id"],
                              expected_role="mainteneur")
        results = fetch_all_assignments(con)
        assert len(results) == 1
        assert results[0]["username"] == "bob_maint"
        assert results[0]["assigned_by_username"] == "alice_admin"

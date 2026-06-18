"""tests/test_mapping_edit.py — Tests for edit_mapping() core logic.

Focuses on the transactional + auditable edit function. Shiny UI rendering
is covered by separate smoke tests in test_app_smoke.py.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db import models
from modules.mod_mapping_edit import edit_mapping, fetch_mapping, fetch_proposals, fetch_justifications
from services import authz

SCHEMA = (ROOT / "db" / "schema_sqlite.sql").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    # Sample users
    models.create_user(c, "alice_admin", "p", role="admin")
    models.create_user(c, "bob_maint", "p", role="mainteneur")
    models.create_user(c, "carla_valid", "p", role="valideur")
    # A mapping to edit
    c.execute("""INSERT INTO mappings (
        direction, source_code, source_kind, target_kind, target_mms_code,
        relation_type, fiabilite, source_decision, status, created_by
    ) VALUES ('forward', 'A011', 'cim10_code', 'mms_simple', 'BA00',
              'equivalent', 'HAUTE', 'OMS+ANS', 'propose', 2)""")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def admin(con):
    return dict(con.execute("SELECT * FROM users WHERE username='alice_admin'").fetchone())


@pytest.fixture
def maint(con):
    return dict(con.execute("SELECT * FROM users WHERE username='bob_maint'").fetchone())


@pytest.fixture
def valid(con):
    return dict(con.execute("SELECT * FROM users WHERE username='carla_valid'").fetchone())


# ─────────────────────────────────────────────────────────────────────────
# Edit basic
# ─────────────────────────────────────────────────────────────────────────

class TestEditMapping:
    def test_mainteneur_can_edit_target_code(self, con, maint):
        result = edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="mms_simple",
            target_mms_code="BA01",
            target_cim10_code=None,
            target_foundation_uris=None,
            target_components=None,
            relation_type="equivalent",
            new_status=None,
            justification_motif="arbitrage_expert",
            justification_commentaire="Correction du code cible",
        )
        assert result["ok"]
        m = fetch_mapping(con, 1)
        assert m["target_mms_code"] == "BA01"
        assert m["status"] == "propose"  # unchanged
        assert m["revision"] == 2  # incremented
        assert m["updated_by"] == maint["id"]

    def test_proposal_snapshot_old_value(self, con, maint):
        edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA01",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status=None, justification_motif="arbitrage_expert",
            justification_commentaire="test",
        )
        proposals = fetch_proposals(con, 1)
        assert len(proposals) == 1
        assert proposals[0]["target_mms_code_old"] == "BA00"
        assert proposals[0]["status_old"] == "propose"
        assert proposals[0]["proposed_by"] == maint["id"]
        assert proposals[0]["proposed_source"] == "ui_edit"

    def test_justification_inserted(self, con, maint):
        edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA01",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status=None, justification_motif="confirmation_OMS",
            justification_commentaire="Confirmé par OMS table 2024-01",
            justification_references=[{"type": "url", "value": "https://who.int/x"}],
        )
        justifs = fetch_justifications(con, 1)
        assert len(justifs) == 1
        assert justifs[0]["motif"] == "confirmation_OMS"
        assert "OMS table 2024-01" in justifs[0]["commentaire"]
        refs = json.loads(justifs[0]["references_"])
        assert refs[0]["value"] == "https://who.int/x"
        assert justifs[0]["attached_to_action"] == "edit"

    def test_audit_event_recorded(self, con, maint):
        edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA01",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status=None, justification_motif="arbitrage_expert",
            justification_commentaire="x",
        )
        row = con.execute(
            "SELECT * FROM audit_events WHERE object_type='mapping' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["action"] == "edit_mapping"
        old = json.loads(row["old_value_json"])
        new = json.loads(row["new_value_json"])
        assert old["target_mms_code"] == "BA00"
        assert new["target_mms_code"] == "BA01"


# ─────────────────────────────────────────────────────────────────────────
# Workflow transitions
# ─────────────────────────────────────────────────────────────────────────

class TestWorkflowTransitions:
    def test_mainteneur_can_send_to_en_revue(self, con, maint):
        result = edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA00",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status="en_revue",
            justification_motif="arbitrage_expert",
            justification_commentaire="prêt pour revue",
        )
        assert result["new_status"] == "en_revue"

    def test_mainteneur_cannot_validate(self, con, maint):
        # Mainteneur cannot do propose → valide (skipping valideur)
        with pytest.raises(authz.AuthzError, match="cannot transition"):
            edit_mapping(
                con, user=maint, mapping_id=1,
                target_kind="mms_simple", target_mms_code="BA00",
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status="valide",
                justification_motif="arbitrage_expert",
                justification_commentaire="x",
            )

    def test_valideur_can_validate(self, con, valid):
        # First go to en_revue (admin can do it)
        edit_mapping(
            con, user={"id": 1, "username": "alice_admin", "role": "admin"},
            mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA00",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status="en_revue",
            justification_motif="arbitrage_expert", justification_commentaire="x",
        )
        # Now valideur can validate
        result = edit_mapping(
            con, user=valid, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA00",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status="valide",
            justification_motif="confirmation_OMS",
            justification_commentaire="Validé",
        )
        assert result["new_status"] == "valide"
        m = fetch_mapping(con, 1)
        assert m["last_validated_by"] == valid["id"]
        assert m["last_validated_at"] is not None

    def test_gele_status_is_terminal(self, con, admin):
        # Move to gele first
        con.execute("UPDATE mappings SET status='gele' WHERE id=1")
        con.commit()
        with pytest.raises(authz.AuthzError):
            edit_mapping(
                con, user=admin, mapping_id=1,
                target_kind="mms_simple", target_mms_code="BA01",
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status="en_revue",
                justification_motif="arbitrage_expert", justification_commentaire="x",
            )


# ─────────────────────────────────────────────────────────────────────────
# Self-validation (§14 #7)
# ─────────────────────────────────────────────────────────────────────────

class TestSelfValidation:
    def test_self_validation_flagged_and_audited(self, con):
        # Create an admin/valideur user that will propose AND validate
        models.create_user(con, "dave", "p", role="valideur")
        # First also make them admin to bypass transition (or use composite role)
        # Actually: valideur role can validate from en_revue. Let's:
        # 1. Admin pushes propose → en_revue
        # 2. Then 'dave' (valideur) edits the mapping (creates proposal by dave)
        # 3. Then 'dave' validates (en_revue → valide)
        admin = dict(con.execute("SELECT * FROM users WHERE username='alice_admin'").fetchone())
        dave = dict(con.execute("SELECT * FROM users WHERE username='dave'").fetchone())

        # Move to en_revue
        edit_mapping(
            con, user=admin, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA00",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status="en_revue",
            justification_motif="arbitrage_expert", justification_commentaire="step1",
        )

        # Dave edits the mapping (admin role would be needed since mainteneur cap; valideur lacks it).
        # Give dave both roles by upgrading to admin for the test scope
        models.update_user(con, dave["id"], role="admin")
        dave = dict(con.execute("SELECT * FROM users WHERE id=?", (dave["id"],)).fetchone())
        edit_mapping(
            con, user=dave, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA02",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status=None,  # just edit, no status change
            justification_motif="arbitrage_expert", justification_commentaire="dave proposes",
        )

        # Now dave validates → self-validation
        result = edit_mapping(
            con, user=dave, mapping_id=1,
            target_kind="mms_simple", target_mms_code="BA02",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="equivalent",
            new_status="valide",
            justification_motif="confirmation_OMS",
            justification_commentaire="dave validates own work",
        )
        assert result["is_self_validation"] is True
        m = fetch_mapping(con, 1)
        assert m["is_self_validation"] == 1
        # Audit event note
        row = con.execute(
            "SELECT note FROM audit_events WHERE action='validate_mapping' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["note"] == "self-validation"


# ─────────────────────────────────────────────────────────────────────────
# Target kind variants
# ─────────────────────────────────────────────────────────────────────────

class TestTargetKinds:
    def test_switch_to_cluster(self, con, maint):
        # Need cim11_linearizations + foundation cache for auto-resolve.
        # Without cache, auto-resolve gracefully falls back to NULL.
        result = edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="mms_cluster", target_mms_code="BA00&XN8P1",
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="composite",
            new_status=None, justification_motif="postcoord",
            justification_commentaire="Add severity specifier",
            target_release="2024-01",
        )
        assert result["ok"]
        m = fetch_mapping(con, 1)
        assert m["target_kind"] == "mms_cluster"
        assert m["target_mms_code"] == "BA00&XN8P1"

    def test_switch_to_foundation_only(self, con, maint):
        # First ensure the foundation URI exists (FK constraint via mapping_foundation_links)
        con.execute(
            "INSERT INTO cim11_foundation (uri, entity_id, label_fr, kind) VALUES (?, ?, ?, ?)",
            ("http://id.who.int/icd/entity/123", "123", "test entity", "entity"),
        )
        con.commit()
        result = edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="foundation_only", target_mms_code=None,
            target_cim10_code=None,
            target_foundation_uris=["http://id.who.int/icd/entity/123"],
            target_components=None, relation_type="equivalent",
            new_status=None, justification_motif="autre",
            justification_commentaire="Pas d'équivalent MMS, ancrage fondation",
        )
        assert result["ok"]
        m = fetch_mapping(con, 1)
        assert m["target_kind"] == "foundation_only"
        assert m["target_mms_code"] is None
        uris = json.loads(m["target_foundation_uris"])
        assert uris == ["http://id.who.int/icd/entity/123"]
        # Foundation link maintained
        n_links = con.execute(
            "SELECT COUNT(*) FROM mapping_foundation_links WHERE mapping_id=1"
        ).fetchone()[0]
        assert n_links == 1

    def test_switch_to_non_mappable(self, con, maint):
        result = edit_mapping(
            con, user=maint, mapping_id=1,
            target_kind="non_mappable", target_mms_code=None,
            target_cim10_code=None, target_foundation_uris=None,
            target_components=None, relation_type="non_mappable",
            new_status=None, justification_motif="autre",
            justification_commentaire="Concept sans cible CIM-11",
        )
        assert result["ok"]
        m = fetch_mapping(con, 1)
        assert m["target_kind"] == "non_mappable"
        assert m["target_mms_code"] is None


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────

class TestValidation:
    def test_mms_simple_requires_mms_code(self, con, maint):
        with pytest.raises(ValueError, match="target_mms_code required"):
            edit_mapping(
                con, user=maint, mapping_id=1,
                target_kind="mms_simple", target_mms_code=None,
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status=None, justification_motif="autre",
                justification_commentaire="x",
            )

    def test_mms_simple_rejects_cluster_string(self, con, maint):
        with pytest.raises(ValueError, match="code is a cluster"):
            edit_mapping(
                con, user=maint, mapping_id=1,
                target_kind="mms_simple", target_mms_code="BA00&XN8P1",
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status=None, justification_motif="autre",
                justification_commentaire="x",
            )

    def test_foundation_only_requires_uris(self, con, maint):
        with pytest.raises(ValueError, match="requires target_foundation_uris"):
            edit_mapping(
                con, user=maint, mapping_id=1,
                target_kind="foundation_only", target_mms_code=None,
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status=None, justification_motif="autre",
                justification_commentaire="x",
            )

    def test_invalid_foundation_uri_rejected(self, con, maint):
        with pytest.raises(ValueError, match="invalid foundation URI"):
            edit_mapping(
                con, user=maint, mapping_id=1,
                target_kind="foundation_only", target_mms_code=None,
                target_cim10_code=None,
                target_foundation_uris=["http://example.com/bad"],
                target_components=None, relation_type="equivalent",
                new_status=None, justification_motif="autre",
                justification_commentaire="x",
            )

    def test_invalid_motif_rejected(self, con, maint):
        with pytest.raises(ValueError, match="invalid justification_motif"):
            edit_mapping(
                con, user=maint, mapping_id=1,
                target_kind="mms_simple", target_mms_code="BA01",
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status=None, justification_motif="not_a_motif",
                justification_commentaire="x",
            )

    def test_missing_mapping_raises(self, con, maint):
        with pytest.raises(KeyError):
            edit_mapping(
                con, user=maint, mapping_id=9999,
                target_kind="mms_simple", target_mms_code="BA00",
                target_cim10_code=None, target_foundation_uris=None,
                target_components=None, relation_type="equivalent",
                new_status=None, justification_motif="autre",
                justification_commentaire="x",
            )

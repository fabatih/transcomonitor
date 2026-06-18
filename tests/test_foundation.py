"""
tests/test_foundation.py — Tests for services/foundation.py

Covers : URI parsing, cluster decomposition/assembly, MMS→foundation resolution,
mapping_foundation_links sync, inverse lookups.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from services.foundation import (
    ClusterComponent,
    FoundationURI,
    LinearizationURI,
    assemble_cluster_string,
    decompose_cluster_string,
    get_mappings_referencing_foundation,
    is_cluster_string,
    resolve_cluster_components,
    resolve_mms_to_foundation,
    sync_mapping_foundation_links,
    validate_foundation_uri,
    validate_lin_uri,
    validate_mms_code,
)

SCHEMA = (ROOT / "db" / "schema_sqlite.sql").read_text(encoding="utf-8")


@pytest.fixture
def con() -> sqlite3.Connection:
    """Fresh in-memory DB with schema loaded."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def cache_populated(con: sqlite3.Connection) -> sqlite3.Connection:
    """DB with cim11_linearizations cache populated for tests."""
    fixtures = [
        # release, code, uri, label_fr, foundation_uris, is_stem, is_extension
        ("2026-01", "1A07", "http://id.who.int/icd/release/11/2026-01/mms/1A07",
         "Fièvre typhoïde", json.dumps(["http://id.who.int/icd/entity/1376721186"]), 1, 0),
        ("2026-01", "BA00", "http://id.who.int/icd/release/11/2026-01/mms/BA00",
         "Hypertension essentielle", json.dumps(["http://id.who.int/icd/entity/588616678"]), 1, 0),
        ("2026-01", "XN8P1", "http://id.who.int/icd/release/11/2026-01/mms/XN8P1",
         "Sévérité légère", json.dumps(["http://id.who.int/icd/entity/2018344096"]), 0, 1),
        ("2026-01", "1G40", "http://id.who.int/icd/release/11/2026-01/mms/1G40",
         "Septicémie", json.dumps(["http://id.who.int/icd/entity/111111111"]), 1, 0),
        ("2026-01", "1B5Z", "http://id.who.int/icd/release/11/2026-01/mms/1B5Z",
         "Maladie bactérienne SAI", json.dumps(["http://id.who.int/icd/entity/222222222"]), 1, 0),
    ]
    con.executemany(
        """INSERT INTO cim11_linearizations
           (release, code, uri, label_fr, foundation_uris, is_stem, is_extension)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        fixtures,
    )
    # Foundation entities (referenced by FK from mapping_foundation_links)
    foundations = [
        ("http://id.who.int/icd/entity/1376721186", "1376721186", "Fièvre typhoïde", "entity"),
        ("http://id.who.int/icd/entity/588616678", "588616678", "Hypertension essentielle", "entity"),
        ("http://id.who.int/icd/entity/2018344096", "2018344096", "Sévérité légère", "extension_value"),
        ("http://id.who.int/icd/entity/111111111", "111111111", "Septicémie", "entity"),
        ("http://id.who.int/icd/entity/222222222", "222222222", "Maladie bactérienne SAI", "entity"),
    ]
    con.executemany(
        "INSERT INTO cim11_foundation (uri, entity_id, label_fr, kind) VALUES (?, ?, ?, ?)",
        foundations,
    )
    con.commit()
    return con


# ─────────────────────────────────────────────────────────────────────────
# URI parsing
# ─────────────────────────────────────────────────────────────────────────

class TestURIParsing:
    def test_foundation_uri_valid(self):
        u = FoundationURI.parse("http://id.who.int/icd/entity/1376721186")
        assert u.entity_id == "1376721186"
        assert u.uri == "http://id.who.int/icd/entity/1376721186"

    def test_foundation_uri_https(self):
        u = FoundationURI.parse("https://id.who.int/icd/entity/999")
        assert u.entity_id == "999"

    def test_foundation_uri_from_entity_id(self):
        u = FoundationURI.from_entity_id("42")
        assert u.uri == "http://id.who.int/icd/entity/42"

    def test_foundation_uri_invalid(self):
        with pytest.raises(ValueError):
            FoundationURI.parse("http://example.com/icd/entity/123")
        with pytest.raises(ValueError):
            FoundationURI.parse("http://id.who.int/icd/release/11/2026-01/mms/BA00")

    def test_lin_uri_valid(self):
        u = LinearizationURI.parse("http://id.who.int/icd/release/11/2026-01/mms/BA00")
        assert u.release == "2026-01"
        assert u.code == "BA00"

    def test_lin_uri_build(self):
        u = LinearizationURI.build("2026-01", "1A07")
        assert u.uri == "http://id.who.int/icd/release/11/2026-01/mms/1A07"

    def test_lin_uri_build_invalid_release(self):
        with pytest.raises(ValueError):
            LinearizationURI.build("2026", "BA00")
        with pytest.raises(ValueError):
            LinearizationURI.build("abcd-ef", "BA00")

    def test_lin_uri_build_invalid_code(self):
        with pytest.raises(ValueError):
            LinearizationURI.build("2026-01", "ba00")  # lowercase

    def test_validators(self):
        assert validate_foundation_uri("http://id.who.int/icd/entity/123")
        assert not validate_foundation_uri("garbage")
        assert validate_lin_uri("http://id.who.int/icd/release/11/2026-01/mms/BA00")
        assert not validate_lin_uri("http://id.who.int/icd/entity/123")
        assert validate_mms_code("BA00")
        assert validate_mms_code("BA00&XN8P1")
        assert validate_mms_code("1G40/1B5Z")
        assert not validate_mms_code("")
        assert not validate_mms_code("ba00")  # lowercase rejected


# ─────────────────────────────────────────────────────────────────────────
# Cluster decomposition
# ─────────────────────────────────────────────────────────────────────────

class TestClusterDecomposition:
    def test_simple_code(self):
        comps = decompose_cluster_string("BA00")
        assert len(comps) == 1
        assert comps[0].role == "stem"
        assert comps[0].mms_code == "BA00"
        assert comps[0].position == 0

    def test_stem_with_specifier(self):
        comps = decompose_cluster_string("BA00&XN8P1")
        assert [c.mms_code for c in comps] == ["BA00", "XN8P1"]
        assert [c.role for c in comps] == ["stem", "specifier"]
        assert [c.position for c in comps] == [0, 1]

    def test_stem_with_multiple_specifiers(self):
        comps = decompose_cluster_string("BA00&XN8P1&XN8P2")
        assert [c.role for c in comps] == ["stem", "specifier", "specifier"]

    def test_multi_stem(self):
        comps = decompose_cluster_string("1G40/1B5Z")
        assert [c.mms_code for c in comps] == ["1G40", "1B5Z"]
        assert [c.role for c in comps] == ["stem", "stem"]

    def test_multi_stem_with_specifiers(self):
        comps = decompose_cluster_string("1A00&XN8P1/1B5Z")
        assert [(c.mms_code, c.role) for c in comps] == [
            ("1A00", "stem"), ("XN8P1", "specifier"), ("1B5Z", "stem"),
        ]
        assert [c.position for c in comps] == [0, 1, 2]

    def test_empty_string(self):
        with pytest.raises(ValueError):
            decompose_cluster_string("")

    def test_invalid_token(self):
        with pytest.raises(ValueError):
            decompose_cluster_string("ba00&XN8P1")  # lowercase stem

    def test_assemble_roundtrip(self):
        for cluster in ["BA00", "BA00&XN8P1", "1G40/1B5Z", "1A00&XN8P1/1B5Z",
                        "BA00&XN8P1&XN8P2"]:
            assert assemble_cluster_string(decompose_cluster_string(cluster)) == cluster

    def test_assemble_specifier_before_stem(self):
        bad = [ClusterComponent(position=0, role="specifier", mms_code="XN8P1")]
        with pytest.raises(ValueError):
            assemble_cluster_string(bad)

    def test_is_cluster_string(self):
        assert not is_cluster_string("BA00")
        assert is_cluster_string("BA00&XN8P1")
        assert is_cluster_string("1G40/1B5Z")


# ─────────────────────────────────────────────────────────────────────────
# MMS → foundation resolution
# ─────────────────────────────────────────────────────────────────────────

class TestResolveMMSToFoundation:
    def test_simple_code(self, cache_populated):
        uris = resolve_mms_to_foundation(cache_populated, "1A07", "2026-01")
        assert uris == ["http://id.who.int/icd/entity/1376721186"]

    def test_cluster_stem_plus_specifier(self, cache_populated):
        uris = resolve_mms_to_foundation(cache_populated, "BA00&XN8P1", "2026-01")
        assert uris == [
            "http://id.who.int/icd/entity/588616678",
            "http://id.who.int/icd/entity/2018344096",
        ]

    def test_multi_stem(self, cache_populated):
        uris = resolve_mms_to_foundation(cache_populated, "1G40/1B5Z", "2026-01")
        assert uris == [
            "http://id.who.int/icd/entity/111111111",
            "http://id.who.int/icd/entity/222222222",
        ]

    def test_dedup(self, cache_populated):
        # Hypothetical case: same foundation URI listed twice — should dedup
        cache_populated.execute(
            "UPDATE cim11_linearizations SET foundation_uris = ? WHERE code = 'XN8P1'",
            (json.dumps(["http://id.who.int/icd/entity/588616678"]),),  # same as BA00
        )
        uris = resolve_mms_to_foundation(cache_populated, "BA00&XN8P1", "2026-01")
        assert uris == ["http://id.who.int/icd/entity/588616678"]

    def test_missing_code_raises_keyerror(self, cache_populated):
        with pytest.raises(KeyError):
            resolve_mms_to_foundation(cache_populated, "ZZZZ", "2026-01")

    def test_resolve_cluster_components_enrichment(self, cache_populated):
        comps = resolve_cluster_components(cache_populated, "BA00&XN8P1", "2026-01")
        assert len(comps) == 2
        assert comps[0].mms_code == "BA00"
        assert comps[0].lin_uri == "http://id.who.int/icd/release/11/2026-01/mms/BA00"
        assert comps[0].foundation_uri == "http://id.who.int/icd/entity/588616678"
        assert comps[1].role == "specifier"
        assert comps[1].foundation_uri == "http://id.who.int/icd/entity/2018344096"


# ─────────────────────────────────────────────────────────────────────────
# mapping_foundation_links sync
# ─────────────────────────────────────────────────────────────────────────

class TestSyncFoundationLinks:
    def _insert_mapping(self, con, **kwargs):
        defaults = dict(
            direction="forward", source_code="A011", source_kind="cim10_code",
            target_kind="mms_simple", target_mms_code="1A07",
            target_foundation_uris=None, target_components=None,
            relation_type="equivalent", fiabilite="HAUTE", status="propose",
        )
        defaults.update(kwargs)
        cur = con.execute(
            """INSERT INTO mappings
               (direction, source_code, source_kind, target_kind, target_mms_code,
                target_foundation_uris, target_components, relation_type, fiabilite, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (defaults["direction"], defaults["source_code"], defaults["source_kind"],
             defaults["target_kind"], defaults["target_mms_code"],
             defaults["target_foundation_uris"], defaults["target_components"],
             defaults["relation_type"], defaults["fiabilite"], defaults["status"]),
        )
        con.commit()
        return cur.lastrowid

    def test_sync_mms_simple(self, cache_populated):
        mid = self._insert_mapping(
            cache_populated,
            target_foundation_uris=json.dumps(["http://id.who.int/icd/entity/1376721186"]),
        )
        n = sync_mapping_foundation_links(cache_populated, mid)
        assert n == 1
        rows = cache_populated.execute(
            "SELECT foundation_uri, role, position FROM mapping_foundation_links WHERE mapping_id=?",
            (mid,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "http://id.who.int/icd/entity/1376721186"
        assert rows[0][1] == "primary"
        assert rows[0][2] == 0

    def test_sync_mms_cluster(self, cache_populated):
        components = [
            {"position": 0, "role": "stem", "mms_code": "BA00",
             "lin_uri": "http://id.who.int/icd/release/11/2026-01/mms/BA00",
             "foundation_uri": "http://id.who.int/icd/entity/588616678"},
            {"position": 1, "role": "specifier", "mms_code": "XN8P1",
             "lin_uri": "http://id.who.int/icd/release/11/2026-01/mms/XN8P1",
             "foundation_uri": "http://id.who.int/icd/entity/2018344096",
             "axis": "hasSeverity"},
        ]
        mid = self._insert_mapping(
            cache_populated, source_code="I10",
            target_kind="mms_cluster", target_mms_code="BA00&XN8P1",
            target_foundation_uris=json.dumps([
                "http://id.who.int/icd/entity/588616678",
                "http://id.who.int/icd/entity/2018344096",
            ]),
            target_components=json.dumps(components),
        )
        n = sync_mapping_foundation_links(cache_populated, mid)
        assert n == 2
        rows = cache_populated.execute(
            """SELECT foundation_uri, role, position FROM mapping_foundation_links
               WHERE mapping_id=? ORDER BY position""",
            (mid,),
        ).fetchall()
        assert [r[1] for r in rows] == ["component_stem", "component_specifier"]
        assert [r[2] for r in rows] == [0, 1]

    def test_sync_foundation_only(self, cache_populated):
        mid = self._insert_mapping(
            cache_populated, source_code="Z999",
            target_kind="foundation_only", target_mms_code=None,
            target_foundation_uris=json.dumps([
                "http://id.who.int/icd/entity/1376721186",
                "http://id.who.int/icd/entity/588616678",
            ]),
        )
        n = sync_mapping_foundation_links(cache_populated, mid)
        assert n == 2
        rows = cache_populated.execute(
            "SELECT role FROM mapping_foundation_links WHERE mapping_id=? ORDER BY position",
            (mid,),
        ).fetchall()
        assert all(r[0] == "primary" for r in rows)

    def test_sync_non_mappable(self, cache_populated):
        mid = self._insert_mapping(
            cache_populated, source_code="X9999",
            target_kind="non_mappable", target_mms_code=None,
        )
        n = sync_mapping_foundation_links(cache_populated, mid)
        assert n == 0

    def test_sync_idempotent(self, cache_populated):
        mid = self._insert_mapping(
            cache_populated,
            target_foundation_uris=json.dumps(["http://id.who.int/icd/entity/1376721186"]),
        )
        assert sync_mapping_foundation_links(cache_populated, mid) == 1
        assert sync_mapping_foundation_links(cache_populated, mid) == 1
        count = cache_populated.execute(
            "SELECT COUNT(*) FROM mapping_foundation_links WHERE mapping_id=?", (mid,),
        ).fetchone()[0]
        assert count == 1, "sync should be idempotent (no duplicate rows)"

    def test_sync_missing_mapping_raises(self, cache_populated):
        with pytest.raises(KeyError):
            sync_mapping_foundation_links(cache_populated, 99999)


# ─────────────────────────────────────────────────────────────────────────
# Inverse lookup
# ─────────────────────────────────────────────────────────────────────────

class TestInverseLookup:
    def test_lookup_by_foundation_uri(self, cache_populated):
        """A foundation URI must be discoverable from BOTH a forward mapping
        (where it appears as target) and a reverse mapping (where it appears
        as source via cim11_linearizations cache resolution)."""
        c = cache_populated
        # Register the MMS release so the reverse mapping can resolve its source
        cur = c.execute(
            "INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES ('cim11_mms', '2026-01')"
        )
        mms_release_id = cur.lastrowid

        # Forward mapping: A011 → 1A07 (foundation URI is the target's)
        c.execute(
            """INSERT INTO mappings (direction, source_code, source_kind, target_kind,
                                     target_mms_code, target_foundation_uris)
               VALUES ('forward', 'A011', 'cim10_code', 'mms_simple', '1A07', ?)""",
            (json.dumps(["http://id.who.int/icd/entity/1376721186"]),),
        )
        # Reverse mapping: 1A07 → A011 (foundation URI is the source's, resolved via cache)
        c.execute(
            """INSERT INTO mappings (direction, source_code, source_kind, source_version_id,
                                     target_kind, target_cim10_code)
               VALUES ('reverse', '1A07', 'mms_code', ?, 'cim10_code', 'A011')""",
            (mms_release_id,),
        )
        c.commit()
        for mid in [r[0] for r in c.execute("SELECT id FROM mappings")]:
            sync_mapping_foundation_links(c, mid)

        rows = get_mappings_referencing_foundation(
            c, "http://id.who.int/icd/entity/1376721186"
        )
        assert len(rows) == 2, (
            f"Expected 2 mappings (forward+reverse), got {len(rows)}: "
            f"{[(r['direction'], r['source_code']) for r in rows]}"
        )
        directions = sorted([r["direction"] for r in rows])
        assert directions == ["forward", "reverse"]

    def test_lookup_filtered_by_direction(self, cache_populated):
        c = cache_populated
        cur = c.execute(
            "INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES ('cim11_mms', '2026-01')"
        )
        mms_release_id = cur.lastrowid
        c.execute(
            """INSERT INTO mappings (direction, source_code, source_kind, target_kind,
                                     target_mms_code, target_foundation_uris)
               VALUES ('forward', 'A011', 'cim10_code', 'mms_simple', '1A07', ?)""",
            (json.dumps(["http://id.who.int/icd/entity/1376721186"]),),
        )
        c.execute(
            """INSERT INTO mappings (direction, source_code, source_kind, source_version_id,
                                     target_kind, target_cim10_code)
               VALUES ('reverse', '1A07', 'mms_code', ?, 'cim10_code', 'A011')""",
            (mms_release_id,),
        )
        c.commit()
        for mid in [r[0] for r in c.execute("SELECT id FROM mappings")]:
            sync_mapping_foundation_links(c, mid)

        rows = get_mappings_referencing_foundation(
            c, "http://id.who.int/icd/entity/1376721186", direction="forward"
        )
        assert len(rows) == 1
        assert rows[0]["direction"] == "forward"

    def test_lookup_reverse_with_cluster_source(self, cache_populated):
        """Reverse mapping whose source is a cluster (rare but possible :
        e.g., reverse from a post-coordinated CIM-11 cluster to CIM-10)."""
        c = cache_populated
        cur = c.execute(
            "INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES ('cim11_mms', '2026-01')"
        )
        mms_release_id = cur.lastrowid
        c.execute(
            """INSERT INTO mappings (direction, source_code, source_kind, source_version_id,
                                     target_kind, target_cim10_code)
               VALUES ('reverse', 'BA00&XN8P1', 'mms_code', ?, 'cim10_code', 'I10')""",
            (mms_release_id,),
        )
        c.commit()
        mid = c.execute("SELECT id FROM mappings").fetchone()[0]
        n = sync_mapping_foundation_links(c, mid)
        assert n == 2, "cluster source should yield 2 foundation links (BA00 + XN8P1)"

        # Both foundation URIs should be discoverable
        for uri in ["http://id.who.int/icd/entity/588616678",
                    "http://id.who.int/icd/entity/2018344096"]:
            rows = get_mappings_referencing_foundation(c, uri)
            assert len(rows) == 1, f"foundation {uri} not found from cluster source"
            assert rows[0]["direction"] == "reverse"

    def test_lookup_reverse_with_foundation_uri_source(self, cache_populated):
        """Reverse mapping whose source_kind='foundation_uri' (rare : direct
        foundation-anchored reverse mapping)."""
        c = cache_populated
        uri = "http://id.who.int/icd/entity/1376721186"
        c.execute(
            """INSERT INTO mappings (direction, source_code, source_kind,
                                     target_kind, target_cim10_code)
               VALUES ('reverse', ?, 'foundation_uri', 'cim10_code', 'A011')""",
            (uri,),
        )
        c.commit()
        mid = c.execute("SELECT id FROM mappings").fetchone()[0]
        n = sync_mapping_foundation_links(c, mid)
        assert n == 1
        rows = get_mappings_referencing_foundation(c, uri)
        assert len(rows) == 1
        assert rows[0]["direction"] == "reverse"

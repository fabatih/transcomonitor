"""tests/test_mapping_table.py — Tests for mod_mapping_table data layer.

Focuses on fetch_mappings (labels, filters by direction) and fetch_split_pair
(funnel cases per plan §16.13). UI rendering smoke-tested separately.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.database import init_db, set_db_path
from modules.mod_mapping_table import (
    count_mappings, fetch_mappings, fetch_split_pair,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    """Fresh DB with schema + a small synthetic dataset for funnel tests."""
    p = str(tmp_path / "test.sqlite")
    set_db_path(p)
    init_db()
    con = sqlite3.connect(p)
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row

    # Versions
    cur = con.execute("INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES ('cim10_fr', '2026')")
    v10 = cur.lastrowid
    cur = con.execute("INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES ('cim11_mms', '2024-01')")
    v11 = cur.lastrowid

    # CIM-10 codes (with PMSI flags + type_code)
    cim10_data = [
        ("A011", "Paratyphoïde A", "01", 1, 0, None, "INTERNATIONAL_BASE"),
        ("A012", "Paratyphoïde B", "01", 1, 0, None, "INTERNATIONAL_BASE"),
        ("A013", "Paratyphoïde C", "01", 0, 1, 2, "INTERNATIONAL_BASE"),
        ("W30",  "Contact mat. agricole", "20", 1, 0, None, "INTERNATIONAL_BASE"),
        ("W300", "Contact mat. agri. cabines", "20", 1, 0, None, "INTERNATIONAL_EXTENSION"),
        ("W301", "Contact mat. agri. plein air", "20", 0, 0, None, "INTERNATIONAL_EXTENSION"),
        ("U00",  "Code FR spécial", "22", 0, 0, None, "FR_ONLY"),
    ]
    for code, lib, chap, c, cma, n, tc in cim10_data:
        con.execute(
            """INSERT INTO cim10_codes (code, libelle_fr, chapitre, est_classant,
                                         est_cma, niveau_cma, type_code)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (code, lib, chap, c, cma, n, tc),
        )

    # Forward mappings : A011→1A08, A012→1A08, A013→1A08 (funnel n:1 on 1A08)
    # W30→PB6Y, W300→PB6Y, W301→PB6Y (funnel n:1 on PB6Y)
    fwd_data = [
        ("A011", "mms_simple", "1A08", "Fièvre paratyphoïde", "HAUTE", "OMS+ANS"),
        ("A012", "mms_simple", "1A08", "Fièvre paratyphoïde", "HAUTE", "OMS+ANS"),
        ("A013", "mms_simple", "1A08", "Fièvre paratyphoïde", "MOYENNE", "HERITAGE"),
        ("W30",  "mms_simple", "PB6Y", "Contact avec mat. agricole", "HAUTE", "OMS+ANS"),
        ("W300", "mms_simple", "PB6Y", "Contact avec mat. agricole", "HAUTE", "OMS+ANS"),
        ("W301", "mms_simple", "PB6Y", "Contact avec mat. agricole", "HAUTE", "OMS+ANS"),
        ("U00",  "non_mappable", None, None, "NON_RESOLU", "AUCUNE"),
    ]
    for src, kind, tgt, lib, fiab, sd in fwd_data:
        con.execute(
            """INSERT INTO mappings
               (direction, source_code, source_kind, source_version_id,
                target_kind, target_mms_code, target_label, target_release_id,
                relation_type, fiabilite, source_decision, status)
               VALUES ('forward', ?, 'cim10_code', ?, ?, ?, ?, ?,
                       'equivalent', ?, ?, 'propose')""",
            (src, v10, kind, tgt, lib, v11 if tgt else None, fiab, sd),
        )

    # Reverse : 1A08 → A011 (only one canonical reverse choice — represents the
    # canonical "best" reverse mapping for 1A08).
    # PB6Y → W30 (reverse converges).
    rev_data = [
        ("1A08", "cim10_code", "A011", "Paratyphoïde A", "TRES_HAUTE", "ALGO_OMS"),
        ("PB6Y", "cim10_code", "W30",  "Contact mat. agricole", "HAUTE", "ALGO_OMS"),
    ]
    for src, kind, tgt, lib, fiab, sd in rev_data:
        con.execute(
            """INSERT INTO mappings
               (direction, source_code, source_kind, source_version_id,
                target_kind, target_cim10_code, target_label, target_release_id,
                relation_type, fiabilite, source_decision, status)
               VALUES ('reverse', ?, 'mms_code', ?, ?, ?, ?, ?,
                       'equivalent', ?, ?, 'propose')""",
            (src, v11, kind, tgt, lib, v10, fiab, sd),
        )
    con.commit()
    con.close()
    return p


@pytest.fixture
def con(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


# ─────────────────────────────────────────────────────────────────────────
# Labels in fetch_mappings (plan §16.1)
# ─────────────────────────────────────────────────────────────────────────

class TestLabels:
    def test_forward_returns_target_label_from_denorm(self, con):
        df = fetch_mappings(con, "forward", limit=10)
        assert "libelle_target" in df.columns
        # A011 → 1A08 has target_label "Fièvre paratyphoïde"
        row = df[df["source_code"] == "A011"].iloc[0]
        assert "Fièvre paratyphoïde" in row["libelle_target"]

    def test_reverse_returns_target_label(self, con):
        df = fetch_mappings(con, "reverse", limit=10)
        assert "libelle_target" in df.columns
        row = df[df["source_code"] == "1A08"].iloc[0]
        assert "Paratyphoïde" in row["libelle_target"]

    def test_non_mappable_target_label_dash(self, con):
        df = fetch_mappings(con, "forward", limit=10)
        row = df[df["source_code"] == "U00"].iloc[0]
        assert row["libelle_target"] == "—"


# ─────────────────────────────────────────────────────────────────────────
# Filters by direction (plan §16.2)
# ─────────────────────────────────────────────────────────────────────────

class TestFiltersForward:
    def test_filter_classant_forward(self, con):
        n_total = count_mappings(con, "forward")
        n_classant = count_mappings(con, "forward", only_classant=True)
        # A011, A012, W30, W300 are classant (4) — A013, W301, U00 are not (3)
        assert n_classant == 4
        assert n_total == 7

    def test_filter_cma_forward(self, con):
        n = count_mappings(con, "forward", only_cma=True)
        # only A013 has est_cma=1
        assert n == 1

    def test_filter_type_code_fr_only(self, con):
        n = count_mappings(con, "forward", type_code=["FR_ONLY"])
        assert n == 1  # only U00

    def test_filter_type_code_extension(self, con):
        n = count_mappings(con, "forward", type_code=["INTERNATIONAL_EXTENSION"])
        assert n == 2  # W300, W301

    def test_filter_target_kind_non_mappable(self, con):
        n = count_mappings(con, "forward", target_kind=["non_mappable"])
        assert n == 1  # U00


class TestFiltersReverse:
    def test_filter_target_only_classant_reverse(self, con):
        # 1A08 → A011 (classant), PB6Y → W30 (classant) ⇒ 2
        n = count_mappings(con, "reverse", target_only_classant=True)
        assert n == 2

    def test_filter_target_type_code_reverse(self, con):
        n = count_mappings(con, "reverse",
                            target_type_code=["INTERNATIONAL_BASE"])
        assert n == 2  # both targets A011 and W30 are INTERNATIONAL_BASE

    def test_filter_target_only_cma_reverse_zero(self, con):
        n = count_mappings(con, "reverse", target_only_cma=True)
        # Neither A011 nor W30 is CMA → 0
        assert n == 0


# ─────────────────────────────────────────────────────────────────────────
# Split pair / funnel (plan §16.13)
# ─────────────────────────────────────────────────────────────────────────

class TestSplitPair:
    def test_no_funnel_simple(self, con):
        """A013 → 1A08, no siblings, no funnel."""
        pair = fetch_split_pair(con, "A013", "forward")
        assert len(pair["forward"]) == 1
        assert len(pair["reverse"]) == 1
        # A013 → 1A08 — siblings are A011 and A012 (other CIM-10 → 1A08)
        assert len(pair["forward_siblings"]) == 2
        assert pair["is_funnel_forward"] is True
        # Sibling codes
        sib_codes = sorted(s["source_code"] for s in pair["forward_siblings"])
        assert sib_codes == ["A011", "A012"]

    def test_funnel_forward_siblings(self, con):
        """W30 → PB6Y ; W300, W301 also → PB6Y."""
        pair = fetch_split_pair(con, "W30", "forward")
        assert len(pair["forward"]) == 1
        assert len(pair["forward_siblings"]) == 2
        assert pair["is_funnel_forward"] is True
        # No reverse_siblings expected (W30 is unique reverse target of PB6Y)
        assert len(pair["reverse_siblings"]) == 0

    def test_reverse_direction_funnel(self, con):
        """From PB6Y reverse, see all the CIM-10 funneled into it via forward_siblings."""
        pair = fetch_split_pair(con, "PB6Y", "reverse")
        # 1 reverse (PB6Y → W30) + 1 forward main (W30 → PB6Y) +
        # 2 forward siblings (W300, W301 → PB6Y)
        assert len(pair["reverse"]) == 1
        assert len(pair["forward"]) == 1
        assert len(pair["forward_siblings"]) == 2

    def test_split_pair_includes_source_label(self, con):
        pair = fetch_split_pair(con, "A013", "forward")
        assert pair["source_label"] == "Paratyphoïde C"
        assert pair["source_chapter"] == "01"

    def test_split_pair_coherence_matrix(self, con):
        pair = fetch_split_pair(con, "A013", "forward")
        # A013 forward → 1A08, reverse 1A08 → A011 ; A013 ≠ A011 → DISCORDANT or CATEGORIE
        assert pair["roundtrip_matrix"]
        # A013 and A011 share prefix A01 → CATEGORIE
        first = pair["roundtrip_matrix"][0]
        assert first["coherence"] in ("CATEGORIE", "DISCORDANT")

    def test_split_pair_strict_when_matching(self, con):
        # A011 forward → 1A08, reverse 1A08 → A011 → STRICT
        pair = fetch_split_pair(con, "A011", "forward")
        # The matrix should include a STRICT entry
        strict = [m for m in pair["roundtrip_matrix"] if m["coherence"] == "STRICT"]
        assert strict, f"Expected STRICT, got {[m['coherence'] for m in pair['roundtrip_matrix']]}"

    def test_split_pair_empty_source(self, con):
        pair = fetch_split_pair(con, "ZZZZ", "forward")
        assert pair["forward"] == []
        assert pair["reverse"] == []
        assert pair["roundtrip_coherence"] == "NO_DATA"

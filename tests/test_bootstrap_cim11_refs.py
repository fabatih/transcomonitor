"""tests/test_bootstrap_cim11_refs.py — Tests for the bootstrap script (non-live parts)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bootstrap_cim11_refs import (
    Stats, _extract_chapitre, _extract_definition, _extract_title,
    _is_residual_mms, load_codes_from_csv,
)


# ─────────────────────────────────────────────────────────────────────────
# CSV loading
# ─────────────────────────────────────────────────────────────────────────

class TestCSVLoading:
    def test_loads_with_default_column(self, tmp_path):
        f = tmp_path / "codes.csv"
        f.write_text("code,label\nBA00,Hypertension\n1A07.Z,Typhoid\nQD00,Carrier\n",
                     encoding="utf-8")
        codes = load_codes_from_csv(str(f))
        assert codes == ["BA00", "1A07.Z", "QD00"]

    def test_detects_semicolon_separator(self, tmp_path):
        f = tmp_path / "codes_semi.csv"
        f.write_text("code;label\nBA00;x\nQD00;y\n", encoding="utf-8")
        codes = load_codes_from_csv(str(f))
        assert codes == ["BA00", "QD00"]

    def test_uses_pipeline_column_name(self, tmp_path):
        # Pipeline output uses 'code_cim11_final'
        f = tmp_path / "pipeline.csv"
        f.write_text("code_cim10;code_cim11_final;fiabilite\nA011;1A07.Z;HAUTE\nI10;BA00;TRES_HAUTE\n",
                     encoding="utf-8")
        codes = load_codes_from_csv(str(f))
        assert codes == ["1A07.Z", "BA00"]

    def test_skips_empty_rows(self, tmp_path):
        f = tmp_path / "with_empty.csv"
        f.write_text("code\nBA00\n\n1A07\n  \nQD00\n", encoding="utf-8")
        codes = load_codes_from_csv(str(f))
        assert codes == ["BA00", "1A07", "QD00"]

    def test_raises_when_no_code_column(self, tmp_path):
        f = tmp_path / "bad.csv"
        f.write_text("foo,bar\n1,2\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_codes_from_csv(str(f))


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_is_residual_z_suffix(self):
        assert _is_residual_mms("1A07.Z")
        assert _is_residual_mms("BA00.Y")
        assert not _is_residual_mms("BA00")
        assert not _is_residual_mms("XN8P1")

    def test_extract_title_from_who_response(self):
        # WHO API returns titles as {"@language": "fr", "@value": "..."}
        data = {"title": {"@language": "fr", "@value": "Fièvre typhoïde"}}
        assert _extract_title(data) == "Fièvre typhoïde"

    def test_extract_title_missing(self):
        assert _extract_title({}) is None
        assert _extract_title({"title": "string"}) is None  # not a dict

    def test_extract_definition_present(self):
        data = {"definition": {"@language": "fr", "@value": "Définition X"}}
        assert _extract_definition(data) == "Définition X"

    def test_extract_definition_missing(self):
        assert _extract_definition({}) is None

    def test_extract_chapitre_from_field(self):
        assert _extract_chapitre({"chapter": "01"}, "BA00") == "01"

    def test_extract_chapitre_fallback_to_first_char(self):
        assert _extract_chapitre({}, "BA00") == "B"
        assert _extract_chapitre({}, "1A07.Z") == "1"
        assert _extract_chapitre({}, "") is None


# ─────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────

class TestStats:
    def test_fresh_stats_zero(self):
        s = Stats()
        assert s.total_input == 0
        assert s.errors == 0
        assert s.fetched_mms == 0
        assert s.fetched_foundation == 0
        assert s.cached_skipped == 0

    def test_summary_contains_key_metrics(self):
        import time
        s = Stats(total_input=10, fetched_mms=5, cached_skipped=4, errors=1,
                  start_time=time.time() - 2)
        summary = s.summary()
        assert "Total input codes: 10" in summary
        assert "fetched        : 5" in summary
        assert "cached" in summary.lower()
        assert "Errors" in summary

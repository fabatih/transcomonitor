"""
services/ingest.py — Ingestion of the pipeline XLSX seed into transcomonitor DB.

Reads `transcodage_pipeline_complete.xlsx` (produced by ../transcodage/ pipeline)
and populates :
  - cim10_codes      : 42 897 CIM-10 FR/PMSI codes with PMSI flags + ClaML type
  - mappings forward : 42 897 mapping rows, status='propose', traceability JSON
  - mappings reverse : 18 505 mapping rows
  - frozen_versions  : one initial entry 'v_pipeline_initial' marking the seed

Preserves the FULL pipeline traceability (37 colonnes étape1→étape5) as JSON
in mappings.pipeline_traceability so no information is lost.

Idempotent : checks for 'v_pipeline_initial' in frozen_versions and skips
re-ingestion unless --force-reset is used.

Performance : uses executemany() in chunks of 1000 rows.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterator, Optional

import openpyxl

from services.foundation import is_cluster_string

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────

SEED_VERSION_LABEL = "v_pipeline_initial"
DEFAULT_SEED_PATH  = Path(__file__).parent.parent / "data" / "seed" / "transcodage_pipeline_complete.xlsx"

FORWARD_SHEET = "CIM-10 vers CIM-11"
REVERSE_SHEET = "CIM-11 vers CIM-10"

# Forward pipeline columns (36) — mapped to schema
FORWARD_COLUMNS = [
    "code_cim10", "libelle_cim10", "code_cim11_final", "libelle_cim11_final",
    "fiabilite_finale", "source_decision", "validation_cluster", "version_cim11",
    "correction_syntaxe", "est_classant", "est_cma", "est_expe", "niveau_cma",
    "type_code", "parent_international", "type_extension",
    "etape1_code_cim11", "etape1_source", "etape1_fiabilite", "etape1_relation_oms",
    "etape2_verdict_llm", "etape2_code_cim11", "etape2_confiance_llm", "etape2_justification_llm",
    "etape3_code_cim10_reverse", "etape3_coherence_bidir", "etape3_cause_bidir",
    "etape4_verdict_arbitrage", "etape4_confiance_arbitrage", "etape4_justification_arbitrage",
    "etape5_verdict_rerev", "etape5_code_cim11_rerev", "etape5_confiance_rerev",
    "etape5_justification_rerev", "postcoordination_proposee", "qualite_bidir",
]

# Reverse pipeline columns (32)
REVERSE_COLUMNS = [
    "code_cim11", "libelle_cim11", "chapitre_cim11", "code_cim10_final",
    "libelle_cim10_final", "fiabilite_finale", "source_decision",
    "est_classant", "est_cma", "est_expe", "niveau_cma",
    "etape1_code_cim10_oms", "etape1_source_mapping", "etape1_fiabilite",
    "etape1_code_cim10_ans", "etape1_concordance_oms_ans", "etape1_score_composite",
    "etape2_verdict_llm", "etape2_code_cim10", "etape2_confiance_llm",
    "etape2_justification_llm", "etape2_garde_fou_classant", "etape2_garde_fou_detail",
    "etape2_set_review",
    "etape3_verdict_arbitrage", "etape3_confiance_arbitrage",
    "etape3_justification_arbitrage", "etape3_code_cim10_forward",
    "etape4_verdict_classant", "etape4_code_propose", "etape4_confiance_classant",
    "etape4_justification_classant",
]

# Chunk size for executemany (compromise between memory and round-trips)
CHUNK_SIZE = 1000


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def is_already_seeded(con: sqlite3.Connection) -> bool:
    """Check whether the v_pipeline_initial frozen_version exists."""
    row = con.execute(
        "SELECT id FROM frozen_versions WHERE label = ?", (SEED_VERSION_LABEL,)
    ).fetchone()
    return row is not None


def _to_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _to_bool(v) -> int:
    """Convert pipeline bool (True/False/None/'TRUE'/'FALSE') to 0/1."""
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        return 1 if v.upper() == "TRUE" else 0
    return int(bool(v))


def _to_str_or_none(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _to_float_or_none(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _detect_target_kind_forward(target_code: Optional[str]) -> str:
    if not target_code:
        return "non_mappable"
    if is_cluster_string(target_code):
        return "mms_cluster"
    return "mms_simple"


def _detect_relation_type(
    target_kind: str, fiabilite: Optional[str], source_decision: Optional[str],
) -> str:
    """Best-effort default relation type based on pipeline metadata.
    Curators can refine this per mapping through the UI."""
    if target_kind == "non_mappable":
        return "non_mappable"
    if target_kind == "mms_cluster":
        return "composite"
    # mms_simple : derive from source_decision hints
    if source_decision == "BIDIR_CONTESTE":
        return "ambigu"
    if source_decision and "POST_COORD" in source_decision:
        return "necessite_postcoord"
    if source_decision == "HERITAGE":
        return "plus_large"
    # Default for confirmed mappings
    return "equivalent"


def _normalize_source_decision(v: Optional[str]) -> Optional[str]:
    """Pipeline produces some source_decision values that aren't in our
    seeded catalogue. Normalize a few aliases ; otherwise pass-through."""
    if not v:
        return None
    # All pipeline values are pre-seeded in source_decisions table (cf. schema_sqlite.sql)
    return v


def _safe_fiabilite(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    upper = v.upper().strip()
    valid = {"TRES_HAUTE", "HAUTE", "MOYENNE", "BASSE", "HERITAGE", "CONTESTEE", "NON_RESOLU"}
    return upper if upper in valid else None


# ─────────────────────────────────────────────────────────────────────────
# Sheet readers
# ─────────────────────────────────────────────────────────────────────────

def _iter_sheet(xlsx_path: str | Path, sheet_name: str, expected_columns: list[str]
                ) -> Iterator[dict]:
    """Stream a sheet row by row, yielding {col_name: value} dicts."""
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    try:
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter)
        header = list(header)
        # Validate columns
        if header[:len(expected_columns)] != expected_columns:
            missing = set(expected_columns) - set(header)
            extra = set(header) - set(expected_columns)
            raise ValueError(
                f"Unexpected columns in '{sheet_name}': missing={missing}, extra={extra}"
            )
        for row in rows_iter:
            if all(v is None for v in row):
                continue  # skip blank trailing rows
            yield dict(zip(header, row))
    finally:
        wb.close()


# ─────────────────────────────────────────────────────────────────────────
# Ingestion
# ─────────────────────────────────────────────────────────────────────────

def ingest_cim10_codes(
    con: sqlite3.Connection, xlsx_path: str | Path,
    *, cim10_version: str = "2026",
) -> int:
    """Ingest cim10_codes from the forward sheet. Returns number of rows inserted."""
    batch: list[tuple] = []
    n_inserted = 0
    n_existing = 0
    seen_codes: set[str] = set()

    for row in _iter_sheet(xlsx_path, FORWARD_SHEET, FORWARD_COLUMNS):
        code = _to_str_or_none(row["code_cim10"])
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        # Determine chapitre : first character of the code if alpha
        chap_char = code[0] if code else None
        chapitre = chap_char if chap_char and chap_char.isalpha() else None

        batch.append((
            code,
            _to_str_or_none(row["libelle_cim10"]) or "",
            chapitre,
            _to_bool(row["est_classant"]),
            _to_bool(row["est_cma"]),
            _to_int(row["niveau_cma"]),
            _to_bool(row["est_expe"]),
            _to_str_or_none(row["type_code"]),
            _to_str_or_none(row["parent_international"]),
            _to_str_or_none(row["type_extension"]),
            1,  # is_terminal default
            0,  # is_category default
            cim10_version,
        ))
        if len(batch) >= CHUNK_SIZE:
            cur = con.executemany(
                """INSERT OR IGNORE INTO cim10_codes
                   (code, libelle_fr, chapitre, est_classant, est_cma, niveau_cma,
                    est_expe, type_code, parent_international, type_extension,
                    is_terminal, is_category, cim10_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            n_inserted += cur.rowcount
            batch = []
    if batch:
        cur = con.executemany(
            """INSERT OR IGNORE INTO cim10_codes
               (code, libelle_fr, chapitre, est_classant, est_cma, niveau_cma,
                est_expe, type_code, parent_international, type_extension,
                is_terminal, is_category, cim10_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )
        n_inserted += cur.rowcount
    con.commit()
    return n_inserted


def ingest_forward_mappings(
    con: sqlite3.Connection, xlsx_path: str | Path,
    *, cim10_version_id: int, cim11_release_id: int,
) -> int:
    """Ingest forward (CIM-10 → CIM-11) mappings. Returns count inserted."""
    # Pipeline trace columns to preserve in JSON
    trace_keys = [c for c in FORWARD_COLUMNS if c.startswith("etape")]
    extra_keys = ["validation_cluster", "version_cim11", "correction_syntaxe",
                  "postcoordination_proposee", "qualite_bidir"]

    batch: list[tuple] = []
    n_inserted = 0
    for row in _iter_sheet(xlsx_path, FORWARD_SHEET, FORWARD_COLUMNS):
        src = _to_str_or_none(row["code_cim10"])
        if not src:
            continue
        tgt = _to_str_or_none(row["code_cim11_final"])
        target_kind = _detect_target_kind_forward(tgt)
        relation = _detect_relation_type(target_kind, row["fiabilite_finale"],
                                          row["source_decision"])

        # Pipeline traceability — preserve all etape* + extras as JSON
        trace = {k: row.get(k) for k in trace_keys + extra_keys
                 if row.get(k) is not None}

        # impacts_aval : derived from PMSI flags as a starting point
        impacts: dict = {}
        if _to_bool(row["est_classant"]):
            impacts["pmsi_classant"] = True
        if _to_bool(row["est_cma"]):
            impacts["pmsi_cma"] = True
            niv = _to_int(row["niveau_cma"])
            if niv is not None:
                impacts["niveau_cma"] = niv
        if _to_bool(row["est_expe"]):
            impacts["expe"] = True

        batch.append((
            "forward", src, "cim10_code", cim10_version_id,
            target_kind,
            tgt if target_kind in ("mms_simple", "mms_cluster") else None,
            None,                                   # target_cim10_code (reverse only)
            None,                                   # target_foundation_uris (filled later by sync)
            None,                                   # target_components (filled later for clusters)
            cim11_release_id if target_kind in ("mms_simple", "mms_cluster") else None,
            relation,
            _safe_fiabilite(row["fiabilite_finale"]),
            _normalize_source_decision(row["source_decision"]),
            "propose",
            json.dumps(trace, ensure_ascii=False, default=str),
            json.dumps(impacts, ensure_ascii=False) if impacts else None,
            None,                                   # actions_necessaires (filled in UI)
        ))
        if len(batch) >= CHUNK_SIZE:
            cur = con.executemany(
                """INSERT INTO mappings (
                       direction, source_code, source_kind, source_version_id,
                       target_kind, target_mms_code, target_cim10_code,
                       target_foundation_uris, target_components, target_release_id,
                       relation_type, fiabilite, source_decision, status,
                       pipeline_traceability, impacts_aval, actions_necessaires
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            n_inserted += cur.rowcount
            batch = []
    if batch:
        cur = con.executemany(
            """INSERT INTO mappings (
                   direction, source_code, source_kind, source_version_id,
                   target_kind, target_mms_code, target_cim10_code,
                   target_foundation_uris, target_components, target_release_id,
                   relation_type, fiabilite, source_decision, status,
                   pipeline_traceability, impacts_aval, actions_necessaires
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )
        n_inserted += cur.rowcount
    con.commit()
    return n_inserted


def ingest_reverse_mappings(
    con: sqlite3.Connection, xlsx_path: str | Path,
    *, cim10_version_id: int, cim11_release_id: int,
) -> int:
    """Ingest reverse (CIM-11 → CIM-10) mappings. Returns count inserted."""
    trace_keys = [c for c in REVERSE_COLUMNS if c.startswith("etape")]
    extra_keys = ["chapitre_cim11"]

    batch: list[tuple] = []
    n_inserted = 0
    for row in _iter_sheet(xlsx_path, REVERSE_SHEET, REVERSE_COLUMNS):
        src = _to_str_or_none(row["code_cim11"])
        if not src:
            continue
        tgt_cim10 = _to_str_or_none(row["code_cim10_final"])
        if tgt_cim10:
            target_kind = "cim10_code"
        else:
            target_kind = "non_mappable"

        relation = "equivalent" if target_kind == "cim10_code" else "non_mappable"

        trace = {k: row.get(k) for k in trace_keys + extra_keys
                 if row.get(k) is not None}

        impacts: dict = {}
        if _to_bool(row["est_classant"]):
            impacts["pmsi_classant"] = True
        if _to_bool(row["est_cma"]):
            impacts["pmsi_cma"] = True

        batch.append((
            "reverse", src, "mms_code", cim11_release_id,
            target_kind,
            None,                                   # target_mms_code (forward only)
            tgt_cim10,
            None,                                   # target_foundation_uris (reverse: derived from source via sync)
            None,                                   # target_components
            cim10_version_id if target_kind == "cim10_code" else None,
            relation,
            _safe_fiabilite(row["fiabilite_finale"]),
            _normalize_source_decision(row["source_decision"]),
            "propose",
            json.dumps(trace, ensure_ascii=False, default=str),
            json.dumps(impacts, ensure_ascii=False) if impacts else None,
            None,
        ))
        if len(batch) >= CHUNK_SIZE:
            cur = con.executemany(
                """INSERT INTO mappings (
                       direction, source_code, source_kind, source_version_id,
                       target_kind, target_mms_code, target_cim10_code,
                       target_foundation_uris, target_components, target_release_id,
                       relation_type, fiabilite, source_decision, status,
                       pipeline_traceability, impacts_aval, actions_necessaires
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            n_inserted += cur.rowcount
            batch = []
    if batch:
        cur = con.executemany(
            """INSERT INTO mappings (
                   direction, source_code, source_kind, source_version_id,
                   target_kind, target_mms_code, target_cim10_code,
                   target_foundation_uris, target_components, target_release_id,
                   relation_type, fiabilite, source_decision, status,
                   pipeline_traceability, impacts_aval, actions_necessaires
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )
        n_inserted += cur.rowcount
    con.commit()
    return n_inserted


def _ensure_version(
    con: sqlite3.Connection, nomenclature: str, version_label: str,
) -> int:
    """Get or create a nomenclature_versions entry, return its id."""
    row = con.execute(
        "SELECT id FROM nomenclature_versions WHERE nomenclature=? AND version_label=?",
        (nomenclature, version_label),
    ).fetchone()
    if row:
        return row[0] if not isinstance(row, sqlite3.Row) else row["id"]
    cur = con.execute(
        "INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES (?, ?)",
        (nomenclature, version_label),
    )
    con.commit()
    return cur.lastrowid


# ─────────────────────────────────────────────────────────────────────────
# Top-level driver
# ─────────────────────────────────────────────────────────────────────────

def ingest_seed(
    con: sqlite3.Connection,
    xlsx_path: str | Path = DEFAULT_SEED_PATH,
    *,
    cim10_version: str = "2026",
    cim11_release: str = "2024-01",
    force_reset: bool = False,
    progress: bool = True,
) -> dict:
    """One-shot ingestion of the pipeline XLSX into a fresh DB.

    Workflow :
      1. Check idempotence (skip if already seeded — unless force_reset)
      2. Register nomenclature versions
      3. Ingest cim10_codes
      4. Ingest forward mappings
      5. Ingest reverse mappings
      6. Create the v_pipeline_initial frozen_version marker
      7. Audit the seed event

    Returns stats dict.
    """
    from services.audit import audit_system_action

    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Seed XLSX not found: {xlsx_path}")

    if is_already_seeded(con) and not force_reset:
        if progress:
            print(f"[ingest] Already seeded (frozen_version '{SEED_VERSION_LABEL}' exists). Skip.")
        return {"skipped": True}

    if force_reset:
        if progress:
            print(f"[ingest] --force-reset : wiping mappings + cim10_codes + frozen_versions...")
        con.execute("DELETE FROM mapping_foundation_links")
        con.execute("DELETE FROM mapping_proposals")
        con.execute("DELETE FROM justifications")
        con.execute("DELETE FROM version_mappings_snapshot")
        con.execute("DELETE FROM mappings")
        con.execute("DELETE FROM cim10_codes")
        con.execute(f"DELETE FROM frozen_versions WHERE label = '{SEED_VERSION_LABEL}'")
        con.commit()

    import time
    t0 = time.time()

    cim10_v_id = _ensure_version(con, "cim10_fr", cim10_version)
    cim11_v_id = _ensure_version(con, "cim11_mms", cim11_release)

    if progress:
        print(f"[ingest] Reading {xlsx_path.name} ({xlsx_path.stat().st_size / 1024 / 1024:.2f} MB)...")

    n_cim10 = ingest_cim10_codes(con, xlsx_path, cim10_version=cim10_version)
    t_cim10 = time.time()
    if progress:
        print(f"[ingest]   cim10_codes : {n_cim10} inserted in {t_cim10-t0:.1f}s")

    n_forward = ingest_forward_mappings(
        con, xlsx_path,
        cim10_version_id=cim10_v_id, cim11_release_id=cim11_v_id,
    )
    t_fwd = time.time()
    if progress:
        print(f"[ingest]   forward mappings : {n_forward} inserted in {t_fwd-t_cim10:.1f}s "
              f"({n_forward / max(t_fwd-t_cim10, 0.1):.0f}/s)")

    n_reverse = ingest_reverse_mappings(
        con, xlsx_path,
        cim10_version_id=cim10_v_id, cim11_release_id=cim11_v_id,
    )
    t_rev = time.time()
    if progress:
        print(f"[ingest]   reverse mappings : {n_reverse} inserted in {t_rev-t_fwd:.1f}s "
              f"({n_reverse / max(t_rev-t_fwd, 0.1):.0f}/s)")

    # Create the seed marker frozen_version
    stats = {
        "cim10_codes": n_cim10,
        "forward_mappings": n_forward,
        "reverse_mappings": n_reverse,
        "cim10_version": cim10_version,
        "cim11_release": cim11_release,
        "duration_seconds": round(t_rev - t0, 1),
    }
    cur = con.execute(
        """INSERT INTO frozen_versions
               (label, description, is_initial_seed, stats_json)
           VALUES (?, ?, 1, ?)""",
        (SEED_VERSION_LABEL,
         "Seed initial issu de la pipeline ../transcodage/ "
         f"(source: {xlsx_path.name}, durée: {stats['duration_seconds']}s)",
         json.dumps(stats, ensure_ascii=False)),
    )
    seed_version_id = cur.lastrowid

    # Tag all freshly-inserted mappings with this version
    con.execute(
        "UPDATE mappings SET current_version_id = ? WHERE current_version_id IS NULL",
        (seed_version_id,),
    )
    con.commit()

    # Audit the operation
    audit_system_action(
        con, action="freeze_version", source="system",
        object_type="frozen_version", object_id=seed_version_id,
        note=f"Initial seed ingested from {xlsx_path.name}",
        new_value=stats,
    )

    if progress:
        print(f"[ingest] ✓ Done in {stats['duration_seconds']}s. "
              f"frozen_version id={seed_version_id} '{SEED_VERSION_LABEL}' created.")

    stats["seed_version_id"] = seed_version_id
    stats["skipped"] = False
    return stats


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    from db.database import get_connection, init_db, set_db_path

    parser = argparse.ArgumentParser(description="Ingest the pipeline XLSX seed")
    parser.add_argument("--xlsx", default=str(DEFAULT_SEED_PATH),
                        help=f"Path to XLSX (default: {DEFAULT_SEED_PATH})")
    parser.add_argument("--cim10-version", default="2026",
                        help="CIM-10 FR version label (default: 2026)")
    parser.add_argument("--cim11-release", default="2024-01",
                        help="CIM-11 MMS release label (default: 2024-01)")
    parser.add_argument("--db-path", help="Override SQLite DB path")
    parser.add_argument("--force-reset", action="store_true",
                        help="Wipe mappings + cim10_codes before re-ingesting")
    args = parser.parse_args(argv)

    if args.db_path:
        set_db_path(args.db_path)
    init_db()
    con = get_connection()
    try:
        stats = ingest_seed(
            con, args.xlsx,
            cim10_version=args.cim10_version,
            cim11_release=args.cim11_release,
            force_reset=args.force_reset,
        )
        print("\n=== Stats ===")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())

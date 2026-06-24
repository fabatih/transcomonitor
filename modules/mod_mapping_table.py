"""
modules/mod_mapping_table.py — Mapping table screens (forward / reverse / split bidir)

Server-side DataGrid for 42 897 forward + 18 505 reverse mappings.
Uses Shiny's `render.data_frame` with paginated SQL queries to avoid loading
the full dataset in memory.

Three views :
  - forward  : CIM-10 → CIM-11
  - reverse  : CIM-11 → CIM-10
  - bidir    : split view (forward + reverse for the same source code) with
               round-trip coherence indicator

Filters (URL-state friendly) :
  - chapitre, fiabilite, status, target_kind, source_decision, est_classant,
  - text search (source_code prefix, libellé contains)
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from shiny import module, reactive, render, ui

import pandas as pd

from db.database import get_connection


# ─────────────────────────────────────────────────────────────────────────
# Helpers — SQL filter building
# ─────────────────────────────────────────────────────────────────────────

FIABILITE_VALUES = ["TRES_HAUTE", "HAUTE", "MOYENNE", "BASSE", "HERITAGE", "CONTESTEE", "NON_RESOLU"]
STATUS_VALUES    = ["propose", "en_revue", "valide", "conteste", "rejete", "gele"]
TARGET_KIND_VALUES = ["mms_simple", "mms_cluster", "foundation_only", "cim10_code", "non_mappable"]


def _build_where(
    direction: str,
    *,
    chapitre: Optional[str] = None,
    fiabilite: Optional[list[str]] = None,
    status: Optional[list[str]] = None,
    target_kind: Optional[list[str]] = None,
    source_decision: Optional[str] = None,
    only_classant: bool = False,
    only_cma: bool = False,
    type_code: Optional[list[str]] = None,
    text_search: Optional[str] = None,
    # Reverse-only filters (passed through but handled in fetch_mappings reverse branch)
    target_type_code: Optional[list[str]] = None,
    target_only_classant: bool = False,
    target_only_cma: bool = False,
) -> tuple[str, list]:
    """Build a parameterized WHERE clause for a mappings query (forward direction).

    Reverse direction filters are handled directly in fetch_mappings to keep
    the SQL clean (the JOIN structure differs).
    """
    where = ["m.direction = ?"]
    params: list = [direction]

    if chapitre:
        where.append("c.chapitre = ?"); params.append(chapitre)
    if fiabilite:
        ph = ",".join("?" for _ in fiabilite)
        where.append(f"m.fiabilite IN ({ph})"); params.extend(fiabilite)
    if status:
        ph = ",".join("?" for _ in status)
        where.append(f"m.status IN ({ph})"); params.extend(status)
    if target_kind:
        ph = ",".join("?" for _ in target_kind)
        where.append(f"m.target_kind IN ({ph})"); params.extend(target_kind)
    if source_decision:
        where.append("m.source_decision = ?"); params.append(source_decision)
    if only_classant:
        where.append("c.est_classant = 1")
    if only_cma:
        where.append("c.est_cma = 1")
    if type_code:
        ph = ",".join("?" for _ in type_code)
        where.append(f"c.type_code IN ({ph})"); params.extend(type_code)
    if text_search:
        s = text_search.strip()
        if s:
            where.append("(m.source_code LIKE ? OR c.libelle_fr LIKE ?)")
            params.extend([f"{s}%", f"%{s}%"])

    return " AND ".join(where), params


# ─────────────────────────────────────────────────────────────────────────
# Data fetchers (server-side pagination)
# ─────────────────────────────────────────────────────────────────────────

def fetch_mappings(
    con: sqlite3.Connection, direction: str, *,
    limit: int = 200, offset: int = 0, sort: str = "source_code", desc: bool = False,
    **filters,
) -> pd.DataFrame:
    """Fetch a page of mappings with optional filters.

    For forward direction, the source is in cim10_codes (joined).
    For reverse, the source is in cim11_linearizations (joined when available).

    Per plan §16.1 : both source and target labels are resolved server-side.
    Per plan §16.5 : `assignment_list_id` filter narrows to the codes of a
                     specific worklist (via list_codes_in_assignment).
    """
    # Extract worklist filter first (applied as a code IN list)
    assignment_list_id = filters.pop("assignment_list_id", None)
    code_filter: Optional[list[str]] = None
    if assignment_list_id is not None:
        from modules.mod_worklist import list_codes_in_assignment
        code_filter = list_codes_in_assignment(con, int(assignment_list_id))
    where, params = _build_where(direction, **filters)
    safe_sort = sort if sort in (
        "source_code", "target_mms_code", "target_cim10_code", "fiabilite",
        "status", "relation_type", "updated_at",
    ) else "source_code"
    direction_kw = "DESC" if desc else "ASC"

    if direction == "forward":
        # Resolve target_mms_code label via cim11_linearizations using
        # target_release_id → version_label. For clusters (BA00&XN8P1),
        # the LEFT JOIN won't match — fallback to denormalized target_label
        # (populated at ingest from libelle_cim11_final) per plan §16.1.
        # Worklist filter (§16.5) : restrict to a set of source codes.
        if code_filter is not None:
            if not code_filter:
                # Empty list → no result
                return pd.DataFrame()
            ph = ",".join("?" for _ in code_filter)
            where = where + f" AND m.source_code IN ({ph})"
            params = params + list(code_filter)
        sql = f"""
            SELECT m.id, m.source_code, c.libelle_fr AS libelle_source,
                   m.target_mms_code AS target_code, m.target_kind,
                   m.target_components, m.target_foundation_uris,
                   COALESCE(lt.label_fr, m.target_label) AS libelle_target_simple,
                   m.relation_type, m.fiabilite, m.source_decision, m.status,
                   c.est_classant, c.est_cma, c.niveau_cma, c.chapitre, c.type_code,
                   m.last_validated_at, m.updated_at
            FROM mappings m
            LEFT JOIN cim10_codes c ON c.code = m.source_code
            LEFT JOIN nomenclature_versions nv ON nv.id = m.target_release_id
            LEFT JOIN cim11_linearizations lt
                ON lt.code = m.target_mms_code
                AND lt.release = nv.version_label
            WHERE {where}
            ORDER BY {safe_sort} {direction_kw}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = con.execute(sql, params).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
        if not df.empty:
            df["libelle_target"] = df.apply(
                lambda r: _assemble_target_label(
                    r["target_kind"], r["target_code"],
                    r["libelle_target_simple"], r["target_components"],
                ),
                axis=1,
            )
            df = df.drop(columns=["target_components", "libelle_target_simple"])
        return df

    # ── Reverse ──
    sql = """
        SELECT m.id, m.source_code, l.label_fr AS libelle_source,
               m.target_cim10_code AS target_code, m.target_kind,
               m.target_components, m.target_foundation_uris,
               COALESCE(ct.libelle_fr, m.target_label) AS libelle_target,
               m.relation_type, m.fiabilite, m.source_decision, m.status,
               ct.est_classant, ct.est_cma, ct.niveau_cma, l.chapitre, ct.type_code,
               m.last_validated_at, m.updated_at
        FROM mappings m
        LEFT JOIN cim11_linearizations l
            ON l.code = m.source_code
            AND l.release = (SELECT version_label FROM nomenclature_versions WHERE id = m.source_version_id)
        LEFT JOIN cim10_codes ct ON ct.code = m.target_cim10_code
        WHERE m.direction = ?
    """
    rev_params: list = [direction]
    # Reverse filters: source side = cim11 (chapitre via l.chapitre),
    #                  target side = cim10 (type_code, classant, cma via ct.*).
    if filters.get("chapitre"):
        sql += " AND l.chapitre = ?"; rev_params.append(filters["chapitre"])
    if filters.get("fiabilite"):
        ph = ",".join("?" for _ in filters["fiabilite"])
        sql += f" AND m.fiabilite IN ({ph})"; rev_params.extend(filters["fiabilite"])
    if filters.get("status"):
        ph = ",".join("?" for _ in filters["status"])
        sql += f" AND m.status IN ({ph})"; rev_params.extend(filters["status"])
    if filters.get("target_kind"):
        ph = ",".join("?" for _ in filters["target_kind"])
        sql += f" AND m.target_kind IN ({ph})"; rev_params.extend(filters["target_kind"])
    if filters.get("source_decision"):
        sql += " AND m.source_decision = ?"; rev_params.append(filters["source_decision"])
    if filters.get("target_type_code"):
        ph = ",".join("?" for _ in filters["target_type_code"])
        sql += f" AND ct.type_code IN ({ph})"; rev_params.extend(filters["target_type_code"])
    if filters.get("target_only_classant"):
        sql += " AND ct.est_classant = 1"
    if filters.get("target_only_cma"):
        sql += " AND ct.est_cma = 1"
    if filters.get("text_search"):
        s = filters["text_search"].strip()
        if s:
            sql += " AND (m.source_code LIKE ? OR l.label_fr LIKE ?)"
            rev_params.extend([f"{s}%", f"%{s}%"])
    # Worklist filter (§16.5) : restrict to a set of source codes
    if code_filter is not None:
        if not code_filter:
            return pd.DataFrame()
        ph = ",".join("?" for _ in code_filter)
        sql += f" AND m.source_code IN ({ph})"
        rev_params.extend(code_filter)
    sql += f" ORDER BY {safe_sort} {direction_kw} LIMIT ? OFFSET ?"
    rev_params.extend([limit, offset])
    rows = con.execute(sql, rev_params).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        # Reverse target is always a cim10_code → label already resolved
        df = df.drop(columns=["target_components"])
    return df


def _assemble_target_label(
    target_kind: str, target_code: Optional[str],
    simple_label: Optional[str], target_components_json: Optional[str],
) -> str:
    """Forward only : build the human-readable label for the target.

    - mms_simple   : just the simple label (cim11_linearizations.label_fr)
    - mms_cluster  : join component labels with ' & ' / ' / '
    - foundation_only : '(fondation seule)'
    - non_mappable : '—'
    """
    if target_kind == "non_mappable" or not target_code:
        return "—"
    if target_kind == "foundation_only":
        return "(fondation directe)"
    if target_kind == "mms_simple":
        return simple_label or "(libellé manquant)"
    if target_kind == "mms_cluster":
        # Try to assemble from target_components first
        try:
            comps = json.loads(target_components_json) if target_components_json else []
        except (json.JSONDecodeError, TypeError):
            comps = []
        # Components may have been enriched at edit time with `label_fr` but the seed
        # ingest doesn't store per-component labels. Fallback : show the raw cluster.
        labels = [c.get("label_fr") for c in comps if c.get("label_fr")]
        if labels:
            sep = " & "  # simplistic — distinguishing / vs & requires re-parsing
            return sep.join(labels)
        return simple_label or target_code  # fallback to raw cluster string
    return simple_label or "—"


def count_mappings(con: sqlite3.Connection, direction: str, **filters) -> int:
    """Count matching mappings (for pagination)."""
    assignment_list_id = filters.pop("assignment_list_id", None)
    code_filter: Optional[list[str]] = None
    if assignment_list_id is not None:
        from modules.mod_worklist import list_codes_in_assignment
        code_filter = list_codes_in_assignment(con, int(assignment_list_id))
        if not code_filter:
            return 0
    if direction == "forward":
        where, params = _build_where(direction, **filters)
        if code_filter is not None:
            ph = ",".join("?" for _ in code_filter)
            where = where + f" AND m.source_code IN ({ph})"
            params = params + list(code_filter)
        sql = f"""
            SELECT COUNT(*) FROM mappings m
            LEFT JOIN cim10_codes c ON c.code = m.source_code
            WHERE {where}
        """
        return con.execute(sql, params).fetchone()[0]
    # reverse
    sql = """
        SELECT COUNT(*) FROM mappings m
        LEFT JOIN cim11_linearizations l
            ON l.code = m.source_code
            AND l.release = (SELECT version_label FROM nomenclature_versions WHERE id = m.source_version_id)
        LEFT JOIN cim10_codes ct ON ct.code = m.target_cim10_code
        WHERE m.direction = ?
    """
    params: list = [direction]
    if filters.get("chapitre"):
        sql += " AND l.chapitre = ?"; params.append(filters["chapitre"])
    if filters.get("fiabilite"):
        ph = ",".join("?" for _ in filters["fiabilite"])
        sql += f" AND m.fiabilite IN ({ph})"; params.extend(filters["fiabilite"])
    if filters.get("status"):
        ph = ",".join("?" for _ in filters["status"])
        sql += f" AND m.status IN ({ph})"; params.extend(filters["status"])
    if filters.get("target_kind"):
        ph = ",".join("?" for _ in filters["target_kind"])
        sql += f" AND m.target_kind IN ({ph})"; params.extend(filters["target_kind"])
    if filters.get("source_decision"):
        sql += " AND m.source_decision = ?"; params.append(filters["source_decision"])
    if filters.get("target_type_code"):
        ph = ",".join("?" for _ in filters["target_type_code"])
        sql += f" AND ct.type_code IN ({ph})"; params.extend(filters["target_type_code"])
    if filters.get("target_only_classant"):
        sql += " AND ct.est_classant = 1"
    if filters.get("target_only_cma"):
        sql += " AND ct.est_cma = 1"
    if filters.get("text_search"):
        s = filters["text_search"].strip()
        if s:
            sql += " AND (m.source_code LIKE ? OR l.label_fr LIKE ?)"
            params.extend([f"{s}%", f"%{s}%"])
    if code_filter is not None:
        ph = ",".join("?" for _ in code_filter)
        sql += f" AND m.source_code IN ({ph})"
        params.extend(code_filter)
    return con.execute(sql, params).fetchone()[0]


def fetch_split_pair(
    con: sqlite3.Connection, source_code: str, source_direction: str,
) -> dict:
    """For a given source code in a direction, return ALL the forward and
    reverse mappings (round-trip view, supports funnels per plan §16.13).

    Returns dict :
      - forward      : list[dict] of forward mappings (may be empty)
      - reverse      : list[dict] of reverse mappings (may be empty)
      - forward_siblings : list[dict] — other forward mappings that share the
                          SAME target_mms_code as the selected forward(s)
                          (i.e. the "n:1 forward" funnel : N CIM-10 → 1 CIM-11)
      - reverse_siblings : list[dict] — other reverse mappings that share the
                          SAME target_cim10_code as the selected reverse(s)
                          (i.e. the "n:1 reverse" funnel : N CIM-11 → 1 CIM-10)
      - is_funnel_forward : bool (len(forward) > 1 OR len(forward_siblings) > 0)
      - is_funnel_reverse : bool (len(reverse) > 1 OR len(reverse_siblings) > 0)
      - roundtrip_coherence : best coherence across all pairs
      - roundtrip_explanation : prose summary
      - roundtrip_matrix     : list of {fwd_idx, rev_idx, coherence, detail}

    Each forward/reverse entry includes libelle_source and libelle_target
    resolved via cim10_codes / cim11_linearizations joins (plan §16.1).
    """
    src_label, src_chap = _resolve_code_label(con, source_code, source_direction)

    if source_direction == "forward":
        fwds = _fetch_split_forward_rows(con, source_code=source_code)
        # Reverse rows for each distinct target_mms_code (funnel n:1)
        target_codes = sorted({f["target_mms_code"] for f in fwds if f["target_mms_code"]})
        revs: list[dict] = []
        for tc in target_codes:
            revs.extend(_fetch_split_reverse_rows(con, source_code=tc))
        # Forward siblings : other CIM-10 codes that map to the same target_mms_code(s)
        forward_siblings: list[dict] = []
        for tc in target_codes:
            sibs = _fetch_forward_siblings_by_target(con, target_mms_code=tc,
                                                      exclude_source=source_code)
            forward_siblings.extend(sibs)
        # Reverse siblings : other reverses pointing to the same target as the
        # *main* reverse(s), excluding the ones already in `revs`.
        rev_ids = {r["id"] for r in revs}
        rev_targets = sorted({r["target_cim10_code"] for r in revs if r["target_cim10_code"]})
        reverse_siblings: list[dict] = []
        for cim10 in rev_targets:
            sibs = _fetch_reverse_siblings_by_target(con, target_cim10_code=cim10,
                                                      exclude_source=None)
            reverse_siblings.extend(s for s in sibs if s["id"] not in rev_ids)
    else:
        revs = _fetch_split_reverse_rows(con, source_code=source_code)
        target_codes = sorted({r["target_cim10_code"] for r in revs if r["target_cim10_code"]})
        fwds: list[dict] = []
        for tc in target_codes:
            fwds.extend(_fetch_split_forward_rows(con, source_code=tc))
        # Reverse siblings : other CIM-11 codes pointing to same target_cim10_code(s)
        reverse_siblings: list[dict] = []
        for tc in target_codes:
            sibs = _fetch_reverse_siblings_by_target(con, target_cim10_code=tc,
                                                      exclude_source=source_code)
            reverse_siblings.extend(sibs)
        # Forward siblings : other forwards pointing to the same MMS target as the
        # *main* forward(s), excluding the ones already in `fwds`.
        fwd_ids = {f["id"] for f in fwds}
        fwd_targets = sorted({f["target_mms_code"] for f in fwds if f["target_mms_code"]})
        forward_siblings: list[dict] = []
        for mms in fwd_targets:
            sibs = _fetch_forward_siblings_by_target(con, target_mms_code=mms,
                                                      exclude_source=None)
            forward_siblings.extend(s for s in sibs if s["id"] not in fwd_ids)

    # ── Compute coherence matrix ──
    matrix: list[dict] = []
    for i, f in enumerate(fwds):
        for j, r in enumerate(revs):
            fs = f["source_code"]
            rt = r.get("target_cim10_code")
            if fs and rt and fs == rt:
                c = "STRICT"
                detail = f"Round-trip strict : {fs} ↔ {f['target_mms_code']}"
            elif fs and rt and fs[:3] == rt[:3]:
                c = "CATEGORIE"
                detail = f"Même catégorie 3 caractères ({fs[:3]})"
            else:
                c = "DISCORDANT"
                detail = f"Forward {fs} → {f['target_mms_code']} ; reverse → {rt}"
            matrix.append({
                "fwd_idx": i, "rev_idx": j,
                "coherence": c, "detail": detail,
            })

    # Synthesize best coherence
    if not fwds and not revs:
        best = "NO_DATA"; expl = "Aucun mapping trouvé."
    elif fwds and not revs:
        best = "C11_NOT_IN_REVERSE"
        expl = f"Le(s) code(s) cible(s) {[f['target_mms_code'] for f in fwds]} n'apparaît pas en reverse."
    elif revs and not fwds:
        best = "C10_NOT_IN_FORWARD"
        expl = f"Le(s) code(s) cible(s) {[r['target_cim10_code'] for r in revs]} n'apparaît pas en forward."
    else:
        order = ["STRICT", "CATEGORIE", "DISCORDANT"]
        ranks = [order.index(m["coherence"]) for m in matrix if m["coherence"] in order]
        best_rank = min(ranks) if ranks else 999
        best = order[best_rank] if best_rank < len(order) else "DISCORDANT"
        if len(fwds) > 1 or len(revs) > 1 or forward_siblings or reverse_siblings:
            n_sib = len(forward_siblings) + len(reverse_siblings)
            expl = (f"Funnel : {len(fwds)} forward × {len(revs)} reverse principal(s)"
                    + (f" + {n_sib} code(s) apparenté(s)" if n_sib else "")
                    + f". Meilleure cohérence : {best}")
        else:
            expl = matrix[0]["detail"] if matrix else "Cohérence indéterminée."

    is_funnel_fwd = len(fwds) > 1 or len(forward_siblings) > 0
    is_funnel_rev = len(revs) > 1 or len(reverse_siblings) > 0

    return {
        "source_code": source_code,
        "source_direction": source_direction,
        "source_label": src_label,
        "source_chapter": src_chap,
        "forward": fwds,
        "reverse": revs,
        "forward_siblings": forward_siblings,
        "reverse_siblings": reverse_siblings,
        "is_funnel_forward": is_funnel_fwd,
        "is_funnel_reverse": is_funnel_rev,
        "roundtrip_coherence": best,
        "roundtrip_explanation": expl,
        "roundtrip_matrix": matrix,
    }


def _fetch_forward_siblings_by_target(
    con: sqlite3.Connection, *,
    target_mms_code: str, exclude_source: Optional[str],
) -> list[dict]:
    """Forward mappings other than `exclude_source` that share the same MMS target."""
    sql = """
        SELECT m.*, c.libelle_fr AS libelle_source,
               c.est_classant, c.est_cma, c.type_code AS source_type_code,
               COALESCE(lt.label_fr, m.target_label) AS libelle_target
        FROM mappings m
        LEFT JOIN cim10_codes c ON c.code = m.source_code
        LEFT JOIN nomenclature_versions nv ON nv.id = m.target_release_id
        LEFT JOIN cim11_linearizations lt
            ON lt.code = m.target_mms_code AND lt.release = nv.version_label
        WHERE m.direction = 'forward' AND m.target_mms_code = ?
    """
    params: list = [target_mms_code]
    if exclude_source:
        sql += " AND m.source_code <> ?"
        params.append(exclude_source)
    sql += " ORDER BY m.source_code LIMIT 200"  # cap to avoid huge lists
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _fetch_reverse_siblings_by_target(
    con: sqlite3.Connection, *,
    target_cim10_code: str, exclude_source: Optional[str],
) -> list[dict]:
    """Reverse mappings other than `exclude_source` that share the same CIM-10 target."""
    sql = """
        SELECT m.*, l.label_fr AS libelle_source, l.chapitre AS cim11_chapitre,
               COALESCE(ct.libelle_fr, m.target_label) AS libelle_target,
               ct.est_classant, ct.est_cma, ct.type_code AS target_type_code
        FROM mappings m
        LEFT JOIN cim11_linearizations l
            ON l.code = m.source_code
            AND l.release = (SELECT version_label FROM nomenclature_versions WHERE id = m.source_version_id)
        LEFT JOIN cim10_codes ct ON ct.code = m.target_cim10_code
        WHERE m.direction = 'reverse' AND m.target_cim10_code = ?
    """
    params: list = [target_cim10_code]
    if exclude_source:
        sql += " AND m.source_code <> ?"
        params.append(exclude_source)
    sql += " ORDER BY m.source_code LIMIT 200"
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _resolve_code_label(
    con: sqlite3.Connection, code: str, direction: str,
) -> tuple[Optional[str], Optional[str]]:
    """Return (label_fr, chapitre) for a source code in a given direction."""
    if direction == "forward":
        row = con.execute(
            "SELECT libelle_fr, chapitre FROM cim10_codes WHERE code = ?", (code,)
        ).fetchone()
        if row:
            return row[0], row[1]
    else:
        # CIM-11 source : try the most recent release first
        row = con.execute(
            """SELECT label_fr, chapitre FROM cim11_linearizations
               WHERE code = ? ORDER BY release DESC LIMIT 1""", (code,)
        ).fetchone()
        if row:
            return row[0], row[1]
    return None, None


def _fetch_split_forward_rows(
    con: sqlite3.Connection, *, source_code: str,
) -> list[dict]:
    """Forward mappings for a given CIM-10 source code, with target label."""
    rows = con.execute(
        """SELECT m.*, c.libelle_fr AS libelle_source,
                  c.est_classant, c.est_cma, c.type_code AS source_type_code,
                  COALESCE(lt.label_fr, m.target_label) AS libelle_target
           FROM mappings m
           LEFT JOIN cim10_codes c ON c.code = m.source_code
           LEFT JOIN nomenclature_versions nv ON nv.id = m.target_release_id
           LEFT JOIN cim11_linearizations lt
               ON lt.code = m.target_mms_code AND lt.release = nv.version_label
           WHERE m.direction = 'forward' AND m.source_code = ?
           ORDER BY m.id""",
        (source_code,),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_split_reverse_rows(
    con: sqlite3.Connection, *, source_code: str,
) -> list[dict]:
    """Reverse mappings for a given CIM-11 source code, with target label."""
    rows = con.execute(
        """SELECT m.*, l.label_fr AS libelle_source, l.chapitre AS cim11_chapitre,
                  COALESCE(ct.libelle_fr, m.target_label) AS libelle_target,
                  ct.est_classant, ct.est_cma,
                  ct.type_code AS target_type_code
           FROM mappings m
           LEFT JOIN cim11_linearizations l
               ON l.code = m.source_code
               AND l.release = (SELECT version_label FROM nomenclature_versions WHERE id = m.source_version_id)
           LEFT JOIN cim10_codes ct ON ct.code = m.target_cim10_code
           WHERE m.direction = 'reverse' AND m.source_code = ?
           ORDER BY m.id""",
        (source_code,),
    ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# UI helpers — badges for fiabilite, status, target_kind
# ─────────────────────────────────────────────────────────────────────────

_FIAB_BADGE = {
    "TRES_HAUTE": "success",
    "HAUTE":      "success",
    "MOYENNE":    "warning",
    "BASSE":      "warning",
    "HERITAGE":   "secondary",
    "CONTESTEE":  "danger",
    "NON_RESOLU": "danger",
}
_STATUS_BADGE = {
    "propose":   "secondary",
    "en_revue":  "info",
    "valide":    "success",
    "conteste":  "warning",
    "rejete":    "danger",
    "gele":      "dark",
}


def _badge_html(value: str, mapping: dict) -> str:
    color = mapping.get(value, "secondary")
    return f'<span class="badge bg-{color}">{value or "—"}</span>'


# ─────────────────────────────────────────────────────────────────────────
# UI — filter bar
# ─────────────────────────────────────────────────────────────────────────

def _filter_bar(direction: str) -> ui.Tag:
    """Render the filter sidebar for a mappings view.

    Per plan §16.2 : filters are direction-specific.
      - Forward : source = CIM-10 → expose classant/cma/type_code on the source ;
                  target_kind in {mms_simple, mms_cluster, foundation_only, non_mappable}.
      - Reverse : target = CIM-10 → expose target_classant/target_cma/target_type_code
                  on the target ; target_kind in {cim10_code, non_mappable}.
    """
    # target_kind choices per direction
    if direction == "forward":
        target_kind_choices = {
            "mms_simple":      "MMS simple (1 code)",
            "mms_cluster":     "Cluster post-coordonné",
            "foundation_only": "Fondation seule",
            "non_mappable":    "Non mappable",
        }
    else:
        target_kind_choices = {
            "cim10_code":   "Code CIM-10 cible",
            "non_mappable": "Non mappable",
        }

    # type_code choices (same for both, but filter applies on source CIM-10
    # in forward and on target CIM-10 in reverse)
    type_code_choices = {
        "INTERNATIONAL_BASE":      "International (base)",
        "INTERNATIONAL_EXTENSION": "International (extension ClaML)",
        "FR_ONLY":                 "Spécifique France (FR_ONLY)",
        "WHO_POST_2019":           "OMS post-2019 (ex. COVID)",
    }

    # PMSI checkbox section label adapts to direction
    if direction == "forward":
        pmsi_label = "Sur la source CIM-10"
        type_code_label = "Type de code CIM-10 (source)"
    else:
        pmsi_label = "Sur la cible CIM-10"
        type_code_label = "Type de code CIM-10 (cible)"

    return ui.div(
        ui.h6("Filtres", class_="mb-2"),
        ui.input_text("filter_search", "Recherche",
                      placeholder="Code ou libellé…"),
        ui.input_select(
            "filter_chapitre", "Chapitre",
            choices={"": "(tous)", **{f"{c:02d}": f"Chapitre {c:02d}" for c in range(1, 23)},
                     **({"XX": "Extension XX"} if direction == "forward" else {})},
            selected="",
        ),
        ui.input_checkbox_group(
            "filter_fiabilite", "Fiabilité",
            choices={v: v for v in FIABILITE_VALUES},
        ),
        ui.input_checkbox_group(
            "filter_status", "Statut",
            choices={v: v for v in STATUS_VALUES},
            selected=["propose", "en_revue", "valide"],
        ),
        ui.input_checkbox_group(
            "filter_target_kind", "Type de cible",
            choices=target_kind_choices,
        ),
        ui.hr(),
        ui.tags.div(pmsi_label, class_="small fw-bold text-muted mb-1"),
        ui.input_checkbox("filter_classant", "Uniquement classant PMSI", value=False),
        ui.input_checkbox("filter_cma", "Uniquement CMA", value=False),
        ui.input_checkbox_group(
            "filter_type_code", type_code_label,
            choices=type_code_choices,
        ),
        ui.input_action_button("apply_filters", "Appliquer",
                                class_="btn btn-primary btn-sm w-100 mt-2"),
        ui.input_action_button("reset_filters", "Réinitialiser",
                                class_="btn btn-outline-secondary btn-sm w-100 mt-1"),
        class_="card card-body bg-light",
        style="position: sticky; top: 1rem;",
    )


# ─────────────────────────────────────────────────────────────────────────
# Module : single mapping table view
# ─────────────────────────────────────────────────────────────────────────

@module.ui
def mapping_table_ui(direction: str = "forward") -> ui.Tag:
    """Layout : filter sidebar + paginated table + count + action bar + cross-direction panel."""
    title = "CIM-10 → CIM-11" if direction == "forward" else "CIM-11 → CIM-10"
    other_dir_label = "reverse" if direction == "forward" else "forward"
    return ui.div(
        ui.h4(
            ui.tags.i(class_="bi bi-table me-2"),
            f"Mappings {title}",
        ),
        ui.layout_sidebar(
            ui.sidebar(_filter_bar(direction), width=280),
            ui.div(
                ui.output_ui("count_info"),
                ui.output_data_frame("mappings_grid"),
                ui.output_ui("pagination_controls"),
                # Cross-direction panel (plan §16.3.2)
                ui.tags.hr(class_="my-3"),
                ui.h6(
                    ui.tags.i(class_="bi bi-arrows-collapse me-2"),
                    f"Correspondances {other_dir_label} pour la ligne sélectionnée",
                    class_="small text-muted",
                ),
                ui.output_ui("cross_direction_panel"),
            ),
        ),
    )


@module.server
def mapping_table_server(
    input, output, session,
    direction: str = "forward",
    page_size: int = 100,
    on_row_select=None,  # callback : on_row_select(mapping_id)
):
    """Server logic for the mapping table view."""

    current_page = reactive.value(1)
    applied_filters = reactive.value({})

    @reactive.effect
    @reactive.event(input.apply_filters)
    def _apply():
        f = {
            "text_search": input.filter_search() or None,
            "chapitre":    input.filter_chapitre() or None,
            "fiabilite":   list(input.filter_fiabilite() or []),
            "status":      list(input.filter_status() or []),
            "target_kind": list(input.filter_target_kind() or []),
        }
        type_codes = list(input.filter_type_code() or [])
        if direction == "forward":
            # On forward, PMSI flags + type_code apply to the source CIM-10
            f["only_classant"] = bool(input.filter_classant())
            f["only_cma"]      = bool(input.filter_cma())
            f["type_code"]     = type_codes or None
        else:
            # On reverse, PMSI flags + type_code apply to the target CIM-10
            f["target_only_classant"] = bool(input.filter_classant())
            f["target_only_cma"]      = bool(input.filter_cma())
            f["target_type_code"]     = type_codes or None
        applied_filters.set(f)
        current_page.set(1)

    @reactive.effect
    @reactive.event(input.reset_filters)
    def _reset():
        applied_filters.set({})
        current_page.set(1)
        ui.update_text("filter_search", value="")
        ui.update_select("filter_chapitre", selected="")
        ui.update_checkbox_group("filter_fiabilite", selected=[])
        ui.update_checkbox_group("filter_status", selected=["propose", "en_revue", "valide"])
        ui.update_checkbox_group("filter_target_kind", selected=[])
        ui.update_checkbox_group("filter_type_code", selected=[])
        ui.update_checkbox("filter_classant", value=False)
        ui.update_checkbox("filter_cma", value=False)

    # Trigger filter apply once at startup so default status filter takes effect
    @reactive.effect
    def _initial():
        applied_filters.set({
            "status": ["propose", "en_revue", "valide"],
        })

    @reactive.calc
    def total_count() -> int:
        con = get_connection()
        try:
            return count_mappings(con, direction, **applied_filters())
        finally:
            con.close()

    @reactive.calc
    def page_data() -> pd.DataFrame:
        con = get_connection()
        try:
            return fetch_mappings(
                con, direction,
                limit=page_size,
                offset=(current_page() - 1) * page_size,
                **applied_filters(),
            )
        finally:
            con.close()

    @output
    @render.ui
    def count_info():
        n = total_count()
        page = current_page()
        n_pages = max(1, (n + page_size - 1) // page_size)
        return ui.div(
            ui.span(f"{n:,} mapping(s) — page {page}/{n_pages}",
                    class_="text-muted small"),
            class_="mb-2",
        )

    @output
    @render.data_frame
    def mappings_grid():
        df = page_data()
        if df.empty:
            return render.DataGrid(pd.DataFrame(columns=["(aucun résultat)"]))
        # Project useful columns + format (per plan §16.1 : show both
        # source and target labels)
        cols = ["id", "source_code", "libelle_source", "target_code", "libelle_target",
                "target_kind", "relation_type", "fiabilite",
                "source_decision", "status"]
        if direction == "forward":
            cols += ["est_classant", "est_cma", "chapitre"]
        else:
            cols += ["est_classant", "est_cma", "chapitre", "type_code"]
        cols = [c for c in cols if c in df.columns]
        df = df[cols].copy()
        # Truncate long labels for grid readability
        for label_col in ("libelle_source", "libelle_target"):
            if label_col in df.columns:
                df[label_col] = df[label_col].astype(str).str.slice(0, 60)
        return render.DataGrid(df, selection_mode="row", height="600px",
                                width="100%", summary=False)

    @output
    @render.ui
    def pagination_controls():
        n = total_count()
        n_pages = max(1, (n + page_size - 1) // page_size)
        page = current_page()
        return ui.div(
            ui.input_action_button("page_prev", "« Précédent",
                                    class_="btn btn-outline-primary btn-sm me-2",
                                    disabled=(page <= 1)),
            ui.span(f"Page {page} / {n_pages}", class_="mx-2"),
            ui.input_action_button("page_next", "Suivant »",
                                    class_="btn btn-outline-primary btn-sm ms-2",
                                    disabled=(page >= n_pages)),
            class_="d-flex align-items-center mt-3",
        )

    @reactive.effect
    @reactive.event(input.page_prev)
    def _prev():
        if current_page() > 1:
            current_page.set(current_page() - 1)

    @reactive.effect
    @reactive.event(input.page_next)
    def _next():
        n = total_count()
        n_pages = max(1, (n + page_size - 1) // page_size)
        if current_page() < n_pages:
            current_page.set(current_page() + 1)

    # Row selection → trigger callback with mapping_id + remember the row's data
    selected_row_data = reactive.value(None)

    @reactive.effect
    def _on_select():
        sel = input.mappings_grid_selected_rows()
        if sel and on_row_select is not None:
            df = page_data()
            if not df.empty and sel[0] < len(df):
                row = df.iloc[sel[0]]
                mapping_id = int(row["id"])
                selected_row_data.set(row.to_dict())
                on_row_select(mapping_id)
        else:
            selected_row_data.set(None)

    @output
    @render.ui
    def cross_direction_panel():
        """Panel below the grid showing the corresponding mappings in the other
        direction for the selected row (plan §16.3.2)."""
        row = selected_row_data()
        if row is None:
            return ui.div("Sélectionnez une ligne pour voir les correspondances.",
                          class_="text-muted small")
        # The "other direction" depends on this table's direction
        if direction == "forward":
            # forward: source=CIM-10, target=MMS. Lookup all reverses where source=target_mms_code
            target_code = row.get("target_code")
            if not target_code:
                return ui.div("Pas de cible (non mappable).", class_="text-muted small")
            con = get_connection()
            try:
                rows = con.execute(
                    """SELECT m.id, m.source_code, COALESCE(l.label_fr, m.target_label) AS lib,
                              m.target_cim10_code AS target_code,
                              m.target_kind, m.fiabilite, m.status
                       FROM mappings m
                       LEFT JOIN cim11_linearizations l
                           ON l.code = m.source_code
                           AND l.release = (SELECT version_label FROM nomenclature_versions
                                             WHERE id = m.source_version_id)
                       WHERE m.direction = 'reverse' AND m.source_code = ?
                       LIMIT 20""",
                    (target_code,),
                ).fetchall()
            finally:
                con.close()
            other = "reverse"
        else:
            target_code = row.get("target_code")
            if not target_code:
                return ui.div("Pas de cible (non mappable).", class_="text-muted small")
            con = get_connection()
            try:
                rows = con.execute(
                    """SELECT m.id, m.source_code, c.libelle_fr AS lib,
                              m.target_mms_code AS target_code,
                              m.target_kind, m.fiabilite, m.status
                       FROM mappings m
                       LEFT JOIN cim10_codes c ON c.code = m.source_code
                       WHERE m.direction = 'forward' AND m.source_code = ?
                       LIMIT 20""",
                    (target_code,),
                ).fetchall()
            finally:
                con.close()
            other = "forward"

        if not rows:
            return ui.div(f"Aucun mapping {other} trouvé pour le code cible {target_code}.",
                          class_="alert alert-light small py-2")
        items = []
        for r in rows:
            items.append(ui.tags.li(
                ui.tags.span(
                    f"{r['source_code']} → {r['target_code'] or '—'}",
                    class_="fw-bold me-2",
                ),
                ui.tags.span(
                    (r["lib"] or "(libellé manquant)")[:60],
                    class_="text-muted small me-2",
                ),
                ui.HTML(f'<span class="badge bg-light text-dark me-1">{r["fiabilite"] or "—"}</span>'),
                ui.HTML(f'<span class="badge bg-secondary">{r["status"]}</span>'),
                class_="mb-1",
            ))
        return ui.div(
            ui.tags.span(f"{len(rows)} mapping(s) {other} ", class_="badge bg-info me-2"),
            ui.tags.small(f"pour la cible {target_code} :", class_="text-muted"),
            ui.tags.ul(*items, class_="list-unstyled mt-2 small",
                        style="max-height: 200px; overflow-y: auto;"),
        )

    return {
        "current_page": current_page,
        "total_count":  total_count,
        "page_data":    page_data,
    }


# ─────────────────────────────────────────────────────────────────────────
# Module : split bidirectional view
# ─────────────────────────────────────────────────────────────────────────

@module.ui
def split_bidir_ui() -> ui.Tag:
    """Split view : enter a source code, see both directions side by side."""
    return ui.div(
        ui.h4(
            ui.tags.i(class_="bi bi-arrow-left-right me-2"),
            "Vue bidirectionnelle (round-trip)",
        ),
        ui.div(
            ui.input_radio_buttons(
                "split_direction", "Direction de départ :",
                choices={"forward": "CIM-10 (forward)", "reverse": "CIM-11 (reverse)"},
                selected="forward", inline=True,
            ),
            ui.input_text("split_code", "Code source",
                          placeholder="ex: A011 ou 1A07"),
            ui.input_action_button("split_lookup", "Afficher",
                                    class_="btn btn-primary"),
            class_="d-flex align-items-end gap-2 mb-3",
        ),
        ui.output_ui("split_coherence"),
        ui.layout_columns(
            ui.div(
                ui.h6(ui.tags.i(class_="bi bi-arrow-right-circle me-1"),
                      "Forward (CIM-10 → CIM-11)", class_="text-primary"),
                ui.output_ui("split_forward_card"),
            ),
            ui.div(
                ui.h6(ui.tags.i(class_="bi bi-arrow-left-circle me-1"),
                      "Reverse (CIM-11 → CIM-10)", class_="text-success"),
                ui.output_ui("split_reverse_card"),
            ),
            col_widths=(6, 6),
        ),
    )


@module.server
def split_bidir_server(
    input, output, session,
    selected_bidir_source: Optional[reactive.Value] = None,
):
    """Server for the bidirectional split view.

    `selected_bidir_source` (optional) : reactive value broadcasting
    {"direction": "forward"|"reverse", "code": "A011"} from other tabs.
    When non-None and changed, auto-pre-fills the inputs and triggers lookup.
    The user can still override manually by typing a code and clicking Afficher
    (override flag latches until the next external change).
    """
    pair_data = reactive.value(None)
    last_external = reactive.value(None)

    # Auto-prefill from external selection (Forward/Reverse tab row selection)
    if selected_bidir_source is not None:
        @reactive.effect
        def _auto_prefill():
            sel = selected_bidir_source()
            # Only react to new values (not the same one)
            if sel is None or sel == last_external():
                return
            last_external.set(sel)
            direction = sel.get("direction")
            code = (sel.get("code") or "").strip().upper()
            if not code or direction not in ("forward", "reverse"):
                return
            # Push values into the inputs so the user sees them
            try:
                ui.update_radio_buttons("split_direction", selected=direction)
                ui.update_text("split_code", value=code)
            except Exception:
                pass
            # Trigger the lookup
            con = get_connection()
            try:
                pair_data.set(fetch_split_pair(con, code, direction))
            finally:
                con.close()

    @reactive.effect
    @reactive.event(input.split_lookup)
    def _lookup():
        code = (input.split_code() or "").strip().upper()
        direction = input.split_direction()
        if not code:
            pair_data.set(None)
            return
        con = get_connection()
        try:
            pair_data.set(fetch_split_pair(con, code, direction))
        finally:
            con.close()

    def _format_one_card(m: dict, kind: str) -> ui.Tag:
        """Render a single mapping as a card.
        Used by both forward (kind='forward') and reverse (kind='reverse') panels."""
        source_label = m.get("libelle_source") or "(libellé manquant)"
        target_code = m.get("target_mms_code") or m.get("target_cim10_code") or "(non mappable)"
        target_label = m.get("libelle_target") or "—"
        try:
            f_uris = json.loads(m["target_foundation_uris"]) if m.get("target_foundation_uris") else []
        except (json.JSONDecodeError, TypeError):
            f_uris = []
        return ui.div(
            ui.div(
                ui.div(
                    ui.tags.strong(m["source_code"]),
                    ui.tags.small(f" — {source_label}", class_="text-muted ms-2"),
                    class_="mb-1",
                ),
                ui.div(
                    ui.tags.i(class_="bi bi-arrow-down me-2 text-muted"),
                    ui.tags.strong(target_code),
                    ui.tags.small(f" — {target_label}", class_="text-muted ms-2"),
                    class_="h6 mb-2",
                ),
                ui.div(
                    ui.HTML(_badge_html(m.get("fiabilite"), _FIAB_BADGE)), " ",
                    ui.HTML(_badge_html(m.get("status"), _STATUS_BADGE)), " ",
                    ui.tags.span(f"target_kind: {m.get('target_kind')}",
                                 class_="badge bg-light text-dark"),
                ),
                ui.tags.dl(
                    ui.tags.dt("Relation"), ui.tags.dd(m.get("relation_type") or "—"),
                    ui.tags.dt("Source de décision"), ui.tags.dd(m.get("source_decision") or "—"),
                    ui.tags.dt("Foundation URIs"),
                    ui.tags.dd(
                        ", ".join(u.split("/")[-1] for u in f_uris) if f_uris else "—",
                        title=", ".join(f_uris) if f_uris else None,
                    ),
                    class_="row mt-3 small",
                ),
                class_="card-body",
            ),
            class_="card mb-2",
        )

    def _format_cards(items: list[dict], kind: str) -> ui.Tag:
        """Render a list of mappings as a stack of cards (supports funnels per §16.13)."""
        if not items:
            return ui.div("Aucun mapping trouvé.", class_="alert alert-warning")
        if len(items) > 1:
            heading = ui.div(
                ui.tags.span(
                    f"⚠ Funnel détecté : {len(items)} mappings",
                    class_="badge bg-warning text-dark me-2",
                ),
                class_="mb-2",
            )
        else:
            heading = ui.div()
        return ui.div(heading, *[_format_one_card(m, kind) for m in items])

    @output
    @render.ui
    def split_forward_card():
        pair = pair_data()
        if pair is None:
            return ui.div("Saisir un code source et cliquer sur Afficher.",
                          class_="text-muted small")
        fwd_main = pair.get("forward") or []
        siblings = pair.get("forward_siblings") or []
        return ui.div(
            _format_cards(fwd_main, "forward"),
            _format_siblings_section(siblings, "forward") if siblings else ui.div(),
        )

    @output
    @render.ui
    def split_reverse_card():
        pair = pair_data()
        if pair is None:
            return ui.div()
        rev_main = pair.get("reverse") or []
        siblings = pair.get("reverse_siblings") or []
        return ui.div(
            _format_cards(rev_main, "reverse"),
            _format_siblings_section(siblings, "reverse") if siblings else ui.div(),
        )

    def _format_siblings_section(siblings: list[dict], kind: str) -> ui.Tag:
        """Render a collapsible section showing other mappings sharing the same target.
        Limited to 15 by default to keep the UI usable on heavy funnels."""
        n = len(siblings)
        show_max = 15
        return ui.div(
            ui.tags.hr(class_="my-2"),
            ui.tags.details(
                ui.tags.summary(
                    ui.HTML(f'<span class="badge bg-info text-white me-2">'
                            f'+{n} code(s) apparenté(s)</span>'),
                    ui.tags.span(
                        f"Autres {kind}s pointant vers la même cible "
                        f"(top {min(n, show_max)})",
                        class_="small text-muted",
                    ),
                    class_="mb-2",
                ),
                *[_format_one_card(s, kind) for s in siblings[:show_max]],
                ui.tags.div(
                    f"… {n - show_max} autre(s) non affiché(s).",
                    class_="text-muted small mt-2",
                ) if n > show_max else ui.div(),
            ),
        )

    @output
    @render.ui
    def split_coherence():
        pair = pair_data()
        if pair is None:
            return ui.div()
        coherence = pair["roundtrip_coherence"]
        explanation = pair["roundtrip_explanation"]
        color = {
            "STRICT":              "success",
            "CATEGORIE":           "info",
            "DISCORDANT":          "warning",
            "C11_NOT_IN_REVERSE":  "secondary",
            "C10_NOT_IN_FORWARD":  "secondary",
            "NO_DATA":             "secondary",
        }.get(coherence, "secondary")
        # Add source context header (libellé of the source code)
        src_label = pair.get("source_label") or ""
        src_header = ui.div(
            ui.tags.strong(f"Source : {pair.get('source_code')}"),
            ui.tags.small(f" — {src_label}",
                          class_="text-muted ms-2") if src_label else "",
            ui.tags.small(
                f" (chapitre {pair.get('source_chapter')})",
                class_="text-muted ms-2",
            ) if pair.get("source_chapter") else "",
            class_="mb-2",
        )
        return ui.div(
            src_header,
            ui.div(
                ui.tags.strong("Cohérence round-trip : "),
                ui.HTML(f'<span class="badge bg-{color}">{coherence}</span>'), " ",
                explanation,
                class_=f"alert alert-{color}",
            ),
        )

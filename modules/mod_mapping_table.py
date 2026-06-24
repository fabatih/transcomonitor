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

    Per plan §16.1 : both source and target labels are resolved server-side :
      - Forward source label : cim10_codes.libelle_fr
      - Forward target label : cim11_linearizations.label_fr (or assembled from components)
      - Reverse source label : cim11_linearizations.label_fr
      - Reverse target label : cim10_codes.libelle_fr
    """
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
    if direction == "forward":
        where, params = _build_where(direction, **filters)
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
    return con.execute(sql, params).fetchone()[0]


def fetch_split_pair(
    con: sqlite3.Connection, source_code: str, source_direction: str,
) -> dict:
    """For a given source code in a direction, return ALL the forward and
    reverse mappings (round-trip view, supports funnels per plan §16.13).

    Returns dict :
      - forward      : list[dict] of forward mappings (may be empty)
      - reverse      : list[dict] of reverse mappings (may be empty)
      - is_funnel_forward : bool (len(forward) > 1)
      - is_funnel_reverse : bool (len(reverse) > 1)
      - roundtrip_coherence : best coherence across all pairs (STRICT > CATEGORIE > DISCORDANT)
      - roundtrip_explanation : prose summary
      - roundtrip_matrix     : list of {fwd_idx, rev_idx, coherence, detail} for each pair

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
    else:
        revs = _fetch_split_reverse_rows(con, source_code=source_code)
        # Forward rows whose source_code matches the reverse target (1:n)
        target_codes = sorted({r["target_cim10_code"] for r in revs if r["target_cim10_code"]})
        fwds: list[dict] = []
        for tc in target_codes:
            fwds.extend(_fetch_split_forward_rows(con, source_code=tc))

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
        if len(fwds) > 1 or len(revs) > 1:
            expl = (f"Funnel : {len(fwds)} forward × {len(revs)} reverse = "
                    f"{len(matrix)} paires. Meilleure cohérence : {best}")
        else:
            expl = matrix[0]["detail"] if matrix else "Cohérence indéterminée."

    return {
        "source_code": source_code,
        "source_direction": source_direction,
        "source_label": src_label,
        "source_chapter": src_chap,
        "forward": fwds,
        "reverse": revs,
        "is_funnel_forward": len(fwds) > 1,
        "is_funnel_reverse": len(revs) > 1,
        "roundtrip_coherence": best,
        "roundtrip_explanation": expl,
        "roundtrip_matrix": matrix,
    }


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
    """Render the filter sidebar for a mappings view."""
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
            choices={
                "mms_simple": "MMS simple",
                "mms_cluster": "Cluster post-coord.",
                "foundation_only": "Fondation seule",
                "cim10_code": "Code CIM-10 (reverse)",
                "non_mappable": "Non mappable",
            },
        ),
        ui.input_checkbox("filter_classant", "Uniquement classant PMSI", value=False)
            if direction == "forward" else None,
        ui.input_checkbox("filter_cma", "Uniquement CMA", value=False)
            if direction == "forward" else None,
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
    """Layout : filter sidebar + paginated table + count + action bar."""
    title = "CIM-10 → CIM-11" if direction == "forward" else "CIM-11 → CIM-10"
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
        if direction == "forward":
            f["only_classant"] = bool(input.filter_classant())
            f["only_cma"]      = bool(input.filter_cma())
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
        if direction == "forward":
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

    # Row selection → trigger callback with mapping_id
    @reactive.effect
    def _on_select():
        sel = input.mappings_grid_selected_rows()
        if sel and on_row_select is not None:
            df = page_data()
            if not df.empty and sel[0] < len(df):
                mapping_id = int(df.iloc[sel[0]]["id"])
                on_row_select(mapping_id)

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
def split_bidir_server(input, output, session):
    pair_data = reactive.value(None)

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
        return _format_cards(pair.get("forward") or [], "forward")

    @output
    @render.ui
    def split_reverse_card():
        pair = pair_data()
        if pair is None:
            return ui.div()
        return _format_cards(pair.get("reverse") or [], "reverse")

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

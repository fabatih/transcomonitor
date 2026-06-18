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
    text_search: Optional[str] = None,
) -> tuple[str, list]:
    """Build a parameterized WHERE clause for a mappings query."""
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
    """
    where, params = _build_where(direction, **filters)
    safe_sort = sort if sort in (
        "source_code", "target_mms_code", "target_cim10_code", "fiabilite",
        "status", "relation_type", "updated_at",
    ) else "source_code"
    direction_kw = "DESC" if desc else "ASC"

    if direction == "forward":
        sql = f"""
            SELECT m.id, m.source_code, c.libelle_fr AS libelle_source,
                   m.target_mms_code AS target_code, m.target_kind,
                   m.target_foundation_uris,
                   m.relation_type, m.fiabilite, m.source_decision, m.status,
                   c.est_classant, c.est_cma, c.niveau_cma, c.chapitre,
                   m.last_validated_at, m.updated_at
            FROM mappings m
            LEFT JOIN cim10_codes c ON c.code = m.source_code
            WHERE {where}
            ORDER BY {safe_sort} {direction_kw}
            LIMIT ? OFFSET ?
        """
    else:  # reverse
        sql = f"""
            SELECT m.id, m.source_code, l.label_fr AS libelle_source,
                   m.target_cim10_code AS target_code, m.target_kind,
                   m.target_foundation_uris,
                   m.relation_type, m.fiabilite, m.source_decision, m.status,
                   NULL AS est_classant, NULL AS est_cma, NULL AS niveau_cma,
                   l.chapitre,
                   m.last_validated_at, m.updated_at
            FROM mappings m
            LEFT JOIN cim11_linearizations l
                ON l.code = m.source_code
                AND l.release = (SELECT version_label FROM nomenclature_versions WHERE id = m.source_version_id)
            LEFT JOIN cim10_codes c ON c.code = m.target_cim10_code
            WHERE {where.replace('c.', 'l.' if 'c.chapitre' in where else 'c.')}
            ORDER BY {safe_sort} {direction_kw}
            LIMIT ? OFFSET ?
        """
        # In reverse mode, c. references must point to target c (joined separately).
        # Above's pragmatic fix isn't perfect — use a cleaner pattern for reverse :
        sql = f"""
            SELECT m.id, m.source_code, l.label_fr AS libelle_source,
                   m.target_cim10_code AS target_code, m.target_kind,
                   m.target_foundation_uris,
                   m.relation_type, m.fiabilite, m.source_decision, m.status,
                   c.est_classant, c.est_cma, c.niveau_cma, l.chapitre,
                   m.last_validated_at, m.updated_at
            FROM mappings m
            LEFT JOIN cim11_linearizations l
                ON l.code = m.source_code
                AND l.release = (SELECT version_label FROM nomenclature_versions WHERE id = m.source_version_id)
            LEFT JOIN cim10_codes c ON c.code = m.target_cim10_code
            WHERE m.direction = ?
        """
        # Rebuild params for reverse (no cim10_codes join on source side)
        rev_params: list = [direction]
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
        if filters.get("text_search"):
            s = filters["text_search"].strip()
            if s:
                sql += " AND (m.source_code LIKE ? OR l.label_fr LIKE ?)"
                rev_params.extend([f"{s}%", f"%{s}%"])
        sql += f" ORDER BY {safe_sort} {direction_kw} LIMIT ? OFFSET ?"
        rev_params.extend([limit, offset])
        rows = con.execute(sql, rev_params).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    params.extend([limit, offset])
    rows = con.execute(sql, params).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


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
    if filters.get("text_search"):
        s = filters["text_search"].strip()
        if s:
            sql += " AND (m.source_code LIKE ? OR l.label_fr LIKE ?)"
            params.extend([f"{s}%", f"%{s}%"])
    return con.execute(sql, params).fetchone()[0]


def fetch_split_pair(
    con: sqlite3.Connection, source_code: str, source_direction: str,
) -> dict:
    """For a given source code in a direction, return both the forward and
    reverse mappings (round-trip view). Returns dict with 'forward', 'reverse',
    'roundtrip_coherence', 'roundtrip_explanation'.
    """
    if source_direction == "forward":
        # forward: A → B ; lookup reverse where source_code = B
        fwd = con.execute(
            "SELECT * FROM mappings WHERE direction='forward' AND source_code = ?",
            (source_code,),
        ).fetchone()
        rev = None
        if fwd and fwd["target_mms_code"]:
            rev = con.execute(
                "SELECT * FROM mappings WHERE direction='reverse' AND source_code = ?",
                (fwd["target_mms_code"],),
            ).fetchone()
    else:
        # reverse: B → A ; lookup forward where source_code = A
        rev = con.execute(
            "SELECT * FROM mappings WHERE direction='reverse' AND source_code = ?",
            (source_code,),
        ).fetchone()
        fwd = None
        if rev and rev["target_cim10_code"]:
            fwd = con.execute(
                "SELECT * FROM mappings WHERE direction='forward' AND source_code = ?",
                (rev["target_cim10_code"],),
            ).fetchone()

    # Round-trip coherence
    coherence = "NO_DATA"
    explanation = ""
    if fwd and rev:
        # Forward says A → B ; reverse from B says B → A' ; compare A vs A'
        forward_source = fwd["source_code"]
        reverse_target = rev["target_cim10_code"]
        if forward_source == reverse_target:
            coherence = "STRICT"
            explanation = f"Round-trip strict : {forward_source} ↔ {fwd['target_mms_code']}"
        elif (forward_source and reverse_target and
              forward_source[:3] == reverse_target[:3]):
            coherence = "CATEGORIE"
            explanation = (f"Même catégorie 3 caractères ({forward_source[:3]}) : "
                           f"{forward_source} → {fwd['target_mms_code']} → {reverse_target}")
        else:
            coherence = "DISCORDANT"
            explanation = (f"Discordance : forward {forward_source} → {fwd['target_mms_code']}, "
                           f"reverse → {reverse_target}")
    elif fwd and not rev:
        coherence = "C11_NOT_IN_REVERSE"
        explanation = f"Le code cible {fwd['target_mms_code']} n'apparaît pas en reverse."
    elif rev and not fwd:
        coherence = "C10_NOT_IN_FORWARD"
        explanation = f"Le code cible {rev['target_cim10_code']} n'apparaît pas en forward."

    return {
        "forward": dict(fwd) if fwd else None,
        "reverse": dict(rev) if rev else None,
        "roundtrip_coherence": coherence,
        "roundtrip_explanation": explanation,
    }


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
        # Project useful columns + format
        cols = ["id", "source_code", "libelle_source", "target_code",
                "target_kind", "relation_type", "fiabilite",
                "source_decision", "status"]
        if direction == "forward":
            cols += ["est_classant", "est_cma", "chapitre"]
        else:
            cols += ["chapitre"]
        df = df[cols].copy()
        # Truncate long labels
        if "libelle_source" in df.columns:
            df["libelle_source"] = df["libelle_source"].astype(str).str.slice(0, 60)
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

    def _format_card(m: Optional[dict], kind: str) -> ui.Tag:
        if not m:
            return ui.div("Aucun mapping trouvé.", class_="alert alert-warning")
        target = m.get("target_mms_code") or m.get("target_cim10_code") or "(non mappable)"
        return ui.div(
            ui.div(
                ui.div(
                    ui.tags.strong(f"{m['source_code']}"),
                    " → ",
                    ui.tags.strong(target),
                    class_="h5 mb-2",
                ),
                ui.div(
                    ui.HTML(_badge_html(m["fiabilite"], _FIAB_BADGE)), " ",
                    ui.HTML(_badge_html(m["status"], _STATUS_BADGE)), " ",
                    ui.tags.span(f"target_kind: {m['target_kind']}",
                                 class_="badge bg-light text-dark"),
                ),
                ui.tags.dl(
                    ui.tags.dt("Relation"), ui.tags.dd(m.get("relation_type") or "—"),
                    ui.tags.dt("Source de décision"), ui.tags.dd(m.get("source_decision") or "—"),
                    ui.tags.dt("Foundation URIs"),
                    ui.tags.dd(
                        ", ".join(json.loads(m["target_foundation_uris"]))
                        if m.get("target_foundation_uris") else "—"
                    ),
                    class_="row mt-3",
                ),
                class_="card-body",
            ),
            class_="card",
        )

    @output
    @render.ui
    def split_forward_card():
        pair = pair_data()
        return _format_card(pair["forward"] if pair else None, "forward")

    @output
    @render.ui
    def split_reverse_card():
        pair = pair_data()
        return _format_card(pair["reverse"] if pair else None, "reverse")

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
        return ui.div(
            ui.tags.strong(f"Cohérence round-trip : "),
            ui.HTML(f'<span class="badge bg-{color}">{coherence}</span>'), " ",
            explanation,
            class_=f"alert alert-{color}",
        )

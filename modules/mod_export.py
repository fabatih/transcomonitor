"""
modules/mod_export.py — Export UI module.

Glue between services/exporter.py and the Shiny download mechanism. Audit
events written for every export.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Optional

from shiny import module, reactive, render, req, ui

from db.database import get_connection
from services import authz
from services.audit import audit_user_action
from services.exporter import (
    export_audit_csv, export_complete_xlsx, export_foundation_csv,
    export_foundation_jsonld, export_pmsi_csv, export_version_diff_csv,
)


@module.ui
def export_ui() -> ui.Tag:
    return ui.div(
        ui.h4(ui.tags.i(class_="bi bi-download me-2"), "Exports"),
        ui.layout_columns(
            ui.div(
                ui.h6("Profils standards"),
                ui.input_radio_buttons(
                    "export_only_valide",
                    "Filtrer aux mappings validés/gelés ?",
                    choices={"yes": "Validés + gelés uniquement", "no": "Tous les mappings"},
                    selected="yes",
                ),
                ui.tags.hr(),
                ui.div(
                    ui.download_button("dl_complete_xlsx",
                                        "📚 Export complet (XLSX, forward + reverse)",
                                        class_="btn btn-primary mb-2 w-100"),
                    ui.download_button("dl_pmsi_csv",
                                        "🏥 Profil PMSI (CSV minimal)",
                                        class_="btn btn-outline-primary mb-2 w-100"),
                    ui.download_button("dl_foundation_jsonld",
                                        "🌐 Profil fondation (JSON-LD)",
                                        class_="btn btn-outline-success mb-2 w-100"),
                    ui.download_button("dl_foundation_csv",
                                        "🌐 Profil fondation (CSV à plat)",
                                        class_="btn btn-outline-success mb-2 w-100"),
                ),
            ),
            ui.div(
                ui.h6("Exports admin"),
                ui.output_ui("admin_export_section"),
                ui.tags.hr(),
                ui.h6("Diff entre 2 versions"),
                ui.output_ui("diff_export_section"),
            ),
            col_widths=(6, 6),
        ),
    )


@module.server
def export_server(input, output, session, current_user: reactive.Value):

    def _user() -> dict:
        u = current_user()
        if u is None:
            raise ValueError("not authenticated")
        return u

    def _only_valide() -> bool:
        return (input.export_only_valide() or "yes") == "yes"

    @render.download(filename=lambda: f"transcomonitor_complete_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    def dl_complete_xlsx():
        u = _user()
        authz.require_capability(u, "export_complete")
        con = get_connection()
        try:
            data, _ct, _fn = export_complete_xlsx(con)
            audit_user_action(con, user=u, action="export",
                               object_type="system", object_id="export.complete_xlsx",
                               note=f"complete xlsx, only_valide={_only_valide()}")
        finally:
            con.close()
        yield data

    @render.download(filename=lambda: f"transcomonitor_pmsi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    def dl_pmsi_csv():
        u = _user()
        authz.require_capability(u, "export_complete")
        con = get_connection()
        try:
            data, _ct, _fn = export_pmsi_csv(con, only_valide=_only_valide())
            audit_user_action(con, user=u, action="export",
                               object_type="system", object_id="export.pmsi_csv",
                               note=f"pmsi csv, only_valide={_only_valide()}")
        finally:
            con.close()
        yield data

    @render.download(filename=lambda: f"transcomonitor_foundation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonld")
    def dl_foundation_jsonld():
        u = _user()
        authz.require_capability(u, "export_complete")
        con = get_connection()
        try:
            data, _ct, _fn = export_foundation_jsonld(con, only_valide=_only_valide())
            audit_user_action(con, user=u, action="export",
                               object_type="system", object_id="export.foundation_jsonld",
                               note=f"foundation jsonld, only_valide={_only_valide()}")
        finally:
            con.close()
        yield data

    @render.download(filename=lambda: f"transcomonitor_foundation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    def dl_foundation_csv():
        u = _user()
        authz.require_capability(u, "export_complete")
        con = get_connection()
        try:
            data, _ct, _fn = export_foundation_csv(con, only_valide=_only_valide())
            audit_user_action(con, user=u, action="export",
                               object_type="system", object_id="export.foundation_csv",
                               note=f"foundation csv, only_valide={_only_valide()}")
        finally:
            con.close()
        yield data

    @render.download(filename=lambda: f"transcomonitor_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    def dl_audit_csv():
        u = _user()
        authz.require_capability(u, "export_audit")
        con = get_connection()
        try:
            data, _ct, _fn = export_audit_csv(con)
            audit_user_action(con, user=u, action="export",
                               object_type="system", object_id="export.audit_csv",
                               note="audit csv")
        finally:
            con.close()
        yield data

    @output
    @render.ui
    def admin_export_section():
        u = current_user()
        if u is None or not authz.has_capability(u, "export_audit"):
            return ui.div("Réservé aux administrateurs.", class_="text-muted small")
        return ui.div(
            ui.download_button("dl_audit_csv",
                                "📋 Export du journal d'audit (CSV)",
                                class_="btn btn-outline-warning mb-2 w-100"),
        )

    @output
    @render.ui
    def diff_export_section():
        con = get_connection()
        try:
            versions = [(r["id"], r["label"]) for r in con.execute(
                "SELECT id, label FROM frozen_versions ORDER BY id DESC"
            ).fetchall()]
        finally:
            con.close()
        if len(versions) < 2:
            return ui.div("Au moins 2 versions gelées requises pour un diff.",
                          class_="text-muted small")
        choices = {str(vid): label for vid, label in versions}
        return ui.div(
            ui.input_select("diff_from", "Version source", choices=choices,
                             selected=str(versions[-1][0])),
            ui.input_select("diff_to", "Version cible", choices=choices,
                             selected=str(versions[0][0])),
            ui.download_button("dl_version_diff", "📊 Télécharger le diff (CSV)",
                                class_="btn btn-outline-info mb-2 w-100"),
        )

    @render.download(filename=lambda: f"transcomonitor_diff_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    def dl_version_diff():
        u = _user()
        authz.require_capability(u, "view_mappings")
        from_id = int(input.diff_from())
        to_id   = int(input.diff_to())
        con = get_connection()
        try:
            data, _ct, _fn = export_version_diff_csv(con, from_version_id=from_id,
                                                       to_version_id=to_id)
            audit_user_action(con, user=u, action="export",
                               object_type="system",
                               object_id=f"export.diff_{from_id}_vs_{to_id}",
                               note=f"version diff {from_id}→{to_id}")
        finally:
            con.close()
        yield data

"""
app.py — Plateforme ATIH de maintenance du transcodage CIM
============================================================

Entry point Shiny pour transcomonitor.

- Init DB (auto-seed on first boot)
- Restore S3 if local DB is empty
- Sync API keys with DB
- Wire all modules (auth, mapping table, mapping edit, worklist, admin, export)
- Mount the WHO API proxy at /who-api-proxy/*
"""
from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path

# Path setup
_app_dir = Path(__file__).resolve().parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from shiny import App, reactive, render, ui

from db.database import get_connection, init_db, set_db_path, get_db_path
from db.models import ensure_default_admin
from utils.config_manager import load_config


# ─────────────────────────────────────────────────────────────────────────
# Configuration & DB initialization
# ─────────────────────────────────────────────────────────────────────────

config = load_config()
APP_NAME = config["app"]["name"]
APP_SHORT = config["app"]["short_name"]
APP_VERSION = config["app"]["version"]

# DB path resolution :
#   - TRANSCOMONITOR_DB_PATH env var wins
#   - On shinyapps.io (read-only filesystem) → /tmp/
#   - Locally → repo root
_env_db = os.environ.get("TRANSCOMONITOR_DB_PATH")
if _env_db:
    _resolved_db = _env_db
else:
    _is_shinyapps = bool(os.environ.get("SHINYAPPS_ACCOUNT")) or not os.access(str(_app_dir), os.W_OK)
    if _is_shinyapps:
        _resolved_db = str(Path("/tmp") / config["app"]["db_path"])
    else:
        _resolved_db = str(_app_dir / config["app"]["db_path"])
set_db_path(_resolved_db)
print(f"[transcomonitor] DB path: {_resolved_db}")


# ─────────────────────────────────────────────────────────────────────────
# S3 restore on boot (if local DB is empty)
# ─────────────────────────────────────────────────────────────────────────

from utils import s3_storage

if s3_storage.is_s3_available():
    _restored = s3_storage.restore_db_from_s3_if_empty(_resolved_db)
    if _restored:
        print("[transcomonitor] DB restored from S3.")
    else:
        print("[transcomonitor] S3 available; local DB already has data or S3 empty.")
else:
    print("[transcomonitor] S3 not configured (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY missing).")


# ─────────────────────────────────────────────────────────────────────────
# Initialize DB schema + auto-seed on first boot
# ─────────────────────────────────────────────────────────────────────────

init_db()

# Auto-seed if data/seed/ XLSX is present AND DB has no mappings yet
def _maybe_auto_seed():
    seed_path = _app_dir / "data" / "seed" / "transcodage_pipeline_complete.xlsx"
    if not seed_path.exists():
        return
    con = get_connection()
    try:
        n = con.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]
        if n > 0:
            print(f"[transcomonitor] DB already has {n} mappings, skipping auto-seed.")
            return
        print(f"[transcomonitor] Auto-seeding from {seed_path.name}…")
        from services.ingest import ingest_seed
        stats = ingest_seed(con, seed_path, progress=True)
        print(f"[transcomonitor] Auto-seed done: {stats}")
    finally:
        con.close()

_maybe_auto_seed()


# ─────────────────────────────────────────────────────────────────────────
# Default admin & API key sync
# ─────────────────────────────────────────────────────────────────────────

_con_init = get_connection()
ensure_default_admin(
    _con_init,
    username=config["app"].get("default_admin_user", "admin"),
    password=os.environ.get("DEFAULT_ADMIN_PASS", ""),
)
_con_init.close()


# ─────────────────────────────────────────────────────────────────────────
# Module imports
# ─────────────────────────────────────────────────────────────────────────

from modules.mod_auth import (
    auth_login_ui, auth_login_server,
    auth_user_bar_ui, auth_user_bar_server,
    is_admin,
)
from modules.mod_ect_browser import (
    ect_head_tags, ect_hidden_input_ui, eb_browser_modal_ui,
)
from modules.mod_mapping_table import (
    mapping_table_ui, mapping_table_server,
    split_bidir_ui, split_bidir_server,
)
from modules.mod_mapping_edit import mapping_edit_ui, mapping_edit_server
from modules.mod_worklist import worklist_ui, worklist_server
from modules.mod_export import export_ui, export_server
from modules.mod_admin import admin_ui, admin_server


# ─────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────

CIM11_RELEASES = config.get("cim11", {}).get("releases_supported", ["2024-01"])
DEFAULT_RELEASE = config.get("cim11", {}).get("release_current", "2024-01")


app_ui = ui.page_bootstrap(
    ui.head_content(
        ui.tags.title(f"{APP_SHORT} — {APP_NAME}"),
        # Bootswatch Flatly (matches icd11pycode for visual consistency)
        ui.tags.link(
            rel="stylesheet",
            href="https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/flatly/bootstrap.min.css",
        ),
        ui.tags.link(
            rel="stylesheet",
            href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css",
        ),
        ui.tags.link(rel="stylesheet", href="custom.css"),
        ect_head_tags(config.get("ect", {})),
    ),
    # Hidden ECT bridge (must be present in DOM)
    ect_hidden_input_ui(),
    # Embedded browser modal (shared, opened on demand)
    eb_browser_modal_ui(releases=CIM11_RELEASES, default_release=DEFAULT_RELEASE),

    # Top navbar
    ui.tags.nav(
        ui.div(
            ui.tags.span(
                ui.tags.i(class_="bi bi-shield-check me-2"),
                APP_SHORT,
                class_="navbar-brand h5 mb-0 text-light",
            ),
            ui.tags.span(
                APP_NAME,
                class_="navbar-text small text-light ms-3",
                style="opacity: 0.85;",
            ),
            ui.div(
                auth_user_bar_ui("user_bar"),
                class_="ms-auto",
            ),
            class_="container-fluid d-flex align-items-center",
        ),
        class_="navbar navbar-dark bg-primary px-3 py-2",
    ),

    # Main content : either login or app
    ui.output_ui("main_content"),
)


# ─────────────────────────────────────────────────────────────────────────
# Server
# ─────────────────────────────────────────────────────────────────────────

def _db_con_factory() -> sqlite3.Connection:
    """Per-call connection factory (each Shiny handler gets a fresh con)."""
    return get_connection()


def server(input, output, session):
    # Auth
    current_user = auth_login_server("login", db_con_factory=_db_con_factory)
    auth_user_bar_server("user_bar", current_user=current_user)

    # Shared reactive values for cross-tab navigation (plan §16.3)
    selected_mapping_id    = reactive.value(None)  # → edit panel
    selected_bidir_source  = reactive.value(None)  # → bidir auto-prefill : {direction, code}

    def _on_row_select(mapping_id: int):
        """Row selected in Forward or Reverse table → propagate to Edit + Bidir."""
        selected_mapping_id.set(mapping_id)
        # Also seed the Bidir tab with this mapping's source code (§16.3.3)
        con = get_connection()
        try:
            row = con.execute(
                "SELECT direction, source_code FROM mappings WHERE id = ?",
                (mapping_id,),
            ).fetchone()
        finally:
            con.close()
        if row:
            selected_bidir_source.set({
                "direction": row[0],
                "code":      row[1],
            })

    mapping_table_server("fwd_table", direction="forward",
                          on_row_select=_on_row_select)
    mapping_table_server("rev_table", direction="reverse",
                          on_row_select=_on_row_select)
    split_bidir_server("split_view",
                         selected_bidir_source=selected_bidir_source)
    mapping_edit_server("edit_panel",
                         mapping_id=selected_mapping_id,
                         current_user=current_user,
                         on_saved=lambda mid: None)
    worklist_server("worklist", current_user=current_user)
    export_server("exports", current_user=current_user)
    admin_server("admin", current_user=current_user)

    @output
    @render.ui
    def main_content():
        user = current_user()
        if user is None:
            return auth_login_ui("login")

        # Build tabs depending on role
        tabs = [
            ui.nav_panel(
                ui.HTML('<i class="bi bi-arrow-right-circle"></i> Forward'),
                ui.div(mapping_table_ui("fwd_table", direction="forward"),
                       class_="p-3"),
            ),
            ui.nav_panel(
                ui.HTML('<i class="bi bi-arrow-left-circle"></i> Reverse'),
                ui.div(mapping_table_ui("rev_table", direction="reverse"),
                       class_="p-3"),
            ),
            ui.nav_panel(
                ui.HTML('<i class="bi bi-arrow-left-right"></i> Bidir'),
                ui.div(split_bidir_ui("split_view"), class_="p-3"),
            ),
            ui.nav_panel(
                ui.HTML('<i class="bi bi-pencil-square"></i> Édition'),
                ui.div(mapping_edit_ui("edit_panel"), class_="p-3"),
            ),
            ui.nav_panel(
                ui.HTML('<i class="bi bi-list-check"></i> Mes worklists'),
                ui.div(worklist_ui("worklist"), class_="p-3"),
            ),
            ui.nav_panel(
                ui.HTML('<i class="bi bi-download"></i> Exports'),
                ui.div(export_ui("exports"), class_="p-3"),
            ),
        ]
        if is_admin(user):
            tabs.append(ui.nav_panel(
                ui.HTML('<i class="bi bi-gear"></i> Administration'),
                ui.div(admin_ui("admin"), class_="p-3"),
            ))

        return ui.div(
            ui.navset_tab(*tabs, id="main_tabs"),
            class_="container-fluid",
        )


# ─────────────────────────────────────────────────────────────────────────
# App + WHO proxy wrap
# ─────────────────────────────────────────────────────────────────────────

# Async upload of DB to S3 after handler executions (debounced)
def _save_db_to_s3_async():
    if s3_storage.is_s3_available():
        s3_storage.s3_upload_db_async(_resolved_db)


app = App(app_ui, server, static_assets=str(_app_dir / "www"))

# Mount WHO proxy as ASGI middleware
from services.who_proxy import wrap_with_proxy
app.starlette_app = wrap_with_proxy(
    app.starlette_app,
    language_default=config.get("ect", {}).get("language", "fr"),
)

"""
modules/mod_admin.py — Administration module.

Onglets :
  - Users        : CRUD users, reset password, deactivate, role changes
  - Listes       : CRUD assignment_lists, affecter users
  - Paramètres   : app_config key-value (audit toggle, releases, etc.)
  - Secrets      : API keys (chiffrés via Fernet)
  - Backup       : S3 sync status, download/upload DB, test connection,
                   trigger cache refresh

Toutes les actions sensibles nécessitent role=admin et sont auditées.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from shiny import module, reactive, render, ui

from db import models
from db.database import get_connection
from services import authz
from services.audit import audit_user_action


# ─────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────

@module.ui
def admin_ui() -> ui.Tag:
    return ui.div(
        ui.h4(ui.tags.i(class_="bi bi-gear me-2"), "Administration"),
        ui.navset_tab(
            ui.nav_panel("👤 Utilisateurs", ui.output_ui("users_tab")),
            ui.nav_panel("📋 Listes & assignations", ui.output_ui("lists_tab")),
            ui.nav_panel("⚙️ Paramètres", ui.output_ui("config_tab")),
            ui.nav_panel("🔑 Secrets API", ui.output_ui("secrets_tab")),
            ui.nav_panel("💾 Backup & cache", ui.output_ui("backup_tab")),
            id="admin_tabs",
        ),
    )


@module.server
def admin_server(input, output, session, current_user: reactive.Value):

    refresh_trigger = reactive.value(0)

    def _refresh():
        refresh_trigger.set(refresh_trigger() + 1)

    def _require_admin() -> Optional[dict]:
        u = current_user()
        if u is None:
            return None
        if not authz.is_admin(u):
            return None
        return u

    def _toast(message: str, *, kind: str = "auto", duration: int = 5) -> None:
        """Show a Shiny notification (toast) that survives _refresh() re-renders.

        Per plan §16.6 : the previous `output_ui("user_action_msg")` pattern
        lost the message when _refresh() rebuilt the parent tab — toasts are
        rendered at the session level and don't get torn down.

        `kind` :
          - 'auto'    : infer from prefix (✓ → message ; ✗ → error)
          - 'message' : info-style (default for ✓)
          - 'warning' : amber
          - 'error'   : red
        """
        if kind == "auto":
            kind = "message" if message.startswith("✓") else "error"
        try:
            ui.notification_show(message, type=kind, duration=duration)
        except Exception:
            # Fallback : stdout log
            print(f"[admin] {message}")

    # ── Users tab ────────────────────────────────────────────────────────

    @output
    @render.ui
    def users_tab():
        u = _require_admin()
        if u is None:
            return ui.div("Accès admin requis.", class_="alert alert-warning")
        _ = refresh_trigger()
        con = get_connection()
        try:
            users = models.list_users(con)
        finally:
            con.close()

        rows = []
        for usr in users:
            badge_color = {"admin": "primary", "mainteneur": "secondary", "valideur": "success"}.get(usr["role"], "light")
            active_badge = "success" if usr["active"] else "danger"
            rows.append(ui.tags.tr(
                ui.tags.td(usr["username"]),
                ui.tags.td(usr.get("full_name") or ""),
                ui.tags.td(ui.HTML(f'<span class="badge bg-{badge_color}">{usr["role"]}</span>')),
                ui.tags.td(ui.HTML(f'<span class="badge bg-{active_badge}">{"actif" if usr["active"] else "désactivé"}</span>')),
                ui.tags.td(usr.get("last_login") or "—"),
                ui.tags.td(usr.get("created_at") or ""),
            ))
        return ui.div(
            ui.h6("Liste des utilisateurs"),
            ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Nom"), ui.tags.th("Nom complet"),
                    ui.tags.th("Rôle"), ui.tags.th("Actif"),
                    ui.tags.th("Dernière connexion"), ui.tags.th("Créé le"),
                )),
                ui.tags.tbody(*rows),
                class_="table table-sm table-striped",
            ),
            ui.hr(),
            ui.h6("Créer un utilisateur"),
            ui.layout_columns(
                ui.input_text("new_user_name", "Username"),
                ui.input_text("new_user_full_name", "Nom complet"),
                ui.input_text("new_user_email", "Email"),
                ui.input_password("new_user_pass", "Mot de passe initial"),
                ui.input_select("new_user_role", "Rôle",
                                 choices={"mainteneur": "Mainteneur",
                                          "valideur": "Valideur", "admin": "Administrateur"},
                                 selected="mainteneur"),
                col_widths=(3, 3, 3, 3, 12),
            ),
            ui.div(
                ui.input_action_button("create_user_btn", "Créer",
                                        class_="btn btn-primary"),
                ui.output_ui("user_action_msg"),
                class_="mt-2",
            ),
            ui.hr(),
            ui.h6("Modifier / désactiver un utilisateur"),
            ui.layout_columns(
                ui.input_select(
                    "edit_user_select", "Utilisateur",
                    choices={str(u["id"]): u["username"] for u in users},
                ),
                ui.input_select("edit_user_role", "Nouveau rôle",
                                 choices={"mainteneur": "Mainteneur",
                                          "valideur": "Valideur", "admin": "Administrateur"}),
                ui.input_password("edit_user_pass", "Nouveau MdP (vide=inchangé)"),
                col_widths=(4, 4, 4),
            ),
            ui.div(
                ui.input_action_button("update_user_btn", "Mettre à jour",
                                        class_="btn btn-primary me-2"),
                ui.input_action_button("toggle_user_btn", "Activer/Désactiver",
                                        class_="btn btn-warning"),
                class_="mt-2",
            ),
        )

    user_action_message = reactive.value("")

    @output
    @render.ui
    def user_action_msg():
        m = user_action_message()
        if not m:
            return ui.div()
        ok = m.startswith("✓")
        return ui.div(m, class_=f"alert alert-{'success' if ok else 'danger'} mt-2 py-2 small")

    @reactive.effect
    @reactive.event(input.create_user_btn)
    def _create():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")

            user_action_message.set("✗ Accès refusé")
            return
        username = (input.new_user_name() or "").strip()
        password = (input.new_user_pass() or "").strip()
        if not username or not password:
            _toast("✗ Username et mot de passe requis")

            user_action_message.set("✗ Username et mot de passe requis")
            return
        con = get_connection()
        try:
            uid = models.create_user(con, username=username, password=password,
                                       role=input.new_user_role(),
                                       email=(input.new_user_email() or None),
                                       full_name=(input.new_user_full_name() or None))
            audit_user_action(con, user=u, action="admin_user_create",
                               object_type="user", object_id=uid,
                               new_value={"username": username, "role": input.new_user_role()})
            _toast(f"✓ Utilisateur '{username}' créé (id={uid})")

            user_action_message.set(f"✓ Utilisateur '{username}' créé (id={uid})")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")

            user_action_message.set(f"✗ Erreur : {e}")
        finally:
            con.close()

    @reactive.effect
    @reactive.event(input.update_user_btn)
    def _update():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")

            user_action_message.set("✗ Accès refusé")
            return
        try:
            uid = int(input.edit_user_select())
        except (TypeError, ValueError):
            return
        new_pass = (input.edit_user_pass() or "").strip() or None
        new_role = input.edit_user_role()
        con = get_connection()
        try:
            old = models.get_user(con, uid)
            models.update_user(con, uid, role=new_role, password=new_pass)
            audit_user_action(con, user=u, action="admin_user_update",
                               object_type="user", object_id=uid,
                               old_value={"role": old["role"]},
                               new_value={"role": new_role,
                                          "password_changed": bool(new_pass)})
            _toast(f"✓ Utilisateur #{uid} mis à jour")

            user_action_message.set(f"✓ Utilisateur #{uid} mis à jour")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")

            user_action_message.set(f"✗ Erreur : {e}")
        finally:
            con.close()

    @reactive.effect
    @reactive.event(input.toggle_user_btn)
    def _toggle():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")

            user_action_message.set("✗ Accès refusé")
            return
        try:
            uid = int(input.edit_user_select())
        except (TypeError, ValueError):
            return
        con = get_connection()
        try:
            old = models.get_user(con, uid)
            new_active = 0 if old["active"] else 1
            models.update_user(con, uid, active=new_active)
            audit_user_action(con, user=u, action="admin_user_deactivate",
                               object_type="user", object_id=uid,
                               old_value={"active": bool(old["active"])},
                               new_value={"active": bool(new_active)})
            _toast(
                f"✓ Utilisateur #{uid} {'activé' if new_active else 'désactivé'}"
            )

            user_action_message.set(
                f"✓ Utilisateur #{uid} {'activé' if new_active else 'désactivé'}"
            )
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")

            user_action_message.set(f"✗ Erreur : {e}")
        finally:
            con.close()

    # ── Lists tab ────────────────────────────────────────────────────────

    @output
    @render.ui
    def lists_tab():
        u = _require_admin()
        if u is None:
            return ui.div("Accès admin requis.", class_="alert alert-warning")
        _ = refresh_trigger()
        con = get_connection()
        try:
            lists = [dict(r) for r in con.execute(
                "SELECT * FROM assignment_lists ORDER BY created_at DESC"
            ).fetchall()]
        finally:
            con.close()
        list_rows = []
        for lst in lists:
            list_rows.append(ui.tags.tr(
                ui.tags.td(str(lst["id"])),
                ui.tags.td(lst["name"]),
                ui.tags.td(lst.get("direction") or ""),
                ui.tags.td(lst.get("description") or ""),
                ui.tags.td(lst["created_at"]),
            ))
        return ui.div(
            ui.h6("Listes d'affectation existantes"),
            ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("id"), ui.tags.th("Nom"),
                    ui.tags.th("Direction"), ui.tags.th("Description"),
                    ui.tags.th("Créée le"),
                )),
                ui.tags.tbody(*list_rows),
                class_="table table-sm table-striped",
            ),
            ui.hr(),
            ui.h6("Créer une liste (statique — codes explicites)"),
            ui.layout_columns(
                ui.input_text("new_list_name", "Nom"),
                ui.input_select("new_list_direction", "Direction",
                                 choices={"forward": "Forward", "reverse": "Reverse",
                                          "both": "Les deux"},
                                 selected="forward"),
                ui.input_text_area("new_list_codes",
                                    "Codes (un par ligne ou séparés par virgule)",
                                    rows=3),
                col_widths=(4, 4, 4),
            ),
            ui.input_text("new_list_description", "Description"),
            ui.div(
                ui.input_action_button("create_list_btn", "Créer liste",
                                        class_="btn btn-primary"),
                ui.output_ui("list_action_msg"),
                class_="mt-2",
            ),
        )

    list_action_message = reactive.value("")

    @output
    @render.ui
    def list_action_msg():
        m = list_action_message()
        if not m:
            return ui.div()
        ok = m.startswith("✓")
        return ui.div(m, class_=f"alert alert-{'success' if ok else 'danger'} mt-2 py-2 small")

    @reactive.effect
    @reactive.event(input.create_list_btn)
    def _create_list():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")

            list_action_message.set("✗ Accès refusé")
            return
        from modules.mod_worklist import create_assignment_list
        codes_raw = (input.new_list_codes() or "").strip()
        codes = [c.strip() for c in codes_raw.replace("\n", ",").split(",") if c.strip()]
        if not codes:
            _toast("✗ Liste de codes vide")

            list_action_message.set("✗ Liste de codes vide")
            return
        con = get_connection()
        try:
            lid = create_assignment_list(
                con, user=u, name=input.new_list_name() or "Liste sans nom",
                description=input.new_list_description() or "",
                direction=input.new_list_direction(),
                static_codes=codes,
            )
            _toast(f"✓ Liste #{lid} créée avec {len(codes)} codes")

            list_action_message.set(f"✓ Liste #{lid} créée avec {len(codes)} codes")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")

            list_action_message.set(f"✗ Erreur : {e}")
        finally:
            con.close()

    # ── Config tab ───────────────────────────────────────────────────────

    @output
    @render.ui
    def config_tab():
        u = _require_admin()
        if u is None:
            return ui.div("Accès admin requis.", class_="alert alert-warning")
        _ = refresh_trigger()
        con = get_connection()
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT key, value, is_secret, updated_at FROM app_config ORDER BY key"
            ).fetchall()]
        finally:
            con.close()

        items = []
        for r in rows:
            display_val = "(secret)" if r["is_secret"] else (r["value"] or "")
            items.append(ui.tags.tr(
                ui.tags.td(ui.tags.code(r["key"])),
                ui.tags.td(display_val),
                ui.tags.td(r["updated_at"] or ""),
            ))
        return ui.div(
            ui.h6("Paramètres applicatifs"),
            ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Clé"), ui.tags.th("Valeur"),
                    ui.tags.th("Modifiée le"),
                )),
                ui.tags.tbody(*items),
                class_="table table-sm table-striped",
            ),
            ui.hr(),
            ui.h6("Modifier / ajouter un paramètre"),
            ui.layout_columns(
                ui.input_text("config_key", "Clé"),
                ui.input_text("config_value", "Valeur"),
                col_widths=(4, 8),
            ),
            ui.input_action_button("config_save_btn", "Enregistrer",
                                    class_="btn btn-primary"),
            ui.output_ui("config_action_msg"),
        )

    config_action_message = reactive.value("")

    @output
    @render.ui
    def config_action_msg():
        m = config_action_message()
        if not m:
            return ui.div()
        ok = m.startswith("✓")
        return ui.div(m, class_=f"alert alert-{'success' if ok else 'danger'} mt-2 py-2 small")

    @reactive.effect
    @reactive.event(input.config_save_btn)
    def _save_config():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")

            config_action_message.set("✗ Accès refusé")
            return
        key = (input.config_key() or "").strip()
        val = input.config_value() or ""
        if not key:
            _toast("✗ Clé vide")

            config_action_message.set("✗ Clé vide")
            return
        con = get_connection()
        try:
            old_val = models.get_config_value(con, key)
            models.set_config_value(con, key, val, updated_by=u["id"])
            audit_user_action(con, user=u, action="admin_config_change",
                               object_type="config", object_id=key,
                               old_value={"value": old_val},
                               new_value={"value": "(secret)" if "SECRET" in key else val})
            _toast(f"✓ Paramètre '{key}' mis à jour")

            config_action_message.set(f"✓ Paramètre '{key}' mis à jour")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")

            config_action_message.set(f"✗ Erreur : {e}")
        finally:
            con.close()

    # ── Secrets tab ──────────────────────────────────────────────────────

    @output
    @render.ui
    def secrets_tab():
        u = _require_admin()
        if u is None:
            return ui.div("Accès admin requis.", class_="alert alert-warning")
        return ui.div(
            ui.div(
                ui.tags.strong("Clés API stockées chiffrées en base (Fernet)."),
                ui.tags.br(),
                "Les valeurs ne sont JAMAIS affichées en clair après saisie.",
                class_="alert alert-info small",
            ),
            ui.layout_columns(
                ui.input_select("secret_key", "Clé",
                                 choices={
                                     "WHO_CLIENT_SECRET": "WHO_CLIENT_SECRET",
                                     "MISTRAL_API_KEY":   "MISTRAL_API_KEY",
                                     "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
                                 }),
                ui.input_password("secret_value", "Nouvelle valeur"),
                col_widths=(4, 8),
            ),
            ui.input_action_button("secret_save_btn", "Enregistrer (chiffré)",
                                    class_="btn btn-danger"),
            ui.output_ui("secret_action_msg"),
        )

    secret_action_message = reactive.value("")

    @output
    @render.ui
    def secret_action_msg():
        m = secret_action_message()
        if not m:
            return ui.div()
        ok = m.startswith("✓")
        return ui.div(m, class_=f"alert alert-{'success' if ok else 'danger'} mt-2 py-2 small")

    @reactive.effect
    @reactive.event(input.secret_save_btn)
    def _save_secret():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")

            secret_action_message.set("✗ Accès refusé")
            return
        key = input.secret_key()
        val = input.secret_value() or ""
        if not val:
            _toast("✗ Valeur vide")

            secret_action_message.set("✗ Valeur vide")
            return
        con = get_connection()
        try:
            models.set_config_value(con, key, val, is_secret=True, updated_by=u["id"])
            audit_user_action(con, user=u, action="admin_secret_set",
                               object_type="config", object_id=key,
                               note="secret stored (encrypted)")
            _toast(f"✓ Secret '{key}' enregistré (chiffré).")

            secret_action_message.set(f"✓ Secret '{key}' enregistré (chiffré).")
        except Exception as e:
            _toast(f"✗ Erreur : {e}")

            secret_action_message.set(f"✗ Erreur : {e}")
        finally:
            con.close()

    # ── Backup tab ───────────────────────────────────────────────────────

    @output
    @render.ui
    def backup_tab():
        u = _require_admin()
        if u is None:
            return ui.div("Accès admin requis.", class_="alert alert-warning")
        _ = refresh_trigger()
        from utils import s3_storage
        from db.database import get_db_path

        # S3 connection status
        s3_status = s3_storage.test_connection()
        s3_info = s3_storage.s3_get_db_info() if s3_status.get("ok") else None
        db_size = os.path.getsize(get_db_path()) if os.path.exists(get_db_path()) else 0

        return ui.div(
            ui.h6("État de la persistance"),
            ui.tags.dl(
                ui.tags.dt("DB locale"),
                ui.tags.dd(f"{get_db_path()} ({db_size / 1024 / 1024:.2f} MB)"),
                ui.tags.dt("S3 disponible"),
                ui.tags.dd(
                    ui.HTML(f'<span class="badge bg-{"success" if s3_status.get("ok") else "danger"}">'
                            f'{"OK" if s3_status.get("ok") else "KO"}</span>'),
                    " ",
                    s3_status.get("error", ""),
                ),
                ui.tags.dt("Bucket"),
                ui.tags.dd(s3_status.get("bucket", "—")),
                ui.tags.dt("Région"),
                ui.tags.dd(s3_status.get("region", "—")),
                ui.tags.dt("Versioning S3"),
                ui.tags.dd(s3_status.get("versioning", "—")),
                ui.tags.dt("Dernière sauvegarde S3"),
                ui.tags.dd(s3_info.get("last_modified") if s3_info else "—"),
                ui.tags.dt("Taille DB en S3"),
                ui.tags.dd(f"{s3_info.get('size', 0) / 1024 / 1024:.2f} MB" if s3_info else "—"),
                class_="row",
            ),
            ui.hr(),
            ui.h6("Actions"),
            ui.div(
                ui.input_action_button("backup_now_btn", "Sauvegarder maintenant",
                                        class_="btn btn-primary me-2"),
                ui.input_action_button("refresh_cache_btn", "Rafraîchir cache CIM-11 (admin)",
                                        class_="btn btn-outline-info me-2"),
                ui.input_action_button("test_s3_btn", "Re-tester la connexion S3",
                                        class_="btn btn-outline-secondary"),
            ),
            ui.output_ui("backup_action_msg"),
        )

    backup_action_message = reactive.value("")

    @output
    @render.ui
    def backup_action_msg():
        m = backup_action_message()
        if not m:
            return ui.div()
        ok = m.startswith("✓")
        return ui.div(m, class_=f"alert alert-{'success' if ok else 'danger'} mt-2 py-2 small")

    @reactive.effect
    @reactive.event(input.backup_now_btn)
    def _backup_now():
        u = _require_admin()
        if u is None:
            return
        from utils import s3_storage
        from db.database import get_db_path
        ok = s3_storage.s3_upload_db(get_db_path())
        con = get_connection()
        try:
            audit_user_action(con, user=u, action="backup_snapshot",
                               object_type="system", object_id="db_snapshot",
                               note="manual backup via UI")
        finally:
            con.close()
        _toast("✓ Sauvegarde S3 effectuée" if ok else "✗ Sauvegarde S3 échouée")

        backup_action_message.set("✓ Sauvegarde S3 effectuée" if ok else "✗ Sauvegarde S3 échouée")
        _refresh()

    @reactive.effect
    @reactive.event(input.test_s3_btn)
    def _test_s3():
        from utils import s3_storage
        st = s3_storage.test_connection()
        if st.get("ok"):
            _toast(f"✓ S3 OK — bucket={st['bucket']}, region={st['region']}, "
                                       f"versioning={st['versioning']}")

            backup_action_message.set(f"✓ S3 OK — bucket={st['bucket']}, region={st['region']}, "
                                       f"versioning={st['versioning']}")
        else:
            _toast(f"✗ S3 KO : {st.get('error', 'unknown error')}")

            backup_action_message.set(f"✗ S3 KO : {st.get('error', 'unknown error')}")
        _refresh()

    @reactive.effect
    @reactive.event(input.refresh_cache_btn)
    def _refresh_cache():
        u = _require_admin()
        if u is None:
            return
        # We don't run the full bootstrap here (1-2h) ; just log the intent
        # and recommend running scripts/bootstrap_cim11_refs.py
        con = get_connection()
        try:
            audit_user_action(con, user=u, action="cache_refresh",
                               object_type="system", object_id="cim11_cache",
                               note="manual refresh trigger (run scripts/bootstrap_cim11_refs.py)")
        finally:
            con.close()
        _toast(
            "✓ Action loggée. Pour effectuer le rafraîchissement, lancer "
            "manuellement : python -m scripts.bootstrap_cim11_refs --from-csv "
            "data/seed/transcodage_pipeline_complete.xlsx --release 2024-01 --upload-s3"
        )

        backup_action_message.set(
            "✓ Action loggée. Pour effectuer le rafraîchissement, lancer "
            "manuellement : python -m scripts.bootstrap_cim11_refs --from-csv "
            "data/seed/transcodage_pipeline_complete.xlsx --release 2024-01 --upload-s3"
        )

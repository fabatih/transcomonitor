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
            users = models.list_users(con, include_inactive=False)
            # Existing assignments overview
            from modules.mod_worklist import fetch_all_assignments
            assignments = fetch_all_assignments(con)
        finally:
            con.close()
        list_rows = []
        for lst in lists:
            list_rows.append(ui.tags.tr(
                ui.tags.td(str(lst["id"])),
                ui.tags.td(lst["name"]),
                ui.tags.td(lst.get("direction") or ""),
                ui.tags.td((lst.get("description") or "")[:60]),
                ui.tags.td("statique" if lst.get("static_codes") else "dynamique"),
                ui.tags.td(lst["created_at"]),
            ))
        assign_rows = []
        for a in assignments[:30]:
            assign_rows.append(ui.tags.tr(
                ui.tags.td(str(a["list_id"])),
                ui.tags.td(a["list_name"]),
                ui.tags.td(a["username"]),
                ui.tags.td(a["expected_role"]),
                ui.tags.td(a["status"]),
                ui.tags.td(a["due_date"] or "—"),
                ui.tags.td(a["assigned_at"]),
            ))

        list_choices = {str(lst["id"]): f"#{lst['id']} — {lst['name']}" for lst in lists}
        user_choices = {str(usr["id"]): usr["username"] for usr in users}
        if not list_choices:
            list_choices = {"": "(aucune liste)"}
        if not user_choices:
            user_choices = {"": "(aucun user)"}

        return ui.div(
            # — Existing lists table
            ui.h6("Listes d'affectation existantes"),
            ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("id"), ui.tags.th("Nom"),
                    ui.tags.th("Direction"), ui.tags.th("Description"),
                    ui.tags.th("Type"), ui.tags.th("Créée le"),
                )),
                ui.tags.tbody(*list_rows),
                class_="table table-sm table-striped",
            ),

            # — Create static list (existing form, kept)
            ui.hr(),
            ui.h6("Créer une liste (statique — codes explicites)"),
            ui.layout_columns(
                ui.input_text("new_list_name", "Nom", width="100%"),
                ui.input_select("new_list_direction", "Direction",
                                 choices={"forward": "Forward", "reverse": "Reverse",
                                          "both": "Les deux"},
                                 selected="forward", width="100%"),
                ui.input_text_area("new_list_codes",
                                    "Codes (un par ligne ou séparés par virgule)",
                                    rows=3, width="100%"),
                col_widths=(4, 4, 4),
            ),
            ui.input_text("new_list_description", "Description", width="100%"),
            ui.div(
                ui.input_action_button("create_list_btn", "Créer liste statique",
                                        class_="btn btn-primary"),
                class_="mt-2",
            ),

            # — Create dynamic list from filters (plan §16.5)
            ui.hr(),
            ui.h6("Créer une liste dynamique (filtres)"),
            ui.layout_columns(
                ui.input_text("dyn_list_name", "Nom", width="100%"),
                ui.input_select("dyn_list_direction", "Direction",
                                 choices={"forward": "Forward", "reverse": "Reverse"},
                                 selected="forward", width="100%"),
                ui.input_text("dyn_list_chapitre", "Chapitre (ex: 01)",
                                placeholder="laisser vide pour tous", width="100%"),
                col_widths=(4, 4, 4),
            ),
            ui.layout_columns(
                ui.input_checkbox_group("dyn_list_fiabilite", "Fiabilité",
                                         choices=["TRES_HAUTE", "HAUTE", "MOYENNE",
                                                  "BASSE", "CONTESTEE", "NON_RESOLU"]),
                ui.input_checkbox_group("dyn_list_status", "Statut",
                                         choices=["propose", "en_revue", "conteste"]),
                col_widths=(6, 6),
            ),
            ui.input_checkbox("dyn_list_classant", "Forward source classant (PMSI)"),
            ui.div(
                ui.input_action_button("preview_dyn_btn", "Aperçu (count)",
                                        class_="btn btn-outline-info me-2"),
                ui.input_action_button("create_dyn_btn", "Créer liste dynamique",
                                        class_="btn btn-primary"),
                class_="mt-2",
            ),
            ui.output_ui("preview_dyn_result"),

            # — Edit existing list (plan §16.5)
            ui.hr(),
            ui.h6("Éditer une liste existante"),
            ui.layout_columns(
                ui.input_select("edit_list_id", "Liste à éditer",
                                 choices=list_choices, width="100%"),
                ui.input_text("edit_list_name", "Nouveau nom (vide = inchangé)",
                                width="100%"),
                ui.input_text("edit_list_description", "Nouvelle description (vide = inchangée)",
                                width="100%"),
                col_widths=(4, 4, 4),
            ),
            ui.div(
                ui.input_action_button("update_list_btn", "Mettre à jour la liste",
                                        class_="btn btn-primary"),
                class_="mt-2",
            ),

            # — Assign user to list (plan §16.5)
            ui.hr(),
            ui.h6("Affecter un utilisateur à une liste"),
            ui.layout_columns(
                ui.input_select("assign_list_id", "Liste", choices=list_choices, width="100%"),
                ui.input_select("assign_user_id", "Utilisateur", choices=user_choices, width="100%"),
                ui.input_select("assign_role", "Rôle attendu",
                                 choices={"mainteneur": "Mainteneur",
                                          "valideur":   "Valideur"},
                                 selected="mainteneur", width="100%"),
                ui.input_date("assign_due", "Échéance (optionnel)", width="100%"),
                col_widths=(3, 3, 3, 3),
            ),
            ui.div(
                ui.input_action_button("assign_btn", "Affecter",
                                        class_="btn btn-success"),
                class_="mt-2",
            ),

            # — Existing assignments overview
            ui.hr(),
            ui.h6("Affectations en cours (30 dernières)"),
            ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Liste id"), ui.tags.th("Nom liste"),
                    ui.tags.th("Utilisateur"), ui.tags.th("Rôle"),
                    ui.tags.th("Statut"), ui.tags.th("Échéance"),
                    ui.tags.th("Affectée le"),
                )),
                ui.tags.tbody(*assign_rows) if assign_rows else
                    ui.tags.tbody(ui.tags.tr(ui.tags.td(
                        "Aucune affectation pour l'instant.",
                        colspan="7", class_="text-muted small text-center",
                    ))),
                class_="table table-sm table-striped",
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

    # ── Dynamic list (filters-based) ──────────────────────────────────────

    def _build_dyn_query_def() -> dict:
        """Read the dyn_list_* inputs into a query_definition dict."""
        return {
            "direction":   input.dyn_list_direction(),
            "chapitre":    (input.dyn_list_chapitre() or "").strip() or None,
            "fiabilite":   list(input.dyn_list_fiabilite() or []),
            "status":      list(input.dyn_list_status() or []),
            "est_classant": bool(input.dyn_list_classant())
                            if input.dyn_list_direction() == "forward" else False,
        }

    preview_dyn_result_state = reactive.value("")

    @output
    @render.ui
    def preview_dyn_result():
        v = preview_dyn_result_state()
        if not v:
            return ui.div()
        return ui.div(v, class_="alert alert-info py-1 small mt-2")

    @reactive.effect
    @reactive.event(input.preview_dyn_btn)
    def _preview_dyn():
        u = _require_admin()
        if u is None:
            return
        from modules.mod_worklist import preview_filter_count
        try:
            qdef = _build_dyn_query_def()
            con = get_connection()
            try:
                n = preview_filter_count(con, qdef)
            finally:
                con.close()
            preview_dyn_result_state.set(
                f"Aperçu : {n} mappings correspondants au filtre."
            )
        except Exception as e:
            _toast(f"✗ Erreur preview : {e}")

    @reactive.effect
    @reactive.event(input.create_dyn_btn)
    def _create_dyn():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")
            return
        from modules.mod_worklist import create_assignment_list
        name = (input.dyn_list_name() or "").strip()
        if not name:
            _toast("✗ Nom requis")
            return
        qdef = _build_dyn_query_def()
        con = get_connection()
        try:
            lid = create_assignment_list(
                con, user=u, name=name,
                description=f"Liste dynamique : {qdef}",
                direction=qdef["direction"],
                query_definition=qdef,
            )
            _toast(f"✓ Liste dynamique #{lid} créée")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")
        finally:
            con.close()

    # ── Update existing list (plan §16.5) ────────────────────────────────

    @reactive.effect
    @reactive.event(input.update_list_btn)
    def _update_list():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")
            return
        try:
            lid = int(input.edit_list_id())
        except (TypeError, ValueError):
            _toast("✗ Liste non sélectionnée")
            return
        from modules.mod_worklist import update_assignment_list
        new_name = (input.edit_list_name() or "").strip() or None
        new_desc = (input.edit_list_description() or "").strip() or None
        if not new_name and not new_desc:
            _toast("✗ Rien à mettre à jour (renseignez nom ou description)")
            return
        con = get_connection()
        try:
            update_assignment_list(con, user=u, list_id=lid,
                                     name=new_name, description=new_desc)
            _toast(f"✓ Liste #{lid} mise à jour")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")
        finally:
            con.close()

    # ── Assign user to list (plan §16.5) ─────────────────────────────────

    @reactive.effect
    @reactive.event(input.assign_btn)
    def _do_assign():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")
            return
        try:
            lid = int(input.assign_list_id())
            uid = int(input.assign_user_id())
        except (TypeError, ValueError):
            _toast("✗ Liste ou utilisateur non sélectionné")
            return
        role = input.assign_role()
        due  = input.assign_due()
        due_str = due.isoformat() if due else None
        from modules.mod_worklist import assign_user_to_list
        con = get_connection()
        try:
            aid = assign_user_to_list(con, user=u, list_id=lid,
                                        target_user_id=uid,
                                        expected_role=role,
                                        due_date=due_str)
            _toast(f"✓ Affectation #{aid} créée")
            _refresh()
        except sqlite3.IntegrityError:
            _toast("✗ Cette affectation existe déjà (liste + user + rôle)")
        except Exception as e:
            _toast(f"✗ Erreur : {e}")
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
            # Problematique types (plan §16.7)
            from services.problematiques import list_problematiques
            problems = list_problematiques(con, only_active=False)
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
        problem_rows = []
        for p in problems:
            badge_color = p["color"] or "secondary"
            status_badge = "success" if p["active"] else "danger"
            problem_rows.append(ui.tags.tr(
                ui.tags.td(ui.tags.code(p["code"])),
                ui.tags.td(ui.HTML(f'<span class="badge bg-{badge_color}">{p["libelle"]}</span>')),
                ui.tags.td((p.get("description") or "")[:60]),
                ui.tags.td(p["color"]),
                ui.tags.td(str(p.get("sort_order") or "")),
                ui.tags.td(ui.HTML(f'<span class="badge bg-{status_badge}">{"actif" if p["active"] else "désactivé"}</span>')),
            ))
        problem_codes = {p["code"]: f"{p['code']} — {p['libelle']}" for p in problems}
        if not problem_codes:
            problem_codes = {"": "(aucune)"}

        # Help block explaining the Paramètres tab (plan §16.4 hint, §16.6)
        help_block = ui.div(
            ui.tags.p(
                ui.tags.strong("Comment ajouter ou modifier un paramètre ?"),
                class_="mb-1",
            ),
            ui.tags.ol(
                ui.tags.li("Saisir une clé existante OU une nouvelle clé dans le champ « Clé »."),
                ui.tags.li("Saisir la valeur souhaitée dans « Valeur » (texte libre)."),
                ui.tags.li("Cliquer sur « Enregistrer ». Un toast confirme la sauvegarde."),
                ui.tags.li("Pour les secrets (clés API), utiliser plutôt l'onglet « 🔑 Secrets API »."),
                class_="small mb-2",
            ),
            ui.tags.p(
                "Clés notables : ",
                ui.tags.code("audit_capture_request_meta"),
                " (0/1 pour enregistrer IP+UA), ",
                ui.tags.code("default_precedence_policy"),
                " (auto_unless_valid | never_override_valid | always_override | manual_per_row).",
                class_="small text-muted mb-0",
            ),
            class_="alert alert-light py-2",
        )

        return ui.div(
            ui.h6("Paramètres applicatifs (clé / valeur)"),
            help_block,
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
                ui.input_text("config_key", "Clé", width="100%"),
                ui.input_text("config_value", "Valeur", width="100%"),
                col_widths=(4, 8),
            ),
            ui.input_action_button("config_save_btn", "Enregistrer",
                                    class_="btn btn-primary"),

            # — Problematique types (plan §16.7)
            ui.hr(),
            ui.h6("Typologie des problématiques de transcodage"),
            ui.div(
                "Cette liste alimente le sélecteur « Problématique éventuelle » dans "
                "l'onglet Édition (section Justification).",
                class_="small text-muted mb-2",
            ),
            ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Code"), ui.tags.th("Libellé (badge)"),
                    ui.tags.th("Description"), ui.tags.th("Couleur"),
                    ui.tags.th("Ordre"), ui.tags.th("Statut"),
                )),
                ui.tags.tbody(*problem_rows),
                class_="table table-sm table-striped",
            ),
            ui.layout_columns(
                ui.input_text("problem_code", "Code (unique, sans espaces)",
                                placeholder="ex: lignes_perdues", width="100%"),
                ui.input_text("problem_libelle", "Libellé", width="100%"),
                ui.input_select("problem_color", "Couleur badge",
                                 choices=["primary", "secondary", "success",
                                          "danger", "warning", "info",
                                          "light", "dark"],
                                 selected="secondary", width="100%"),
                ui.input_numeric("problem_sort", "Ordre", value=100,
                                  min=0, step=10, width="100%"),
                col_widths=(3, 3, 3, 3),
            ),
            ui.input_text("problem_description", "Description", width="100%"),
            ui.div(
                ui.input_action_button("problem_create_btn", "Créer",
                                        class_="btn btn-success me-2"),
                ui.input_select("problem_existing", "Code existant à éditer/désactiver",
                                 choices=problem_codes, selected="", width="200px"),
                ui.input_action_button("problem_update_btn", "Mettre à jour (libellé+couleur+ordre)",
                                        class_="btn btn-primary me-2"),
                ui.input_action_button("problem_toggle_btn", "Activer/Désactiver",
                                        class_="btn btn-warning"),
                class_="d-flex gap-2 align-items-end mt-2 flex-wrap",
            ),
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

    # ── Problematique types handlers (plan §16.7) ────────────────────────

    @reactive.effect
    @reactive.event(input.problem_create_btn)
    def _problem_create():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")
            return
        from services.problematiques import create_problematique
        code = (input.problem_code() or "").strip().lower()
        lib  = (input.problem_libelle() or "").strip()
        if not code or not lib:
            _toast("✗ Code et libellé requis")
            return
        con = get_connection()
        try:
            create_problematique(
                con, user=u, code=code, libelle=lib,
                description=input.problem_description() or "",
                color=input.problem_color(),
                sort_order=int(input.problem_sort() or 100),
            )
            _toast(f"✓ Problématique '{code}' créée")
            _refresh()
        except sqlite3.IntegrityError:
            _toast(f"✗ Le code '{code}' existe déjà")
        except Exception as e:
            _toast(f"✗ Erreur : {e}")
        finally:
            con.close()

    @reactive.effect
    @reactive.event(input.problem_update_btn)
    def _problem_update():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")
            return
        from services.problematiques import update_problematique
        code = (input.problem_existing() or "").strip()
        if not code:
            _toast("✗ Sélectionner un code existant")
            return
        lib = (input.problem_libelle() or "").strip() or None
        desc = input.problem_description() or None
        con = get_connection()
        try:
            update_problematique(
                con, user=u, code=code,
                libelle=lib, description=desc,
                color=input.problem_color(),
                sort_order=int(input.problem_sort() or 100),
            )
            _toast(f"✓ Problématique '{code}' mise à jour")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")
        finally:
            con.close()

    @reactive.effect
    @reactive.event(input.problem_toggle_btn)
    def _problem_toggle():
        u = _require_admin()
        if u is None:
            _toast("✗ Accès refusé")
            return
        from services.problematiques import (
            deactivate_problematique, get_problematique,
        )
        code = (input.problem_existing() or "").strip()
        if not code:
            _toast("✗ Sélectionner un code existant")
            return
        con = get_connection()
        try:
            p = get_problematique(con, code)
            if p is None:
                _toast(f"✗ Code '{code}' introuvable")
                return
            new_active = not bool(p["active"])
            deactivate_problematique(con, user=u, code=code, active=new_active)
            _toast(f"✓ Problématique '{code}' "
                   f"{'activée' if new_active else 'désactivée'}")
            _refresh()
        except Exception as e:
            _toast(f"✗ Erreur : {e}")
        finally:
            con.close()
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

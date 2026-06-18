"""
modules/mod_auth.py — Authentication module for transcomonitor.

Adapté d'icd11pycode/modules/mod_auth.py pour le modèle à 3 rôles
(admin / mainteneur / valideur — cf. plan §14 #7 et #15).
"""
from __future__ import annotations

from shiny import module, reactive, render, ui


ROLE_ICONS = {
    "admin":      "🛡️",
    "mainteneur": "📝",
    "valideur":   "✅",
}
ROLE_LABELS = {
    "admin":      "Administrateur",
    "mainteneur": "Mainteneur",
    "valideur":   "Valideur",
}


# ═══════════════════════════════════════════════════════════════════════════
# Login page
# ═══════════════════════════════════════════════════════════════════════════

@module.ui
def auth_login_ui() -> ui.TagList:
    """Centered login card for the platform."""
    return ui.div(
        ui.div(
            ui.div(
                ui.div(
                    ui.h4(
                        ui.tags.i(class_="bi bi-shield-check me-2"),
                        "transcomonitor",
                        class_="card-title text-center mb-1",
                    ),
                    ui.p(
                        "Plateforme ATIH de maintenance du transcodage CIM",
                        class_="text-center small mb-0",
                        style="color: rgba(255,255,255,0.85);",
                    ),
                    class_="card-header text-center py-3",
                ),
                ui.div(
                    ui.input_text("login_user", "Identifiant",
                                  placeholder="Nom d'utilisateur"),
                    ui.input_password("login_pass", "Mot de passe",
                                      placeholder="Mot de passe"),
                    ui.output_ui("login_msg"),
                    ui.input_action_button(
                        "login_btn", "Se connecter",
                        class_="btn btn-primary w-100 mt-3",
                    ),
                    ui.p(
                        "ATIH · propulsé par l'OMS · transcodage CIM-10 ↔ CIM-11",
                        class_="text-muted text-center small mt-4 mb-0",
                    ),
                    class_="card-body px-4 py-4",
                ),
                class_="card shadow-lg border-0",
                style="width: 420px; border-radius: 0.75rem; overflow: hidden;",
            ),
            class_="d-flex justify-content-center align-items-center",
            style="min-height: 85vh;",
        ),
    )


@module.server
def auth_login_server(input, output, session, db_con_factory):
    """Login server. `db_con_factory` must be a callable returning a fresh
    sqlite3 connection (with row_factory=sqlite3.Row)."""
    from db.models import authenticate

    current_user = reactive.value(None)

    @output
    @render.ui
    def login_msg():
        return None

    @reactive.effect
    @reactive.event(input.login_btn)
    def _do_login():
        username = (input.login_user() or "").strip()
        password = input.login_pass() or ""

        if not username or not password:
            @output
            @render.ui
            def login_msg():
                return ui.div(
                    "Veuillez remplir tous les champs.",
                    class_="alert alert-warning mt-2 py-2 small",
                )
            return

        con = db_con_factory()
        try:
            result = authenticate(con, username, password)
        finally:
            con.close()

        if result is None:
            @output
            @render.ui
            def login_msg():
                return ui.div(
                    "Identifiants incorrects.",
                    class_="alert alert-danger mt-2 py-2 small",
                )
        elif isinstance(result, dict) and result.get("error") == "inactive":
            @output
            @render.ui
            def login_msg():
                return ui.div(
                    "Compte désactivé. Contactez un administrateur.",
                    class_="alert alert-danger mt-2 py-2 small",
                )
        else:
            current_user.set(result)
            @output
            @render.ui
            def login_msg():
                return None

    return current_user


# ═══════════════════════════════════════════════════════════════════════════
# User bar (top-right of navbar)
# ═══════════════════════════════════════════════════════════════════════════

@module.ui
def auth_user_bar_ui() -> ui.Tag:
    return ui.div(
        ui.output_ui("user_bar_content"),
        class_="d-flex align-items-center gap-2",
    )


@module.server
def auth_user_bar_server(input, output, session, current_user):
    @output
    @render.ui
    def user_bar_content():
        user = current_user()
        if user is None:
            return ui.span()
        role = user.get("role", "mainteneur")
        icon = ROLE_ICONS.get(role, "👤")
        label = ROLE_LABELS.get(role, role)
        display = user.get("full_name") or user.get("username", "")
        return ui.div(
            ui.span(
                f"{icon} {display}",
                title=label,
                class_="text-light me-2 small",
            ),
            ui.input_action_button(
                "logout_btn", "",
                icon=ui.tags.i(class_="bi bi-box-arrow-right"),
                class_="btn btn-outline-light btn-sm",
            ),
            class_="d-flex align-items-center gap-1",
        )

    @reactive.effect
    @reactive.event(input.logout_btn)
    def _do_logout():
        current_user.set(None)


# ═══════════════════════════════════════════════════════════════════════════
# Role-based access helpers
# ═══════════════════════════════════════════════════════════════════════════

def has_role(user: dict | None, *roles: str) -> bool:
    """True if `user` has any of the given roles. Admin always returns True
    (admin has all privileges by design)."""
    if user is None:
        return False
    user_role = user.get("role", "")
    if user_role == "admin":
        return True
    return user_role in roles


def is_admin(user: dict | None) -> bool:
    return user is not None and user.get("role") == "admin"


def is_mainteneur(user: dict | None) -> bool:
    """True if user is admin OR mainteneur (admin has all privileges)."""
    return has_role(user, "mainteneur")


def is_valideur(user: dict | None) -> bool:
    """True if user is admin OR valideur (admin has all privileges)."""
    return has_role(user, "valideur")

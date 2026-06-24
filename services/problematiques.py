"""
services/problematiques.py — CRUD for the problematique_types catalog (plan §16.7).

Admins create / edit / deactivate typology values via the Admin → Paramètres
UI. Maintainers select a problematique when annotating a justification.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from services import authz


VALID_BOOTSTRAP_COLORS = frozenset({
    "primary", "secondary", "success", "danger", "warning", "info",
    "light", "dark",
})


def list_problematiques(
    con: sqlite3.Connection, *, only_active: bool = True,
) -> list[dict]:
    sql = "SELECT * FROM problematique_types"
    if only_active:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, libelle"
    return [dict(r) for r in con.execute(sql).fetchall()]


def get_problematique(con: sqlite3.Connection, code: str) -> Optional[dict]:
    row = con.execute(
        "SELECT * FROM problematique_types WHERE code = ?", (code,),
    ).fetchone()
    return dict(row) if row else None


def create_problematique(
    con: sqlite3.Connection, *,
    user: dict,
    code: str, libelle: str,
    description: str = "",
    color: str = "secondary",
    sort_order: int = 100,
) -> None:
    """Create a new problematique type. Admin only."""
    authz.require_admin(user, action="problematique_create")
    code = code.strip().lower()
    if not code or not libelle.strip():
        raise ValueError("code et libelle requis")
    if color not in VALID_BOOTSTRAP_COLORS:
        raise ValueError(f"color must be in {sorted(VALID_BOOTSTRAP_COLORS)}")
    con.execute(
        """INSERT INTO problematique_types
               (code, libelle, description, color, sort_order, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (code, libelle.strip(), description, color, sort_order, user["id"]),
    )
    con.commit()
    from services.audit import audit_user_action
    audit_user_action(
        con, user=user, action="admin_config_change",
        object_type="config", object_id=f"problematique_types/{code}",
        new_value={"libelle": libelle, "color": color, "active": True},
        note="problematique type created",
    )


def update_problematique(
    con: sqlite3.Connection, *,
    user: dict, code: str,
    libelle: Optional[str] = None,
    description: Optional[str] = None,
    color: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> None:
    """Update an existing problematique type. Admin only."""
    authz.require_admin(user, action="problematique_update")
    sets, params = [], []
    if libelle is not None:
        sets.append("libelle = ?"); params.append(libelle.strip())
    if description is not None:
        sets.append("description = ?"); params.append(description)
    if color is not None:
        if color not in VALID_BOOTSTRAP_COLORS:
            raise ValueError(f"color must be in {sorted(VALID_BOOTSTRAP_COLORS)}")
        sets.append("color = ?"); params.append(color)
    if sort_order is not None:
        sets.append("sort_order = ?"); params.append(int(sort_order))
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(code)
    con.execute(
        f"UPDATE problematique_types SET {', '.join(sets)} WHERE code = ?",
        params,
    )
    con.commit()
    from services.audit import audit_user_action
    audit_user_action(
        con, user=user, action="admin_config_change",
        object_type="config", object_id=f"problematique_types/{code}",
        new_value={k: v for k, v in [
            ("libelle", libelle), ("description", description),
            ("color", color), ("sort_order", sort_order),
        ] if v is not None},
        note="problematique type updated",
    )


def deactivate_problematique(
    con: sqlite3.Connection, *,
    user: dict, code: str, active: bool = False,
) -> None:
    """Soft-disable a problematique type. Admin only."""
    authz.require_admin(user, action="problematique_deactivate")
    con.execute(
        "UPDATE problematique_types SET active = ?, updated_at = datetime('now') WHERE code = ?",
        (1 if active else 0, code),
    )
    con.commit()
    from services.audit import audit_user_action
    audit_user_action(
        con, user=user, action="admin_config_change",
        object_type="config", object_id=f"problematique_types/{code}",
        new_value={"active": bool(active)},
        note="problematique type (de)activated",
    )

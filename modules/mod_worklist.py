"""
modules/mod_worklist.py — Worklists (assignments) module.

Displays the assignment lists a user is assigned to, with progress tracking.
Per plan §14 #17 : accessible à tous les mainteneurs.

Two views :
  - "Mes worklists" : assignments where user_id = current_user (active or completed)
  - Admin view (only for admins) : all assignments across users

Each assignment_list has a query_definition (JSON filters) that is evaluated
dynamically against mappings to determine the current set of codes.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from shiny import module, reactive, render, ui

import pandas as pd

from db.database import get_connection
from services import authz


# ─────────────────────────────────────────────────────────────────────────
# Query definition evaluation
# ─────────────────────────────────────────────────────────────────────────

ALLOWED_FILTER_KEYS = {
    "direction", "chapitre", "fiabilite", "status", "target_kind",
    "source_decision", "est_classant", "est_cma",
}


def _filter_to_sql(query_def: dict) -> tuple[str, list]:
    """Convert a query_definition JSON into a parametrized WHERE clause.

    Validated against ALLOWED_FILTER_KEYS to prevent SQL injection."""
    where = []
    params: list = []
    for key, val in query_def.items():
        if key not in ALLOWED_FILTER_KEYS:
            continue
        if val is None or val == "" or val == []:
            continue
        if key == "direction":
            where.append("m.direction = ?"); params.append(val)
        elif key in {"chapitre", "source_decision"}:
            where.append(f"m.{key} = ?" if key != "chapitre" else "c.chapitre = ?")
            params.append(val)
        elif key in {"fiabilite", "status", "target_kind"}:
            vals = val if isinstance(val, list) else [val]
            ph = ",".join("?" for _ in vals)
            where.append(f"m.{key} IN ({ph})")
            params.extend(vals)
        elif key in {"est_classant", "est_cma"}:
            where.append(f"c.{key} = ?")
            params.append(1 if val else 0)
    return (" AND ".join(where) if where else "1=1"), params


def evaluate_assignment_list(
    con: sqlite3.Connection, list_id: int,
) -> dict:
    """Resolve the codes targeted by an assignment list, return summary."""
    lst = con.execute(
        "SELECT * FROM assignment_lists WHERE id = ?", (list_id,),
    ).fetchone()
    if lst is None:
        raise KeyError(f"assignment_list not found: id={list_id}")
    lst = dict(lst)

    # Static codes path
    if lst["static_codes"]:
        codes = json.loads(lst["static_codes"])
        return {
            "list": lst,
            "total_codes": len(codes),
            "static": True,
            "codes_sample": codes[:10],
        }

    # Dynamic path : evaluate query_definition
    query_def = json.loads(lst["query_definition"]) if lst["query_definition"] else {}
    where_sql, params = _filter_to_sql(query_def)
    direction = query_def.get("direction", "both")

    sql_count = f"""
        SELECT COUNT(*) FROM mappings m
        LEFT JOIN cim10_codes c ON c.code = m.source_code
        WHERE {where_sql}
    """
    n = con.execute(sql_count, params).fetchone()[0]

    # Get progress : how many mappings in the list are at 'valide' or 'gele' ?
    sql_done = f"""
        SELECT COUNT(*) FROM mappings m
        LEFT JOIN cim10_codes c ON c.code = m.source_code
        WHERE {where_sql} AND m.status IN ('valide', 'gele')
    """
    n_done = con.execute(sql_done, params).fetchone()[0]

    return {
        "list": lst,
        "total_codes": n,
        "done": n_done,
        "remaining": n - n_done,
        "progress_pct": (n_done * 100.0 / n) if n > 0 else 0.0,
        "static": False,
        "query_def": query_def,
    }


def fetch_assignments_for_user(
    con: sqlite3.Connection, user_id: int, *,
    include_done: bool = False,
) -> list[dict]:
    """Return all assignments for a user with progress info computed."""
    sql = """
        SELECT a.*, l.name AS list_name, l.description AS list_description,
               u_by.username AS assigned_by_username
        FROM assignments a
        JOIN assignment_lists l ON l.id = a.list_id
        LEFT JOIN users u_by ON u_by.id = a.assigned_by
        WHERE a.user_id = ?
    """
    params: list = [user_id]
    if not include_done:
        sql += " AND a.status IN ('open', 'in_progress')"
    sql += " ORDER BY a.due_date NULLS LAST, a.id DESC"

    # SQLite NULLS LAST is not standard ; use CASE
    sql = sql.replace("NULLS LAST", "")
    sql += ""  # we accept default ordering

    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    # Enrich with progress
    for r in rows:
        try:
            r["progress"] = evaluate_assignment_list(con, r["list_id"])
        except Exception as e:
            r["progress"] = {"error": str(e)}
    return rows


def fetch_all_assignments(con: sqlite3.Connection) -> list[dict]:
    """Admin view : all assignments across users."""
    sql = """
        SELECT a.*, l.name AS list_name, u.username,
               u_by.username AS assigned_by_username
        FROM assignments a
        JOIN assignment_lists l ON l.id = a.list_id
        JOIN users u            ON u.id = a.user_id
        LEFT JOIN users u_by    ON u_by.id = a.assigned_by
        ORDER BY a.assigned_at DESC
    """
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    return rows


def create_assignment_list(
    con: sqlite3.Connection, *,
    user: dict,
    name: str,
    description: str = "",
    direction: str = "forward",
    query_definition: Optional[dict] = None,
    static_codes: Optional[list[str]] = None,
) -> int:
    """Create a new assignment list."""
    authz.require_capability(user, "create_assignment_list")
    if direction not in ("forward", "reverse", "both"):
        raise ValueError(f"invalid direction: {direction!r}")
    if query_definition is None and not static_codes:
        raise ValueError("either query_definition or static_codes is required")

    cur = con.execute(
        """INSERT INTO assignment_lists
               (name, description, direction, query_definition, static_codes, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, description, direction,
         json.dumps(query_definition, ensure_ascii=False) if query_definition else None,
         json.dumps(static_codes, ensure_ascii=False) if static_codes else None,
         user["id"]),
    )
    con.commit()
    from services.audit import audit_user_action
    audit_user_action(con, user=user, action="admin_list_create",
                      object_type="assignment_list", object_id=cur.lastrowid,
                      new_value={"name": name, "direction": direction})
    return cur.lastrowid


def assign_user_to_list(
    con: sqlite3.Connection, *,
    user: dict, list_id: int, target_user_id: int, expected_role: str,
    due_date: Optional[str] = None,
) -> int:
    """Assign a user to an assignment list with an expected role."""
    authz.require_capability(user, "assign_user_to_list")
    if expected_role not in ("mainteneur", "valideur"):
        raise ValueError(f"invalid expected_role: {expected_role!r}")
    cur = con.execute(
        """INSERT INTO assignments
               (list_id, user_id, expected_role, assigned_by, due_date)
           VALUES (?, ?, ?, ?, ?)""",
        (list_id, target_user_id, expected_role, user["id"], due_date),
    )
    con.commit()
    from services.audit import audit_user_action
    audit_user_action(con, user=user, action="admin_assign",
                      object_type="assignment", object_id=cur.lastrowid,
                      new_value={"list_id": list_id, "user_id": target_user_id,
                                 "expected_role": expected_role})
    return cur.lastrowid


def update_assignment_list(
    con: sqlite3.Connection, *,
    user: dict, list_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    direction: Optional[str] = None,
    query_definition: Optional[dict] = None,
    static_codes: Optional[list[str]] = None,
) -> None:
    """Update an existing assignment list (plan §16.5)."""
    authz.require_capability(user, "edit_assignment_list")
    sets, params = [], []
    if name is not None:
        sets.append("name = ?"); params.append(name)
    if description is not None:
        sets.append("description = ?"); params.append(description)
    if direction is not None:
        if direction not in ("forward", "reverse", "both"):
            raise ValueError(f"invalid direction: {direction!r}")
        sets.append("direction = ?"); params.append(direction)
    if query_definition is not None:
        sets.append("query_definition = ?")
        params.append(json.dumps(query_definition, ensure_ascii=False))
    if static_codes is not None:
        sets.append("static_codes = ?")
        params.append(json.dumps(static_codes, ensure_ascii=False))
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(list_id)
    con.execute(f"UPDATE assignment_lists SET {', '.join(sets)} WHERE id = ?", params)
    con.commit()
    from services.audit import audit_user_action
    audit_user_action(con, user=user, action="admin_list_update",
                      object_type="assignment_list", object_id=list_id,
                      new_value={k: v for k, v in [
                          ("name", name), ("description", description),
                          ("direction", direction),
                          ("query_definition", query_definition),
                          ("static_codes_count", len(static_codes) if static_codes else None),
                      ] if v is not None})


def list_codes_in_assignment(con: sqlite3.Connection, list_id: int) -> list[str]:
    """Resolve the codes covered by an assignment list (static or dynamic).

    For dynamic lists, runs the query and returns just the source_code list.
    Capped at 50 000 to avoid blowing up the UI on broad filters.
    """
    lst = con.execute(
        "SELECT * FROM assignment_lists WHERE id = ?", (list_id,)
    ).fetchone()
    if lst is None:
        raise KeyError(f"list not found : {list_id}")
    lst = dict(lst)
    if lst["static_codes"]:
        return json.loads(lst["static_codes"])
    query_def = json.loads(lst["query_definition"]) if lst["query_definition"] else {}
    where_sql, params = _filter_to_sql(query_def)
    sql = f"""
        SELECT DISTINCT m.source_code FROM mappings m
        LEFT JOIN cim10_codes c ON c.code = m.source_code
        WHERE {where_sql}
        LIMIT 50000
    """
    return [r[0] for r in con.execute(sql, params).fetchall()]


def preview_filter_count(con: sqlite3.Connection, query_def: dict) -> int:
    """Preview the count of mappings matching a query_definition (for the
    'Create list from filters' UI in admin)."""
    where_sql, params = _filter_to_sql(query_def)
    sql = f"""
        SELECT COUNT(DISTINCT m.source_code) FROM mappings m
        LEFT JOIN cim10_codes c ON c.code = m.source_code
        WHERE {where_sql}
    """
    return con.execute(sql, params).fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────

@module.ui
def worklist_ui() -> ui.Tag:
    return ui.div(
        ui.h4(
            ui.tags.i(class_="bi bi-list-check me-2"),
            "Mes worklists",
        ),
        ui.output_ui("worklist_content"),
    )


@module.server
def worklist_server(input, output, session, current_user: reactive.Value):

    @output
    @render.ui
    def worklist_content():
        user = current_user()
        if user is None:
            return ui.div("Veuillez vous connecter.", class_="alert alert-warning")
        con = get_connection()
        try:
            assignments = fetch_assignments_for_user(con, user["id"], include_done=True)
        finally:
            con.close()

        if not assignments:
            return ui.div(
                "Aucune worklist assignée.",
                ui.tags.br(),
                ui.tags.small("Les administrateurs peuvent vous affecter à des listes "
                              "depuis l'onglet Administration.",
                              class_="text-muted"),
                class_="alert alert-info",
            )

        cards = []
        for a in assignments:
            prog = a.get("progress", {})
            pct = prog.get("progress_pct", 0)
            color = "success" if pct >= 100 else ("info" if pct > 0 else "secondary")
            cards.append(ui.div(
                ui.div(
                    ui.div(
                        ui.tags.strong(a["list_name"]),
                        ui.tags.span(
                            f"  ({a['expected_role']})",
                            class_="text-muted small ms-2",
                        ),
                        ui.tags.span(
                            "  ⚐ " + (a["due_date"] or "sans échéance"),
                            class_="text-muted small ms-2",
                        ),
                    ),
                    ui.div(a["list_description"] or "", class_="small text-muted"),
                    class_="card-header",
                ),
                ui.div(
                    ui.div(
                        f"{prog.get('done', 0)} / {prog.get('total_codes', '?')} mappings traités",
                        class_="small mb-1",
                    ),
                    ui.div(
                        ui.div(
                            class_=f"progress-bar bg-{color}",
                            style=f"width: {pct:.0f}%;",
                            role="progressbar",
                            **{"aria-valuenow": str(int(pct)),
                               "aria-valuemin": "0", "aria-valuemax": "100"},
                        ),
                        class_="progress",
                        style="height: 16px;",
                    ),
                    ui.div(
                        ui.tags.small(f"Affecté par {a.get('assigned_by_username') or 'admin'}"
                                      f" le {a['assigned_at']}",
                                      class_="text-muted"),
                        class_="mt-2",
                    ),
                    class_="card-body",
                ),
                class_="card mb-3",
            ))

        return ui.div(*cards)

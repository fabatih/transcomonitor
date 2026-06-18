"""
services/audit.py — Append-only audit service.

Centralized writer for `audit_events`. Every state-changing operation in the
application MUST go through this service (no direct INSERT INTO audit_events
in modules) so the audit trail stays consistent.

Properties (enforced by the schema + this layer) :
  - Append-only : UPDATE / DELETE blocked by triggers (cf. db/schema_sqlite.sql)
  - Actor identification : user_id + dénormalized username (preserves trace
    even if user is deactivated/deleted)
  - Object identification : type + id (string for flexibility)
  - Diff capture : old_value_json + new_value_json (JSON-serializable)
  - Source attribution : ui / import / rule_engine / api / system
  - Optional request metadata : IP + UA (controlled by app_config toggle, §14 #9)

Helpers :
  - write_audit() : low-level writer
  - audit_user_action() : convenience for user-initiated changes
  - audit_system_action() : convenience for system/import/rule events
  - get_recent_events() / get_object_history() : consultation
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

# Allowed enum values mirror the schema's CHECK constraints. Validated client-side
# for better error messages (the DB layer is the source of truth).
ALLOWED_ACTIONS = frozenset({
    "login", "logout", "login_failed",
    "create_mapping", "edit_mapping", "validate_mapping",
    "contest_mapping", "reject_mapping",
    "freeze_version", "compare_versions",
    "import_preview", "import_apply", "import_resolve_diff",
    "export", "admin_user_create", "admin_user_update",
    "admin_user_deactivate", "admin_config_change",
    "admin_secret_set", "admin_list_create", "admin_list_update",
    "admin_assign", "rule_apply", "cache_refresh",
    "backup_snapshot", "backup_restore",
})

ALLOWED_OBJECT_TYPES = frozenset({
    "mapping", "user", "frozen_version", "import_batch",
    "assignment_list", "assignment", "rule", "config", "system",
})

ALLOWED_SOURCES = frozenset({"ui", "import", "rule_engine", "api", "system"})


# ─────────────────────────────────────────────────────────────────────────
# Core writer
# ─────────────────────────────────────────────────────────────────────────

def write_audit(
    con: sqlite3.Connection,
    *,
    action: str,
    actor_user_id: Optional[int] = None,
    actor_username: Optional[str] = None,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    source: str = "ui",
    request_ip: Optional[str] = None,
    request_ua: Optional[str] = None,
    note: Optional[str] = None,
) -> int:
    """Append a single audit event. Returns the new event id.

    `old_value` and `new_value` are JSON-serialized automatically. Pass dicts,
    lists, strings, numbers, booleans, or None. Non-serializable objects raise
    TypeError.

    `request_ip` and `request_ua` are written as-is — the caller is responsible
    for checking the `audit_capture_request_meta` app_config toggle (§14 #9).
    """
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"action must be in {sorted(ALLOWED_ACTIONS)}, got {action!r}")
    if object_type is not None and object_type not in ALLOWED_OBJECT_TYPES:
        raise ValueError(f"object_type must be in {sorted(ALLOWED_OBJECT_TYPES)}, got {object_type!r}")
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"source must be in {sorted(ALLOWED_SOURCES)}, got {source!r}")

    old_json = json.dumps(old_value, ensure_ascii=False, default=str) if old_value is not None else None
    new_json = json.dumps(new_value, ensure_ascii=False, default=str) if new_value is not None else None

    cur = con.execute(
        """INSERT INTO audit_events (
                actor_user_id, actor_username, action,
                object_type, object_id, old_value_json, new_value_json,
                source, request_ip, request_ua, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (actor_user_id, actor_username, action,
         object_type, str(object_id) if object_id is not None else None,
         old_json, new_json, source, request_ip, request_ua, note),
    )
    con.commit()
    return cur.lastrowid


# ─────────────────────────────────────────────────────────────────────────
# Convenience writers
# ─────────────────────────────────────────────────────────────────────────

def audit_user_action(
    con: sqlite3.Connection,
    *,
    user: dict,
    action: str,
    object_type: Optional[str] = None,
    object_id: Optional[Any] = None,
    old_value: Any = None,
    new_value: Any = None,
    note: Optional[str] = None,
    request_ip: Optional[str] = None,
    request_ua: Optional[str] = None,
) -> int:
    """Audit an action initiated by a logged-in user (source='ui')."""
    return write_audit(
        con,
        action=action,
        actor_user_id=user.get("id"),
        actor_username=user.get("username"),
        object_type=object_type, object_id=object_id,
        old_value=old_value, new_value=new_value,
        source="ui",
        request_ip=request_ip, request_ua=request_ua,
        note=note,
    )


def audit_system_action(
    con: sqlite3.Connection,
    *,
    action: str,
    object_type: Optional[str] = None,
    object_id: Optional[Any] = None,
    old_value: Any = None,
    new_value: Any = None,
    source: str = "system",
    note: Optional[str] = None,
) -> int:
    """Audit an action by the system itself (boot, scheduled job)
    or by an import/rule engine (set source explicitly)."""
    return write_audit(
        con,
        action=action,
        actor_user_id=None, actor_username=None,
        object_type=object_type, object_id=object_id,
        old_value=old_value, new_value=new_value,
        source=source,
        note=note,
    )


# ─────────────────────────────────────────────────────────────────────────
# Consultation
# ─────────────────────────────────────────────────────────────────────────

def get_recent_events(
    con: sqlite3.Connection,
    *,
    limit: int = 100,
    actor_user_id: Optional[int] = None,
    action: Optional[str] = None,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Fetch recent audit events with optional filters."""
    where: list[str] = []
    params: list[Any] = []
    if actor_user_id is not None:
        where.append("actor_user_id = ?"); params.append(actor_user_id)
    if action is not None:
        where.append("action = ?"); params.append(action)
    if object_type is not None:
        where.append("object_type = ?"); params.append(object_type)
    if object_id is not None:
        where.append("object_id = ?"); params.append(str(object_id))
    sql = "SELECT * FROM audit_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return con.execute(sql, params).fetchall()


def get_object_history(
    con: sqlite3.Connection, object_type: str, object_id: Any,
) -> list[sqlite3.Row]:
    """Full chronological history (oldest first) for a specific object."""
    return con.execute(
        """SELECT * FROM audit_events
           WHERE object_type = ? AND object_id = ?
           ORDER BY ts ASC, id ASC""",
        (object_type, str(object_id)),
    ).fetchall()


def get_user_actions(
    con: sqlite3.Connection, user_id: int, *, limit: int = 100
) -> list[sqlite3.Row]:
    """All actions performed by a given user."""
    return con.execute(
        """SELECT * FROM audit_events
           WHERE actor_user_id = ?
           ORDER BY ts DESC, id DESC LIMIT ?""",
        (user_id, int(limit)),
    ).fetchall()


def count_events(con: sqlite3.Connection, since_ts: Optional[str] = None) -> int:
    """Total event count (optionally since a given ISO timestamp)."""
    if since_ts:
        return con.execute(
            "SELECT COUNT(*) FROM audit_events WHERE ts >= ?", (since_ts,)
        ).fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────
# Diff helpers (for capturing before/after state)
# ─────────────────────────────────────────────────────────────────────────

def diff_dicts(before: dict, after: dict, *, ignored_keys: Iterable[str] = ()) -> dict:
    """Compute the field-level diff between two dicts. Returns a dict with
    only the keys that differ, each mapped to {'before': v1, 'after': v2}.

    Useful pattern :
        old = dict(row)
        # ... mutate ...
        new = dict(updated_row)
        if (changes := diff_dicts(old, new, ignored_keys=('updated_at',))):
            audit_user_action(con, user=user, action='edit_mapping',
                              object_type='mapping', object_id=mid,
                              old_value=changes, new_value=None)
    """
    ignored = set(ignored_keys)
    out: dict = {}
    all_keys = set(before) | set(after)
    for k in all_keys:
        if k in ignored:
            continue
        b = before.get(k)
        a = after.get(k)
        if b != a:
            out[k] = {"before": b, "after": a}
    return out

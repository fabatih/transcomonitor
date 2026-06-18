"""
db/models.py — CRUD operations for transcomonitor entities.

This MVP version covers the operations needed by the auth and admin modules.
More CRUD will be added as other modules come online.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime
from typing import Optional

from utils.security import hash_password, verify_password


# ─────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────

ALLOWED_ROLES = ("admin", "mainteneur", "valideur")


def create_user(
    con: sqlite3.Connection,
    username: str,
    password: str,
    role: str = "mainteneur",
    email: Optional[str] = None,
    full_name: Optional[str] = None,
) -> int:
    if role not in ALLOWED_ROLES:
        raise ValueError(f"role must be one of {ALLOWED_ROLES}, got {role!r}")
    pw_hash = hash_password(password)
    cur = con.execute(
        """INSERT INTO users (username, password_hash, role, email, full_name)
           VALUES (?, ?, ?, ?, ?)""",
        (username, pw_hash, role, email, full_name),
    )
    con.commit()
    return cur.lastrowid


def authenticate(
    con: sqlite3.Connection, username: str, password: str
) -> Optional[dict]:
    """Return user dict on success ; None on bad credentials ;
    {'error': 'inactive'} on deactivated account.

    On success, the returned dict reflects the newly-updated last_login.
    """
    row = con.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return None
    if not row["active"]:
        return {"error": "inactive"}
    if not verify_password(password, row["password_hash"]):
        return None
    new_login = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "UPDATE users SET last_login = ? WHERE id = ?",
        (new_login, row["id"]),
    )
    con.commit()
    user = dict(row)
    user["last_login"] = new_login
    return user


def get_user(con: sqlite3.Connection, user_id: int) -> Optional[dict]:
    row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(con: sqlite3.Connection, username: str) -> Optional[dict]:
    row = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def list_users(con: sqlite3.Connection, include_inactive: bool = True) -> list[dict]:
    sql = (
        "SELECT id, username, role, active, email, full_name, created_at, last_login "
        "FROM users"
    )
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY username"
    return [dict(r) for r in con.execute(sql).fetchall()]


def update_user(
    con: sqlite3.Connection,
    user_id: int,
    role: Optional[str] = None,
    email: Optional[str] = None,
    full_name: Optional[str] = None,
    password: Optional[str] = None,
    active: Optional[int] = None,
) -> None:
    updates, params = [], []
    if role is not None:
        if role not in ALLOWED_ROLES:
            raise ValueError(f"role must be one of {ALLOWED_ROLES}")
        updates.append("role = ?"); params.append(role)
    if email is not None:
        updates.append("email = ?"); params.append(email)
    if full_name is not None:
        updates.append("full_name = ?"); params.append(full_name)
    if password is not None:
        updates.append("password_hash = ?"); params.append(hash_password(password))
    if active is not None:
        updates.append("active = ?"); params.append(int(bool(active)))
    if not updates:
        return
    params.append(user_id)
    con.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    con.commit()


def ensure_default_admin(
    con: sqlite3.Connection,
    username: str = "admin",
    password: Optional[str] = None,
) -> None:
    """Ensure a default admin user exists. If none, create one with the
    provided password (or a random 16-char password logged once to stdout)."""
    existing = con.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchone()
    if existing:
        return
    if not password:
        password = secrets.token_urlsafe(12)
        print(f"[transcomonitor] ⚠️  No DEFAULT_ADMIN_PASS env var — generated random password for '{username}':")
        print(f"[transcomonitor]     {password}")
        print(f"[transcomonitor] ⚠️  Save this password — it will NOT be shown again.")
    create_user(con, username=username, password=password, role="admin",
                full_name="Administrateur initial")
    print(f"[transcomonitor] Default admin '{username}' created.")


# ─────────────────────────────────────────────────────────────────────────
# App config (key-value with optional encryption)
# ─────────────────────────────────────────────────────────────────────────

def get_config_value(
    con: sqlite3.Connection, key: str, default: str = ""
) -> str:
    row = con.execute(
        "SELECT value, is_secret FROM app_config WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return default
    value = row["value"] or ""
    if row["is_secret"]:
        from utils.crypto import decrypt_value
        return decrypt_value(value)
    return value


def set_config_value(
    con: sqlite3.Connection,
    key: str,
    value: str,
    *,
    is_secret: bool = False,
    updated_by: Optional[int] = None,
) -> None:
    from utils.crypto import encrypt_value, is_sensitive_key

    auto_secret = is_secret or is_sensitive_key(key)
    stored = encrypt_value(value) if auto_secret else value
    con.execute(
        """INSERT INTO app_config (key, value, is_secret, updated_at, updated_by)
           VALUES (?, ?, ?, datetime('now'), ?)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               is_secret = excluded.is_secret,
               updated_at = excluded.updated_at,
               updated_by = excluded.updated_by""",
        (key, stored, int(auto_secret), updated_by),
    )
    con.commit()

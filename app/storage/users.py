"""
User accounts -- the three states are represented as a single `role` column:
unauthenticated is simply "no valid token", `role='user'` and `role='admin'`
cover everyone with an account. `role='user'` still exists at the storage
layer (create_user takes a role, defaulting to 'user') for any future
caller that wants it, but this app's own /auth/signup (app.routers.auth)
always passes role='admin' -- this Ionic app is the internal ops console,
not the end-user product, so every account created through it is trusted
staff. tools/create_admin.py can still create/promote accounts directly.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.auth.security import hash_password, verify_password
from app.storage.db import db_conn
from app.storage.league_memory import _init_db

VALID_ROLES = {"user", "admin"}


class UserExistsError(Exception):
    pass


def create_user(email: str, password: str, *, display_name: str = "", role: str = "user") -> dict[str, Any]:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    _init_db()
    email_norm = email.strip().lower()
    password_hash, salt = hash_password(password)
    with db_conn(timeout=20) as conn:
        try:
            cur = conn.execute(
                """
                insert into users (email, password_hash, password_salt, display_name, role)
                values (?, ?, ?, ?, ?)
                """,
                (email_norm, password_hash, salt, display_name.strip(), role),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise UserExistsError(f"An account with {email_norm} already exists") from exc
        user_id = cur.lastrowid
    user = get_user_by_id(user_id)
    assert user is not None  # just inserted it
    return user


def get_user_by_email(email: str) -> dict[str, Any] | None:
    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from users where email = ?", (email.strip().lower(),)).fetchone()
    return _row_to_dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
    return _row_to_dict(row) if row else None


def verify_credentials(email: str, password: str) -> dict[str, Any] | None:
    """Returns the user dict on success, None on a bad email/password —
    deliberately doesn't distinguish "no such user" from "wrong password"
    to the caller, so callers can't leak account existence."""
    _init_db()
    email_norm = email.strip().lower()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from users where email = ?", (email_norm,)).fetchone()
    if not row or not verify_password(password, row["password_hash"], row["password_salt"]):
        return None
    _touch_last_login(row["id"])
    # Re-fetch rather than patch the in-memory dict, so the returned
    # last_login_at always matches what SQLite actually stored (its
    # current_timestamp format), not a Python-side approximation of it.
    user = get_user_by_id(row["id"])
    assert user is not None  # the row we just updated can't have vanished
    return user


def set_user_role(email: str, role: str) -> bool:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    _init_db()
    with db_conn(timeout=20) as conn:
        cur = conn.execute(
            "update users set role = ? where email = ?", (role, email.strip().lower())
        )
        conn.commit()
        return cur.rowcount > 0


def _touch_last_login(user_id: int) -> None:
    with db_conn(timeout=20) as conn:
        conn.execute("update users set last_login_at = current_timestamp where id = ?", (user_id,))
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # Never include password_hash/password_salt — this is the shape that
    # goes straight into API responses and JWT lookups.
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }

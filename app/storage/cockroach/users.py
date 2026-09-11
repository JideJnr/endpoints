"""
CockroachDB pilot mirror of app/storage/users.py -- NOT used by the live app.

Same function names and return shapes as the SQLite version so that,
if the pilot proves solid, swapping the import in app/auth/dependencies.py
and app/routers/auth.py is the only change needed later. Until then this
module is only ever called by scripts/pilot_test_cockroach_users.py.
"""
from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.extras

from app.auth.security import hash_password, verify_password
from app.storage.cockroach.db import init_users_schema, pg_conn

VALID_ROLES = {"user", "admin"}


class UserExistsError(Exception):
    pass


def create_user(email: str, password: str, *, display_name: str = "", role: str = "user") -> dict[str, Any]:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    init_users_schema()
    email_norm = email.strip().lower()
    password_hash, salt = hash_password(password)
    with pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO users (email, password_hash, password_salt, display_name, role)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (email_norm, password_hash, salt, display_name.strip(), role),
                )
                user_id = cur.fetchone()["id"]
                conn.commit()
            except psycopg2.errors.UniqueViolation as exc:  # type: ignore[attr-defined]
                conn.rollback()
                raise UserExistsError(f"An account with {email_norm} already exists") from exc
    user = get_user_by_id(user_id)
    assert user is not None  # just inserted it
    return user


def get_user_by_email(email: str) -> dict[str, Any] | None:
    init_users_schema()
    with pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email.strip().lower(),))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    init_users_schema()
    with pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def verify_credentials(email: str, password: str) -> dict[str, Any] | None:
    """Returns the user dict on success, None on a bad email/password --
    deliberately doesn't distinguish "no such user" from "wrong password",
    same as the SQLite version."""
    init_users_schema()
    email_norm = email.strip().lower()
    with pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email_norm,))
            row = cur.fetchone()
    if not row or not verify_password(password, row["password_hash"], row["password_salt"]):
        return None
    _touch_last_login(row["id"])
    user = get_user_by_id(row["id"])
    assert user is not None
    return user


def set_user_role(email: str, role: str) -> bool:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    init_users_schema()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET role = %s WHERE email = %s", (role, email.strip().lower())
            )
            updated = cur.rowcount > 0
            conn.commit()
    return updated


def _touch_last_login(user_id: int) -> None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
        conn.commit()


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    # Never include password_hash/password_salt -- same contract as the
    # SQLite version, this shape goes straight into API responses.
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }

"""
Authentication & authorization — Postgres-backed.

Users, sessions, and roles all persist to Postgres. Sessions survive
server restarts.

Roles:
  admin   → everything, can manage users
  it      → read-only access to all files + mark LTMC uploaded
  module  → only their module's files
"""
from __future__ import annotations

import hashlib
import secrets

from services.db import get_conn


ALL_MODULES = ["SD", "MM", "PP", "QM", "FICO"]
ROLES = ["admin", "it", "module"]


def _hash_password(pw: str) -> str:
    """SHA-256 for prototype. Use bcrypt in production."""
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────────
# Login / sessions
# ────────────────────────────────────────────────────────────────────────────

def login(username: str, password: str) -> dict | None:
    """Validate credentials and issue a session token.
    Returns payload dict or None if invalid."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, password_hash, display_name, role, module FROM users WHERE username = %s",
                (username,),
            )
            user = cur.fetchone()
            if not user:
                return None
            if user["password_hash"] != _hash_password(password):
                return None

            token = secrets.token_urlsafe(24)
            cur.execute(
                "INSERT INTO sessions (token, username) VALUES (%s, %s)",
                (token, user["username"]),
            )

            return {
                "token": token,
                "username": user["username"],
                "display_name": user["display_name"],
                "role": user["role"],
                "module": user["module"],
            }


def logout(token: str):
    if not token:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


def get_user(token: str | None) -> dict | None:
    """Resolve a session token to a user payload."""
    if not token:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.username, u.display_name, u.role, u.module
                     FROM sessions s
                     JOIN users u ON u.username = s.username
                    WHERE s.token = %s
                      AND s.issued_at > NOW() - INTERVAL '8 hours'""",
                (token,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "token": token,
                "username": row["username"],
                "display_name": row["display_name"],
                "role": row["role"],
                "module": row["module"],
            }


def require_user(token: str | None) -> dict:
    user = get_user(token)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(401, "Not authenticated")
    return user


def require_admin(token: str | None) -> dict:
    user = require_user(token)
    if user["role"] != "admin":
        from fastapi import HTTPException
        raise HTTPException(403, "Admin only")
    return user


def can_access_module(user: dict, module: str) -> bool:
    if user["role"] in ("admin", "it"):
        return True
    if user["role"] == "module":
        return user.get("module") == module
    return False


def can_mark_ltmc_uploaded(user: dict) -> bool:
    return user["role"] in ("admin", "it")


def can_validate(user: dict, module: str) -> bool:
    if user["role"] == "admin":
        return True
    if user["role"] == "module" and user.get("module") == module:
        return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# User management (admin only)
# ────────────────────────────────────────────────────────────────────────────

def list_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT username, display_name, role, module,
                          EXTRACT(EPOCH FROM created_at) AS created_at
                     FROM users
                    ORDER BY created_at"""
            )
            return [dict(r) for r in cur.fetchall()]


def create_user(username: str, password: str, display_name: str,
                role: str, module: str | None = None) -> dict:
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    if role == "module" and module not in ALL_MODULES:
        raise ValueError(f"Invalid module: {module}")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                raise ValueError(f"User '{username}' already exists")
            cur.execute(
                """INSERT INTO users (username, password_hash, display_name, role, module)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING username, display_name, role, module""",
                (username, _hash_password(password), display_name, role,
                 module if role == "module" else None),
            )
            return dict(cur.fetchone())


def change_password(username: str, new_password: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s",
                (_hash_password(new_password), username),
            )
            if cur.rowcount == 0:
                raise ValueError(f"User not found: {username}")


def change_role(username: str, new_role: str, new_module: str | None = None):
    """Change a user's role (admin / it / module / sd_sme / etc.).
    If new_role is 'module', new_module must be one of the 5 module codes.
    For any other role, the module column is cleared. Guard: refuses to
    change the 'admin' user off the admin role — prevents lockout."""
    if username == "admin" and new_role != "admin":
        raise ValueError("Cannot demote the primary admin user")
    # module is only meaningful when role=module; otherwise null
    module = new_module if new_role == "module" else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET role = %s, module = %s WHERE username = %s",
                (new_role, module, username),
            )
            if cur.rowcount == 0:
                raise ValueError(f"User not found: {username}")


def delete_user(username: str):
    if username == "admin":
        raise ValueError("Cannot delete admin")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))

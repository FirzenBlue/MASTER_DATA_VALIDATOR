"""
Global audit trail — Postgres-backed.
Every state-changing action gets a row in `audit_entries`.
"""
from __future__ import annotations

import datetime as dt
import json

from services.db import get_conn


def log(
    *,
    user: dict,
    action: str,
    file_id: str | None = None,
    filename: str | None = None,
    module: str | None = None,
    sheet: str | None = None,
    rule_id: str | None = None,
    affected_count: int = 0,
    reason: str = "",
    details: dict | None = None,
):
    """Append one audit entry."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_entries
                    (username, display_name, role, action,
                     file_id, filename, module, sheet, rule_id,
                     affected_count, reason, details)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    user.get("username"),
                    user.get("display_name", user.get("username")),
                    user.get("role"),
                    action,
                    file_id,
                    filename,
                    module,
                    sheet,
                    rule_id,
                    affected_count,
                    reason,
                    json.dumps(details or {}),
                ),
            )


def _row_to_dict(row: dict) -> dict:
    out = dict(row)
    # Convert timestamp to epoch seconds for frontend
    ts = out.get("timestamp")
    if isinstance(ts, dt.datetime):
        out["timestamp"] = ts.timestamp()
    return out


def list_all(limit: int = 1000) -> list:
    """Return newest-first audit entries."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, timestamp, username, display_name, role, action,
                          file_id, filename, module, sheet, rule_id,
                          affected_count, reason, details
                     FROM audit_entries
                    ORDER BY timestamp DESC
                    LIMIT %s""",
                (limit,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def list_for_file(file_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, timestamp, username, display_name, role, action,
                          file_id, filename, module, sheet, rule_id,
                          affected_count, reason, details
                     FROM audit_entries
                    WHERE file_id = %s
                    ORDER BY timestamp DESC""",
                (file_id,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

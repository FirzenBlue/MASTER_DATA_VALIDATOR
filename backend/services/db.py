"""
Postgres database layer for Master Data Validator.

Connection configured via DATABASE_URL env var, default:
  postgresql://postgres:Lumbini@localhost:5432/masterdata

Schema (auto-created on first run):
  users              — accounts + roles
  sessions           — active login tokens
  files              — repository metadata (file blobs stay on disk)
  audit_entries      — global audit trail

File BLOBS remain on disk at backend/storage/{module}/{file_id}_{filename}.
Postgres holds only the path reference. This is correct; don't put
multi-MB XML into Postgres.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


DEFAULT_DSN = "postgresql://postgres:Lumbini@localhost:5432/masterdata"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DSN)


# ── Connection management ───────────────────────────────────────────────────

@contextmanager
def get_conn():
    """Open a connection. Commits on clean exit, rolls back on exception."""
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> tuple[bool, str]:
    """Return (True, version_string) if DB is reachable; (False, error) if not."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
                return True, row["version"]
    except Exception as e:
        return False, str(e)


# ── Schema ──────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'it', 'module')),
    module          TEXT CHECK (module IN ('SD','MM','PP','QM','FICO')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,
    username        TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);

CREATE TABLE IF NOT EXISTS files (
    file_id               TEXT PRIMARY KEY,
    filename              TEXT NOT NULL,
    module                TEXT NOT NULL,
    size_bytes            BIGINT NOT NULL,
    path                  TEXT NOT NULL,
    uploaded_by           TEXT NOT NULL,
    uploaded_by_name      TEXT NOT NULL,
    uploaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status                TEXT NOT NULL DEFAULT 'in_progress'
                            CHECK (status IN ('in_progress','validated','ltmc_uploaded')),
    validated_by          TEXT,
    validated_by_name     TEXT,
    validated_at          TIMESTAMPTZ,
    ltmc_uploaded_by      TEXT,
    ltmc_uploaded_by_name TEXT,
    ltmc_uploaded_at      TIMESTAMPTZ,
    row_count             INTEGER,
    error_count           INTEGER,
    decision_count        INTEGER,
    notes                 TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_module_status ON files(module, status);
CREATE INDEX IF NOT EXISTS idx_files_uploaded_by ON files(uploaded_by);
CREATE INDEX IF NOT EXISTS idx_files_uploaded_at ON files(uploaded_at DESC);

CREATE TABLE IF NOT EXISTS audit_entries (
    id               BIGSERIAL PRIMARY KEY,
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    username         TEXT,
    display_name     TEXT,
    role             TEXT,
    action           TEXT NOT NULL,
    file_id          TEXT,
    filename         TEXT,
    module           TEXT,
    sheet            TEXT,
    rule_id          TEXT,
    affected_count   INTEGER DEFAULT 0,
    reason           TEXT DEFAULT '',
    details          JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_entries(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_file ON audit_entries(file_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_entries(username);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_entries(action);
"""


def init_schema():
    """Create all tables if missing. Safe to call on every startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)


def seed_default_users():
    """Insert the 7 demo users if no users exist yet."""
    import hashlib
    import time

    def _hash(pw):
        return hashlib.sha256(pw.encode("utf-8")).hexdigest()

    default_users = [
        ("admin",    "admin123", "Administrator", "admin",  None),
        ("ituser",   "it123",    "IT User",       "it",     None),
        ("sduser",   "sd123",    "SD SME",        "module", "SD"),
        ("mmuser",   "mm123",    "MM SME",        "module", "MM"),
        ("ppuser",   "pp123",    "PP SME",        "module", "PP"),
        ("qmuser",   "qm123",    "QM SME",        "module", "QM"),
        ("ficouser", "fico123",  "FICO SME",      "module", "FICO"),
    ]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            count = cur.fetchone()["n"]
            if count > 0:
                return  # Already seeded
            for u, pw, name, role, module in default_users:
                cur.execute(
                    """INSERT INTO users (username, password_hash, display_name, role, module)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (u, _hash(pw), name, role, module),
                )


# ── Migration from legacy JSON ──────────────────────────────────────────────

def migrate_from_json_if_present(storage_root: Path):
    """One-time migration: if old JSON files exist, import them into Postgres.
    Safe to call repeatedly — imports only if DB is empty."""
    import json

    users_json = storage_root / "users.json"
    metadata_json = storage_root / "metadata.json"
    audit_json = storage_root / "audit.json"

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Users
            if users_json.exists():
                cur.execute("SELECT COUNT(*) AS n FROM users")
                if cur.fetchone()["n"] == 0:
                    try:
                        data = json.loads(users_json.read_text())
                        for u in data.values():
                            cur.execute(
                                """INSERT INTO users (username, password_hash, display_name, role, module)
                                   VALUES (%s, %s, %s, %s, %s)
                                   ON CONFLICT (username) DO NOTHING""",
                                (u["username"], u["password_hash"], u["display_name"],
                                 u["role"], u.get("module")),
                            )
                    except Exception as e:
                        print(f"[migration] users.json skipped: {e}")

            # Files
            if metadata_json.exists():
                cur.execute("SELECT COUNT(*) AS n FROM files")
                if cur.fetchone()["n"] == 0:
                    try:
                        import datetime as dt
                        def _ts(x):
                            if x is None:
                                return None
                            return dt.datetime.fromtimestamp(x, tz=dt.timezone.utc)
                        data = json.loads(metadata_json.read_text())
                        for f in data.values():
                            cur.execute(
                                """INSERT INTO files
                                    (file_id, filename, module, size_bytes, path,
                                     uploaded_by, uploaded_by_name, uploaded_at,
                                     status, validated_by, validated_by_name, validated_at,
                                     ltmc_uploaded_by, ltmc_uploaded_by_name, ltmc_uploaded_at,
                                     row_count, error_count, decision_count, notes)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (file_id) DO NOTHING""",
                                (f["file_id"], f["filename"], f["module"], f["size_bytes"], f["path"],
                                 f["uploaded_by"], f["uploaded_by_name"], _ts(f.get("uploaded_at")),
                                 f["status"], f.get("validated_by"), f.get("validated_by_name"),
                                 _ts(f.get("validated_at")),
                                 f.get("ltmc_uploaded_by"), f.get("ltmc_uploaded_by_name"),
                                 _ts(f.get("ltmc_uploaded_at")),
                                 f.get("row_count"), f.get("error_count"), f.get("decision_count"),
                                 f.get("notes", "")),
                            )
                    except Exception as e:
                        print(f"[migration] metadata.json skipped: {e}")

            # Audit
            if audit_json.exists():
                cur.execute("SELECT COUNT(*) AS n FROM audit_entries")
                if cur.fetchone()["n"] == 0:
                    try:
                        import datetime as dt
                        data = json.loads(audit_json.read_text())
                        for e in data:
                            cur.execute(
                                """INSERT INTO audit_entries
                                    (timestamp, username, display_name, role, action,
                                     file_id, filename, module, sheet, rule_id,
                                     affected_count, reason, details)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (dt.datetime.fromtimestamp(e.get("timestamp", 0), tz=dt.timezone.utc),
                                 e.get("username"), e.get("display_name"), e.get("role"),
                                 e["action"], e.get("file_id"), e.get("filename"),
                                 e.get("module"), e.get("sheet"), e.get("rule_id"),
                                 e.get("affected_count", 0), e.get("reason", ""),
                                 json.dumps(e.get("details", {}))),
                            )
                    except Exception as er:
                        print(f"[migration] audit.json skipped: {er}")

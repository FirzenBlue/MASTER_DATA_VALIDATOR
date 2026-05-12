"""
Repository: file metadata in Postgres, blobs on disk.

Files:     {STORAGE_ROOT}/{MODULE}/{file_id}_{filename}
Metadata:  Postgres `files` table

STORAGE_ROOT is configurable via the MDV_STORAGE_PATH env var, so the
same storage folder persists across code upgrades. When moving to a new
version, just point MDV_STORAGE_PATH at the existing folder — no copying.

Default location: ~/MDV_storage (your home directory). Falls back to
backend/storage inside the code folder if the home path can't be created.

Status lifecycle:
  in_progress       → file uploaded, being worked on
  validated         → user clicked "Mark as Validated"
  ltmc_uploaded     → IT/admin clicked "Mark as LTMC Uploaded"
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path

from services.db import get_conn


def _resolve_storage_root() -> Path:
    """Pick a storage path that survives version upgrades.

    Priority:
      1. MDV_STORAGE_PATH env var (production + power users)
      2. ~/MDV_storage (default — survives code upgrades)
      3. backend/storage (fallback if home not writable)
    """
    env_path = os.environ.get("MDV_STORAGE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    try:
        home = Path.home() / "MDV_storage"
        home.mkdir(parents=True, exist_ok=True)
        return home
    except (OSError, RuntimeError):
        return (Path(__file__).parent.parent / "storage").resolve()


STORAGE_ROOT = _resolve_storage_root()
STATUSES = ["in_progress", "validated", "ltmc_uploaded"]


def _ensure_storage():
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    for mod in ("SD", "MM", "PP", "QM", "FICO"):
        (STORAGE_ROOT / mod).mkdir(exist_ok=True)


def _new_file_id() -> str:
    return secrets.token_urlsafe(8)


def _row_to_dict(row: dict) -> dict:
    """Normalize a files row (convert timestamps to epoch floats for JSON)."""
    if row is None:
        return None
    out = dict(row)
    for k in ("uploaded_at", "validated_at", "ltmc_uploaded_at"):
        v = out.get(k)
        if isinstance(v, dt.datetime):
            out[k] = v.timestamp()
        elif v is None:
            out[k] = None
    return out


# ────────────────────────────────────────────────────────────────────────────

def save_file(module: str, filename: str, content: bytes, user: dict) -> dict:
    """Save a file to disk and register metadata in Postgres.
    Path in DB is relative to STORAGE_ROOT so the storage folder can be moved."""
    _ensure_storage()
    file_id = _new_file_id()
    safe_name = filename.replace("/", "_").replace("\\", "_")
    rel_path = f"{module}/{file_id}_{safe_name}"
    file_path = STORAGE_ROOT / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(content)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO files
                    (file_id, filename, module, size_bytes, path,
                     uploaded_by, uploaded_by_name, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'in_progress')
                   RETURNING *""",
                (file_id, safe_name, module, len(content), rel_path,
                 user["username"], user.get("display_name", user["username"])),
            )
            return _row_to_dict(cur.fetchone())


def save_mm_bundle(filename_main: str, content_main: bytes,
                   filename_alt: str, content_alt: bytes,
                   filename_lt: str, content_lt: bytes,
                   user: dict) -> dict:
    """Save the 3 MM input files under one file_id (as a bundle folder).

    On-disk layout:
        {STORAGE_ROOT}/MM/{file_id}/
            main.xlsx    (original filename_main)
            alt_uom.xlsx (original filename_alt)
            longtext.xlsx (original filename_lt)

    The DB row uses the MAIN filename as its display name — that's what
    users will see in the repository list. Size = total bytes across all 3.

    KDS catalogs are NOT stored per-bundle — they live globally at
    `backend/kds/MM_KDS.xlsx` and are loaded once at process start.
    Per-customer KDS overrides were briefly available (v51) and removed
    (v54) — single source of truth on the server.

    Follows the same `files` table schema as save_file() so the repo list,
    status transitions (Mark Validated / Mark LTMC Uploaded), and audit
    logging all work without changes.
    """
    _ensure_storage()
    file_id = _new_file_id()
    rel_folder = f"MM/{file_id}"
    folder = STORAGE_ROOT / rel_folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.xlsx").write_bytes(content_main)
    (folder / "alt_uom.xlsx").write_bytes(content_alt)
    (folder / "longtext.xlsx").write_bytes(content_lt)

    # Also write a small manifest for debug + recovery. Tools like ops can
    # read this without hitting the DB.
    manifest = (
        f"module=MM\n"
        f"file_id={file_id}\n"
        f"main={filename_main}\n"
        f"alt_uom={filename_alt}\n"
        f"longtext={filename_lt}\n"
    )
    (folder / "manifest.txt").write_text(manifest)

    total_bytes = len(content_main) + len(content_alt) + len(content_lt)
    # filename stored in DB is the main file's — that's what shows in list views
    safe_main = filename_main.replace("/", "_").replace("\\", "_")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO files
                    (file_id, filename, module, size_bytes, path,
                     uploaded_by, uploaded_by_name, status)
                   VALUES (%s, %s, 'MM', %s, %s, %s, %s, 'in_progress')
                   RETURNING *""",
                (file_id, safe_main, total_bytes, rel_folder,
                 user["username"], user.get("display_name", user["username"])),
            )
            return _row_to_dict(cur.fetchone())


def save_pp_bundle(filename_bom: str, content_bom: bytes,
                   filename_routing: str, content_routing: bytes,
                   user: dict) -> dict:
    """Save the BOM (required) + Routing (optional) files for a PP upload.

    On-disk layout (mirrors MM's, with routing in the alt_uom slot for
    code reuse — get_mm_bundle_paths works for both modules):
        {STORAGE_ROOT}/PP/{file_id}/
            main.xlsx     (BOM file, original filename_bom)
            alt_uom.xlsx  (Routing file or 0 bytes if absent)
            longtext.xlsx (always 0 bytes for PP)

    The reason we keep the slot names "main"/"alt_uom"/"longtext"
    instead of renaming to "bom"/"routing": get_mm_bundle_paths already
    works on those slot names and the open-file flow in main.py reads
    them via that helper. Adding a parallel get_pp_bundle_paths would
    duplicate code for one column-name difference. The "alt_uom" name
    is purely the disk filename — users never see it.
    """
    _ensure_storage()
    file_id = _new_file_id()
    rel_folder = f"PP/{file_id}"
    folder = STORAGE_ROOT / rel_folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.xlsx").write_bytes(content_bom)
    (folder / "alt_uom.xlsx").write_bytes(content_routing)
    (folder / "longtext.xlsx").write_bytes(b"")

    manifest = (
        f"module=PP\n"
        f"file_id={file_id}\n"
        f"bom={filename_bom}\n"
        f"routing={filename_routing or '(absent)'}\n"
    )
    (folder / "manifest.txt").write_text(manifest)

    total_bytes = len(content_bom) + len(content_routing)
    safe_main = filename_bom.replace("/", "_").replace("\\", "_")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO files
                    (file_id, filename, module, size_bytes, path,
                     uploaded_by, uploaded_by_name, status)
                   VALUES (%s, %s, 'PP', %s, %s, %s, %s, 'in_progress')
                   RETURNING *""",
                (file_id, safe_main, total_bytes, rel_folder,
                 user["username"], user.get("display_name", user["username"])),
            )
            return _row_to_dict(cur.fetchone())


def get_mm_bundle_paths(file_id: str) -> dict | None:
    """Return absolute paths for each slot in an MM or PP bundle.

    Returns {"main": Path, "alt_uom": Path, "longtext": Path, "folder": Path}
    or None if the bundle doesn't exist.

    Both MM and PP use the same on-disk slot layout (main / alt_uom /
    longtext), so a single helper serves both modules. For PP:
      - main slot     = BOM file
      - alt_uom slot  = Routing file (or 0 bytes if absent)
      - longtext slot = always 0 bytes

    KDS is NOT a per-bundle slot — it lives globally at
    `backend/kds/MM_KDS.xlsx`. v51 had a per-bundle override path; v54
    removed it.
    """
    entry = get_file(file_id)
    if not entry or entry["module"] not in ("MM", "PP"):
        return None
    folder = STORAGE_ROOT / entry["path"]
    if not folder.is_dir():
        return None
    return {
        "folder": folder,
        "main": folder / "main.xlsx",
        "alt_uom": folder / "alt_uom.xlsx",
        "longtext": folder / "longtext.xlsx",
    }


def get_file(file_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM files WHERE file_id = %s", (file_id,))
            return _row_to_dict(cur.fetchone())


def get_file_path(file_id: str) -> Path | None:
    """Return the absolute path to a file's bytes on disk."""
    entry = get_file(file_id)
    if not entry:
        return None
    return STORAGE_ROOT / entry["path"]


def get_file_bytes(file_id: str) -> bytes | None:
    """Read a file's bytes. Returns None if missing."""
    path = get_file_path(file_id)
    if path is None or not path.exists():
        return None
    return path.read_bytes()


def list_files(user: dict, module: str | None = None,
               status: str | None = None) -> list[dict]:
    """List files visible to this user, filtered as requested."""
    conditions = []
    params: list = []

    if user["role"] == "module":
        conditions.append("module = %s")
        params.append(user.get("module"))
    if module:
        conditions.append("module = %s")
        params.append(module)
    if status:
        conditions.append("status = %s")
        params.append(status)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM files{where} ORDER BY uploaded_at DESC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_dict(r) for r in cur.fetchall()]


def update_stats(file_id: str, row_count: int, error_count: int, decision_count: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE files SET row_count = %s, error_count = %s, decision_count = %s
                    WHERE file_id = %s""",
                (row_count, error_count, decision_count, file_id),
            )


def save_working_copy(file_id: str, content: bytes):
    """Overwrite the stored file with the latest working state."""
    entry = get_file(file_id)
    if not entry:
        return
    (STORAGE_ROOT / entry["path"]).write_bytes(content)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE files SET size_bytes = %s WHERE file_id = %s",
                (len(content), file_id),
            )


def mark_validated(file_id: str, user: dict) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE files
                      SET status = 'validated',
                          validated_by = %s,
                          validated_by_name = %s,
                          validated_at = NOW()
                    WHERE file_id = %s
                RETURNING *""",
                (user["username"], user.get("display_name", user["username"]), file_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("File not found")
            return _row_to_dict(row)


def revoke_validation(file_id: str, user: dict) -> dict:
    """Revoke validation — only original validator or admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM files WHERE file_id = %s", (file_id,))
            entry = cur.fetchone()
            if not entry:
                raise ValueError("File not found")
            if entry["status"] != "validated":
                raise ValueError("File is not in validated state")
            if user["role"] != "admin" and entry.get("validated_by") != user["username"]:
                raise PermissionError("Only the validator or admin can revoke")

            cur.execute(
                """UPDATE files
                      SET status = 'in_progress',
                          validated_by = NULL,
                          validated_by_name = NULL,
                          validated_at = NULL
                    WHERE file_id = %s
                RETURNING *""",
                (file_id,),
            )
            return _row_to_dict(cur.fetchone())


def mark_ltmc_uploaded(file_id: str, user: dict) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM files WHERE file_id = %s", (file_id,))
            entry = cur.fetchone()
            if not entry:
                raise ValueError("File not found")
            if entry["status"] != "validated":
                raise ValueError("File must be validated first")

            cur.execute(
                """UPDATE files
                      SET status = 'ltmc_uploaded',
                          ltmc_uploaded_by = %s,
                          ltmc_uploaded_by_name = %s,
                          ltmc_uploaded_at = NOW()
                    WHERE file_id = %s
                RETURNING *""",
                (user["username"], user.get("display_name", user["username"]), file_id),
            )
            return _row_to_dict(cur.fetchone())


def delete_file(file_id: str, user: dict):
    """Delete a file. Permissions: uploader, IT, or admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM files WHERE file_id = %s", (file_id,))
            entry = cur.fetchone()
            if not entry:
                return

            is_uploader = entry["uploaded_by"] == user["username"]
            is_admin = user["role"] == "admin"
            is_it = user["role"] == "it"
            if not (is_uploader or is_admin or is_it):
                raise PermissionError(
                    "Only the uploader, IT, or admin can delete this file"
                )

            fp = STORAGE_ROOT / entry["path"]
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass
            cur.execute("DELETE FROM files WHERE file_id = %s", (file_id,))

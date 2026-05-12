"""
PP routes — upload, format-check, and manifest+chunk export for BOM
and Routing files.

Endpoints
---------
POST /api/pp/format-check                       → BOM-vs-Routing detection
POST /api/pp/upload                             → BOM file (required) + optional Routing → file_id
POST /api/pp/session/export_ltmc                → manifest of BOM chunks
GET  /api/pp/session/export_ltmc/chunk/{n}      → stream one BOM chunk
POST /api/pp/session/export_ltmc_routing        → manifest of Routing chunks
GET  /api/pp/session/export_ltmc_routing/chunk/{n} → stream one Routing chunk

Bundle layout on disk
---------------------
PP re-uses the MM bundle directory pattern (3 slots: main / alt_uom /
longtext). For PP we use:
  - main slot     → BOM file
  - alt_uom slot  → Routing file (optional)
  - longtext slot → unused
This avoids needing a parallel save_pp_bundle in repository.py while
keeping the on-disk layout consistent.

The session-open endpoint in main.py knows to interpret the slots this
way for entries with module="PP".
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from services import auth as auth_svc
from services import audit_log, repository as repo
from services.pp_file_detector import detect as pp_detect
from services.pp_loader import load_bom
from services.routing_loader import load_routing
from services.pp_kds import load_pp_catalogs
from services.pp_rulebook import get_rulebook
from services.pp_validator import validate_bom, validate_routing
from services.pp_merger import merge_bom
from services.routing_merger import merge_routing
from services.pp_splitter import (
    split_into_chunks as split_bom_chunks,
    BomChunk,
    SAFE_MAX_BYTES as BOM_SAFE_MAX,
)
from services.routing_splitter import (
    split_into_chunks as split_routing_chunks,
    RoutingChunk,
    SAFE_MAX_BYTES as RT_SAFE_MAX,
)
from services.pp_generator import (
    generate_chunk_xml as bom_gen_one,
    generate_all_chunks_xml as bom_gen_all,
    generate_chunk_xml_routing as rt_gen_one,
    generate_all_chunks_xml_routing as rt_gen_all,
)


router = APIRouter(prefix="/api/pp", tags=["pp"])


def _current_user(session: str | None = Cookie(None)) -> dict:
    user = auth_svc.get_user(session)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


# Cached catalogs — loaded once per process. The catalogs are immutable
# once loaded so caching is safe; in particular pp_kds.load_pp_catalogs
# already caches internally.
_PP_CATALOGS_CACHE = None
def _get_pp_catalogs():
    global _PP_CATALOGS_CACHE
    if _PP_CATALOGS_CACHE is None:
        _PP_CATALOGS_CACHE = load_pp_catalogs(None)
    return _PP_CATALOGS_CACHE


# ─── Format check ──────────────────────────────────────────────────────

@router.post("/format-check")
async def format_check(
    file: UploadFile = File(...),
    user: dict = Depends(_current_user),
):
    """Peek at an uploaded xlsx and detect whether it's BOM or Routing.

    Frontend uses this on file-drop to validate slot placement BEFORE
    triggering the real upload. For files >100 MB the frontend skips
    this check and lets the real upload's parser surface format errors.
    """
    if not auth_svc.can_access_module(user, "PP"):
        raise HTTPException(403, "You don't have access to module PP")

    contents = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        tf.write(contents)
        tmp = tf.name
    try:
        result = pp_detect(tmp, filename=file.filename)
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass

    return {
        "ok": result.role in ("bom", "routing"),
        "role": result.role,
        "reason": result.reason,
        "confidence": result.confidence,
        "matched_sheets": result.matched_sheets,
    }


# ─── Upload ───────────────────────────────────────────────────────────

@router.post("/upload")
async def pp_upload(
    bom_file: UploadFile = File(...),
    routing_file: UploadFile | None = File(None),
    user: dict = Depends(_current_user),
):
    """Accept a BOM file (required) and optionally a Routing file.
    Saves them as a PP bundle and returns file_id + parse stats.

    Same main-required-others-optional pattern as MM (v46): SMEs
    commonly start with just the BOM and add Routing when ready.
    Pre-upload format-check confirms file types match their slots; we
    re-check server-side as defence-in-depth.
    """
    if not auth_svc.can_access_module(user, "PP"):
        raise HTTPException(403, "You don't have access to module PP")

    bom_bytes = await bom_file.read()
    routing_bytes = await routing_file.read() if routing_file is not None else b""

    # Extension check — reject CSV, XLS, etc. with helpful hints.
    slots = [("BOM", bom_file)]
    if routing_file is not None:
        slots.append(("Routing", routing_file))
    for slot_name, f in slots:
        ext = f.filename.lower().rsplit(".", 1)[-1] if "." in f.filename else ""
        if ext != "xlsx":
            raise HTTPException(
                400,
                f"{slot_name} slot: '{f.filename}' is not a .xlsx file. "
                f"Only .xlsx is supported. "
                + ("Old .xls files: open in Excel and 'Save As → Excel Workbook (.xlsx)'."
                   if ext == "xls"
                   else f"Got extension '.{ext}'."),
            )

    # Format check each provided slot
    def _check_role(content: bytes, fname: str, expected: str):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
            tf.write(content)
            p = tf.name
        try:
            r = pp_detect(p, filename=fname)
        finally:
            try:
                Path(p).unlink()
            except OSError:
                pass
        if r.role != expected:
            raise HTTPException(
                400,
                f"File '{fname}' detected as '{r.role}' but expected "
                f"'{expected}'. {r.reason}",
            )

    _check_role(bom_bytes, bom_file.filename, "bom")
    if routing_file is not None:
        _check_role(routing_bytes, routing_file.filename, "routing")

    # Save bundle. PP has its own save function (mirrors save_mm_bundle
    # but folder is PP/{file_id}/ and module='PP' is set in the INSERT).
    entry = repo.save_pp_bundle(
        bom_file.filename, bom_bytes,
        (routing_file.filename if routing_file is not None else ""),
        routing_bytes,
        user,
    )

    # Parse + validate for stats. Don't fail upload on parse errors —
    # surface them via parse_error so the UI can offer delete + retry.
    try:
        paths = repo.get_mm_bundle_paths(entry["file_id"])
        bom = load_bom(paths["main"], bom_file.filename)
        merged = merge_bom(bom)
        catalogs = _get_pp_catalogs()
        rulebook = get_rulebook()
        errors = validate_bom(bom, catalogs, rulebook)

        if routing_file is not None:
            routing = load_routing(paths["alt_uom"], routing_file.filename)
            errors.extend(validate_routing(routing, catalogs, rulebook))

        repo.update_stats(
            entry["file_id"],
            row_count=merged.summary["material_count"],
            error_count=len(errors),
            decision_count=0,  # PP decision grouping not yet implemented
        )
        entry = repo.get_file(entry["file_id"])
    except Exception as e:
        entry["parse_error"] = str(e)

    audit_log.log(
        user=user, action="file_upload", file_id=entry["file_id"],
        filename=entry["filename"], module="PP",
        affected_count=entry.get("row_count") or 0,
        details={
            "size_bytes": entry["size_bytes"],
            "bom_filename": bom_file.filename,
            "routing_filename": (routing_file.filename if routing_file else None),
        },
    )

    return entry


# ─── Export cache management ──────────────────────────────────────────

def _release_pp_export_cache(state: dict) -> None:
    """Clean up any prior PP export's temp dir."""
    cache = state.get("pp_export_cache")
    if not cache:
        return
    try:
        shutil.rmtree(cache["dir"], ignore_errors=True)
    except Exception:
        pass
    state["pp_export_cache"] = None


# ─── Export — BOM ─────────────────────────────────────────────────────

@router.post("/session/export_ltmc")
async def pp_export_bom(
    session: str | None = Cookie(None),
    user: dict = Depends(_current_user),
):
    """Build the BOM LTMC export and return a manifest of chunks.

    Two-phase flow:
      1. POST → manifest with chunk metadata
      2. GET  /chunk/{n} → stream one xml file
    """
    from main import _ensure_session_loaded, _session_loaded

    if not auth_svc.can_access_module(user, "PP"):
        raise HTTPException(403, "You don't have access to module PP")

    state = _ensure_session_loaded(session, user)
    if not _session_loaded(state) or not state.get("pp_bundle"):
        raise HTTPException(400, "No PP file open in this session")

    bundle = state["pp_bundle"]
    merged = bundle.get("merged_bom")
    if not merged:
        raise HTTPException(400, "No BOM data in this PP session")

    rulebook = get_rulebook()
    base = Path(state["filename"]).stem or "BOM"

    # "Try as a single chunk first" — see v55.6 changelog. The estimator
    # is calibrated but we still validate by attempting a full
    # single-file generation; if it fits under 95 MB we return one file,
    # otherwise we fall back to the splitter's bin-packing.
    single = BomChunk(
        chunk_index=0,
        materials=list(merged.materials),
        estimated_bytes=0,
    )
    single_bytes = bom_gen_one(single, rulebook)

    _release_pp_export_cache(state)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pp_export_"))
    chunks_meta: list[dict] = []

    if len(single_bytes) <= BOM_SAFE_MAX:
        filename = f"{base}_LTMC.xml"
        (tmp_dir / filename).write_bytes(single_bytes)
        chunks_meta.append({
            "index": 0,
            "filename": filename,
            "size_bytes": len(single_bytes),
        })
    else:
        chunks = split_bom_chunks(merged, safe_max_bytes=BOM_SAFE_MAX)
        files = bom_gen_all(chunks, rulebook, base_filename=base)
        for idx, (fname, data) in enumerate(files):
            (tmp_dir / fname).write_bytes(data)
            chunks_meta.append({
                "index": idx,
                "filename": fname,
                "size_bytes": len(data),
            })

    state["pp_export_cache"] = {
        "dir": tmp_dir,
        "format": "xml",
        "chunks": chunks_meta,
        "kind": "bom",
        "created_at": time.time(),
    }

    audit_log.log(
        user=user, action="export_ltmc", file_id=state["file_id"],
        module="PP", affected_count=merged.summary["material_count"],
        details={"chunk_count": len(chunks_meta), "kind": "bom"},
    )

    return {
        "chunk_count": len(chunks_meta),
        "format": "xml",
        "single_file": len(chunks_meta) == 1,
        "material_count": merged.summary["material_count"],
        "chunks": chunks_meta,
    }


@router.get("/session/export_ltmc/chunk/{chunk_index}")
def pp_export_bom_chunk(
    chunk_index: int,
    session: str | None = Cookie(None),
    user: dict = Depends(_current_user),
):
    return _stream_chunk(chunk_index, session, user, expected_kind="bom")


# ─── Export — Routing ─────────────────────────────────────────────────

@router.post("/session/export_ltmc_routing")
async def pp_export_routing(
    session: str | None = Cookie(None),
    user: dict = Depends(_current_user),
):
    """Build the Routing LTMC export and return a manifest of chunks.
    Mirrors pp_export_bom but for the Routing slot of a PP bundle."""
    from main import _ensure_session_loaded, _session_loaded

    if not auth_svc.can_access_module(user, "PP"):
        raise HTTPException(403, "You don't have access to module PP")

    state = _ensure_session_loaded(session, user)
    if not _session_loaded(state) or not state.get("pp_bundle"):
        raise HTTPException(400, "No PP file open in this session")

    bundle = state["pp_bundle"]
    merged = bundle.get("merged_routing")
    if not merged or not merged.groups:
        raise HTTPException(400, "No Routing data in this PP bundle.")

    rulebook = get_rulebook()
    base = (Path(state["filename"]).stem or "Routing") + "_routing"

    single = RoutingChunk(
        chunk_index=0,
        groups=list(merged.groups),
        estimated_bytes=0,
    )
    single_bytes = rt_gen_one(single, rulebook)

    _release_pp_export_cache(state)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pp_export_routing_"))
    chunks_meta: list[dict] = []

    if len(single_bytes) <= RT_SAFE_MAX:
        filename = f"{base}_LTMC.xml"
        (tmp_dir / filename).write_bytes(single_bytes)
        chunks_meta.append({
            "index": 0,
            "filename": filename,
            "size_bytes": len(single_bytes),
        })
    else:
        chunks = split_routing_chunks(merged, safe_max_bytes=RT_SAFE_MAX)
        files = rt_gen_all(chunks, rulebook, base_filename=base)
        for idx, (fname, data) in enumerate(files):
            (tmp_dir / fname).write_bytes(data)
            chunks_meta.append({
                "index": idx,
                "filename": fname,
                "size_bytes": len(data),
            })

    state["pp_export_cache"] = {
        "dir": tmp_dir,
        "format": "xml",
        "chunks": chunks_meta,
        "kind": "routing",
        "created_at": time.time(),
    }

    audit_log.log(
        user=user, action="export_ltmc", file_id=state["file_id"],
        module="PP", affected_count=merged.summary["routing_count"],
        details={"chunk_count": len(chunks_meta), "kind": "routing"},
    )

    return {
        "chunk_count": len(chunks_meta),
        "format": "xml",
        "single_file": len(chunks_meta) == 1,
        "routing_count": merged.summary["routing_count"],
        "chunks": chunks_meta,
    }


@router.get("/session/export_ltmc_routing/chunk/{chunk_index}")
def pp_export_routing_chunk(
    chunk_index: int,
    session: str | None = Cookie(None),
    user: dict = Depends(_current_user),
):
    return _stream_chunk(chunk_index, session, user, expected_kind="routing")


# ─── Shared chunk-stream helper ───────────────────────────────────────

def _stream_chunk(
    chunk_index: int,
    session: str | None,
    user: dict,
    expected_kind: str,
):
    """Stream one cached export chunk back to the client.

    expected_kind: "bom" or "routing" — must match the cache's kind so
    that BOM-chunk URLs don't accidentally serve Routing chunks (or
    vice versa) if the user kicked off both exports in sequence.
    """
    from main import _ensure_session_loaded, _session_loaded

    if not auth_svc.can_access_module(user, "PP"):
        raise HTTPException(403, "You don't have access to module PP")

    state = _ensure_session_loaded(session, user)
    if not _session_loaded(state):
        raise HTTPException(400, "No PP file open in this session")

    cache = state.get("pp_export_cache")
    if not cache:
        raise HTTPException(410, "Export cache missing — re-run the export.")

    if cache.get("kind") != expected_kind:
        raise HTTPException(
            410,
            f"Export cache holds {cache.get('kind')} chunks, but you "
            f"requested {expected_kind}. Re-run the {expected_kind} export.",
        )

    if chunk_index < 0 or chunk_index >= len(cache["chunks"]):
        raise HTTPException(404, f"Chunk index {chunk_index} out of range")

    meta = cache["chunks"][chunk_index]
    file_path = Path(cache["dir"]) / meta["filename"]
    if not file_path.exists():
        raise HTTPException(410, "Cached chunk missing on disk — re-run the export.")

    def _stream():
        with open(file_path, "rb") as f:
            while True:
                blk = f.read(1024 * 1024)
                if not blk:
                    break
                yield blk

    return StreamingResponse(
        _stream(),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{meta["filename"]}"',
            "Content-Length": str(meta["size_bytes"]),
            "X-Chunk-Index": str(chunk_index),
            "X-Chunk-Count": str(len(cache["chunks"])),
        },
    )

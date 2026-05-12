"""
MM (Material Master) HTTP routes.

This module adds FastAPI endpoints for the MM workflow:
  - POST /api/mm/upload        — 3 files in, bundle saved + validated
  - GET  /api/mm/template/{kind} — download a blank template xlsx
  - POST /api/mm/session/open/{file_id} — open an MM bundle session
  - GET  /api/mm/format-check   — sanity-check a single uploaded file
                                  before the user hits submit (used by
                                  the upload form to show ✓/✗ inline)

MM sessions reuse the Customer session model (same state dict keyed by
session cookie, same errors list format). This means the existing
Decisions + Group & Replace + Fix Individually UI works against MM data
without modification — we just populate state["errors"] with MM-style
errors and state["mm_materials"] with the merged material list.

Templates are served from backend/mm_templates/, which holds blank
copies of the 3 required input files. Users download them from the
upload page.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Response, UploadFile

from services import auth as auth_svc
from services import audit_log, repository as repo
from services.mm_file_detector import detect as mm_detect, DetectionResult, FileRole
from services.mm_loader import LOADERS_BY_ROLE
from services.mm_merger import merge as mm_merge
from services.mm_validator import validate_mm
from services.mm_kds import load_mm_catalogs
from services.decision_engine import group_errors


router = APIRouter(prefix="/api/mm", tags=["mm"])


# Local auth dependency — same behaviour as main.current_user but defined
# here to avoid importing main (which would create a circular dependency,
# main imports this router). Small duplication that keeps the module
# boundary clean.
def _current_user(session: str | None = Cookie(None)) -> dict:
    user = auth_svc.get_user(session)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


# Paths — mm_routes.py lives at backend/mm_routes.py; .parent is backend/
_TEMPLATES_DIR = Path(__file__).resolve().parent / "mm_templates"
_KDS_PATH = Path(__file__).resolve().parent / "kds" / "MM_KDS.xlsx"

# In-memory cache for MM catalogs (loaded once per process). Same pattern
# as kds_reference.py. A service restart picks up a new KDS file.
_MM_CATALOGS_CACHE: dict | None = None


def _get_mm_catalogs() -> dict:
    """Lazy-load MM KDS catalogs. Falls back to empty dict if file missing."""
    global _MM_CATALOGS_CACHE
    if _MM_CATALOGS_CACHE is not None:
        return _MM_CATALOGS_CACHE
    if not _KDS_PATH.exists():
        print(f"[mm_routes] no KDS xlsx at {_KDS_PATH} — MM catalogs will be empty",
              flush=True)
        _MM_CATALOGS_CACHE = {}
        return _MM_CATALOGS_CACHE
    try:
        _MM_CATALOGS_CACHE = load_mm_catalogs(_KDS_PATH)
    except Exception as e:
        print(f"[mm_routes] WARN: failed to load MM KDS: {e}", flush=True)
        _MM_CATALOGS_CACHE = {}
    return _MM_CATALOGS_CACHE


# ─── TEMPLATES ────────────────────────────────────────────────────────────

_TEMPLATE_FILES = {
    "main":     ("main_template.xlsx",     "MM_Main_Template.xlsx"),
    "alt_uom":  ("alt_uom_template.xlsx",  "MM_Alt_UoM_Template.xlsx"),
    "longtext": ("longtext_template.xlsx", "MM_Long_Text_Template.xlsx"),
}


@router.get("/template/{kind}")
def download_template(kind: str, user: dict = Depends(_current_user)):
    """Serve a blank MM template for the data team to use as reference.

    kind ∈ {"main", "alt_uom", "longtext"}.
    """
    if kind not in _TEMPLATE_FILES:
        raise HTTPException(404, f"Unknown template kind '{kind}'. "
                                 f"Valid: {list(_TEMPLATE_FILES)}")
    stored_name, download_name = _TEMPLATE_FILES[kind]
    path = _TEMPLATES_DIR / stored_name
    if not path.exists():
        raise HTTPException(503, f"Template file not installed on server "
                                 f"(admin needs to place {stored_name} in "
                                 f"backend/mm_templates/)")
    content = path.read_bytes()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


# ─── PRE-UPLOAD FORMAT CHECK ──────────────────────────────────────────────

@router.post("/format-check")
async def format_check(
    slot: str = Form(...),       # "main" | "alt_uom" | "longtext"
    file: UploadFile = File(...),
    user: dict = Depends(_current_user),
):
    """Check a single file BEFORE the user submits all 3.

    Used by the upload page to show green-tick / red-cross + a sample-data
    row count as soon as the user picks a file, without actually starting
    validation. Keeps the submit button disabled until all 3 slots show
    green.

    Returns the DetectionResult fields plus a `matches_slot` bool so the
    frontend can tell the user "you put the Alt UoM file in the Main slot."
    """
    if slot not in ("main", "alt_uom", "longtext"):
        raise HTTPException(400, f"Invalid slot '{slot}'")

    # ── v62: LTMC source data form (XML) short-circuit ────────────────
    # When the user picks a .xml file for the main slot, run the LTMC
    # form detector + parser instead of the xlsx-based mm_detect (which
    # would crash on a non-xlsx). Returns a result shape compatible
    # with the xlsx detector so the UI's ✓/✗ rendering works unchanged.
    contents = await file.read()
    if slot == "main":
        from services.mm_ltmc_form_loader import (
            looks_like_ltmc_form, parse_ltmc_form,
        )
        if looks_like_ltmc_form(file.filename, contents):
            try:
                sheets = parse_ltmc_form(contents)
                basic = sheets.get("Basic Data")
                data_rows = len(basic.rows) if basic else 0
                column_count = len(basic.sap_fields) if basic else 0
                if not basic or data_rows == 0:
                    return {
                        "filename": file.filename,
                        "detected_role": "unknown",
                        "matches_slot": False,
                        "column_count": column_count,
                        "data_rows": 0,
                        "reason": "LTMC source data form detected, but Basic Data sheet has no data rows.",
                        "hint": "This appears to be the empty SAP template. "
                                "Populate it with material data in Excel first, then re-upload.",
                    }
                # Count populated supporting sheets for the info readout.
                supporting = []
                for name in ("Plant Data", "Distribution Chains", "Tax Classification",
                             "Valuation Data", "Alternative Units of Measure",
                             "Additional Descriptions"):
                    s = sheets.get(name)
                    if s and s.rows:
                        supporting.append(f"{name} ({len(s.rows)})")
                reason = (f"LTMC source data form · {data_rows} material(s) in Basic Data"
                          + (f" · also: {', '.join(supporting)}" if supporting else ""))
                return {
                    "filename": file.filename,
                    "detected_role": "main",
                    "matches_slot": True,
                    "column_count": column_count,
                    "data_rows": data_rows,
                    "reason": reason,
                    "hint": "",
                }
            except Exception as ex:
                return {
                    "filename": file.filename,
                    "detected_role": "unknown",
                    "matches_slot": False,
                    "column_count": 0,
                    "data_rows": 0,
                    "reason": f"Failed to parse LTMC source data form: {ex}",
                    "hint": "Check that the file is a valid SAP LTMC source data form XML.",
                }
        # Falls through to xlsx detector if the .xml didn't match the
        # LTMC form signature.

    # Write to a temp path so mm_detect can openpyxl it
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        tf.write(contents)
        tmp_path = tf.name
    try:
        result = mm_detect(tmp_path, filename=file.filename)
    finally:
        # best-effort cleanup
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    return {
        "filename": result.filename,
        "detected_role": result.role,
        "matches_slot": result.role == slot,
        "column_count": result.column_count,
        "data_rows": result.data_rows,
        "reason": result.reason,
        # hint explains how to fix the problem (empty string if file is fine).
        # For wrong-slot cases (file OK but in the wrong place) we override
        # the generic hint with a slot-specific message so the user knows
        # exactly where to move it.
        "hint": _slot_specific_hint(slot, result),
    }


def _slot_specific_hint(slot: str, result) -> str:
    """Build a targeted hint for the user based on what happened.

    Cases:
      - File matches slot → empty hint (nothing to fix)
      - File's role is "unknown" → use the detector's own hint (root cause
        is file content, not slot choice)
      - File is valid but in wrong slot → specific "move to X slot" message
    """
    if result.role == slot:
        return ""   # all good
    if result.role == "unknown":
        return result.hint  # detector's diagnostic is better than any generic wrong-slot message
    # Valid MM file but in the wrong slot
    slot_names = {
        "main": "Main Material Data",
        "alt_uom": "Alternate Units of Measure",
        "longtext": "Long Text",
    }
    detected_name = slot_names.get(result.role, result.role)
    target_name = slot_names.get(slot, slot)
    return (
        f"This file looks like a {detected_name} file, but you put it in "
        f"the {target_name} slot. Click 'Remove' on this slot and drop "
        f"the file in the {detected_name} slot instead."
    )


# ─── MM UPLOAD (the 3-file bundle) ─────────────────────────────────────────

@router.post("/upload")
async def mm_upload(
    main_file: UploadFile = File(...),
    alt_uom_file: UploadFile | None = File(None),
    longtext_file: UploadFile | None = File(None),
    user: dict = Depends(_current_user),
):
    """Accept an MM upload. Only the MAIN file is required — Alt UoM and
    Long Text are optional.

    SMEs commonly have just the Main material file when starting a new
    migration; the alt-UoM and long-text companions arrive later in the
    process. Forcing all three on every upload was blocking that workflow
    (the UI button stayed disabled until all three slots were filled).
    The merger and validator already handle missing alt/longtext via
    None — see services/mm_merger.merge() — so this is just a matter of
    relaxing the upload contract.

    KDS catalogs are NOT uploaded by the customer — they're bundled
    server-side at `backend/kds/MM_KDS.xlsx`. The validator loads them
    once at process start and reuses the same catalogs for every
    session. Per-customer KDS overrides were tried briefly (v51) and
    removed (v54) — the SME prefers a single source of truth on the
    server and to manage KDS updates by replacing the bundled file in
    the deploy package, not via per-upload overrides.

    When a slot is absent we save an empty placeholder file to disk
    (zero bytes) so the bundle directory layout stays consistent across
    main-only and full-bundle uploads. The corresponding LTMC sheets in
    the export simply come out empty.

    Each provided slot's file is detected and verified against the slot's
    expected role — wrong-file-in-slot is an error the frontend should
    catch via format-check, but we enforce it server-side too.
    """
    if not auth_svc.can_access_module(user, "MM"):
        raise HTTPException(403, "You don't have access to module MM")

    # Read main (required); alt and lt are optional and may be None.
    main_bytes = await main_file.read()
    alt_bytes = await alt_uom_file.read() if alt_uom_file is not None else b""
    lt_bytes = await longtext_file.read() if longtext_file is not None else b""

    # ── v62: LTMC source data form (XML) upload mode ──────────────
    # When the SME uploads an SAP LTMC standard "Source Data Form" XML
    # (the canonical 29-sheet template SAP's LTMC migration cockpit
    # uses), route to the LTMC form loader instead of the customer
    # xlsx loader. The loader extracts Basic Data, Plant Data,
    # Distribution Chains, Valuation Data, Tax Classification rows AND
    # produces companion alt-UoM and long-text LoadedFile objects from
    # the form's own "Alternative Units of Measure" and "Additional
    # Descriptions" sheets — so the alt_uom_file and longtext_file
    # slots can stay empty.
    #
    # Detection is conservative — must be .xml with the SpreadsheetML
    # 2003 marker AND a Basic Data / Plant Data sheet header. False
    # negatives (we miss an LTMC form) just route to the xlsx path
    # and fail the extension check there. False positives (we treat
    # something else as an LTMC form) raise a clear ValueError from
    # the loader.
    from services.mm_ltmc_form_loader import (
        looks_like_ltmc_form, load_ltmc_form,
    )
    is_ltmc_form_upload = looks_like_ltmc_form(main_file.filename, main_bytes)
    if is_ltmc_form_upload:
        try:
            main_loaded, alt_from_form, lt_from_form = load_ltmc_form(
                main_bytes, filename=main_file.filename,
            )
        except ValueError as ve:
            raise HTTPException(400, str(ve))
        # If the SME also uploaded explicit alt/lt files, those win over
        # the form's embedded sheets — explicit upload is a stronger
        # signal of intent than "happened to be in the LTMC template".
        # If only the LTMC form was uploaded, the form's embedded sheets
        # populate the alt_loaded/lt_loaded for the merger.
        # For persistence, we still need bytes for alt/lt slots — so
        # serialize the form-derived rows into a tiny xlsx if they came
        # from the form. For v62 MVP we just save zero bytes when the
        # alt/lt came from the form (the bundle on disk doesn't perfectly
        # round-trip, but the in-memory pipeline works correctly which
        # is what matters for validation + LTMC export).
        # Save the LTMC form xml to disk in the main slot so the bundle
        # folder has the original artifact for audit. (Note: Path is
        # already imported at module level — don't re-import here, that
        # would make Python treat Path as a function-local name and
        # raise UnboundLocalError in the xlsx code path further down
        # which also references Path.)
        entry = repo.save_mm_bundle(
            main_file.filename, main_bytes,
            (alt_uom_file.filename if alt_uom_file is not None else ""), alt_bytes,
            (longtext_file.filename if longtext_file is not None else ""), lt_bytes,
            user,
        )
        # Validate using the in-memory loaded objects (skip re-loading
        # from disk since the LTMC loader doesn't go through paths).
        try:
            # Explicit alt/lt files override the form's embedded sheets
            if alt_uom_file is not None:
                alt_loaded = LOADERS_BY_ROLE["alt_uom"](
                    repo.get_mm_bundle_paths(entry["file_id"])["alt_uom"],
                    alt_uom_file.filename,
                )
            else:
                alt_loaded = alt_from_form

            if longtext_file is not None:
                lt_loaded = LOADERS_BY_ROLE["longtext"](
                    repo.get_mm_bundle_paths(entry["file_id"])["longtext"],
                    longtext_file.filename,
                )
            else:
                lt_loaded = lt_from_form

            merged = mm_merge(main_loaded, alt_loaded, lt_loaded)
            catalogs = _get_mm_catalogs()
            errors = validate_mm(
                merged.materials, catalogs, main_loaded.sap_fields,
                merged_result=merged,
                alt_uom_rows=(alt_loaded.rows if alt_loaded else None),
                longtext_rows=(lt_loaded.rows if lt_loaded else None),
                friendly_labels=getattr(main_loaded, "header_labels", None),
            )
            decisions = group_errors(errors)
            repo.update_stats(
                entry["file_id"],
                row_count=len(merged.materials),
                error_count=len(errors),
                decision_count=len(decisions),
            )
            entry = repo.get_file(entry["file_id"])
        except Exception as e:
            entry["parse_error"] = str(e)

        audit_log.log(
            user=user, action="file_upload", file_id=entry["file_id"],
            filename=entry["filename"], module="MM",
            affected_count=entry.get("row_count") or 0,
            details={
                "size_bytes": entry["size_bytes"],
                "main_filename": main_file.filename,
                "upload_mode": "ltmc_source_data_form",
                "alt_uom_from_form": alt_uom_file is None and alt_from_form is not None,
                "longtext_from_form": longtext_file is None and lt_from_form is not None,
            },
        )
        return entry

    # ── Customer xlsx mode (existing) ─────────────────────────────────

    # Extension check — users occasionally drop a CSV or XLS. Only check
    # the slots actually provided.
    slot_files = [("Main", main_file)]
    if alt_uom_file is not None:
        slot_files.append(("Alt UoM", alt_uom_file))
    if longtext_file is not None:
        slot_files.append(("Long Text", longtext_file))
    for slot, f in slot_files:
        ext = f.filename.lower().rsplit(".", 1)[-1] if "." in f.filename else ""
        if ext != "xlsx":
            # Build a targeted hint based on the extension we see. The most
            # common mistakes are .xls (old Excel) and .csv (exported data).
            if ext == "xls":
                hint = ("Old .xls files aren't supported. Open it in Excel "
                        "and use File → Save As → 'Excel Workbook (.xlsx)'.")
            elif ext == "csv":
                hint = ("CSV files aren't supported. Open the CSV in Excel "
                        "and save as .xlsx. Note: CSVs don't have the 2-row "
                        "header (friendly labels + SAP codes) that MM files "
                        "need — you may need to paste your data into the "
                        "template (download button at top).")
            elif not ext:
                hint = "File has no extension. Rename it with a .xlsx extension and try again."
            else:
                hint = f"Only .xlsx is supported. File extension '.{ext}' isn't valid here."
            raise HTTPException(
                400,
                f"{slot} slot: '{f.filename}' is not a .xlsx file. {hint}",
            )

    # Format check each PROVIDED file by writing to temp and running detect().
    # We skip absent slots — there's nothing to detect, and the merger
    # accepts None for both alt_uom and longtext.
    import tempfile
    paths_and_slots: list[tuple[str, bytes, str]] = [
        ("main", main_bytes, main_file.filename),
    ]
    if alt_uom_file is not None:
        paths_and_slots.append(("alt_uom", alt_bytes, alt_uom_file.filename))
    if longtext_file is not None:
        paths_and_slots.append(("longtext", lt_bytes, longtext_file.filename))

    for expected_role, content, filename in paths_and_slots:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
            tf.write(content)
            tmp = tf.name
        try:
            det = mm_detect(tmp, filename=filename)
        finally:
            try: Path(tmp).unlink()
            except OSError: pass
        if det.role != expected_role:
            # Build the same human-friendly message the format-check endpoint
            # uses. This is a defence-in-depth check — the frontend's
            # format-check should have already caught this — but if a user
            # bypasses the UI (curl, script, browser bug), they still get
            # an actionable error.
            hint = _slot_specific_hint(expected_role, det)
            raise HTTPException(
                400,
                f"File '{filename}' does not match the {expected_role.upper()} slot. "
                f"Detected as '{det.role}'. {det.reason}. {hint}",
            )

    # Save bundle to disk + DB. Use empty-string filenames for absent
    # slots so the manifest is honest about what was uploaded; the bytes
    # for those slots are b"" so save_mm_bundle still writes 0-byte
    # placeholder files (keeps the bundle folder layout uniform).
    entry = repo.save_mm_bundle(
        main_file.filename, main_bytes,
        (alt_uom_file.filename if alt_uom_file is not None else ""), alt_bytes,
        (longtext_file.filename if longtext_file is not None else ""), lt_bytes,
        user,
    )

    # Parse + validate for quick stats (same pattern as SD upload).
    # For absent slots we pass None to the merger rather than calling the
    # loader on an empty file (which would raise). The merger's signature
    # already accepts None for alt_uom_file and longtext_file.
    try:
        paths = repo.get_mm_bundle_paths(entry["file_id"])
        main_loaded = LOADERS_BY_ROLE["main"](paths["main"], main_file.filename)
        alt_loaded = (LOADERS_BY_ROLE["alt_uom"](paths["alt_uom"], alt_uom_file.filename)
                      if alt_uom_file is not None else None)
        lt_loaded = (LOADERS_BY_ROLE["longtext"](paths["longtext"], longtext_file.filename)
                     if longtext_file is not None else None)
        merged = mm_merge(main_loaded, alt_loaded, lt_loaded)
        # MM catalogs come from the bundled MM_KDS.xlsx — no per-customer
        # override (removed in v54). Validator gets the same catalogs
        # every session.
        catalogs = _get_mm_catalogs()
        # v57: cross-file checks (orphan MATNRs, sales-area mismatches)
        # require the MergeResult plus alt/lt source rows.
        errors = validate_mm(
            merged.materials, catalogs, main_loaded.sap_fields,
            merged_result=merged,
            alt_uom_rows=(alt_loaded.rows if alt_loaded else None),
            longtext_rows=(lt_loaded.rows if lt_loaded else None),
            friendly_labels=getattr(main_loaded, "header_labels", None),
        )
        decisions = group_errors(errors)
        repo.update_stats(
            entry["file_id"],
            row_count=len(merged.materials),
            error_count=len(errors),
            decision_count=len(decisions),
        )
        entry = repo.get_file(entry["file_id"])
    except Exception as e:
        # Don't fail upload on a parse error; surface it so user can
        # still delete the file + re-upload a fixed version.
        entry["parse_error"] = str(e)

    audit_log.log(
        user=user, action="file_upload", file_id=entry["file_id"],
        filename=entry["filename"], module="MM",
        affected_count=entry.get("row_count") or 0,
        details={
            "size_bytes": entry["size_bytes"],
            "main_filename": main_file.filename,
            "alt_uom_filename": alt_uom_file.filename if alt_uom_file is not None else None,
            "longtext_filename": longtext_file.filename if longtext_file is not None else None,
        },
    )

    return entry

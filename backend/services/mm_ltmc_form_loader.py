"""LTMC Source Data Form (XML) loader.

Why this module exists
----------------------
v62 adds a third upload mode for MM: instead of the customer's flat
2-row-header xlsx, the SME can upload the SAP LTMC standard "Source
Data Form" XML directly. This is the canonical SAP S/4HANA 2025 LTMC
template (29 sheets, SpreadsheetML format) — the same file SAP's LTMC
migration cockpit expects to receive. See sample at
`/mnt/user-data/uploads/source_data_form_MM.xml`.

The 29 sheets carry the same logical data as the customer xlsx, just
denormalised across multiple sheets:

  Basic Data           - 1 row per material     → material.main
  Plant Data           - N rows per material    → material.plant_rows
  Distribution Chains  - N rows per material    → material.main (sales)
  Valuation Data       - N rows per material    → mixed into plant_rows
  Tax Classification   - N rows per material    → material.main (ALAND, TATYP1, TAXM1)
  Alt. Units           - N rows per material    → material.alt_uoms
  Add. Descriptions    - N rows per material    → material.longtexts

This loader produces a `LoadedFile`-shape object compatible with the
existing mm_merger.merge() so all downstream code (validator, decisions,
LTMC export, KDS checks) works without changes. Output looks identical
to what mm_loader.load_main produces from the customer xlsx — same
sap_fields list, same row dicts.

Sheets we don't load
--------------------
22 sheets are skipped (Seasons, Article Hierarchy, Class/Characteristic,
Forecasting, Storage Locations, Inspection Setup, MRP Area, Warehouse,
Storage Type, Production Resources, Future Price, etc.). Reasons:

  - No validator rule covers them yet (Healthium scope).
  - No KDS catalog to validate against.
  - SME marked these `skip_for_pe01_migration` in mm_ltmc_mandatory.

If any of these become relevant later, add the sheet to SHEET_MAPPERS
and the field rules to mm_checklist.

Format of the XML (per sheet)
-----------------------------
SpreadsheetML 2003 format. Each `<Worksheet ss:Name="…">` has a
`<Table>` with rows. Layout:

  Row 1-3: title rows (description, version, blank)
  Row 4:   hidden styled placeholder
  Row 5:   SAP field codes (e.g. "PRODUCT", "MTART", "WERKS")  ← KEY
  Row 6:   ETE field-type metadata (hidden)
  Row 7:   section header ("Key", "Sales Tax", etc.)
  Row 8:   friendly labels (e.g. "Product Number*")
  Row 9+:  data rows                                            ← KEY

We read the SAP codes from row 5 and the data from row 9 onward.
Cells in SpreadsheetML can have an `ss:Index="N"` attribute that
forces an absolute column position — we honor that, otherwise we
auto-increment. Same logic the LTMC export's template loader uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from services.mm_loader import LoadedFile, LoadedRow


# ─── Sheet→field mappings ─────────────────────────────────────────────────
# Each entry says: from this LTMC sheet, take these SAP fields and put
# them into a particular shape. The rest of the columns in that sheet
# are still loaded into `extras` for traceability but aren't surfaced
# to the merger (the merger doesn't know about them).

# Sheets we actively map. Key = sheet name, value = role:
#   "main_per_material" — 1 row per material; goes to a single dict
#   "main_per_plant"    — N rows per material; merge into plant_rows
#   "alt_uoms"          — alt-unit-of-measure rows
#   "longtexts"         — additional description rows
#   "tax"               — tax classification rows (per material × ALAND)
#   "valuation_per_plant" — valuation data rows (per material × BWKEY)
# For "skip", we still parse to validate the sheet structure but the
# data doesn't flow into MergedMaterial.
SHEET_ROLES: dict[str, str] = {
    "Basic Data":                   "main_per_material",
    "Distribution Chains":          "main_per_distribution",
    "Plant Data":                   "main_per_plant",
    "Tax Classification":           "tax",
    "Valuation Data":               "valuation_per_plant",
    "Alternative Units of Measure": "alt_uoms",
    "Additional Descriptions":      "longtexts",
    # Other 22 sheets parsed but not flowed to MergedMaterial.
}


# ─── Data classes ─────────────────────────────────────────────────────────

@dataclass
class LtmcFormSheet:
    """One worksheet's parsed contents."""
    name: str
    role: str                # see SHEET_ROLES
    sap_fields: list[str]    # row 5 codes, e.g. ["PRODUCT", "MTART", ...]
    rows: list[dict[str, Any]] = field(default_factory=list)
    # rows is a list of {sap_field: value} dicts, one per data row (row 9+).


# ─── XML parsing ──────────────────────────────────────────────────────────

# SpreadsheetML namespace constant. The XML uses xmlns:ss=… but we work
# with the literal string form since the data form always uses ss: prefix.
_WORKSHEET_RE = re.compile(
    r'<Worksheet ss:Name="([^"]+)"[^>]*>(.*?)</Worksheet>',
    re.DOTALL,
)
_ROW_RE = re.compile(r'<Row\b([^>]*)>(.*?)</Row>', re.DOTALL)
_CELL_RE = re.compile(
    r'<Cell\b([^>]*)>(?:<Data\b[^>]*>(.*?)</Data>)?\s*</Cell>',
    re.DOTALL,
)
_INDEX_RE = re.compile(r'ss:Index="(\d+)"')


def _row_cells(row_body: str) -> list[tuple[int, str]]:
    """Parse a row's <Cell> entries into (col_idx_1based, value) pairs.

    Handles `ss:Index="N"` (absolute column position) — when seen, the
    next cell sits at col N, then col N+1, N+2, etc. Without it, cells
    auto-increment from the last seen index.

    Returns a list of (col_idx, value) tuples. Missing cells in the
    middle of a row are NOT inserted as empty — caller handles gaps.
    """
    cells: list[tuple[int, str]] = []
    cur_idx = 1
    for cm in _CELL_RE.finditer(row_body):
        attrs = cm.group(1)
        val = cm.group(2) or ""
        idx_attr = _INDEX_RE.search(attrs)
        if idx_attr:
            cur_idx = int(idx_attr.group(1))
        # Decode XML entities — the LTMC template uses &#10; for newlines
        # in friendly labels, etc. For data cells we typically only need
        # & and < unescaped.
        val = (val.replace("&amp;", "&")
                  .replace("&lt;", "<")
                  .replace("&gt;", ">")
                  .replace("&quot;", '"')
                  .replace("&#10;", "\n"))
        cells.append((cur_idx, val))
        cur_idx += 1
    return cells


def _parse_sheet(name: str, body: str) -> LtmcFormSheet | None:
    """Parse one Worksheet's body into a LtmcFormSheet.

    Returns None if the sheet has fewer than 5 rows (header rows don't
    fully exist) — there's no schema to read. Common for the
    Introduction and Field List sheets, which are documentation, not
    data.
    """
    rows = _ROW_RE.findall(body)
    if len(rows) < 5:
        return None

    # Row 5: SAP codes. Build positional map col_idx → sap_code.
    row5_cells = _row_cells(rows[4][1])
    if not row5_cells:
        return None
    col_to_sap: dict[int, str] = {}
    for col, val in row5_cells:
        v = val.strip()
        if v:
            col_to_sap[col] = v

    # Sap_fields list, ordered by column index.
    sap_fields = [col_to_sap[k] for k in sorted(col_to_sap.keys())]

    role = SHEET_ROLES.get(name, "skip")

    sheet = LtmcFormSheet(name=name, role=role, sap_fields=sap_fields)

    # Data rows start at row 9 (index 8 in 0-based). Rows 6, 7, 8 are
    # ETE metadata, section headers, friendly labels.
    if role == "skip":
        return sheet

    for row_attrs, row_body in rows[8:]:
        cells = _row_cells(row_body)
        # Empty rows (no cells with content) are skipped silently —
        # SAP's exported templates often have trailing blank rows.
        row_dict: dict[str, Any] = {}
        for col, val in cells:
            sap_code = col_to_sap.get(col)
            if not sap_code:
                continue
            v = val.strip()
            if v == "":
                continue
            row_dict[sap_code] = v
        if not row_dict:
            continue
        # Skip rows that don't have PRODUCT (the material number) — those
        # are typically blank rows below the data block.
        if "PRODUCT" not in row_dict:
            continue
        sheet.rows.append(row_dict)

    return sheet


def parse_ltmc_form(xml_bytes: bytes) -> dict[str, LtmcFormSheet]:
    """Parse an LTMC source data form into a dict of sheet_name → LtmcFormSheet.

    Only sheets with role != 'skip' will have populated `rows`. The full
    sheet list is preserved so callers can introspect what's present
    without re-parsing.
    """
    raw = xml_bytes.decode("utf-8")

    sheets: dict[str, LtmcFormSheet] = {}
    for m in _WORKSHEET_RE.finditer(raw):
        name = m.group(1)
        body = m.group(2)
        parsed = _parse_sheet(name, body)
        if parsed:
            sheets[name] = parsed

    return sheets


# ─── Flatten into customer-xlsx-shape LoadedFile ──────────────────────────

# After parsing, we collapse the multi-sheet structure into the same
# LoadedFile shape that mm_loader.load_main produces from the customer
# xlsx. The rest of the pipeline (mm_merger, mm_validator, etc.)
# doesn't know or care that the data came from a different upload mode.

def flatten_to_loaded_file(sheets: dict[str, LtmcFormSheet],
                           filename: str = "ltmc_form.xml") -> LoadedFile:
    """Collapse parsed LTMC sheets into a single LoadedFile.

    Strategy:
      - Basic Data row → main attributes per material (1:1)
      - Distribution Chains rows → per-(material, VKORG, VTWEG) sales rows
      - Plant Data rows → per-(material, WERKS) plant rows
      - Tax Classification rows → ALAND/TATYP1/TAXM1 merged onto material
      - Valuation Data rows → BKLAS/VPRSV/STPRS/VERPR/PEINH merged onto plant
      - Alt UoM rows → captured separately (used by the merger as alt_uom_loaded)
      - Additional Descriptions rows → captured separately (longtexts)

    The customer xlsx format has ONE row per (material × plant × sales),
    so we cross-join Plant Data × Distribution Chains × Tax Classification
    to produce the same shape. For materials without plant/sales rows
    (rare — basic-data-only materials), we emit one row with just the
    main attributes.

    Returns a LoadedFile with rows sorted by (MATNR, WERKS, VKORG, VTWEG)
    to match the customer xlsx's natural ordering.
    """
    basic = sheets.get("Basic Data")
    plants = sheets.get("Plant Data")
    distrib = sheets.get("Distribution Chains")
    tax = sheets.get("Tax Classification")
    valuation = sheets.get("Valuation Data")

    if not basic or not basic.rows:
        # Nothing to load — empty Basic Data sheet means no materials.
        return LoadedFile(
            filename=filename,
            role="main",
            sap_fields=[],
            header_labels=[],
            rows=[],
        )

    # Index supporting sheets by PRODUCT for fast lookups.
    plants_by_matnr: dict[str, list[dict]] = {}
    for r in (plants.rows if plants else []):
        plants_by_matnr.setdefault(r["PRODUCT"], []).append(r)
    distrib_by_matnr: dict[str, list[dict]] = {}
    for r in (distrib.rows if distrib else []):
        distrib_by_matnr.setdefault(r["PRODUCT"], []).append(r)
    tax_by_matnr: dict[str, list[dict]] = {}
    for r in (tax.rows if tax else []):
        tax_by_matnr.setdefault(r["PRODUCT"], []).append(r)
    valuation_by_pair: dict[tuple[str, str], dict] = {}
    for r in (valuation.rows if valuation else []):
        valuation_by_pair[(r["PRODUCT"], r.get("BWKEY", ""))] = r

    # Build the unified sap_fields list — union of all useful sheets,
    # ordered: Basic Data fields first, then Plant Data, then Distrib,
    # then Tax. Customer xlsx puts MATNR/MTART first too, so this is
    # consistent.
    sap_fields: list[str] = []
    seen: set[str] = set()

    def _extend(src_fields: list[str]) -> None:
        for f in src_fields:
            if f not in seen:
                sap_fields.append(f)
                seen.add(f)

    # MATNR/REC_NO at the front to match the xlsx convention.
    sap_fields.append("MATNR"); seen.add("MATNR")
    _extend(basic.sap_fields)
    if plants: _extend(plants.sap_fields)
    if distrib: _extend(distrib.sap_fields)
    if tax: _extend(tax.sap_fields)
    if valuation: _extend(valuation.sap_fields)

    # PRODUCT and MATNR are aliases — drop PRODUCT from the field list
    # since the rest of the pipeline expects MATNR.
    sap_fields = [f for f in sap_fields if f != "PRODUCT"]

    # Emit one row per (material × plant × distrib × tax) cross-join.
    out_rows: list[LoadedRow] = []
    excel_row_counter = 3  # Row 3 in the merged xlsx convention; rows 1-2 are headers

    for basic_row in basic.rows:
        matnr = basic_row["PRODUCT"]
        # Cross-join supporting sheets. Use [{}] (single empty dict) when
        # a sheet is absent so the cross-join still produces 1 row per
        # material with the basic data filled in.
        plant_rows = plants_by_matnr.get(matnr, [{}])
        distrib_rows = distrib_by_matnr.get(matnr, [{}])
        tax_rows = tax_by_matnr.get(matnr, [{}])

        # If a material has multiple plant rows AND multiple distrib
        # rows, the customer xlsx convention is to denormalise all
        # combinations. SAP's data model allows different sales orgs
        # per plant, but in practice 1:1 is most common.
        for plant_row in plant_rows:
            for distrib_row in distrib_rows:
                for tax_row in tax_rows:
                    excel_row_counter += 1
                    values: dict[str, Any] = {"MATNR": matnr}
                    # Layer in: basic → plant → distrib → tax. Later layers
                    # override (rare conflict — they shouldn't share fields).
                    for k, v in basic_row.items():
                        if k != "PRODUCT":
                            values[k] = v
                    for k, v in plant_row.items():
                        if k != "PRODUCT":
                            values[k] = v
                    for k, v in distrib_row.items():
                        if k != "PRODUCT":
                            values[k] = v
                    for k, v in tax_row.items():
                        if k != "PRODUCT":
                            values[k] = v
                    # Valuation Data merge — keyed by (MATNR, BWKEY) and
                    # BWKEY equals WERKS for plant-level valuation. So we
                    # try BWKEY=WERKS first, then BWKEY="" (catch-all).
                    werks = values.get("WERKS", "")
                    val_row = (valuation_by_pair.get((matnr, werks))
                               or valuation_by_pair.get((matnr, "")))
                    if val_row:
                        for k, v in val_row.items():
                            if k != "PRODUCT" and k not in values:
                                values[k] = v

                    out_rows.append(LoadedRow(
                        excel_row=excel_row_counter,
                        values=values,
                    ))

    # Friendly labels — for now, mirror sap_fields. The LTMC form's
    # row 8 has long descriptive text we could extract, but those
    # descriptions are noisy ("Product Number*\n\nA key that uniquely…")
    # and the SME has v60's _CANONICAL_SAP_LABELS fallback for the
    # standard codes. Mirroring sap_fields keeps the records-editor
    # field labels stable.
    header_labels = list(sap_fields)

    return LoadedFile(
        filename=filename,
        role="main",
        sap_fields=sap_fields,
        header_labels=header_labels,
        rows=out_rows,
    )


# ─── Public entry point ───────────────────────────────────────────────────

def load_ltmc_form(xml_bytes: bytes,
                   filename: str = "ltmc_form.xml") -> tuple[LoadedFile, LoadedFile | None, LoadedFile | None]:
    """Public: parse an LTMC source data form XML and produce up to 3
    LoadedFile objects matching the customer xlsx bundle shape.

    Returns:
        (main_loaded, alt_loaded_or_None, lt_loaded_or_None)

        - main_loaded: cross-joined Basic + Plant + Distrib + Tax data
        - alt_loaded: Alternative Units of Measure rows, or None if absent
        - lt_loaded: Additional Descriptions rows, or None if absent

    The 3-tuple matches the existing MM bundle shape (main + alt + lt).
    Downstream code (merger, validator) receives the same objects it
    would from mm_loader, so no changes are needed there.

    Raises ValueError if the XML doesn't look like an LTMC form (no
    Basic Data sheet, or Basic Data has no rows).
    """
    sheets = parse_ltmc_form(xml_bytes)
    if "Basic Data" not in sheets or not sheets["Basic Data"].rows:
        raise ValueError(
            "Uploaded XML doesn't look like an LTMC source data form. "
            "Expected a 'Basic Data' sheet with at least one data row. "
            "Common cause: the file is the empty SAP template, not a "
            "filled-in source data form. Please populate the template "
            "with at least one material and re-upload."
        )
    main_loaded = flatten_to_loaded_file(sheets, filename=filename)

    # Alt UoMs — convert "Alternative Units of Measure" sheet rows to
    # the alt-uom LoadedFile shape (same fields the customer's
    # Eccel_Upload_Program_File_Alternate_Units.xlsx uses: PRODUCT/MATNR,
    # MEINH, UMREN, UMREZ, etc.).
    alt_loaded: LoadedFile | None = None
    alt_sheet = sheets.get("Alternative Units of Measure")
    if alt_sheet and alt_sheet.rows:
        alt_rows = []
        for i, r in enumerate(alt_sheet.rows, start=4):
            v = {"MATNR": r.get("PRODUCT", "")}
            for k, val in r.items():
                if k != "PRODUCT":
                    v[k] = val
            alt_rows.append(LoadedRow(excel_row=i, values=v))
        alt_fields = ["MATNR"] + [f for f in alt_sheet.sap_fields if f != "PRODUCT"]
        alt_loaded = LoadedFile(
            filename=f"{filename}#AltUnits",
            role="alt_uom",
            sap_fields=alt_fields,
            header_labels=list(alt_fields),
            rows=alt_rows,
        )

    # Long texts — "Additional Descriptions" rows. Customer's xlsx for
    # this is Eccel_Upload_Program_File_Long_Text.xlsx with MATNR/SPRAS/
    # MAKTX columns. Mirror that.
    lt_loaded: LoadedFile | None = None
    lt_sheet = sheets.get("Additional Descriptions")
    if lt_sheet and lt_sheet.rows:
        lt_rows = []
        for i, r in enumerate(lt_sheet.rows, start=4):
            v = {"MATNR": r.get("PRODUCT", ""),
                 "BASE_TEXT": r.get("MAKTX", "")}
            for k, val in r.items():
                if k not in ("PRODUCT", "MAKTX"):
                    v[k] = val
            lt_rows.append(LoadedRow(excel_row=i, values=v))
        lt_fields = ["MATNR", "BASE_TEXT"] + [f for f in lt_sheet.sap_fields
                                               if f not in ("PRODUCT", "MAKTX")]
        lt_loaded = LoadedFile(
            filename=f"{filename}#LongTexts",
            role="longtext",
            sap_fields=lt_fields,
            header_labels=list(lt_fields),
            rows=lt_rows,
        )

    return main_loaded, alt_loaded, lt_loaded


# ─── Detection helper for the upload endpoint ─────────────────────────────

def looks_like_ltmc_form(filename: str, head_bytes: bytes) -> bool:
    """Quick check: does this file look like an LTMC source data form?

    Used by the MM upload endpoint to route the file to this loader
    instead of mm_loader.load_main. Conservative — only returns True
    when we're confident, since misrouting causes a hard error.

    Signals (all required):
      - Filename ends with .xml
      - First few KB contain `<?mso-application progid="Excel.Sheet"?>`
        (SpreadsheetML 2003 marker — only appears in this format)
      - First few KB contain the SpreadsheetML namespace URI

    Note: we deliberately don't check for sheet names like "Basic Data"
    here — the LTMC form sheet definitions are buried ~190 KB into the
    file (after styles), past any reasonable head-bytes probe size.
    The two signals above are sufficient: only SAP-shipped LTMC forms
    use SpreadsheetML 2003 in our pipeline.

    False positives risk: a SpreadsheetML 2003 file from another tool
    might match these signals. Mitigation: load_ltmc_form() raises
    ValueError if the parsed file lacks a Basic Data sheet, which the
    upload endpoint converts to a clean 400.

    Returns False for empty bytes, non-XML, plain xlsx, etc.
    """
    if not filename.lower().endswith(".xml"):
        return False
    if not head_bytes:
        return False

    try:
        head = head_bytes[:4096].decode("utf-8", errors="ignore")
    except Exception:
        return False

    # Both markers must be present. The mso-application PI is rarely
    # used outside SpreadsheetML 2003, so it's a strong filter.
    if 'mso-application progid="Excel.Sheet"' not in head:
        return False
    if 'urn:schemas-microsoft-com:office:spreadsheet' not in head:
        return False
    return True

"""
Composite-key duplicate detection for BOM and Routing sheets.

Why
---
SAP rejects duplicate rows at LTMC import — uniqueness per object is
defined by the composite key (the leftmost identity columns of each
LTMC sheet). We catch duplicates BEFORE upload so SMEs see them in the
Error Grid and can fix them locally rather than getting a confusing
rejection report from SAP.

Composite keys
--------------
Composite keys are NOT just "the mandatory columns" — some columns
(WERKS, ITMID) are part of the SAP uniqueness key but not flagged as
mandatory in the LTMC template's row-8 description. The keys below
are the SAP S/4 LTMC standard for each migration object.

Sources:
  - Verified against the customer's real BOM_PHASE_1.xlsx and
    New_Routing_Sheet_peenya.xlsx structures.
  - SAP standard: a BOM is uniquely identified by
    MATNR + WERKS + STLAN + STLAL; items add ITMID; subitems add UPOSZ;
    dependencies add KNNAM_EXT (and SPRAS for descriptions).
  - Routing is uniquely identified by PLNNR + PLNAL; sequences add
    PLNFL; operations add VORNR; sub-ops add SUB_VORNR.

Note on Routing-side shared sheet names
---------------------------------------
The rulebook keys Routing's "Global Dependency" / "Local Dependency"
etc. with a "Routing · " prefix to avoid colliding with the BOM
sheets of the same names. The duplicate detector mirrors that
prefixing — see the validator for how this is bridged at runtime.
"""
from __future__ import annotations

from typing import Iterable, Any


# ─── BOM composite keys ─────────────────────────────────────────────────
PP_KEYS_BY_SHEET: dict[str, tuple[str, ...]] = {
    "BOM Header":                       ("MATNR", "WERKS", "STLAN", "STLAL"),
    "BOM Item":                         ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID"),
    "BOM Subitem":                      ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID", "UPOSZ"),
    "Global Dependency":                ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID"),
    "Local Dependency":                 ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID", "KNNAM_EXT"),
    "Local Dependency Description":     ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID", "KNNAM_EXT", "SPRAS"),
    "Documentation of Dependency":      ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID", "KNNAM_EXT", "SPRAS"),
    "Sources of Local Dependency":      ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID", "KNNAM_EXT"),
    "BOM Item Document Assignment":     ("MATNR", "WERKS", "STLAN", "STLAL", "ITMID", "DOKAR", "DOKNR", "DOKVR", "DOKTL"),
    "BOM Header Document Assignment":   ("MATNR", "WERKS", "STLAN", "STLAL", "DOKAR", "DOKNR", "DOKVR", "DOKTL"),
}

# ─── Routing composite keys ─────────────────────────────────────────────
ROUTING_KEYS_BY_SHEET: dict[str, tuple[str, ...]] = {
    "Routing Group":                    ("PLNNR",),
    "Task List - Header":               ("PLNNR", "PLNAL"),
    "Material Task List Assignment":    ("PLNNR", "PLNAL", "MATNR", "WERKS_MAT"),
    "Sequences":                        ("PLNNR", "PLNAL", "PLNFL"),
    "Operations":                       ("PLNNR", "PLNAL", "PLNFL", "VORNR"),
    "Component Assignment":             ("PLNNR", "PLNAL", "PLNFL", "VORNR", "BOM_TYPE", "MATNR_ROOT", "ITMID"),
    "Production Resources and Tools":   ("PLNNR", "PLNAL", "PLNFL", "VORNR", "PSNFH"),
    "Sub Operations":                   ("PLNNR", "PLNAL", "PLNFL", "VORNR", "SUB_VORNR"),
    "Inspection Plan Characteristic":   ("PLNNR", "PLNAL", "PLNFL", "VORNR", "MERKNR"),
    # Routing-side dependency sheets: validator passes these with the
    # "Routing · " prefix when looking up, so we register both names.
    "Routing · Global Dependency":              ("PLNNR", "PLNAL", "PLNFL", "VORNR", "KNNAM_GLOB"),
    "Routing · Local Dependency":               ("PLNNR", "PLNAL", "PLNFL", "VORNR", "KNNAM_LOCL"),
    "Routing · Local Dependency Description":   ("PLNNR", "PLNAL", "PLNFL", "VORNR", "KNNAM_LOCL", "SPRAS"),
    "Routing · Documentation of Dependency":    ("PLNNR", "PLNAL", "PLNFL", "VORNR", "KNNAM_LOCL", "SPRAS"),
    "Routing · Sources of Local Dependency":    ("PLNNR", "PLNAL", "PLNFL", "VORNR", "KNNAM_LOCL"),
}


def _norm(v: Any) -> str:
    """Normalise a value for key comparison.

    Handles None, Excel float-as-int (".0" trim), whitespace, case.
    Real decimals like "12.5" preserved — only strip ".0".
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s.upper()


def _composite_key(row_values: dict, key_cols: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_norm(row_values.get(c)) for c in key_cols)


def find_duplicate_groups(
    sheets: Iterable[tuple[str, list]],
    keys_by_sheet: dict[str, tuple[str, ...]],
) -> list[dict]:
    """Find composite-key duplicates across all provided sheets.

    Args:
      sheets: iterable of (sheet_name, list_of_rows). Rows must have
        `.values` (dict) and `.excel_row` (int).
      keys_by_sheet: composite-key tuple per sheet name. Sheets not in
        this map are skipped.

    Returns: list of dup-group dicts; one per *duplicated* composite key.
      Rows with completely empty composite keys are not flagged (those
      are usually trailing blank rows; the row-mandatory validators
      already catch them).
    """
    out: list[dict] = []
    for sheet_name, rows in sheets:
        key_cols = keys_by_sheet.get(sheet_name)
        if not key_cols:
            continue

        seen: dict[tuple, list[dict]] = {}
        for row_idx, r in enumerate(rows):
            values = getattr(r, "values", {}) or {}
            key = _composite_key(values, key_cols)
            if all(part == "" for part in key):
                continue
            seen.setdefault(key, []).append({
                "row_idx": row_idx,
                "excel_row": getattr(r, "excel_row", row_idx + 1),
                "values": values,
            })

        for key, occurrences in seen.items():
            if len(occurrences) >= 2:
                out.append({
                    "sheet": sheet_name,
                    "key_columns": key_cols,
                    "key_values": key,
                    "rows": occurrences,
                })

    return out


def format_duplicate_message(group: dict) -> str:
    """Build the user-facing message for a duplicate-row error."""
    pairs = ", ".join(
        f"{col}={val if val else '(blank)'}"
        for col, val in zip(group["key_columns"], group["key_values"])
    )
    sisters = sorted(r["excel_row"] for r in group["rows"])
    sister_list = ", ".join(str(s) for s in sisters)
    return (
        f"Duplicate row — same {pairs} appears at rows {sister_list}. "
        f"SAP will reject duplicates at LTMC import; keep only one and "
        f"remove the others, or differentiate by changing one of the "
        f"key columns ({', '.join(group['key_columns'])})."
    )

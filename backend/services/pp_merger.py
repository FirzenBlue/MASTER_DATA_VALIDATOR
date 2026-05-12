"""
PP merger — groups loaded BOM data by MATNR for chunked export.

Why
---
SAP requires all rows for a given MATNR to be uploaded together (the
BOM Header, all its Items, all the Subitems, all dependencies). When
we split a big export into ≤95 MB chunks, MATNR boundaries must be
preserved — splitting a MATNR's rows across two chunks would produce
an LTMC import that fails on the orphaned rows.

The merger walks every loaded sheet and produces a list of `MaterialBom`
objects, each holding ALL the rows (across ALL sheets) for one MATNR.
The splitter then bin-packs these into chunks.

Note for Routing
----------------
The parallel `routing_merger.py` does the same thing keyed on PLNNR.
Same shape, different key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any

from .pp_loader import LoadedBom, LoadedRow


def _norm_matnr(v: Any) -> str:
    """Normalize a MATNR value to a stable string key.

    - None / blank → "" (filtered out by caller; we never group on a
      blank MATNR).
    - Excel float-as-int: "8903837294708.0" → "8903837294708"
    - Strings: stripped, uppercase
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s.upper()


@dataclass
class MaterialBom:
    """All rows for ONE material number across ALL BOM sheets.

    Attributes:
      matnr: normalized MATNR string (the grouping key)
      rows_by_sheet: dict[sheet_name, list[LoadedRow]] — every loaded
        row whose MATNR matches this group, indexed by source sheet.
        A material with rows on BOM Header + BOM Item + BOM Subitem
        will have all three sheets populated; sheets where the material
        has no rows are absent from the dict.
    """
    matnr: str
    rows_by_sheet: dict[str, list[LoadedRow]] = field(default_factory=dict)

    def total_rows(self) -> int:
        return sum(len(rs) for rs in self.rows_by_sheet.values())


@dataclass
class MergedBom:
    """Result of merging a LoadedBom into MATNR groups."""
    materials: list[MaterialBom] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    # Rows that had no MATNR at all, indexed by sheet. These can't be
    # assigned to any material; the validator will already have flagged
    # them as missing-mandatory. We still preserve them so they appear
    # in the export in their own dedicated chunk (orphan chunk).
    orphan_rows_by_sheet: dict[str, list[LoadedRow]] = field(default_factory=dict)


def merge_bom(bom: LoadedBom) -> MergedBom:
    """Merge a LoadedBom into per-MATNR groups for chunked export.

    Groups every row from every sheet by MATNR. Rows with a blank/missing
    MATNR are collected separately as orphans (validator already flagged
    them — we just preserve them so they don't silently disappear from
    the export).

    Order preservation: within each (sheet, MATNR) group, source order
    is preserved. The materials list is sorted by MATNR for stable
    chunking output.
    """
    by_matnr: dict[str, dict[str, list[LoadedRow]]] = defaultdict(lambda: defaultdict(list))
    orphans: dict[str, list[LoadedRow]] = defaultdict(list)

    for sheet_name, sheet in bom.sheets.items():
        for row in sheet.rows:
            matnr = _norm_matnr(row.values.get("MATNR"))
            if not matnr:
                orphans[sheet_name].append(row)
                continue
            by_matnr[matnr][sheet_name].append(row)

    materials: list[MaterialBom] = []
    for matnr in sorted(by_matnr.keys()):
        materials.append(MaterialBom(
            matnr=matnr,
            rows_by_sheet=dict(by_matnr[matnr]),
        ))

    total_rows = sum(m.total_rows() for m in materials) + sum(len(rs) for rs in orphans.values())
    summary = {
        "material_count": len(materials),
        "orphan_row_count": sum(len(rs) for rs in orphans.values()),
        "total_rows_all_sheets": total_rows,
        "sheet_row_counts": {n: len(s.rows) for n, s in bom.sheets.items()},
    }

    return MergedBom(
        materials=materials,
        summary=summary,
        orphan_rows_by_sheet=dict(orphans),
    )

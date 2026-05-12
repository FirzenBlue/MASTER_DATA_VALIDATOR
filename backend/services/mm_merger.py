"""
MM merger — joins the 3 loaded MM xlsx files into a single per-material
data structure.

After detection + load, we have up to 3 LoadedFile objects:
  - main: rows keyed by (MATNR, WERKS). One material can have N plant rows.
  - alt_uom: N rows per material (one per alternate unit of measure)
  - longtext: N rows per material (one per text type)

All three are joined by the MATNR field. The output of this module is a
list of MergedMaterial objects, one per distinct MATNR found in MAIN.
Each MergedMaterial carries a `plant_rows` list — one entry per plant
the material is assigned to. This replaces the v1 "1 MATNR = 1 row"
assumption which was wrong for real migration data (Healthium's Peenya
file has every material at PE01, PE02 AND SC01).

Materials that appear ONLY in alt_uom or longtext without being in main
are flagged as orphans — we don't migrate them (they have no master
record). The UI surfaces these as a warning if the orphan count is
dominant (likely a wrong-file-pair upload).

We do NOT do any LTMC-row fanout here. That's the LTMC generator's job
(Phase 3) — it'll take these MergedMaterials and produce:
  - 1 "Basic Data" row per material (from ANY plant row; basic fields
    are the same across plants)
  - N "Plant Data" rows per material (one per plant_row)
  - N × M "Distribution Chains" rows per (material × sales org × distr chan)
  - ... etc
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.mm_loader import LoadedFile, LoadedRow


@dataclass
class MergedMaterial:
    """One material with all data joined from the 3 input files.

    Attributes:
        matnr: the MATNR (as string — important even when input was numeric;
            downstream code always treats MATNR as a string identifier).
        main: the primary main-file row — treated as "basic data". For
            multi-plant materials this is the first plant row; fields that
            are truly plant-independent (MAKTX, MTART, MEINS, MATKL, etc.)
            come from here. Retained for validators that don't care about
            plant and for backward compatibility with Phase-1 callers.
        plant_rows: one entry per (MATNR, WERKS) pair. For single-plant
            materials this is a 1-element list containing the same object
            as `main`. For Peenya-style 3-plant materials this is 3
            entries, each with its own plant-specific field set (WERKS
            differs; plant-scoped fields like LGORT, LGPRO, DISPO,
            BSTMI, MHDRZ may differ per plant in richer files).
        alt_uoms: 0+ rows from the alt UoM file matching this MATNR.
            Not plant-scoped — a material's alt units apply everywhere.
        longtexts: 0+ rows from the long text file matching this MATNR.
        source_excel_row: shortcut to main.excel_row for error messages.
    """
    matnr: str
    main: LoadedRow
    plant_rows: list[LoadedRow] = field(default_factory=list)
    alt_uoms: list[LoadedRow] = field(default_factory=list)
    longtexts: list[LoadedRow] = field(default_factory=list)

    @property
    def source_excel_row(self) -> int:
        return self.main.excel_row

    @property
    def plants(self) -> list[str]:
        """Distinct WERKS values across all plant rows. Sorted for stable UI."""
        seen = []
        for r in self.plant_rows:
            w = r.get("WERKS")
            if w is not None and str(w).strip() and str(w).strip() not in seen:
                seen.append(str(w).strip())
        return sorted(seen)


@dataclass
class MergeResult:
    """Output of merge(). Contains the materials + diagnostics.

    Attributes:
        materials: one entry per distinct MATNR in the main file.
        orphans_in_alt_uom: alt-UoM rows whose MATNR has no main record.
            Flagged so SMEs can fix upstream — these rows WILL NOT make it
            into LTMC output.
        orphans_in_longtext: same but for long-text file.
        duplicate_matnrs_in_main: MATNRs that appear more than once in the
            MAIN file WITH the same WERKS. These ARE real duplicates that
            need fixing. (If multi-plant, each plant's row is NOT counted
            as a duplicate — it's a legitimate plant assignment.)
        multi_plant_matnrs: MATNRs that appear in multiple rows differing
            only by WERKS. These are NOT duplicates; they're plant fanout.
            Listed so the UI can tell SMEs "48 materials × 3 plants each".
        file_pair_warning: Non-empty when the alt_uom/longtext files
            reference materials that don't exist in the main file in any
            meaningful quantity. Strong signal of a wrong-file-pair upload.
        field_inventory: union of all SAP fields seen across all main rows.
        summary: quick stats for the UI (material count, plant count, etc.)
    """
    materials: list[MergedMaterial]
    orphans_in_alt_uom: list[LoadedRow] = field(default_factory=list)
    orphans_in_longtext: list[LoadedRow] = field(default_factory=list)
    duplicate_matnrs_in_main: list[tuple[str, list[int]]] = field(default_factory=list)
    multi_plant_matnrs: list[tuple[str, list[str]]] = field(default_factory=list)
    file_pair_warning: str = ""
    field_inventory: set[str] = field(default_factory=set)
    summary: dict[str, Any] = field(default_factory=dict)


def _matnr_str(v: Any) -> str | None:
    """Normalise a MATNR to a string. None if blank.

    Excel often returns large numeric MATNRs as floats (e.g. 8903837589095.0).
    We convert to int first to drop the decimal, then to string. Strings are
    returned with stripped whitespace.
    """
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int,)):
        return str(v)
    s = str(v).strip()
    return s if s else None


def _werks_str(v: Any) -> str:
    """Normalise a WERKS to a string. Empty if blank — a row with no plant
    is treated as a single-plant (or no-plant-defined) material."""
    if v is None:
        return ""
    return str(v).strip()


def _index_by_matnr(rows: list[LoadedRow]) -> dict[str, list[LoadedRow]]:
    """Build a MATNR → [rows] index. Multiple rows per MATNR allowed."""
    idx: dict[str, list[LoadedRow]] = {}
    for row in rows:
        m = _matnr_str(row.get("MATNR"))
        if m is None:
            # Row with no MATNR is skipped silently — real-world files
            # sometimes have trailing annotation rows. The MAIN file's
            # missing-MATNR check happens below with a stronger handling.
            continue
        idx.setdefault(m, []).append(row)
    return idx


def merge(
    main_file: LoadedFile | None,
    alt_uom_file: LoadedFile | None = None,
    longtext_file: LoadedFile | None = None,
) -> MergeResult:
    """Join the 3 files into per-material records.

    Args:
        main_file: required. If None, returns an empty result.
        alt_uom_file: optional. If None, no alt UoM data is attached.
        longtext_file: optional. If None, no long text data is attached.

    Returns a MergeResult. See dataclass doc for fields.

    Key logic: the main file may have multiple rows per MATNR. We classify
    them:
      - Same (MATNR, WERKS) pair appearing 2+ times → REAL duplicate,
        reported in duplicate_matnrs_in_main, first wins.
      - Same MATNR across DIFFERENT WERKS → legitimate multi-plant
        material. All rows preserved in plant_rows; material_count
        counts distinct MATNRs, not rows.
    """
    if main_file is None:
        return MergeResult(
            materials=[],
            summary={"error": "No main file provided"},
        )

    # ── Pass 1: build a MATNR → [rows] index for each file ──
    main_idx = _index_by_matnr(main_file.rows)
    alt_idx = _index_by_matnr(alt_uom_file.rows) if alt_uom_file else {}
    lt_idx = _index_by_matnr(longtext_file.rows) if longtext_file else {}

    # ── Pass 2: classify main-file occurrences by (MATNR, WERKS) ──
    # For each MATNR:
    #   - Group its rows by WERKS
    #   - Within a WERKS group, multiple rows = real duplicates (keep first)
    #   - Across WERKS groups = legitimate multi-plant fanout
    duplicates: list[tuple[str, list[int]]] = []
    multi_plant: list[tuple[str, list[str]]] = []
    materials: list[MergedMaterial] = []

    for matnr, rows in main_idx.items():
        # Sub-index by WERKS within this MATNR
        by_werks: dict[str, list[LoadedRow]] = {}
        for r in rows:
            werks = _werks_str(r.get("WERKS"))
            by_werks.setdefault(werks, []).append(r)

        # Within each WERKS bucket, pick the first row; 2+ is a duplicate
        plant_rows: list[LoadedRow] = []
        for werks, same_plant_rows in by_werks.items():
            plant_rows.append(same_plant_rows[0])
            if len(same_plant_rows) > 1:
                # Real duplicate — same material, same plant, multiple rows
                dup_excel_rows = [r.excel_row for r in same_plant_rows]
                # Use a tag that tells SMEs WHERE the dup is
                dup_key = f"{matnr} @ WERKS='{werks or '(blank)'}'"
                duplicates.append((dup_key, dup_excel_rows))

        # Flag multi-plant for reporting (≥2 distinct plants for this MATNR)
        distinct_plants = [w for w in by_werks.keys() if w]
        if len(distinct_plants) >= 2:
            multi_plant.append((matnr, sorted(distinct_plants)))

        # First plant row becomes the "basic data" anchor for validators
        # that don't care about plant scope. (All plant rows share the same
        # basic-data fields; only plant-scoped fields differ.)
        primary = plant_rows[0]

        mat = MergedMaterial(
            matnr=matnr,
            main=primary,
            plant_rows=plant_rows,
            alt_uoms=alt_idx.get(matnr, []),
            longtexts=lt_idx.get(matnr, []),
        )
        materials.append(mat)

    # ── Pass 3: find orphans (alt_uom/longtext with no main record) ──
    orphans_alt: list[LoadedRow] = []
    for matnr, rows in alt_idx.items():
        if matnr not in main_idx:
            orphans_alt.extend(rows)

    orphans_lt: list[LoadedRow] = []
    for matnr, rows in lt_idx.items():
        if matnr not in main_idx:
            orphans_lt.extend(rows)

    # ── Pass 3b: detect wrong-file-pair uploads ──
    # If the alt/lt files reference NO materials in common with main, the
    # user probably grabbed the wrong files (e.g. FG alt/lt uploaded with
    # an RM/PM main file — real Peenya case). Surface as prominent warning.
    warning_msgs = []
    alt_rows_total = len(alt_uom_file.rows) if alt_uom_file else 0
    lt_rows_total = len(longtext_file.rows) if longtext_file else 0
    alt_match_count = alt_rows_total - len(orphans_alt)
    lt_match_count = lt_rows_total - len(orphans_lt)

    if alt_rows_total > 0 and alt_match_count == 0:
        warning_msgs.append(
            f"Alt-UoM file has {alt_rows_total:,} rows but NONE match a MATNR "
            f"in your main file. This is likely the wrong alt-UoM file for "
            f"this migration batch (check MATNR format: main uses "
            f"{next(iter(main_idx.keys()))[:5] + '...' if main_idx else '?'} "
            f"style, alt-UoM uses "
            f"{next(iter(alt_idx.keys()))[:5] + '...' if alt_idx else '?'} style)."
        )
    elif alt_rows_total > 0 and alt_match_count < alt_rows_total * 0.1:
        # <10% match — probably wrong file too
        warning_msgs.append(
            f"Alt-UoM file: only {alt_match_count}/{alt_rows_total} rows "
            f"match a main-file MATNR. {len(orphans_alt):,} orphans. "
            f"Verify you uploaded the correct pair."
        )

    if lt_rows_total > 0 and lt_match_count == 0:
        warning_msgs.append(
            f"Long-text file has {lt_rows_total:,} rows but NONE match a "
            f"MATNR in your main file. This is likely the wrong long-text "
            f"file for this migration batch."
        )
    elif lt_rows_total > 0 and lt_match_count < lt_rows_total * 0.1:
        warning_msgs.append(
            f"Long-text file: only {lt_match_count}/{lt_rows_total} rows "
            f"match a main-file MATNR. {len(orphans_lt):,} orphans."
        )

    file_pair_warning = " ".join(warning_msgs)

    # ── Pass 4: collect the union of all SAP fields seen in main ──
    # This drives the column set available for validation + LTMC output.
    field_inventory = set(main_file.sap_fields)

    # ── Pass 5: cheap stats for UI preview ──
    # Plants now come from plant_rows, not the single main row — so
    # Peenya's ['PE01','PE02','SC01'] shows up instead of just ['PE02'].
    plants: set[str] = set()
    for m in materials:
        for pr in m.plant_rows:
            w = _werks_str(pr.get("WERKS"))
            if w:
                plants.add(w)

    sales_orgs = {m.main.get("VKORG") for m in materials if m.main.get("VKORG")}
    distr_chls = {str(m.main.get("VTWEG")) for m in materials if m.main.get("VTWEG") is not None}
    mtypes = {m.main.get("MTART") for m in materials if m.main.get("MTART")}
    matgroups = {str(m.main.get("MATKL")) for m in materials if m.main.get("MATKL") is not None}

    # Count total plant-row pairs (how many LTMC Plant Data rows will emit)
    plant_row_total = sum(len(m.plant_rows) for m in materials)

    summary = {
        "material_count": len(materials),
        "plant_row_count": plant_row_total,      # total (material × plant) pairs
        "multi_plant_count": len(multi_plant),   # materials at 2+ plants
        "duplicate_count": len(duplicates),
        "orphan_alt_uom_count": len(orphans_alt),
        "orphan_longtext_count": len(orphans_lt),
        "alt_uom_total_rows": len(alt_uom_file.rows) if alt_uom_file else 0,
        "longtext_total_rows": len(longtext_file.rows) if longtext_file else 0,
        "distinct_plants": sorted(plants),
        "distinct_sales_orgs": sorted(sales_orgs),
        "distinct_distribution_channels": sorted(distr_chls),
        "distinct_material_types": sorted(mtypes),
        "distinct_material_groups": sorted(matgroups),
        "file_pair_warning": file_pair_warning,
        "files_loaded": {
            "main": main_file.filename,
            "alt_uom": alt_uom_file.filename if alt_uom_file else None,
            "longtext": longtext_file.filename if longtext_file else None,
        },
    }

    return MergeResult(
        materials=materials,
        orphans_in_alt_uom=orphans_alt,
        orphans_in_longtext=orphans_lt,
        duplicate_matnrs_in_main=duplicates,
        multi_plant_matnrs=multi_plant,
        file_pair_warning=file_pair_warning,
        field_inventory=field_inventory,
        summary=summary,
    )

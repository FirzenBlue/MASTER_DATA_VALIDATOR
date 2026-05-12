"""Cross-file validation between Main, Alt UoM, and Long Text MM files (v57).

When all 3 files are uploaded together, they're not independent — they
share MATNR as the join key, and Long Text additionally shares VKORG +
VTWEG. Inconsistencies between the 3 files cause SAP LTMC import to
fail (Alt UoM with unknown MATNR, Long Text in wrong sales org, etc.).

This module emits validator-shaped Error objects so the cross-file
problems flow through the normal decisions / review-xlsx pipeline.

Three checks:

1. **Orphan Alt UoM rows** — alt-uom row whose MATNR isn't in main.
   Severity: error. SAP would reject the alt-uom record outright.

2. **Orphan Long Text rows** — same idea, long-text row with unknown MATNR.
   Severity: error.

3. **Long Text sales-org mismatch** — long-text row references a MATNR
   that IS in main, but its SALES_ORG/DISTR_CHAL doesn't match main's
   VKORG/VTWEG for that MATNR. Severity: error. (Long Text is per-
   sales-area; if the sales area doesn't exist on the main material it
   has nothing to attach to.)

The errors get `sheet="AlternateUnits"` or `sheet="LongText"` so the
review xlsx and decisions UI can distinguish them from main-sheet
errors. Their `row_idx` points at the SOURCE row in the alt/lt file
(0-based), not the main-file material index — important for the review
xlsx so we color the right cell.

Why not put this in mm_validator.py: that file iterates RULES against
each MergedMaterial. Cross-file checks need access to the orphan
lists in MergeResult and the long-text rows that DO match (for sales-
area cross-check). Different shape; cleaner as its own module.
"""
from __future__ import annotations

from typing import Any

from .mm_merger import MergeResult, _matnr_str
from .mm_validator import Error


def _str(v: Any) -> str:
    """Stringify a cell value, treating None / blank / NaN as ''."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s


def _matnr(v: Any) -> str:
    """MATNR-aware string conversion. Treats large numerics like
    8903837589095.0 as 8903837589095 to match how the merger indexes
    main MATNRs. Blank → ''. (Without this, alt-uom/longtext MATNRs
    that arrive as floats from python_calamine never match main
    MATNRs that arrive as either ints or strings — every row would
    look orphaned.)
    """
    return _matnr_str(v) or ""


def validate_cross_file(merged: MergeResult,
                        alt_uom_rows: list | None = None,
                        longtext_rows: list | None = None,
                        catalogs: dict | None = None) -> list[Error]:
    """Emit cross-file validation errors.

    Args:
        merged: MergeResult from mm_merger.merge()
        alt_uom_rows: full list of alt-uom LoadedRows (so we can flag the
                      orphans by their original row position).
        longtext_rows: full list of long-text LoadedRows.
        catalogs: optional KDS catalogs dict. When provided, this enables
                  the v66 ISO UoM checks: alt-UoM's MEINH and main's MEINS
                  are validated against the iso_uom catalog. Without the
                  catalog (older callers), these checks are silently
                  skipped to keep backward compatibility.

    Returns: list of Error, one per problem cell. Errors carry:
        - sheet: "AlternateUnits" or "LongText" or "Materials" (for MEINS)
        - row_idx: 0-based index into the alt-uom/longtext list
        - sap_field: the field where the problem is (MATNR for orphans,
                     SALES_ORG/DISTR_CHAL for sales-area mismatch,
                     MEINH/MEINS for UoM checks)
        - rule_id, rule_name, severity, message, value, suggested_value
    """
    errors: list[Error] = []
    catalogs = catalogs or {}
    iso_uom = catalogs.get("iso_uom") or {}

    # v66.1: Prebuild the suggested_options list for UoM rules so SMEs
    # see a dropdown of every valid ISO code in Group & Replace and in
    # the per-error suggestion dropdown — instead of having to type a
    # freeform value. The list is the canonical {value, label} shape
    # the decision_engine/main passes through to the frontend
    # `catalog_sample`. We dedupe by code (internal UoM) since the ISO
    # file has duplicate rows when the 3-char and 6-char codes overlap
    # the Internal UoM. Sorted alphabetically so the dropdown is easy
    # to scan.
    iso_options: list[dict] = []
    if iso_uom:
        seen_codes = set()
        for code, internal in iso_uom.items():
            if internal in seen_codes:
                continue
            seen_codes.add(internal)
            label = internal if internal == code else f"{internal} ({code})"
            iso_options.append({"value": internal, "label": label})
        iso_options.sort(key=lambda o: o["value"])

    # Build MATNR → main MEINS map once (needed for the MEINS/MEINH
    # comparison check below). Empty when main file has no MEINS column.
    main_meins_by_matnr: dict[str, str] = {}
    for m in merged.materials:
        meins = _str(m.main.values.get("MEINS"))
        if meins:
            main_meins_by_matnr[m.matnr] = meins

    # v66: Validate main file's MEINS against ISO UoM catalog.
    # MEINS is the base unit of measure for each material — must be a
    # valid ISO unit (BOX, PC, KG, etc.). Invalid units MAY be rejected
    # by SAP LTMC. Severity is "warning" (not error) because the bundled
    # ISO catalog is a tentative reference list — codes that look invalid
    # by our list might still be valid at the customer's SAP instance
    # (e.g. BOX is widely used at Healthium but not in the tentative file).
    # We emit one warning per material with an unrecognized MEINS so the
    # SME can review and either add to the catalog or confirm it's site-valid.
    if iso_uom:
        seen_meins = set()  # dedupe: report each unique bad MEINS once per material
        for m in merged.materials:
            meins = _str(m.main.values.get("MEINS"))
            if not meins:
                continue
            if meins in iso_uom:
                continue
            # Unrecognized MEINS — flag as warning on the main material row
            errors.append(_make_error(
                sheet="Materials",
                row_idx=merged.materials.index(m),
                excel_row=m.source_excel_row,
                sap_field="MEINS",
                value=meins,
                rule_id="mm_main_meins_not_in_iso",
                rule_name="Base UoM (MEINS) is not in ISO catalog",
                message=f"Base Unit of Measure '{meins}' for MATNR '{m.matnr}' "
                        f"is not in the bundled ISO Unit of Measure catalog. "
                        f"This may still be a valid SAP UoM at your site — "
                        f"if so, add '{meins}' to ISO_Unit_Of_Measure_tentative_file.xlsx. "
                        f"Otherwise change MEINS to a valid ISO code like "
                        f"PC, KG, M, L, EA, DZ, PAC.",
                severity="warning",
                suggested_options=iso_options,
            ))

    # ── 1. Alt UoM orphans ──────────────────────────────────────────
    if alt_uom_rows:
        # Build MATNR set from main once
        main_matnrs = {m.matnr for m in merged.materials}
        for row_idx, row in enumerate(alt_uom_rows):
            matnr = _matnr(row.values.get("MATNR"))
            if not matnr:
                # Blank MATNR is also an orphan-like problem
                errors.append(_make_error(
                    sheet="AlternateUnits",
                    row_idx=row_idx,
                    excel_row=getattr(row, "excel_row", row_idx + 3),
                    sap_field="MATNR",
                    value="",
                    rule_id="mm_alt_uom_matnr_blank",
                    rule_name="Alt UoM: MATNR is blank",
                    message="MATNR cannot be blank in the Alt UoM file. "
                            "Each alt-UoM row must reference a material from the main file.",
                    severity="error",
                ))
                continue
            if matnr not in main_matnrs:
                errors.append(_make_error(
                    sheet="AlternateUnits",
                    row_idx=row_idx,
                    excel_row=getattr(row, "excel_row", row_idx + 3),
                    sap_field="MATNR",
                    value=matnr,
                    rule_id="mm_alt_uom_orphan_matnr",
                    rule_name="Alt UoM: MATNR not in main file",
                    message=f"MATNR '{matnr}' in Alt UoM file does not exist "
                            f"in the main material file. SAP will reject this "
                            f"alt-UoM record. Either add the material to the "
                            f"main file, or remove this alt-UoM row.",
                    severity="error",
                ))
                continue

            # v66: MEINH validation — three checks on the alt-UoM unit:
            #   1. Must be a valid ISO unit code (in iso_uom catalog)
            #   2. Must NOT equal main file's MEINS (redundant alt-UoM)
            #   3. Display the conversion factor in the message so the SME
            #      can see the BOX ↔ PC mapping at a glance.
            meinh = _str(row.values.get("MEINH"))
            umrez = _str(row.values.get("UMREZ"))
            umren = _str(row.values.get("UMREN"))
            main_meins = main_meins_by_matnr.get(matnr, "")

            if meinh:
                # Check 1: MEINH should be a valid ISO unit. Warning level
                # (not error) because the bundled ISO list is tentative —
                # an unknown code might still be valid at the customer's
                # SAP. Surface it so SME can review.
                if iso_uom and meinh not in iso_uom:
                    errors.append(_make_error(
                        sheet="AlternateUnits",
                        row_idx=row_idx,
                        excel_row=getattr(row, "excel_row", row_idx + 3),
                        sap_field="MEINH",
                        value=meinh,
                        rule_id="mm_alt_uom_meinh_not_in_iso",
                        rule_name="Alt UoM: MEINH is not in ISO catalog",
                        message=f"Alt unit '{meinh}' for MATNR '{matnr}' is "
                                f"not in the bundled ISO Unit of Measure "
                                f"catalog. This may still be valid at your "
                                f"SAP site — if so, add '{meinh}' to "
                                f"ISO_Unit_Of_Measure_tentative_file.xlsx. "
                                f"Otherwise change MEINH to a valid ISO "
                                f"code like PC, KG, M, L, EA, DZ, PAC.",
                        severity="warning",
                        suggested_options=iso_options,
                    ))
                # Check 2: MEINH must NOT equal main MEINS (would be redundant)
                elif main_meins and meinh == main_meins:
                    errors.append(_make_error(
                        sheet="AlternateUnits",
                        row_idx=row_idx,
                        excel_row=getattr(row, "excel_row", row_idx + 3),
                        sap_field="MEINH",
                        value=meinh,
                        rule_id="mm_alt_uom_meinh_equals_meins",
                        rule_name="Alt UoM: MEINH is same as MEINS (redundant)",
                        message=f"Alt unit '{meinh}' is the same as the base "
                                f"unit (MEINS='{main_meins}') for MATNR "
                                f"'{matnr}'. Alt UoMs must be DIFFERENT from "
                                f"the base unit — they define alternative "
                                f"measurement units, not duplicate the base. "
                                f"Remove this row, or change MEINH to a "
                                f"different ISO unit code.",
                        severity="error",
                    ))
                # Check 3: For valid pairs, optionally log the mapping
                # as a low-priority "info" so SME can verify the conversion.
                # We don't emit an error — this is just visibility. The
                # mapping shows up in the Alt UoMs section of the Records
                # editor (read-only) and in the Review xlsx.
                # No error emitted for the legitimate alt UoM case.

        # ── v66: MEINS (main) vs MEINH (alt UoM) cross-check ────────────
        # SME requirement (May 2026): "in the main file the 'BOX' is present
        # in another file 'PC' is present it is an error but your not showing".
        # The SME is treating MEINS (main file base unit) and MEINH (alt UoM
        # alternative unit) as the same concept and expects them to match
        # for a given MATNR. In SAP terms these are intentionally different
        # (MEINH represents an ALTERNATIVE to the base unit, with UMREZ/UMREN
        # defining the conversion factor — e.g. 12 PC = 1 BOX). We surface
        # the difference as a WARNING (not blocking error) so the SME can
        # review and confirm the conversion factor is correct, without
        # blocking the LTMC export of materials where alt UoMs are legit.
        #
        # Build MATNR → MEINS map from main file's first plant row.
        if alt_uom_rows:
            main_meins_by_matnr: dict[str, str] = {}
            for mat in merged.materials:
                meins = _str(mat.main.values.get("MEINS"))
                if meins:
                    main_meins_by_matnr[mat.matnr] = meins

            for row_idx, row in enumerate(alt_uom_rows):
                matnr = _matnr(row.values.get("MATNR"))
                if not matnr or matnr not in main_meins_by_matnr:
                    # Blank MATNR or orphan — already flagged above; skip
                    # the UoM check to avoid double-firing on the same row.
                    continue
                meinh = _str(row.values.get("MEINH"))
                if not meinh:
                    continue
                main_meins = main_meins_by_matnr[matnr]
                # Compare uppercased so 'pc' vs 'PC' doesn't false-positive.
                if meinh.strip().upper() != main_meins.strip().upper():
                    # Show conversion factor if present so SME can sanity-check.
                    umrez = _str(row.values.get("UMREZ"))
                    umren = _str(row.values.get("UMREN"))
                    conv_hint = ""
                    if umrez and umren:
                        conv_hint = f" Conversion: {umrez} {meinh} = {umren} {main_meins}."
                    errors.append(_make_error(
                        sheet="AlternateUnits",
                        row_idx=row_idx,
                        excel_row=getattr(row, "excel_row", row_idx + 3),
                        sap_field="MEINH",
                        value=meinh,
                        rule_id="mm_alt_uom_meinh_vs_meins_diff",
                        rule_name="Alt UoM: unit differs from main file base unit",
                        message=(
                            f"MATNR '{matnr}': Alt UoM file has MEINH='{meinh}' "
                            f"but main file has MEINS='{main_meins}'. "
                            f"If MEINH is meant to be an ALTERNATIVE unit, this "
                            f"is normal — confirm the conversion is right.{conv_hint} "
                            f"If MEINH was supposed to match the base unit, "
                            f"update the Alt UoM file to '{main_meins}' or fix "
                            f"the main file's MEINS to '{meinh}'."
                        ),
                        severity="warning",
                        suggested_value=main_meins,
                        # v66.1: full ISO catalog as dropdown options.
                        # The Group & Replace modal renders these as a
                        # <select> so SMEs pick from valid units instead
                        # of typing. suggested_value (main_meins) is the
                        # primary suggestion — shown as a chip below the
                        # input — but the full list lets them choose any
                        # alternative they want (M, KG, L, DZ, PAC, etc.)
                        suggested_options=iso_options,
                    ))

    # ── 2. Long Text orphans + sales-area cross-check ───────────────
    if longtext_rows:
        # Build MATNR → (VKORG, VTWEG) map from main file's first plant
        # row of each material. If a material has multiple sales areas in
        # main (multi-plant), accept any of them.
        main_sales_areas: dict[str, set[tuple[str, str]]] = {}
        main_matnrs = set()
        for mat in merged.materials:
            main_matnrs.add(mat.matnr)
            areas = set()
            # Sales-area is on the main row (basic data) — VKORG/VTWEG
            # are typically not plant-scoped, but check all plant rows
            # to be safe (some templates put sales-area on plant rows).
            for r in [mat.main] + list(mat.plant_rows):
                vkorg = _str(r.values.get("VKORG"))
                vtweg = _str(r.values.get("VTWEG"))
                if vkorg or vtweg:
                    areas.add((vkorg, vtweg))
            main_sales_areas[mat.matnr] = areas

        for row_idx, row in enumerate(longtext_rows):
            matnr = _matnr(row.values.get("MATNR"))
            if not matnr:
                errors.append(_make_error(
                    sheet="LongText",
                    row_idx=row_idx,
                    excel_row=getattr(row, "excel_row", row_idx + 3),
                    sap_field="MATNR",
                    value="",
                    rule_id="mm_longtext_matnr_blank",
                    rule_name="Long Text: MATNR is blank",
                    message="MATNR cannot be blank in the Long Text file.",
                    severity="error",
                ))
                continue

            if matnr not in main_matnrs:
                # Orphan: MATNR doesn't exist in main at all
                errors.append(_make_error(
                    sheet="LongText",
                    row_idx=row_idx,
                    excel_row=getattr(row, "excel_row", row_idx + 3),
                    sap_field="MATNR",
                    value=matnr,
                    rule_id="mm_longtext_orphan_matnr",
                    rule_name="Long Text: MATNR not in main file",
                    message=f"MATNR '{matnr}' in Long Text file does not exist "
                            f"in the main material file. SAP will reject this "
                            f"long-text record. Either add the material to "
                            f"the main file, or remove this long-text row.",
                    severity="error",
                ))
                continue

            # Sales-area cross-check. The long-text file column names vary;
            # we accept either SALES_ORG/DISTR_CHAL (the test file's names)
            # or VKORG/VTWEG (the canonical SAP names).
            lt_vkorg = _str(row.values.get("SALES_ORG") or row.values.get("VKORG"))
            lt_vtweg = _str(row.values.get("DISTR_CHAL") or row.values.get("VTWEG"))

            if not lt_vkorg and not lt_vtweg:
                # Long-text without sales-area is OK in some configurations
                # (Basic Long Text doesn't need it). Don't flag.
                continue

            valid_areas = main_sales_areas.get(matnr, set())
            if not valid_areas:
                # Main has the MATNR but no VKORG/VTWEG on file. Soft
                # warning rather than hard error — could be a matnr-only
                # main file.
                errors.append(_make_error(
                    sheet="LongText",
                    row_idx=row_idx,
                    excel_row=getattr(row, "excel_row", row_idx + 3),
                    sap_field="SALES_ORG"
                              if "SALES_ORG" in row.values else "VKORG",
                    value=lt_vkorg,
                    rule_id="mm_longtext_sales_area_unverified",
                    rule_name="Long Text: sales area can't be verified",
                    message=f"MATNR '{matnr}' has no VKORG/VTWEG in the "
                            f"main file, so the long-text sales area "
                            f"({lt_vkorg}/{lt_vtweg}) can't be verified.",
                    severity="warning",
                ))
                continue

            # Check (lt_vkorg, lt_vtweg) is in main's set
            if (lt_vkorg, lt_vtweg) not in valid_areas:
                # Build a helpful message showing the valid areas
                areas_str = ", ".join(
                    f"{v}/{t}" for v, t in sorted(valid_areas)
                ) or "(none)"
                # Pick the field to flag — if VKORG mismatches we flag VKORG,
                # else VTWEG. If both mismatch we flag VKORG (parent).
                main_vkorgs = {v for v, _ in valid_areas}
                if lt_vkorg not in main_vkorgs:
                    flag_field = "SALES_ORG" if "SALES_ORG" in row.values else "VKORG"
                    flag_value = lt_vkorg
                else:
                    flag_field = "DISTR_CHAL" if "DISTR_CHAL" in row.values else "VTWEG"
                    flag_value = lt_vtweg

                errors.append(_make_error(
                    sheet="LongText",
                    row_idx=row_idx,
                    excel_row=getattr(row, "excel_row", row_idx + 3),
                    sap_field=flag_field,
                    value=flag_value,
                    rule_id="mm_longtext_sales_area_mismatch",
                    rule_name="Long Text: sales area doesn't match main file",
                    message=f"MATNR '{matnr}' has sales area "
                            f"{lt_vkorg}/{lt_vtweg} in Long Text, but main "
                            f"file has {areas_str}. Long-text records attach "
                            f"to specific sales areas; mismatched ones won't "
                            f"upload to SAP.",
                    severity="error",
                    suggested_value=(next(iter(valid_areas))[0]
                                     if flag_field in ("SALES_ORG", "VKORG")
                                     else next(iter(valid_areas))[1])
                                    if len(valid_areas) == 1 else None,
                ))

    return errors


def _make_error(*, sheet: str, row_idx: int, excel_row: int, sap_field: str,
                value: str, rule_id: str, rule_name: str, message: str,
                severity: str, suggested_value: str | None = None,
                suggested_options: list[dict] | None = None) -> Error:
    """Build an Error in the same shape mm_validator emits.

    Cross-file errors don't have a col_idx in the main sheet (different
    sheet entirely), so we set col_idx=-1 as a sentinel. The review
    xlsx and decisions UI handle col_idx<0 by falling back to looking
    up the column position in the alt/lt sheet's header layout.

    v66.1: suggested_options is the canonical {value, label} list the
    Group-and-Replace dropdown and the per-error suggestion dropdown both
    consume. For UoM rules we populate it from the ISO catalog so SMEs
    can pick from a known-valid list instead of typing freeform.
    """
    return Error(
        sheet=sheet,
        row_idx=row_idx,
        xml_row=excel_row,
        col_idx=-1,
        column_label=sap_field,
        sap_field=sap_field,
        value=value,
        rule_id=rule_id,
        rule_name=rule_name,
        severity=severity,
        message=message,
        suggested_value=suggested_value,
        suggested_options=suggested_options or [],
    )

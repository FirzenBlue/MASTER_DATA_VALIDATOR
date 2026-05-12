"""
MM validator — applies checklist + KDS rules to MergedMaterial rows and
emits Error objects (same model as Customer validator, so the frontend
Decisions / Group & Replace / Fix Individually UI works unchanged).

Entry point:
    validate_mm(materials, catalogs) -> list[Error]

Design:
  - Each rule.kind maps to a handler function (HANDLERS dict below).
  - Handlers return 0+ Error objects for a single material + field combo.
  - A rule whose requires_catalog is empty in `catalogs` is skipped. This
    is how we run without Division/Profit Center catalogs yet.
  - All errors use sheet="Materials" since MM's merged view isn't really
    sheet-based — the Customer validator used sheet names for its
    multi-sheet file but here everything is one conceptual table.
  - row_idx is the material's 0-indexed position in the list, xml_row is
    the Excel row from the main file so SMEs can find it.
  - col_idx is synthetic: we use the SAP field's position in main.sap_fields
    so the record editor can point to the right column.
"""
from __future__ import annotations

from typing import Any

from services.mm_merger import MergedMaterial
from services.mm_checklist import ALL_RULES, ChecklistRule
from services.validator import Error


# ─── Cell-value normalisation ─────────────────────────────────────────────

def _as_str(v: Any) -> str:
    """Value → string for comparisons. None/empty → empty string.

    Handles:
      - None → ""
      - int/float that's whole → "N" (not "N.0")
      - bool → "X" for True, "" for False (SAP indicator convention)
      - whitespace-only strings → ""
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "X" if v else ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    return s


def _is_blank(v: Any) -> bool:
    """True if the value should count as "missing" for a mandatory check."""
    return _as_str(v) == ""


# ─── Helpers for Error construction ───────────────────────────────────────

def _col_idx_for(main_fields: list[str], sap_code: str) -> int:
    """Return 1-based column index of SAP code within the main file's
    header. If the field is not in the original main file (e.g. SPRAS,
    ALAND, WAERS, CURTP, BWKEY — fields that LTMC requires but source
    doesn't typically carry), assign a unique synthetic column index
    beyond the original column count.

    This is critical for the frontend record editor: it keys edits by
    col_idx (record.edits[err.col_idx] = ...). If two errors shared the
    same col_idx, the user's edit would propagate to all of them and
    the screen would show the same value in every box. The synthetic
    indexes give each new SAP field its own unique key.

    The synthetic indexes are deterministic per (main_fields, sap_code)
    so the same field always gets the same col_idx — important for
    cross-request consistency (URL hash, test reproducibility).
    """
    try:
        return main_fields.index(sap_code) + 1
    except ValueError:
        # Field not in source — synthesize a stable unique index.
        # Use len(main_fields) + 1-based position in a fixed list of
        # known LTMC-extension fields. Order MUST be stable across runs.
        SYNTHETIC_FIELDS = ["SPRAS", "ALAND", "WAERS", "CURTP", "BWKEY",
                            "EKGRP", "EXTWG", "BERID", "LGNUM", "LGTYP",
                            "TATYP1", "TAXM1", "FSH_SEASON", "CLASS",
                            "CLASSTYPE", "ATNAM", "ART", "LIFNR"]
        if sap_code in SYNTHETIC_FIELDS:
            return len(main_fields) + 1 + SYNTHETIC_FIELDS.index(sap_code)
        # Truly unknown SAP code — extreme tail. Hash for determinism.
        return len(main_fields) + 1 + len(SYNTHETIC_FIELDS) + (abs(hash(sap_code)) % 10000)


# Module-level slot for the active friendly-label map (sap_code → label).
# Set by `validate_mm()` at the start of each call and cleared at the end.
# `_err()` reads it to populate Error.column_label with the friendly label
# from the source upload's row 1 instead of the SAP code. Falls back to
# sap_field when the map is empty (e.g. older callers that don't pass
# the labels arg).
#
# Module-level rather than threaded through every handler because the
# alternative — adding a `labels` arg to every handler signature — would
# touch ~20 functions for one piece of read-only context. The slot is
# only valid within a single `validate_mm()` call; it isn't shared
# across requests because validation is synchronous and single-threaded
# per request.
_ACTIVE_LABELS: dict[str, str] = {}


# Canonical SAP DDIC short text for fields that LTMC requires but
# customers' source uploads typically don't include (we add them during
# export). When the source has no friendly label for these codes, we
# fall back to these short canonical names instead of showing the raw
# SAP code in error messages and decisions.
#
# Keep this map small — only LTMC-standard fields. Customer-specific
# friendly labels should come from the source upload's row 1, not from
# this map. Adding too many entries here risks shadowing what the SME
# actually wrote in their template.
_CANONICAL_SAP_LABELS: dict[str, str] = {
    "BWKEY":  "Valuation Area",      # = WERKS at plant-level valuation
    "ALAND":  "Country",              # default 'IN' for HML
    "WAERS":  "Currency",             # default 'INR'
    "CURTP":  "Currency Type",        # default '10' (company code)
    "SPRAS":  "Language",             # default 'EN'
    "PEINH":  "Price Unit",           # paired with STPRS/VERPR
    "BWTAR":  "Valuation Type",       # split valuation, rare for HML
}



def _err(material: MergedMaterial, row_idx: int, rule: ChecklistRule,
         sap_field: str, value: Any, message: str,
         main_fields: list[str],
         suggested_value: str | None = None,
         suggested_options: list[dict] | None = None) -> Error:
    """Construct an Error for a failing rule on one material.

    row_idx is the material's 0-based index in the materials list; the
    Customer Error model wants row_idx + xml_row so the UI can map clicks
    to rows. We use xml_row = the Excel row number from the main file.

    column_label resolves to the friendly label from the source upload
    (e.g. "Sales org." for VKORG, "Material Description" for MAKTX) when
    `_ACTIVE_LABELS` is populated by the caller via `validate_mm()`. Falls
    back to the SAP code when no friendly label is available — that
    keeps the field identifiable even in degraded contexts.

    suggested_options: optional list of {"value", "label"} dicts — the
    full set of valid values for this field in this context, so the UI
    can render a dropdown. Populated by KDS handlers. Capped at 500
    entries; if the catalog is bigger the UI should treat this as
    "autocomplete candidates" rather than a fixed picker.
    """
    return Error(
        sheet="Materials",                    # single-table MM view
        row_idx=row_idx,
        xml_row=material.source_excel_row,    # Excel row in main file
        col_idx=_col_idx_for(main_fields, sap_field),
        column_label=_ACTIVE_LABELS.get(sap_field, sap_field),
        sap_field=sap_field,
        value=_as_str(value),
        rule_id=rule.rule_id,
        rule_name=rule.description,
        severity=rule.severity,
        message=message,
        suggested_value=suggested_value,
        suggested_options=suggested_options or [],
    )


# ─── Rule handlers ────────────────────────────────────────────────────────
#
# All handlers take (material, row_idx, rule, catalogs, main_fields) and
# return a list of Error objects. The list can be empty if no errors fire.

def _get_field(material: MergedMaterial, sap_field: str) -> Any:
    """Look up a single SAP field value on the material's main row."""
    return material.main.get(sap_field)


def _handle_mandatory(material, row_idx, rule, catalogs, main_fields):
    """Field must be present and non-blank.

    Skipped silently when the field's COLUMN isn't in the upload at all
    (main_fields is the column inventory). Otherwise, blank value fires
    an error. Reasoning: if the customer's template doesn't include
    e.g. QUALITY_ACTIVE as a column, firing a "blank QUALITY_ACTIVE" on
    every row would spam thousands of errors with no path to fix —
    the customer would have to add a column to their template just to
    silence the validator. Instead, treat absent columns as "not in
    scope for this customer".

    Note: when a column IS present but ALL rows have blank values,
    that's a real data gap (not a template gap) and we DO fire — the
    SME has the column and chose not to fill it. Only the truly-absent
    case is silenced.

    Message uses the friendly label when available so non-technical
    users see "Sales org. (VKORG) is mandatory but blank" instead of
    just "VKORG is mandatory but blank".
    """
    sap_field = rule.sap_field
    if isinstance(sap_field, str) and sap_field not in main_fields:
        return []
    val = _get_field(material, sap_field)
    if _is_blank(val):
        friendly = _ACTIVE_LABELS.get(sap_field, sap_field)
        if friendly != sap_field:
            msg = f"{friendly} ({sap_field}) is mandatory but blank"
        else:
            msg = f"{sap_field} is mandatory but blank"
        return [_err(material, row_idx, rule, sap_field, val, msg, main_fields)]
    return []


def _handle_max_length(material, row_idx, rule, catalogs, main_fields):
    val = _as_str(_get_field(material, rule.sap_field))
    max_len = rule.params["max_length"]
    if len(val) > max_len:
        return [_err(material, row_idx, rule, rule.sap_field, val,
                     f"Length {len(val)} exceeds {max_len}", main_fields,
                     suggested_value=val[:max_len])]
    return []


def _handle_exact_value(material, row_idx, rule, catalogs, main_fields):
    """Supports three param shapes:
        {"expected": "X"}              — must equal X (strict)
        {"allowed": ["S", "V"]}        — must be in allowed set
        {"expected": "X"}              — same as expected if only one value

    If sap_field is a list (e.g. ["SALES_MTVFP", "MRP3_MTVFP"]), check EACH
    field independently — don't conflate them into one error.
    """
    expected = rule.params.get("expected")
    allowed = rule.params.get("allowed")
    fields = rule.sap_field if isinstance(rule.sap_field, list) else [rule.sap_field]
    errors = []
    for sap_field in fields:
        val = _as_str(_get_field(material, sap_field))
        if not val:
            # Blank: only fail if rule's intent is "must equal X" (blank
            # doesn't equal X). But skip if field isn't present at all.
            if sap_field not in main_fields:
                continue
            if expected is not None:
                errors.append(_err(
                    material, row_idx, rule, sap_field, val,
                    f"{sap_field} must be '{expected}' but is blank",
                    main_fields, suggested_value=expected,
                ))
            continue
        if expected is not None and val != expected:
            errors.append(_err(
                material, row_idx, rule, sap_field, val,
                f"{sap_field} must be '{expected}' but is '{val}'",
                main_fields, suggested_value=expected,
            ))
        if allowed is not None and val not in allowed:
            errors.append(_err(
                material, row_idx, rule, sap_field, val,
                f"{sap_field} must be one of {', '.join(allowed)} but is '{val}'",
                main_fields,
            ))
    return errors


def _handle_valid_digit_count(material, row_idx, rule, catalogs, main_fields):
    """Check that the value is exactly N digits (no other characters)."""
    val = _as_str(_get_field(material, rule.sap_field))
    digits = rule.params["digits"]
    if not val:
        # Blank counts as missing; checklist lists this as mandatory.
        return [_err(material, row_idx, rule, rule.sap_field, val,
                     f"{rule.sap_field} is mandatory ({digits} digits)", main_fields)]
    if not val.isdigit() or len(val) != digits:
        return [_err(material, row_idx, rule, rule.sap_field, val,
                     f"{rule.sap_field} must be exactly {digits} digits; got '{val}' ({len(val)} chars)",
                     main_fields)]
    return []


def _catalog_to_options(catalog: dict, cap: int = 500) -> list[dict]:
    """Convert a flat catalog dict {code: description} → UI-ready options list.

    Sorts by code (stable, predictable for users). Capped at 500 entries
    so the response payload stays small even for big catalogs (material
    group has 451 entries). The UI can render all 500 in a searchable
    dropdown; beyond that it's better to use typeahead against a separate
    search endpoint.

    Returns [{"value": "PE01", "label": "HEALTHIUM MEDTECH LIMITED"}, ...]
    Label falls back to the code itself when description is empty.
    """
    if not catalog:
        return []
    items = sorted(catalog.items(), key=lambda kv: str(kv[0]))
    out = []
    for code, desc in items[:cap]:
        label = str(desc).strip() if desc else ""
        out.append({
            "value": str(code),
            "label": label or str(code),  # avoid empty labels — UI looks broken
        })
    return out


def _handle_kds_lookup(material, row_idx, rule, catalogs, main_fields):
    catalog_name = rule.params["catalog"]
    catalog = catalogs.get(catalog_name) or {}
    if not catalog:
        # No catalog loaded (pending data). Silently skip.
        return []
    # Pre-compute the options list once per rule+catalog. Same list goes
    # on every error for this rule — the UI dedups by rule_id when it
    # renders the dropdown for Group & Replace, so sending the list on
    # every error is safe and simpler than a separate endpoint.
    #
    # v70.1: If the loader pre-built a `<catalog>_options` list (currently
    # only Division does this — to expose per-sales-org variants like
    # "04 Arthroscopy (INO2)" vs "04 Arthoscopy (UK02)"), prefer that
    # list. Otherwise fall back to deriving options from the dict.
    prebuilt = catalogs.get(f"{catalog_name}_options")
    options = prebuilt if prebuilt else _catalog_to_options(catalog)
    fields = rule.sap_field if isinstance(rule.sap_field, list) else [rule.sap_field]
    errors = []

    # Plant-scoped fields must be checked on EVERY plant row (not just
    # material.main). The WERKS rule is the obvious case — a material at
    # PE01/PE02/SC01 has 3 different WERKS values to validate. We list
    # the SAP codes that are plant-scoped so each plant is checked.
    PLANT_SCOPED_FIELDS = {"WERKS", "LGORT", "LGPRO", "DISPO", "FEVOR"}

    for sap_field in fields:
        if sap_field in PLANT_SCOPED_FIELDS:
            # Iterate every plant row, dedupe (scope, value)
            seen = set()
            for plant_row in material.plant_rows:
                val = _as_str(plant_row.get(sap_field))
                if not val or val in seen:
                    continue
                seen.add(val)
                if val not in catalog:
                    errors.append(_err(
                        material, row_idx, rule, sap_field, val,
                        f"{sap_field} value '{val}' not in {catalog_name} KDS",
                        main_fields,
                        suggested_options=options,
                    ))
        else:
            # Plant-independent field — one check per material using main
            val = _as_str(_get_field(material, sap_field))
            if not val:
                continue  # blank → a different rule (mandatory) handles it
            if val not in catalog:
                errors.append(_err(
                    material, row_idx, rule, sap_field, val,
                    f"{sap_field} value '{val}' not in {catalog_name} KDS",
                    main_fields,
                    suggested_options=options,
                ))
    return errors


def _handle_kds_nested_lookup(material, row_idx, rule, catalogs, main_fields):
    """For catalogs keyed by scope: {WERKS: {LGORT: desc}}.

    Looks up value within material's scope_field value. Example:
        scope_field=WERKS, sap_field=LGORT — checks (PE01, FEU1).

    Multi-plant handling: this handler iterates the material's plant_rows
    so that a material assigned to PE01, PE02 AND SC01 gets validated
    once per plant. A bad LGPRO only at PE01 produces one error; a bad
    LGPRO at all three plants produces three errors (usually grouped
    into one "pattern" decision by the decision engine).

    The options list is computed PER-ERROR because it depends on the
    material's scope value. Each plant's error gets the options valid
    at THAT plant — PE01's 23 storage locations for a PE01 error, a
    different set for the SC01 error.
    """
    catalog_name = rule.params["catalog"]
    scope_field = rule.params["scope_field"]
    catalog = catalogs.get(catalog_name) or {}
    if not catalog:
        return []

    errors = []
    # Iterate every plant assignment. For single-plant materials this is
    # a 1-element list; for multi-plant materials like Peenya, 2-3
    # iterations. Each plant row has its OWN scope (WERKS) value and its
    # own sap_field value (LGPRO/LGORT can legitimately differ per plant).
    seen = set()  # dedupe: avoid double-firing if the same (scope, value) repeats
    for plant_row in material.plant_rows:
        scope = _as_str(plant_row.get(scope_field))
        val = _as_str(plant_row.get(rule.sap_field))
        if not val:
            continue
        if not scope:
            # No scope → can't do a scoped lookup; skip.
            continue
        key = (scope, val)
        if key in seen:
            continue
        seen.add(key)
        inner = catalog.get(scope) or {}
        if val not in inner:
            options = _catalog_to_options(inner)
            errors.append(_err(
                material, row_idx, rule, rule.sap_field, val,
                f"{rule.sap_field} '{val}' not valid for {scope_field}='{scope}'",
                main_fields,
                suggested_options=options,
            ))
    return errors


def _handle_valclass_by_mtart(material, row_idx, rule, catalogs, main_fields):
    """BKLAS must match the expected valuation class for the MTART.

    Unlike other KDS rules, this one is 1-to-1 (each MTART has exactly
    ONE expected valuation class). So suggested_options is always a
    1-element list — but we still populate it so the UI treats KDS
    errors uniformly.
    """
    mapping = catalogs.get("valclass_by_mtart") or {}
    if not mapping:
        return []
    mtart = _as_str(_get_field(material, "MTART"))
    actual = _as_str(_get_field(material, "BKLAS"))
    if not mtart:
        return []  # different rule will catch missing MTART
    expected = mapping.get(mtart)
    if not expected:
        # Mapping doesn't know this MTART — don't fail on missing mapping.
        return []
    # Edge case: the KDS has "Not yet received from FI Business" for some types.
    # Treat those as "can't validate" rather than failing users.
    if "not yet" in expected.lower():
        return []
    # 1-element option list — same value both as code and label (pure SAP
    # class number like "7920"). Keeps UI rendering uniform with multi-
    # option KDS errors.
    options = [{"value": expected, "label": expected}]
    if not actual:
        return [_err(
            material, row_idx, rule, "BKLAS", actual,
            f"Valuation Class blank; expected '{expected}' for MTART '{mtart}'",
            main_fields, suggested_value=expected,
            suggested_options=options,
        )]
    if actual != expected:
        return [_err(
            material, row_idx, rule, "BKLAS", actual,
            f"Valuation Class '{actual}' does not match expected '{expected}' for MTART '{mtart}'",
            main_fields, suggested_value=expected,
            suggested_options=options,
        )]
    return []


def _handle_price_for_vprsv(material, row_idx, rule, catalogs, main_fields):
    """VPRSV=S requires STPRS; VPRSV=V requires VERPR.

    DEPRECATED in v68 — kept for backward compatibility with any rule
    config that still uses kind='price_for_vprsv'. New rules use the
    split pair `mm_stprs_required_when_vprsv_S` and
    `mm_verpr_required_when_vprsv_V`, each with kind='field_required_when_vprsv'
    so the Decisions UI shows a per-field title.
    """
    vprsv = _as_str(_get_field(material, "VPRSV"))
    if vprsv == "S":
        stprs = _as_str(_get_field(material, "STPRS"))
        if not stprs:
            return [_err(material, row_idx, rule, "STPRS", stprs,
                         "Standard price (STPRS) required when VPRSV=S", main_fields)]
    elif vprsv == "V":
        verpr = _as_str(_get_field(material, "VERPR"))
        if not verpr:
            return [_err(material, row_idx, rule, "VERPR", verpr,
                         "Moving average price (VERPR) required when VPRSV=V", main_fields)]
    # blank VPRSV is handled by rule 28
    return []


def _handle_field_required_when_vprsv(material, row_idx, rule, catalogs, main_fields):
    """v68: parameterised version of the VPRSV price-coupling rule.

    Each rule instance declares which VPRSV trigger value it watches
    (params.when ∈ {'S', 'V'}) and which field is required when that
    trigger fires (params.field ∈ {'STPRS', 'VERPR'}). Splitting the old
    combined rule (which used kind='price_for_vprsv') into two single-
    field rules gives the Decisions UI a focused per-field title:

      Old (one rule, two fields):
        "If VPRSV='S', STPRS required. If VPRSV='V', VERPR required."
        (shown on every error, regardless of which side fired)

      New (two rules, one field each):
        "Standard Price (STPRS) is required when Price Control (VPRSV) = 'S'"
        "Moving Price (VERPR) is required when Price Control (VPRSV) = 'V'"

    The error message also includes the current VPRSV value so SMEs see
    the dependency context inline (e.g. "VPRSV='S' on this row, but STPRS
    is blank — enter the standard price amount in INR like 100.50").
    """
    trigger = rule.params["when"]                # 'S' or 'V'
    target_field = rule.params["field"]          # 'STPRS' or 'VERPR'
    target_label = rule.params.get("label", target_field)  # friendly name

    vprsv = _as_str(_get_field(material, "VPRSV"))
    if vprsv != trigger:
        return []
    val = _as_str(_get_field(material, target_field))
    if val:
        return []
    return [_err(
        material, row_idx, rule, target_field, val,
        # Clear two-sentence message: WHY it fires, and WHAT to enter.
        # Naming both VPRSV and STPRS/VERPR by their friendly+code form
        # so SMEs (a) recognise the SAP codes from their files and
        # (b) understand the business meaning at the same time.
        f"Price Control (VPRSV) is '{trigger}' on this row, so "
        f"{target_label} ({target_field}) is required. Enter the price "
        f"amount in INR (e.g. '100.50'). Currently {target_field} is blank.",
        main_fields,
    )]


def _handle_iso_uom_check(material, row_idx, rule, catalogs, main_fields):
    """v69: Validate MEINS (base UoM) against the ISO UoM catalog.

    Pre-v69 the MEINS check only ran when the SME uploaded an alt-UoM file
    alongside main (it lived in mm_cross_file_validator). SMEs uploading
    just the main file got no UoM validation at all — invalid MEINS
    values slipped through into the LTMC export.

    This handler runs on the main validation path so MEINS is checked
    every time, regardless of whether alt-UoM/long-text files are also
    uploaded. Severity is WARNING (not error) because the bundled ISO
    catalog is tentative — codes that look invalid by our list may
    still be valid at the customer's SAP site. The SME can either add
    the code to the catalog or fix MEINS.
    """
    iso_catalog = catalogs.get("iso_uom") or {}
    if not iso_catalog:
        return []   # catalog not loaded — skip silently
    meins = _as_str(_get_field(material, "MEINS"))
    if not meins:
        return []   # blank handled by mandatory rules elsewhere
    if meins in iso_catalog:
        return []   # valid
    return [_err(
        material, row_idx, rule, "MEINS", meins,
        f"Base Unit of Measure '{meins}' for MATNR '{material.matnr}' is "
        f"not in the bundled ISO Unit of Measure catalog. This may still "
        f"be a valid SAP UoM at your site — if so, add '{meins}' to "
        f"ISO_Unit_Of_Measure_tentative_file.xlsx. Otherwise change "
        f"MEINS to a recognised code (PC, KG, M, L, EA, DZ, PAC, …).",
        main_fields,
    )]


def _handle_field_must_equal_when_other(material, row_idx, rule, catalogs, main_fields):
    """v69: emit error when other_field=trigger_value but this_field≠expected.

    Generic mechanism for SME-defined business coupling rules. Example
    instance from HML: "When MRP Type (DISMM) is 'HL', Material Status
    (MMSTA) must be 'Z1'." Declared as:

      params={
        "other_field": "DISMM",
        "other_value": "HL",
        "expected": "Z1",
        "other_label": "MRP Type",
        "this_label": "Material Status",
      }

    The rule fires when `other_field` carries `other_value` AND this
    rule's `sap_field` is not equal to `expected`. The error message
    names both fields by their friendly+code form so the SME sees the
    dependency at a glance.

    The handler iterates plant_rows because both DISMM and MMSTA are
    plant-scoped (HML's MRP setup varies per plant). Each mismatched
    plant_row yields one error so the SME can fix them individually
    or via Group & Replace.
    """
    other_field = rule.params["other_field"]
    trigger = rule.params["other_value"]
    expected = rule.params["expected"]
    this_label = rule.params.get("this_label", rule.sap_field)
    other_label = rule.params.get("other_label", other_field)

    errors = []
    for pr in material.plant_rows:
        other_val = _as_str(pr.get(other_field) or material.main.get(other_field))
        if other_val != trigger:
            continue
        this_val = _as_str(pr.get(rule.sap_field) or material.main.get(rule.sap_field))
        if this_val == expected:
            continue
        # Mismatch — emit one error per affected plant_row
        werks = _as_str(pr.get("WERKS") or material.main.get("WERKS"))
        loc_hint = f" on plant {werks}" if werks else ""
        errors.append(_err(
            material, row_idx, rule, rule.sap_field, this_val,
            f"{other_label} ({other_field}) is '{trigger}'{loc_hint}, so "
            f"{this_label} ({rule.sap_field}) must be '{expected}'. "
            f"Currently {rule.sap_field}='{this_val or '(blank)'}'.",
            main_fields,
        ))
    return errors


def _handle_conditional_mandatory(material, row_idx, rule, catalogs, main_fields):
    """When {when_field} ∈ {when_value_in}, {sap_field} must be filled."""
    when_field = rule.params["when_field"]
    when_value_in = rule.params["when_value_in"]
    trigger = _as_str(_get_field(material, when_field))
    if trigger not in when_value_in:
        return []
    val = _get_field(material, rule.sap_field)
    if _is_blank(val):
        return [_err(
            material, row_idx, rule, rule.sap_field, val,
            f"{rule.sap_field} required when {when_field}='{trigger}'",
            main_fields,
        )]
    return []


def _handle_at_least_one_filled(material, row_idx, rule, catalogs, main_fields):
    """At least one of a list of fields must be non-blank."""
    fields = rule.sap_field if isinstance(rule.sap_field, list) else [rule.sap_field]
    if any(not _is_blank(_get_field(material, f)) for f in fields):
        return []
    # All blank → emit one error on the first field so it shows up once.
    return [_err(
        material, row_idx, rule, fields[0], "",
        f"At least one of {', '.join(fields)} should be set",
        main_fields,
    )]


def _handle_exact_sequence(material, row_idx, rule, catalogs, main_fields):
    """Check fields match a fixed sequence of values, in order."""
    fields = rule.sap_field
    expected = rule.params["expected"]
    errors = []
    for sap_field, expected_val in zip(fields, expected):
        val = _as_str(_get_field(material, sap_field))
        if val != expected_val:
            errors.append(_err(
                material, row_idx, rule, sap_field, val,
                f"{sap_field} must be '{expected_val}'; got '{val}'",
                main_fields, suggested_value=expected_val,
            ))
    return errors


def _handle_mandatory_with_default(material, row_idx, rule, catalogs, main_fields):
    """Field is LTMC-mandatory but typically absent from source files.
    Emits an error with a canonical suggested value so SMEs one-click-fix.

    Used for fields like SPRAS='EN', ALAND='IN', WAERS='INR', CURTP='10'
    — fields where the correct value is unambiguous for Healthium's
    context (Indian company, English migration). If the source already
    has a value, no error fires.

    Expected params: {"default": "EN"}
    """
    val = _get_field(material, rule.sap_field)
    if not _is_blank(val):
        return []
    default = rule.params["default"]
    return [_err(
        material, row_idx, rule, rule.sap_field, "",
        f"{rule.sap_field} is required by LTMC but missing from source. "
        f"Apply default '{default}' for Healthium's standard configuration.",
        main_fields, suggested_value=default,
    )]


def _handle_bwkey_from_werks(material, row_idx, rule, catalogs, main_fields):
    """BWKEY (Valuation Area) derivation check.

    LTMC Valuation Data requires BWKEY per (material × plant). For
    Healthium's SAP config, valuation area is at PLANT level (not
    company-code level), which means BWKEY = WERKS for every plant
    row — same value as the Plant column.

    Per SME spec (May 2026):
        "for the valuation area you need to give suggestion that is
         same value should be present in the plant column"

    So when BWKEY is missing or doesn't match WERKS, the suggested
    value is the plant's WERKS code itself. (e.g. WERKS=PE01 →
    suggest BWKEY=PE01.)

    The KDS plant map is no longer used to derive BWKEY (it had
    SAP-target plant codes like 1010, but the customer's source uses
    proposed plant codes like PE01 — we want to suggest what's IN
    the file, not what the destination SAP system expects, since the
    plant-code translation happens elsewhere).

    Runs per-plant (iterates material.plant_rows) so a multi-plant
    material gets one error per plant — each with its OWN suggested
    BWKEY matching that plant.

    If WERKS is itself blank, no suggestion (a different rule catches
    that — `werks_blank`).
    """
    errors = []
    seen = set()
    for plant_row in material.plant_rows:
        werks = _as_str(plant_row.get("WERKS"))
        if not werks or werks in seen:
            continue
        seen.add(werks)
        current = _as_str(plant_row.get("BWKEY"))
        if not current:
            # Missing — suggest BWKEY = WERKS
            errors.append(_err(
                material, row_idx, rule, "BWKEY", "",
                f"Valuation Area (BWKEY) missing for Plant '{werks}'. "
                f"For plant-level valuation, BWKEY equals the Plant code "
                f"— suggested value: '{werks}'.",
                main_fields, suggested_value=werks,
            ))
        elif current != werks:
            # Present but doesn't match WERKS
            errors.append(_err(
                material, row_idx, rule, "BWKEY", current,
                f"Valuation Area (BWKEY) is '{current}' but Plant (WERKS) "
                f"is '{werks}'. For plant-level valuation these should "
                f"match — suggested value: '{werks}'.",
                main_fields, suggested_value=werks,
            ))
    return errors


def _handle_numeric_only(material, row_idx, rule, catalogs, main_fields):
    """Value, if present, should be all digits. Blank is ignored (a separate
    mandatory rule handles missing values)."""
    val = _as_str(_get_field(material, rule.sap_field))
    if not val:
        return []
    if not val.isdigit():
        return [_err(
            material, row_idx, rule, rule.sap_field, val,
            f"{rule.sap_field} should be numeric (got '{val}')",
            main_fields,
        )]
    return []


def _handle_non_negative_number(material, row_idx, rule, catalogs, main_fields):
    """Value, if present, must parse as number ≥ 0. Blank passes (other
    rules may require the value to be present).

    v68: STPRS/VERPR get a tailored error message that explains the
    business meaning, because SMEs were typing 'S' or 'V' (the Price
    Control codes) into the Standard Price / Moving Price field by
    mistake. The price columns hold *currency amounts*, not the
    'S'/'V' codes — those go in VPRSV. We name both columns clearly to
    eliminate the confusion.
    """
    raw = _get_field(material, rule.sap_field)
    if raw is None or raw == "":
        return []
    try:
        n = float(raw)
    except (TypeError, ValueError):
        sap_field = rule.sap_field
        bad = _as_str(raw)
        # v68: special-case STPRS/VERPR for clearer guidance
        if sap_field in ("STPRS", "VERPR"):
            friendly = "Standard Price" if sap_field == "STPRS" else "Moving Price"
            note = ""
            if bad.strip().upper() in ("S", "V"):
                note = (f" (Note: '{bad}' looks like a Price Control code — "
                        f"that letter belongs in the VPRSV field, not in "
                        f"{sap_field}. {sap_field} holds the price amount.)")
            msg = (f"{friendly} ({sap_field}) is '{bad}' but must be a numeric "
                   f"amount (e.g. '100.50').{note}")
        else:
            msg = f"{sap_field} '{bad}' is not a number"
        return [_err(material, row_idx, rule, sap_field, bad, msg, main_fields)]
    if n < 0:
        return [_err(
            material, row_idx, rule, rule.sap_field, _as_str(raw),
            f"{rule.sap_field} must be ≥ 0 (got {n})",
            main_fields,
        )]
    return []


def _handle_matnr_range(material, row_idx, rule, catalogs, main_fields):
    """MATNR must conform to the number range defined for its MTART.

    The Material Type KDS sheet defines per-MTART range config (from/to
    bounds + whether the range is numeric or alphanumeric). Cases:

    1. Numeric range (e.g. ZFRT: 1e12..9.99e12, ZRMI: 2e9..2.099e9)
       - MATNR must parse as an integer
       - MATNR integer value must be within [from, to]
       - Length must match (leading zeros are OK — both 10 and "0010" are
         in range but SAP stores the text form)

    2. Alphanumeric range (e.g. ZHLB: A..ZZZZZZZZ)
       - MATNR must be at most `range_max_len` characters
       - MATNR must be alphanumeric (letters + digits only, no special chars)
       - We don't strictly check the A..ZZZZZZZZ lexical bound since SAP
         accepts any alphanumeric string within length; the bound is
         nominal.

    Missing MTART on a material → silently skip (different rule catches
    the missing field). MTART not in the KDS → silently skip (don't fail
    on unknown material types; that's a data issue for a different rule).

    Error message names the MTART and its range so SMEs see WHY the MATNR
    was flagged. Example for 'TBC-2' on MTART=ZFRT:
        "MATNR 'TBC-2' doesn't conform to ZFRT's number range
         (External numeric 1000000000000..9999999999999)"
    """
    ranges = catalogs.get("mtart_ranges") or {}
    if not ranges:
        return []

    matnr = _as_str(_get_field(material, "MATNR"))
    mtart = _as_str(_get_field(material, "MTART"))
    if not matnr or not mtart:
        # Other rules catch blanks; don't pile on.
        return []

    cfg = ranges.get(mtart)
    if not cfg:
        # Unknown MTART — material_type_not_in_kds catches that
        return []

    rt = cfg["range_type"]
    if cfg["range_is_numeric"]:
        # Numeric range: MATNR must parse + be in [from, to]
        try:
            n = int(matnr)
        except ValueError:
            return [_err(
                material, row_idx, rule, "MATNR", matnr,
                f"MATNR '{matnr}' is not numeric, but MTART={mtart} requires "
                f"a numeric value in {rt.lower()} range "
                f"{cfg['range_from']}..{cfg['range_to']}",
                main_fields,
            )]
        lo = cfg["range_from_int"]
        hi = cfg["range_to_int"]
        if not (lo <= n <= hi):
            return [_err(
                material, row_idx, rule, "MATNR", matnr,
                f"MATNR '{matnr}' is outside MTART={mtart}'s {rt.lower()} "
                f"range {cfg['range_from']}..{cfg['range_to']}",
                main_fields,
            )]
        return []
    else:
        # Alphanumeric range (e.g. ZHLB A..ZZZZZZZZ): accept letters + digits
        # within the max length. Don't enforce strict lexical A..Z bound;
        # SAP accepts any alphanumeric within length.
        if not matnr.replace(" ", "").isalnum():
            return [_err(
                material, row_idx, rule, "MATNR", matnr,
                f"MATNR '{matnr}' contains non-alphanumeric characters; MTART="
                f"{mtart}'s {rt.lower()} range {cfg['range_from']}..{cfg['range_to']} "
                f"allows only letters and digits",
                main_fields,
            )]
        if len(matnr) > cfg["range_max_len"]:
            return [_err(
                material, row_idx, rule, "MATNR", matnr,
                f"MATNR '{matnr}' is {len(matnr)} chars, exceeds MTART="
                f"{mtart}'s {rt.lower()} max length {cfg['range_max_len']} "
                f"(range {cfg['range_from']}..{cfg['range_to']})",
                main_fields,
            )]
        return []


# Dispatch table
HANDLERS = {
    "mandatory": _handle_mandatory,
    "mandatory_with_default": _handle_mandatory_with_default,
    "bwkey_from_werks": _handle_bwkey_from_werks,
    "max_length": _handle_max_length,
    "exact_value": _handle_exact_value,
    "valid_digit_count": _handle_valid_digit_count,
    "kds_lookup": _handle_kds_lookup,
    "kds_nested_lookup": _handle_kds_nested_lookup,
    "valclass_by_mtart": _handle_valclass_by_mtart,
    "price_for_vprsv": _handle_price_for_vprsv,
    "field_required_when_vprsv": _handle_field_required_when_vprsv,   # v68
    "iso_uom_check": _handle_iso_uom_check,                            # v69
    "field_must_equal_when_other": _handle_field_must_equal_when_other,  # v69
    "conditional_mandatory": _handle_conditional_mandatory,
    "at_least_one_filled": _handle_at_least_one_filled,
    "exact_sequence": _handle_exact_sequence,
    "numeric_only": _handle_numeric_only,
    "matnr_range": _handle_matnr_range,
    "non_negative_number": _handle_non_negative_number,
}


# ─── Public entry point ───────────────────────────────────────────────────

def validate_mm(materials: list[MergedMaterial],
                catalogs: dict[str, Any],
                main_fields: list[str],
                merged_result=None,
                alt_uom_rows: list | None = None,
                longtext_rows: list | None = None,
                friendly_labels: list[str] | dict[str, str] | None = None) -> list[Error]:
    """Run all checklist + KDS rules against each material.

    Args:
        materials: list of MergedMaterial (from mm_merger.merge())
        catalogs: dict of KDS catalogs (from mm_kds.load_mm_catalogs())
        main_fields: ordered list of SAP field codes from main file's row 2
        merged_result: optional MergeResult — when provided AND
                       alt_uom_rows / longtext_rows are also provided,
                       cross-file validation runs (orphan MATNRs in
                       alt/lt files, sales-area mismatches in long-text).
                       v57 added cross-file checks; pre-v57 callers don't
                       pass these and behavior is unchanged.
        alt_uom_rows: original alt-uom LoadedRows (for orphan detection
                      that points at the source row position).
        longtext_rows: original long-text LoadedRows (same idea).
        friendly_labels: optional. Either a dict[sap_code → label] or a
                       list parallel to main_fields. When supplied, every
                       Error's column_label is the friendly label from
                       the source upload's row 1 (e.g. "Sales org." for
                       VKORG). Falls back to SAP code when missing. v59.
                       Older callers that don't pass this still work —
                       column_label just stays as SAP code.

    Returns: list of Error, sorted by (material row_idx, rule sl_no).
    """
    # Populate the module-level friendly-label slot for the duration of
    # this call. Always restore on exit so an exception or partial run
    # doesn't leak labels into a subsequent call (defensive, even though
    # validation is synchronous per-request).
    global _ACTIVE_LABELS
    prev_labels = _ACTIVE_LABELS
    if friendly_labels is None:
        _ACTIVE_LABELS = {}
    elif isinstance(friendly_labels, dict):
        _ACTIVE_LABELS = dict(friendly_labels)
    else:
        # List parallel to main_fields. Zip them into a dict.
        _ACTIVE_LABELS = {
            code: (str(lbl) if lbl else code)
            for code, lbl in zip(main_fields, friendly_labels)
        }

    # Backfill canonical names for LTMC-standard fields that aren't in
    # the customer's source columns (BWKEY, ALAND, WAERS, CURTP, etc.).
    # Without this fallback the friendly-label resolver would return the
    # raw SAP code for these fields — fine for internal lookup but ugly
    # in error messages and the Decisions panel.
    # Source upload labels always win — `setdefault` only adds if the key
    # isn't already present.
    for code, name in _CANONICAL_SAP_LABELS.items():
        _ACTIVE_LABELS.setdefault(code, name)

    try:
        return _validate_mm_inner(
            materials, catalogs, main_fields,
            merged_result=merged_result,
            alt_uom_rows=alt_uom_rows,
            longtext_rows=longtext_rows,
        )
    finally:
        _ACTIVE_LABELS = prev_labels


def _validate_mm_inner(materials: list[MergedMaterial],
                       catalogs: dict[str, Any],
                       main_fields: list[str],
                       merged_result=None,
                       alt_uom_rows: list | None = None,
                       longtext_rows: list | None = None) -> list[Error]:
    """Internal — separated so the `_ACTIVE_LABELS` setup wraps it cleanly."""
    errors: list[Error] = []
    rules_skipped: dict[str, int] = {}

    # Pre-filter: drop rules whose catalog is empty
    active_rules: list[ChecklistRule] = []
    for rule in ALL_RULES:
        if rule.requires_catalog:
            cat = catalogs.get(rule.requires_catalog)
            # nested catalogs are {str: {str: str}} — non-empty check
            if not cat:
                rules_skipped[rule.requires_catalog] = rules_skipped.get(
                    rule.requires_catalog, 0) + 1
                continue
        active_rules.append(rule)

    if rules_skipped:
        print(f"[mm_validator] skipped rules (missing catalogs): {rules_skipped}",
              flush=True)

    for row_idx, material in enumerate(materials):
        for rule in active_rules:
            handler = HANDLERS.get(rule.kind)
            if handler is None:
                print(f"[mm_validator] no handler for rule kind '{rule.kind}' "
                      f"(rule_id={rule.rule_id})", flush=True)
                continue
            try:
                rule_errors = handler(material, row_idx, rule, catalogs, main_fields)
                errors.extend(rule_errors)
            except Exception as e:
                print(f"[mm_validator] handler {rule.kind} crashed on "
                      f"rule={rule.rule_id} matnr={material.matnr}: {e}",
                      flush=True)

    # ── v57: cross-file validation ──────────────────────────────────
    # When the caller passes the MergeResult plus alt_uom/longtext rows,
    # run orphan + sales-area cross-checks. These don't run inside the
    # per-material loop above because they need the full merge picture
    # and the source rows from alt/lt files (not just merged.materials).
    # Backward-compatible: callers that don't pass these (older code or
    # main-only validation) get the original behavior.
    if merged_result is not None and (alt_uom_rows or longtext_rows):
        try:
            from .mm_cross_file_validator import validate_cross_file
            cross_errors = validate_cross_file(
                merged_result,
                alt_uom_rows=alt_uom_rows,
                longtext_rows=longtext_rows,
                catalogs=catalogs,   # v66: enables ISO UoM checks for MEINS/MEINH
            )
            errors.extend(cross_errors)
        except Exception as e:
            print(f"[mm_validator] cross-file validation crashed: {e}",
                  flush=True)

    # Stable ordering: by material row_idx, then by rule_id alphabetical.
    # SMEs tend to work row-by-row so this groups related errors together.
    errors.sort(key=lambda e: (e.row_idx, e.rule_id))
    return errors

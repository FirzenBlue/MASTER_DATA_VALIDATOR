"""
MM Checklist — Business + format + catalog rules, encoded as declarative
data structures that mm_validator can execute.

Each rule is a ChecklistRule dataclass describing:
  - rule_id: machine-stable ID (goes into Error.rule_id)
  - description: human-friendly title (goes into Error.message prefix)
  - sap_field: the SAP field(s) this rule targets
  - kind: one of — mandatory, max_length, exact_length, exact_value,
          valid_digit_count, kds_lookup, conditional_mandatory
  - params: rule-specific parameters
  - severity: "error" (blocks migration) or "warning" (flagged but OK)

The validator in mm_validator.py iterates this list; each kind has a
handler that runs the check and emits Errors where data fails.

Based on the HML data checklist (45 rows, 43 numbered rules) + LTMC
template-mandatory fields + SAP data-dictionary format rules + KDS
catalog validations. All rules are applied equally; no categorisation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChecklistRule:
    """One validation rule."""
    rule_id: str              # e.g. "mm_description_too_long"
    sl_no: str                # original "Sl No" from the xlsx — "01", "02"…
    description: str          # human-friendly title
    sap_field: str | list[str]  # field(s) this rule targets (MAKTX, VPRSV …)
    kind: str                 # handler selector — see types above
    params: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"
    # If this rule depends on a KDS catalog that may not yet be loaded,
    # name it here. mm_validator skips the rule if catalogs[requires_catalog]
    # is empty. Lets us ship without Division/Profit Center data without
    # breaking validation — just those rules stay dormant.
    requires_catalog: str | None = None


# ─── Rule catalogue ──────────────────────────────────────────────────────
# Rules are ordered roughly by checklist Sl No. Comments explain the
# intent of each; parameters are the minimum data the handler needs.

RULES: list[ChecklistRule] = [

    # ── Sl 01 ── Material Group should be correct (KDS-validated)
    ChecklistRule(
        rule_id="mm_matgroup_not_in_kds",
        sl_no="01", description="Material Group must be in KDS",
        sap_field="MATKL", kind="kds_lookup",
        params={"catalog": "material_group"},
        requires_catalog="material_group",
    ),

    # ── Sl 02 ── Material Description ≤ 40 chars
    ChecklistRule(
        rule_id="mm_description_too_long",
        sl_no="02", description="Material Description exceeds 40 characters",
        sap_field="MAKTX", kind="max_length", params={"max_length": 40},
    ),

    # ── Sl 03 ── Base Unit mandatory
    ChecklistRule(
        rule_id="mm_base_unit_missing",
        sl_no="03", description="Base Unit of Measure is mandatory",
        sap_field="MEINS", kind="mandatory",
    ),

    # ── Sl 04 ── Old Material Number mandatory
    ChecklistRule(
        rule_id="mm_old_matnr_missing",
        sl_no="04", description="Old Material Number is mandatory",
        sap_field="BISMT", kind="mandatory",
    ),

    # ── Sl 05 ── Division proper (KDS — sourced from Divison_KDS.xlsx in v70)
    # v70: SME quote "user can input anything he want" — division is a
    # SUGGESTION not a hard rule. Severity = warning so it surfaces in the
    # Decisions panel with the full 17-entry dropdown from Divison_KDS.xlsx
    # (INO2 + IN03 + IN05 + UK02 sheets merged), but doesn't block LTMC
    # export. SME can pick from the dropdown or type their own value.
    ChecklistRule(
        rule_id="mm_division_not_in_kds",
        sl_no="05", description="Division — suggested values from KDS",
        sap_field="SPART", kind="kds_lookup",
        params={"catalog": "division"},
        requires_catalog="division",
        severity="warning",
    ),

    # ── Sl 06 ── Profit Center proper (KDS — pending catalog)
    ChecklistRule(
        rule_id="mm_profit_center_not_in_kds",
        sl_no="06", description="Profit Center must be in KDS",
        sap_field=["SALES_PRCTR", "PLNT2_PRCTR"],  # two places it can appear
        kind="kds_lookup", params={"catalog": "profit_center"},
        requires_catalog="profit_center",
    ),

    # ── Sl 07 ── HSN Code must be exactly 8 digits
    ChecklistRule(
        rule_id="mm_hsn_not_8_digits",
        sl_no="07", description="HSN Code (STEUC) must be exactly 8 digits",
        sap_field="STEUC", kind="valid_digit_count", params={"digits": 8},
    ),

    # ── Sl 08 ── Batch Management flag = X
    ChecklistRule(
        rule_id="mm_batch_mgmt_must_be_X",
        sl_no="08", description="Batch Management (XCHPF) must be 'X'",
        sap_field="XCHPF", kind="exact_value", params={"expected": "X"},
    ),

    # ── Sl 09 ── ABC Indicator should be present (KDS — not required)
    ChecklistRule(
        rule_id="mm_abc_indicator_not_in_kds",
        sl_no="09", description="ABC Indicator must be A, B, or C",
        sap_field="MAABC", kind="exact_value",
        # ABC is SAP-standard — hardcode valid set since no catalog needed
        params={"allowed": ["A", "B", "C"]},
        severity="warning",   # checklist marks this "YES KDS" but "Mandatory" column is blank,
    ),

    # ── Sl 10 ── MRP Type mandatory (valid set)
    ChecklistRule(
        rule_id="mm_mrp_type_missing_or_invalid",
        sl_no="10", description="MRP Type (DISMM) is mandatory and must be a valid SAP code",
        sap_field="DISMM", kind="exact_value",
        params={"allowed": ["PD", "ND", "VB", "VM", "M0", "M1", "M2", "M3", "M4",
                            "R1", "R2", "X0", "X1", "X2", "Z1"]},
    ),

    # ── Sl 11 ── Planning time fence required if applicable (skipped — no "applicable" signal in input)
    # Leaving this as a soft-warn: if DISMM is a planning-time-fence type (P1-P4)
    # but FXHOR is blank, warn.
    ChecklistRule(
        rule_id="mm_planning_fence_missing_for_pX_mrp",
        sl_no="11", description="Planning Time Fence (FXHOR) required when MRP Type uses planning horizon",
        sap_field="FXHOR", kind="conditional_mandatory",
        params={"when_field": "DISMM", "when_value_in": ["P1", "P2", "P3", "P4"]},
        severity="warning",
    ),

    # ── Sl 12 ── MRP Controller mandatory (KDS — pending catalog)
    ChecklistRule(
        rule_id="mm_mrp_controller_not_in_kds",
        sl_no="12", description="MRP Controller (DISPO) must be in KDS",
        sap_field="DISPO", kind="kds_lookup",
        params={"catalog": "mrp_controller"},
        requires_catalog="mrp_controller",
    ),

    # ── Sl 13 ── Lot Sizing Procedure should be set
    ChecklistRule(
        rule_id="mm_lot_sizing_missing",
        sl_no="13", description="Lot Sizing Procedure (DISLS) should be set",
        sap_field="DISLS", kind="mandatory", severity="warning",
    ),

    # ── Sl 14 ── Procurement Type mandatory (E/F/X)
    ChecklistRule(
        rule_id="mm_procurement_type_missing_or_invalid",
        sl_no="14", description="Procurement Type (BESKZ) must be E / F / X",
        sap_field="BESKZ", kind="exact_value",
        params={"allowed": ["E", "F", "X"]},
    ),

    # ── Sl 15 ── Backflush = 1 if BOM component (conditional — hard to tell from input)
    ChecklistRule(
        rule_id="mm_backflush_default_missing",
        sl_no="15", description="Backflush (RGEKZ) should be '1' if material is a BOM component",
        sap_field="RGEKZ", kind="exact_value", params={"expected": "1"},
        severity="warning",   # can't tell from input whether material IS a BOM component,
    ),

    # ── Sl 16 ── Production Storage Loc mandatory for in-house (MTART=ZFRT)
    ChecklistRule(
        rule_id="mm_production_sloc_missing",
        sl_no="16", description="Production Storage Location (LGPRO) required for in-house produced materials",
        sap_field="LGPRO", kind="conditional_mandatory",
        params={"when_field": "MTART", "when_value_in": ["ZFRT", "ZHLB"]},
    ),

    # ── Sl 17 ── In-House Production Time mandatory FOR IN-HOUSE PRODUCED
    # Originally worded as "always mandatory" in the checklist, but real
    # SAP semantics are: DZEIT (in-house production time) only applies to
    # materials that are actually produced in-house — finished goods and
    # semi-finished goods. Purchased RM/PM (ZRMI/ZRMD/ZPMD) have no
    # in-house production time because they're bought, not made.
    # Running the rule as "always mandatory" flags every RM/PM material
    # with a false positive (confirmed on Peenya file: 48 × false error).
    # Scoping the rule to MTARTs that indicate in-house production. The
    # list below is based on Healthium's material-type convention as of
    # Apr 2026 — ZFRT, ZFLQ, ZHLD/ZHLM/ZHLP/ZHRT/ZHRB. If HML revises
    # this list, update the mtart_in parameter here.
    ChecklistRule(
        rule_id="mm_inhouse_prod_time_missing",
        sl_no="17", description="In-house production time (DZEIT) is mandatory for in-house produced materials",
        sap_field="DZEIT", kind="conditional_mandatory",
        params={
            "when_field": "MTART",
            "when_value_in": ["ZFRT", "ZFLQ", "ZHLD", "ZHLM", "ZHLP", "ZHRT", "ZHRB"],
        },
    ),

    # ── Sl 18 ── GR Processing Time mandatory
    ChecklistRule(
        rule_id="mm_gr_processing_time_missing",
        sl_no="18", description="GR Processing Time (WEBAZ) is mandatory",
        sap_field="WEBAZ", kind="mandatory",
    ),

    # ── Sl 19 ── Safety Stock mandatory where applicable
    ChecklistRule(
        rule_id="mm_safety_stock_missing",
        sl_no="19", description="Safety Stock (EISBE) should be set where applicable",
        sap_field="EISBE", kind="mandatory", severity="warning",
    ),

    # ── Sl 20 ── Strategy Group mandatory (KDS — pending catalog)
    ChecklistRule(
        rule_id="mm_strategy_group_not_in_kds",
        sl_no="20", description="Strategy Group (STRGR) must be in KDS",
        sap_field="STRGR", kind="kds_lookup",
        params={"catalog": "strategy_group"},
        requires_catalog="strategy_group",
    ),

    # ── Sl 21 ── Production Supervisor + Scheduling Profile (KDS — pending catalogs)
    ChecklistRule(
        rule_id="mm_production_supervisor_not_in_kds",
        sl_no="21a", description="Production Supervisor (FEVOR) must be in KDS",
        sap_field="FEVOR", kind="kds_lookup",
        params={"catalog": "production_supervisor"},
        requires_catalog="production_supervisor",
    ),
    ChecklistRule(
        rule_id="mm_scheduling_profile_not_in_kds",
        sl_no="21b", description="Production Scheduling Profile (SFCPF) must be in KDS",
        sap_field="SFCPF", kind="kds_lookup",
        params={"catalog": "scheduling_profile"},
        requires_catalog="scheduling_profile",
    ),

    # ── Sl 22 ── Min Remaining Shelf Life = 1
    ChecklistRule(
        rule_id="mm_min_shelf_life_must_be_1",
        sl_no="22", description="Min Remaining Shelf Life (MHDRZ) must be '1'",
        sap_field="MHDRZ", kind="exact_value", params={"expected": "1"},
    ),

    # ── Sl 23 ── Total Shelf Life mandatory
    ChecklistRule(
        rule_id="mm_shelf_life_missing",
        sl_no="23", description="Total Shelf Life (MHDHB) is mandatory",
        sap_field="MHDHB", kind="mandatory",
    ),

    # ── Sl 24 ── Period Indicator = D (days)
    ChecklistRule(
        rule_id="mm_period_indicator_must_be_D",
        sl_no="24", description="Period Indicator (IPRKZ) must be 'D' (days)",
        sap_field="IPRKZ", kind="exact_value", params={"expected": "D"},
    ),

    # ── Sl 25 ── Quality + Stock type = X
    ChecklistRule(
        rule_id="mm_quality_active_must_be_X",
        sl_no="25a", description="Quality Active (QUALITY_ACTIVE) must be 'X'",
        sap_field="QUALITY_ACTIVE", kind="exact_value", params={"expected": "X"},
    ),
    ChecklistRule(
        rule_id="mm_stock_type_must_be_X",
        sl_no="25b", description="Stock Type (INSMK) must be 'X'",
        sap_field="INSMK", kind="exact_value", params={"expected": "X"},
    ),

    # ── Sl 26 ── Price Determination = 3
    ChecklistRule(
        rule_id="mm_price_det_must_be_3",
        sl_no="26", description="Price Determination (MLAST) must be '3'",
        sap_field="MLAST", kind="exact_value", params={"expected": "3"},
    ),

    # ── Sl 27 ── Valuation Class must match material type's expected value
    # This is handled specially by mm_validator using valclass_by_mtart map.
    ChecklistRule(
        rule_id="mm_valuation_class_wrong_for_mtart",
        sl_no="27", description="Valuation Class must match expected for Material Type",
        sap_field="BKLAS", kind="valclass_by_mtart",
        params={},
        requires_catalog="valclass_by_mtart",
    ),

    # ── Sl 28 ── Price Control valid values (NOT mandatory — blank is OK).
    # VPRSV is optional. SAP MM materials can have no price control set
    # (e.g. when valuation is handled at a different level). But IF the
    # user enters a value, it must be either 'S' (standard price) or 'V'
    # (moving average). Anything else is invalid.
    # See `_handle_exact_value` — blank values with `allowed=[...]` and no
    # `expected=...` are explicitly skipped (no error). The error fires
    # only when VPRSV is set to something other than S or V.
    ChecklistRule(
        rule_id="mm_price_control_missing_or_invalid",
        sl_no="28", description="Price Control (VPRSV), if set, must be 'S' or 'V'",
        sap_field="VPRSV", kind="exact_value", params={"allowed": ["S", "V"]},
    ),

    # ── Sl 29 ── Price per unit = 1
    ChecklistRule(
        rule_id="mm_price_unit_must_be_1",
        sl_no="29", description="Price Unit (PEINH) must be '1'",
        sap_field="PEINH", kind="exact_value", params={"expected": "1"},
    ),

    # ── Sl 30 ── Standard or Moving price mandatory (conditional on VPRSV).
    # The price-control field VPRSV declares which price column SAP uses:
    #   VPRSV='S' → STPRS (standard price) must be populated
    #   VPRSV='V' → VERPR (moving average price) must be populated
    #   VPRSV blank → no requirement on either price column (no error fires)
    #
    # v68: split into TWO separate rules so the Decisions panel shows a
    # field-specific title (instead of the generic "If VPRSV='S'... If
    # VPRSV='V'..." text which appeared on every error regardless of
    # which field was actually empty). Each rule has a single sap_field
    # so the decision card naturally groups errors by the missing field.
    # The handler `_handle_field_required_when_vprsv` reads `params.when`
    # to decide whether to fire based on VPRSV='S' or 'V'.
    ChecklistRule(
        rule_id="mm_stprs_required_when_vprsv_S",
        sl_no="30a",
        description="Standard Price (STPRS) is required when Price Control (VPRSV) = 'S'",
        sap_field="STPRS",
        kind="field_required_when_vprsv",
        params={"when": "S", "field": "STPRS", "label": "Standard Price"},
    ),
    ChecklistRule(
        rule_id="mm_verpr_required_when_vprsv_V",
        sl_no="30b",
        description="Moving Price (VERPR) is required when Price Control (VPRSV) = 'V'",
        sap_field="VERPR",
        kind="field_required_when_vprsv",
        params={"when": "V", "field": "VERPR", "label": "Moving Price"},
    ),

    # ── Sl 31 ── Variance Key = 000001
    ChecklistRule(
        rule_id="mm_variance_key_must_be_000001",
        sl_no="31", description="Variance Key (AWSLS) must be '000001'",
        sap_field="AWSLS", kind="exact_value", params={"expected": "000001"},
    ),

    # ── Sl 32 ── Planned Lot Size = 1
    ChecklistRule(
        rule_id="mm_planned_lot_size_must_be_1",
        sl_no="32", description="Planned Lot Size (LOSGR) must be '1'",
        sap_field="LOSGR", kind="exact_value", params={"expected": "1"},
    ),

    # ── Sl 33 ── Material Origin = X
    # Checklist says "1" but SAP expects "X" (checklist typo). Going with X.
    ChecklistRule(
        rule_id="mm_mat_origin_must_be_X",
        sl_no="33", description="Material Origin (HKMAT) must be 'X'",
        sap_field="HKMAT", kind="exact_value", params={"expected": "X"},
    ),

    # ── Sl 34 ── Material Group 1-5 — REMOVED in v61 per SME spec.
    # Previously fired a warning when all of MVGR1-5 were blank for a
    # material. Removed because:
    #   - Customer's 10,565-material source had all 5 MVGR slots blank
    #     for every material → 10,565 warnings flooding the Decisions
    #     panel with no actionable remediation (we can't know which
    #     of the 5 slots the SME should populate).
    #   - SME confirmed (May 2026): "Matl Grp 1..5 are not mandatory,
    #     in decision not consider that much".
    #   - Per-MTART KDS-driven mapping is the proper way to validate
    #     MVGR fields (each MTART has expected slots filled). That's
    #     a future rule when the customer provides the mapping —
    #     until then, a blanket warning isn't useful.
    # The columns still flow through to LTMC export verbatim — if the
    # SME populates them, they go to SAP. Just no validator complaint.

    # ── Sl 35 ── Individual/Collective Requirements = 2
    ChecklistRule(
        rule_id="mm_ind_coll_req_must_be_2",
        sl_no="35", description="Individual/Collective Requirements (SBDKZ) must be '2'",
        sap_field="SBDKZ", kind="exact_value", params={"expected": "2"},
    ),

    # ── Sl 36 ── Consumption Mode = 2
    ChecklistRule(
        rule_id="mm_consumption_mode_must_be_2",
        sl_no="36", description="Consumption Mode (VRMOD) must be '2'",
        sap_field="VRMOD", kind="exact_value", params={"expected": "2"},
    ),

    # ── Sl 37 ── Backward/Forward consumption mandatory (warn)
    ChecklistRule(
        rule_id="mm_consumption_periods_missing",
        sl_no="37", description="Backward/Forward consumption (VINT1 + VINT2) should be set",
        sap_field=["VINT1", "VINT2"], kind="at_least_one_filled", severity="warning",
    ),

    # ── Sl 38 ── Availability Check = 02
    ChecklistRule(
        rule_id="mm_avail_check_must_be_02",
        sl_no="38", description="Availability Check must be '02'",
        sap_field=["SALES_MTVFP", "MRP3_MTVFP", "MTVFP"],
        kind="exact_value", params={"expected": "02"},
    ),

    # ── Sl 39 ── Trans + Loading group = 0001
    ChecklistRule(
        rule_id="mm_trans_group_must_be_0001",
        sl_no="39a", description="Transportation Group (TRAGR) must be '0001'",
        sap_field="TRAGR", kind="exact_value", params={"expected": "0001"},
    ),
    ChecklistRule(
        rule_id="mm_loading_group_must_be_0001",
        sl_no="39b", description="Loading Group (LADGR) must be '0001'",
        sap_field="LADGR", kind="exact_value", params={"expected": "0001"},
    ),

    # ── Sl 40 ── Item Cat + Gen Item Cat = NORM
    ChecklistRule(
        rule_id="mm_item_cat_must_be_NORM",
        sl_no="40a", description="General Item Category (MTPOS_MARA) must be 'NORM'",
        sap_field="MTPOS_MARA", kind="exact_value", params={"expected": "NORM"},
    ),
    ChecklistRule(
        rule_id="mm_item_cat_grp_must_be_NORM",
        sl_no="40b", description="Item Category Group (MTPOS) must be 'NORM'",
        sap_field="MTPOS", kind="exact_value", params={"expected": "NORM"},
    ),

    # ── Sl 41 ── Account Assignment Group = 02
    # TODO(confirm with HML): real Peenya RM/PM data has KTGRM='03' on
    # all 48 materials. The checklist says '02'. Either the rule is wrong
    # for raw materials (some SAP conventions use 03 for non-trading
    # materials) or the data is wrong. Confirm before relaxing. For now
    # we keep the strict rule so SMEs see the discrepancy and can decide.
    ChecklistRule(
        rule_id="mm_acct_asmt_grp_must_be_02",
        sl_no="41", description="Account Assignment Group (KTGRM) must be '02'",
        sap_field="KTGRM", kind="exact_value", params={"expected": "02"},
    ),

    # ── Sl 42 ── Tax classification = 0/0/0/0/1 pattern
    ChecklistRule(
        rule_id="mm_tax_classification_pattern_wrong",
        sl_no="42", description="Tax Classification (TAXKM_01..05) must follow 0/0/0/0/1 pattern",
        sap_field=["TAXKM_01", "TAXKM_02", "TAXKM_03", "TAXKM_04", "TAXKM_05"],
        kind="exact_sequence", params={"expected": ["0", "0", "0", "0", "1"]},
    ),

    # ── Sl 43 ── Industry Sector = H
    ChecklistRule(
        rule_id="mm_industry_sector_must_be_H",
        sl_no="43", description="Industry Sector (MBRSH) must be 'H'",
        sap_field="MBRSH", kind="exact_value", params={"expected": "H"},
    ),
]


# ── v56 — Team-based mandatory fields (per customer's Color Guide) ──
# The Color Guide sheet in the customer's test file lists ~74 fields
# split across 4 teams (Production / Sales / Planning / Finance). Each
# must be non-blank in the source upload.
#
# We append these to RULES so they participate in normal validation.
# Each rule's rule_id is prefixed `mm_team_mandatory_<TAG>_<FIELD>` so
# the review xlsx can map missing cells to the team that owns them and
# color accordingly.
#
# Two extra conditional rules cover the VPRSV/STPRS/VERPR coupling:
#   VPRSV='S' → STPRS required (standard price)
#   VPRSV='V' → VERPR required (moving price)
from .mm_mandatory_by_team import build_team_mandatory_rules

# Track which rule_ids and fields are already covered so duplicates
# (e.g. MAKTX is also in RULES above with kind="mandatory") get skipped.
# First-defined wins; team-mandatory rules only fill gaps.
_existing_ids = {r.rule_id for r in RULES}
_existing_mandatory_fields = {r.sap_field for r in RULES
                               if r.kind == "mandatory" and isinstance(r.sap_field, str)}

for _rule in build_team_mandatory_rules():
    if _rule.sap_field in _existing_mandatory_fields:
        continue
    if _rule.rule_id in _existing_ids:
        continue
    RULES.append(_rule)
    _existing_ids.add(_rule.rule_id)
    _existing_mandatory_fields.add(_rule.sap_field)

# NOTE: We don't append `build_vprsv_conditional_rules()` here because
# the existing rule `mm_price_missing_for_vprsv` (kind="price_for_vprsv",
# Sl 30) already enforces STPRS-for-S and VERPR-for-V via a dedicated
# handler. Adding the conditional_mandatory variants would double-fire
# the same error on the same cell.

del _existing_ids, _existing_mandatory_fields, _rule


# Convenience: quick lookup by rule_id
RULES_BY_ID: dict[str, ChecklistRule] = {r.rule_id: r for r in RULES}


# Additional validation beyond the checklist — catalog checks not in the
# client checklist but required for LTMC upload success. These are "extra" rules
# driven by the MM KDS catalogs directly.
EXTRA_KDS_RULES: list[ChecklistRule] = [
    ChecklistRule(
        rule_id="mm_plant_not_in_kds",
        sl_no="X-PLT", description="Plant (WERKS) must be in KDS",
        sap_field="WERKS", kind="kds_lookup",
        params={"catalog": "plant"},
        requires_catalog="plant",
    ),
    ChecklistRule(
        rule_id="mm_material_type_not_in_kds",
        sl_no="X-MTART", description="Material Type (MTART) must be in KDS",
        sap_field="MTART", kind="kds_lookup",
        params={"catalog": "material_type"},
        requires_catalog="material_type",
    ),
    # Storage Location is scoped by plant — same LGORT code can exist under
    # multiple plants with different descriptions. We check the LGORT and
    # LGPRO fields (LGORT = primary storage loc; LGPRO = production storage
    # loc) against the plant the material is assigned to.
    ChecklistRule(
        rule_id="mm_storage_loc_not_in_kds",
        sl_no="X-LGORT", description="Storage Location (LGORT) must be valid for its Plant",
        sap_field="LGORT", kind="kds_nested_lookup",
        params={"catalog": "storage_loc_by_plant", "scope_field": "WERKS"},
        requires_catalog="storage_loc_by_plant",
    ),
    ChecklistRule(
        rule_id="mm_production_sloc_not_in_kds",
        sl_no="X-LGPRO", description="Production Storage Location (LGPRO) must be valid for its Plant",
        sap_field="LGPRO", kind="kds_nested_lookup",
        params={"catalog": "storage_loc_by_plant", "scope_field": "WERKS"},
        requires_catalog="storage_loc_by_plant",
    ),
]


# ── LTMC template mandatory fields ────────────────────────────────
# Fields marked with '*' in LTMC template row-8 descriptions, for the
# sheets we actually populate for HML's scope. Fields that the main checklist
# checklist already covers (MATKL, MEINS, WERKS, MTART, MTPOS, BKLAS,
# VPRSV) aren't re-duplicated here — they'd fire twice with different
# messages otherwise. Only fields not in the main checklist appear below.

LTMC_RULES: list[ChecklistRule] = [
    ChecklistRule(
        rule_id="mm_ltmc_description_missing",
        sl_no="L-01", description="Product Description (MAKTX) required by LTMC",
        sap_field="MAKTX", kind="mandatory",
    ),
    ChecklistRule(
        rule_id="mm_ltmc_sales_org_missing",
        sl_no="L-02", description="Sales Organization (VKORG) required by LTMC Distribution Chains",
        sap_field="VKORG", kind="mandatory",
    ),
    ChecklistRule(
        rule_id="mm_ltmc_distr_chan_missing",
        sl_no="L-03", description="Distribution Channel (VTWEG) required by LTMC Distribution Chains",
        sap_field="VTWEG", kind="mandatory",
    ),
    ChecklistRule(
        rule_id="mm_ltmc_plant_missing",
        sl_no="L-04", description="Plant (WERKS) required by LTMC Plant Data",
        sap_field="WERKS", kind="mandatory",
    ),
    ChecklistRule(
        rule_id="mm_ltmc_matnr_missing",
        # This shouldn't fire because merger skips rows without MATNR, but
        # if it ever does — missing MATNR blocks the whole row from migration.
        sl_no="L-05", description="Material Number (MATNR) required by LTMC — every sheet keys on it",
        sap_field="MATNR", kind="mandatory",
    ),

    # ── Universal tier-1 mandatory fields that LTMC needs but the
    # source files don't carry. These emit errors with suggested values
    # so SMEs can one-click-fix. The suggested values come from SAP
    # defaults for Indian implementations (Healthium's context):
    #   SPRAS = 'EN'  — LTMC accepts the locale's language code
    #   ALAND = 'IN'  — India (for Tax Classification)
    #   WAERS = 'INR' — India (for Valuation Data + Current Period)
    #   CURTP = '10'  — company code currency (for Valuation Current Period)
    # BWKEY is special — derived from WERKS via the KDS plant map (PE01→1010
    # etc.). Handled by a separate rule kind below.
    # TATYP1/TAXM1 (Tax Category + Classification) are NOT added here —
    # India's GST structure means the right value depends on the material's
    # supply chain; defaulting them risks wrong-migrated tax data. They
    # stay flagged for HML input.
    ChecklistRule(
        rule_id="mm_ltmc_language_missing",
        sl_no="L-06",
        description="Language Key (SPRAS) required by LTMC Basic Data — defaults to 'EN'",
        sap_field="SPRAS", kind="mandatory_with_default",
        params={"default": "EN"},
    ),
    ChecklistRule(
        rule_id="mm_ltmc_country_missing",
        sl_no="L-07",
        description="Country/Region (ALAND) required by LTMC Tax Classification — defaults to 'IN'",
        sap_field="ALAND", kind="mandatory_with_default",
        params={"default": "IN"},
    ),
    ChecklistRule(
        rule_id="mm_ltmc_currency_missing",
        sl_no="L-08",
        description="Currency (WAERS) required by LTMC Valuation Data — defaults to 'INR'",
        sap_field="WAERS", kind="mandatory_with_default",
        params={"default": "INR"},
    ),
    ChecklistRule(
        rule_id="mm_ltmc_currency_type_missing",
        sl_no="L-09",
        description="Currency Type (CURTP) required by LTMC Valuation Current Period — defaults to '10' (company code currency)",
        sap_field="CURTP", kind="mandatory_with_default",
        params={"default": "10"},
    ),

    # ── v70: Tax Category 1 + Tax Classification 1 rules REMOVED ──
    # Pre-v69 these were "Set LTMC default value" Decisions asking the
    # SME to confirm TATYP1='JOCG' and TAXM1='0' as session defaults.
    # In v69 the LTMC export side (ltmc_generator.INDIA_TAX_CATEGORIES)
    # was changed to hardcode TATYP1='JOCG'/TAXM1='0' (along with all
    # other categories ending in TATYP5='JTC1'/TAXM5='1' as the active
    # classification). With those values hardcoded into the export,
    # asking the SME to set them as "defaults" was redundant — the
    # LTMC always writes the hardcoded values regardless of what the
    # SME chose. The Decisions panel showed two clicks that did nothing.
    #
    # v70 (per SME feedback 2026-05-11): removed the two ChecklistRule
    # entries. The export still emits the same hardcoded values. If a
    # future customer needs different India tax codes, edit
    # INDIA_TAX_CATEGORIES in ltmc_generator.py directly.

    # ── BWKEY (Valuation Area) derived from WERKS via KDS plant map.
    # Each plant row in the HML Plants sheet has a "Valuation area" column
    # (PE01 → 1010, PE02 → 1011, SC01 → 1020, CM01 → 1050, etc.).
    # LTMC Valuation Data sheet requires BWKEY — we either read it from
    # source (not present in Peenya files) or suggest the correct value
    # per plant from the KDS map.
    ChecklistRule(
        rule_id="mm_ltmc_valuation_area_missing",
        sl_no="L-10",
        description="Valuation Area (BWKEY) required by LTMC Valuation Data — should equal Plant (WERKS)",
        sap_field="BWKEY", kind="bwkey_from_werks",
        requires_catalog="bwkey_by_werks",
    ),
]


# ── Additional KDS catalog-validation rules ────────────────────────
# Catalogs that were already loaded by mm_kds.py but not wired to any
# validation rule. Adding them here so the coverage matches the KDS
# the client actually maintains.

KDS_EXTRA_RULES: list[ChecklistRule] = [
    ChecklistRule(
        rule_id="mm_purchasing_group_not_in_kds",
        sl_no="X-EKGRP",
        description="Purchasing Group (EKGRP) must be one of the 34 codes in the KDS",
        sap_field="EKGRP", kind="kds_lookup",
        params={"catalog": "purchasing_group"},
        requires_catalog="purchasing_group",
    ),
    ChecklistRule(
        rule_id="mm_ext_material_group_not_in_kds",
        sl_no="X-EXTWG",
        description="External Material Group (EXTWG) must be one of the 28 codes in the KDS",
        sap_field="EXTWG", kind="kds_lookup",
        params={"catalog": "ext_material_group"},
        requires_catalog="ext_material_group",
    ),
]


# ── SAP data-dictionary format rules ───────────────────────────────
# Length caps per SAP data dictionary, enforced even when the checklist does not
# explicitly list them. These prevent LTMC upload from rejecting a
# too-long value. The checklist covers MAKTX (40); SAP data dictionary says:
#   MATNR: 40 chars (was 18 in ECC, 40 in S/4)
#   BISMT: 18 chars   (old material number)
#   MATKL: 9 chars    (material group code)
#   EXTWG: 18 chars
#   PRDHA: 18 chars   (product hierarchy)
#   GROES: 32 chars   (size/dimensions)
#   NORMT: 18 chars
# These are the ones most likely to overflow in real data.

SAP_RULES: list[ChecklistRule] = [
    ChecklistRule(
        rule_id="mm_sap_matnr_too_long",
        sl_no="S-01", description="Material Number (MATNR) exceeds 40 characters",
        sap_field="MATNR", kind="max_length", params={"max_length": 40},
    ),
    ChecklistRule(
        rule_id="mm_sap_old_matnr_too_long",
        sl_no="S-02", description="Old Material Number (BISMT) exceeds 18 characters",
        sap_field="BISMT", kind="max_length", params={"max_length": 18},
    ),
    ChecklistRule(
        rule_id="mm_sap_matgroup_too_long",
        sl_no="S-03", description="Material Group (MATKL) exceeds 9 characters",
        sap_field="MATKL", kind="max_length", params={"max_length": 9},
    ),
    ChecklistRule(
        rule_id="mm_sap_ext_matgroup_too_long",
        sl_no="S-04", description="External Material Group (EXTWG) exceeds 18 characters",
        sap_field="EXTWG", kind="max_length", params={"max_length": 18},
    ),
    ChecklistRule(
        rule_id="mm_sap_prod_hierarchy_too_long",
        sl_no="S-05", description="Product Hierarchy (PRDHA) exceeds 18 characters",
        sap_field="PRDHA", kind="max_length", params={"max_length": 18},
    ),
    ChecklistRule(
        rule_id="mm_sap_size_too_long",
        sl_no="S-06", description="Size/Dimensions (GROES) exceeds 32 characters",
        sap_field="GROES", kind="max_length", params={"max_length": 32},
    ),
    # ── Sl S-07 ── MATNR must conform to the number-range configured for its MTART
    #
    # The MM KDS "Material Type" sheet defines a number range per MTART:
    #   - Range Type "Internal" or "External"
    #   - From No. and End No. bounds
    # Examples:
    #   ZFRT (Finished Materials): External, numeric 1000000000000..9999999999999 (13-digit)
    #   ZHLB (Semi Finished):     External, alphanumeric A..ZZZZZZZZ (1-8 alnum chars)
    #   ZRMI (Raw Materials Imp): Internal, numeric 2000000000..2099999999 (10-digit)
    #   ZHWA (Trading Goods):     External, alphanumeric A..ZZZZZZZZ
    #   ...
    # "Internal" means SAP auto-numbers — if the client already filled a MATNR, it still
    # must fit the range. "External" means the SME provided it, but it must still conform
    # to the range's format (numeric vs alphanumeric) and bounds.
    #
    # This rule replaces the old naive "MATNR contains non-numeric characters" check
    # which incorrectly flagged alphanumeric MATNRs on ZHLB/ZATD/ZHWA/ZNDL/ZFCN (where
    # SAP explicitly allows letters). It also catches cases like 'TBC-2' on a ZFRT
    # material, where the range is numeric 13-digit — TBC-2 is 5-char alphabetic.
    ChecklistRule(
        rule_id="mm_matnr_range_mismatch",
        sl_no="S-07",
        description="Material Number (MATNR) does not conform to the number range for its Material Type",
        sap_field="MATNR",
        kind="matnr_range",        # new handler; see mm_validator.py
        severity="error",
        requires_catalog="mtart_ranges",   # skip rule if KDS doesn't provide range config
    ),
    ChecklistRule(
        rule_id="mm_sap_standard_price_negative",
        sl_no="S-08", description="Standard Price (STPRS) must be non-negative",
        sap_field="STPRS", kind="non_negative_number",
    ),
    ChecklistRule(
        rule_id="mm_sap_moving_price_negative",
        sl_no="S-09", description="Moving Average Price (VERPR) must be non-negative",
        sap_field="VERPR", kind="non_negative_number",
    ),
    # v69: ISO UoM check on main file's MEINS, runs even on main-only
    # uploads (no alt-uom file needed). The earlier MEINS-against-ISO
    # rule lived in mm_cross_file_validator and only fired when SMEs
    # uploaded an alt-uom file alongside main. SMEs uploading just main
    # got no UoM validation. This rule covers the gap. Severity warning
    # so unrecognised codes don't block export — SMEs can add the code
    # to the ISO catalog or change MEINS.
    ChecklistRule(
        rule_id="mm_main_meins_iso_check",
        sl_no="S-10",
        description="Base Unit (MEINS) should be a valid ISO unit",
        sap_field="MEINS",
        kind="iso_uom_check",
        requires_catalog="iso_uom",
    ),
    # v69: When MRP Type (DISMM) is 'HL' (Manual Reorder Point Planning),
    # the Material Status (MMSTA) must be 'Z1'. HML business rule from
    # the SAP migration team. Fires per plant_row (both DISMM and MMSTA
    # are plant-scoped). Error message names both fields by friendly +
    # code form so the dependency reads cleanly in the Decisions panel.
    ChecklistRule(
        rule_id="mm_mmsta_z1_when_dismm_hl",
        sl_no="S-11",
        description="Material Status (MMSTA) must be 'Z1' when MRP Type (DISMM) = 'HL'",
        sap_field="MMSTA",
        kind="field_must_equal_when_other",
        params={
            "other_field": "DISMM",
            "other_value": "HL",
            "expected": "Z1",
            "other_label": "MRP Type",
            "this_label": "Material Status",
        },
    ),
]


ALL_RULES: list[ChecklistRule] = RULES + EXTRA_KDS_RULES + LTMC_RULES + SAP_RULES + KDS_EXTRA_RULES

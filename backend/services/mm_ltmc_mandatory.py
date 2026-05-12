"""LTMC mandatory fields inventory.

Hand-curated map of LTMC-mandatory fields (marked `*` in the SAP S/4HANA
2025 source data form's friendly label row 8) that are typically missing
from customer source uploads. Each entry drives:

  1. A "Set LTMC value" Decision card during validation
  2. The HML hard-coded fallback used by the LTMC generator if the SME
     skips the Decision (preserves v60/v61 behavior — nothing breaks)

Why hand-curated and not parsed from the LTMC template at runtime?
-----------------------------------------------------------------
We *could* re-parse `ltmc_template.xml` every session to discover
mandatory fields. But:

  - The set is stable (SAP updates are rare and we ship the template
    bundled with the build).
  - Some "*" fields are ALWAYS in the customer source and don't need
    the override path (PRODUCT/MATNR is in every sheet but it's just
    the material number — the customer ALWAYS has this).
  - Some "*" fields need a contextual default the template doesn't
    encode (e.g. BWKEY = WERKS for plant-level valuation).
  - Some "*" fields are conditional (FSH_SEASON only matters for
    fashion modules — Healthium doesn't migrate that data).

So we curate. If the schema changes meaningfully, this list updates in
the same commit that updates ltmc_template.xml.

Source: inventory script run against
`/mnt/user-data/uploads/source_data_form_MM.xml` (SAP S/4HANA 2025
Standard Scope) on 2026-05-09. Cross-checked against
`HML_DEFAULTS` in ltmc_generator.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class LtmcMandatoryField:
    """One LTMC-mandatory field that may be missing from customer source.

    sap_field:   The SAP DDIC code (e.g. "BWKEY")
    label:       Friendly label shown in Decisions panel (e.g. "Valuation Area")
    sheets:      LTMC sheet names where this field is mandatory
    default:     Suggested default value (HML config). Empty string if
                 no safe default — SME must enter it.
    derived:     If non-empty, this field's value can be derived from
                 another column. e.g. derived="WERKS" means "if BWKEY is
                 missing, suggest WERKS as the value". The LTMC generator
                 uses this in the per-row resolve path.
    skip_for_pe01_migration:
                 True when this field exists in the SAP standard schema
                 but is irrelevant for Healthium's Peenya scope. We
                 still emit the Decision (in case SME wants to fill it),
                 but mark it `info` severity so it doesn't pollute the
                 main "Resolve" worklist.
    """
    sap_field: str
    label: str
    sheets: list[str] = field(default_factory=list)
    default: str = ""
    derived: str = ""
    skip_for_pe01_migration: bool = False


# Order matters — Decisions render in this order. Put the high-impact
# ones first (BWKEY, ALAND, WAERS — Valuation/Tax) so SMEs see them
# first when they open the Decisions panel.
LTMC_MANDATORY_FIELDS: list[LtmcMandatoryField] = [
    # ── Valuation & Tax (high priority for any HML migration) ───────
    LtmcMandatoryField(
        sap_field="BWKEY", label="Valuation Area",
        sheets=["Valuation Data", "Valuation Current Period",
                "Valuation Future Price"],
        default="", derived="WERKS",
        # Per v60: BWKEY = WERKS for plant-level valuation. The
        # `derived` hint means the generator can fall back to WERKS
        # per row even without an SME-set default. Decision still
        # emits so SME can override the per-WERKS behavior with one
        # uniform value if their config differs.
    ),
    LtmcMandatoryField(
        sap_field="ALAND", label="Country/Region",
        sheets=["Tax Classification"],
        default="IN",   # India for HML
    ),
    LtmcMandatoryField(
        sap_field="WAERS", label="Currency",
        sheets=["Valuation Data", "Valuation Current Period",
                "Valuation Future Price"],
        default="INR",  # India for HML
    ),
    LtmcMandatoryField(
        sap_field="CURTP", label="Currency Type",
        sheets=["Valuation Current Period", "Valuation Future Price"],
        default="10",   # company code currency
    ),
    # ── v70: TATYP1 / TAXM1 inventory entries REMOVED ──
    # Their corresponding ChecklistRules in mm_checklist.py were removed
    # because ltmc_generator.INDIA_TAX_CATEGORIES hardcodes the full 5-row
    # tax block on every export (TATYP1='JOCG'/TAXM1='0' ... TATYP5='JTC1'/
    # TAXM5='1' as the active flag). The SME-facing Decision was a no-op.
    # Inventory cleared to keep this file in sync with the checklist.

    # ── Basic Data ──────────────────────────────────────────────────
    LtmcMandatoryField(
        sap_field="SPRAS", label="Language Key",
        sheets=["Basic Data", "Additional Descriptions", "Receipt Texts"],
        default="EN",
    ),

    # ── MRP / Warehouse / Inspection ────────────────────────────────
    LtmcMandatoryField(
        sap_field="BERID", label="MRP Area",
        sheets=["MRP Area"],
        default="",  # SME must set; depends on plant config
        skip_for_pe01_migration=True,  # HML uses plant-level MRP, not MRP Area
    ),
    LtmcMandatoryField(
        sap_field="LGNUM", label="Warehouse Number",
        sheets=["Warehouse Number Data", "Storage Type Data"],
        default="",
        skip_for_pe01_migration=True,  # HML doesn't use WM/EWM at PE01
    ),
    LtmcMandatoryField(
        sap_field="LGTYP", label="Storage Type",
        sheets=["Storage Type Data"],
        default="",
        skip_for_pe01_migration=True,
    ),
    LtmcMandatoryField(
        sap_field="ART", label="Inspection Type",
        sheets=["Inspection Setup Data"],
        default="04",  # GR for finished goods — HML standard
    ),
    LtmcMandatoryField(
        sap_field="RQGRP", label="Requirement Group",
        sheets=["Store Replenishment"],
        default="",
        skip_for_pe01_migration=True,  # HML isn't using replenishment groups
    ),

    # ── Valuation Future Price (rarely populated for first migration) ─
    LtmcMandatoryField(
        sap_field="ZKPRS_1", label="Future Price",
        sheets=["Valuation Future Price"],
        default="",
        skip_for_pe01_migration=True,  # No future-price tracking on initial load
    ),
    LtmcMandatoryField(
        sap_field="ZPRSDAT_1", label="Future Price From",
        sheets=["Valuation Future Price"],
        default="",
        skip_for_pe01_migration=True,
    ),
]


# Quick lookup by SAP code
BY_SAP_FIELD: dict[str, LtmcMandatoryField] = {
    f.sap_field: f for f in LTMC_MANDATORY_FIELDS
}


def for_sheet(sheet_name: str) -> list[LtmcMandatoryField]:
    """All mandatory fields applicable to a specific LTMC sheet."""
    return [f for f in LTMC_MANDATORY_FIELDS if sheet_name in f.sheets]


def applicable_to_source(source_sap_fields: set[str]) -> list[LtmcMandatoryField]:
    """LTMC-mandatory fields NOT covered by the customer's source upload.

    These are the fields that need either an SME-set default (via
    Decisions) or fall back to HML hard-coded defaults during LTMC
    export.

    PRODUCT/MATNR isn't in this list — the merger handles material number
    mapping and it's always present.
    """
    return [f for f in LTMC_MANDATORY_FIELDS
            if f.sap_field not in source_sap_fields]

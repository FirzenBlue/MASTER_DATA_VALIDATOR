"""LTMC session-level field overrides.

Why this module exists
----------------------
SAP's LTMC migration object schema requires a number of fields that the
customer's source upload may not contain at all. Examples for the
Healthium Peenya migration:

    BWKEY  - Valuation Area    (mandatory in 3 valuation sheets)
    ALAND  - Country/Region    (mandatory in Tax Classification)
    WAERS  - Currency          (mandatory in Valuation sheets)
    CURTP  - Currency Type     (mandatory in Valuation Current Period)
    SPRAS  - Language Key      (mandatory in Basic Data)
    BERID  - MRP Area          (mandatory in MRP Area)
    LGNUM  - Warehouse Number  (mandatory in WH/Storage Type)
    LGTYP  - Storage Type      (mandatory in Storage Type Data)
    ART    - Inspection Type   (mandatory in Inspection Setup)
    RQGRP  - Requirement Group (mandatory in Store Replenishment)

The customer's xlsx upload doesn't have columns for these. Pre-v62 the
LTMC generator hard-coded HML defaults (ALAND='IN', WAERS='INR' etc.)
which worked but was inflexible — the SME couldn't change them per
session, and the system silently filled values they might not agree with.

v62 adds a session-level overrides map: SMEs see a Decision card per
missing-mandatory-LTMC-field, enter a value, and that value flows into
every material's LTMC export. This is "default for all + override per
row" — per-row override happens via the Records editor (existing
capability); the overrides map handles the bulk default case.

Storage model
-------------
Per-session dict: {sap_field: value}. Lives in WORKING[token]['ltmc_overrides'].
Reset on session reload. NOT persisted to DB — these are mid-validation
choices that get baked into the LTMC export, then the export is what
moves forward; storing them long-term would risk stale defaults haunting
later sessions.

Lookup contract for the LTMC generator
--------------------------------------
For any field VALUE during LTMC emit, the resolution order is:

    1. Per-row source value (material.main[sap_field] or plant_row[sap_field])
    2. Session override (overrides[sap_field])
    3. HML built-in default (HML_DEFAULTS[sap_field])
    4. "" (empty cell)

Tier 1 always wins — if the customer's source has the value, use it. The
override only kicks in for fields where source is blank.

This module is deliberately small. The complexity lives in:
  - mm_checklist.py — the new `mandatory_in_ltmc` rule kind
  - main.py — the apply_ltmc_default endpoint
  - ltmc_generator.py — the lookup-with-override behavior
"""
from __future__ import annotations

from typing import Any


def get_overrides(state: dict) -> dict[str, str]:
    """Return the session's LTMC overrides map. Never returns None.

    Creates the map lazily on first access so callers don't have to
    initialize state["ltmc_overrides"] = {} themselves. The dict is
    mutable and shared with the session — modifications persist.
    """
    if "ltmc_overrides" not in state:
        state["ltmc_overrides"] = {}
    return state["ltmc_overrides"]


def set_override(state: dict, sap_field: str, value: str) -> None:
    """Record a session-level default for a SAP field.

    Pass empty string to remove the override (cleaner than a separate
    delete function and more forgiving when the UI clears the input).
    """
    overrides = get_overrides(state)
    if value is None or value == "":
        overrides.pop(sap_field, None)
    else:
        overrides[sap_field] = str(value)


def resolve(state: dict, sap_field: str,
            source_value: Any = None,
            hml_default: str = "") -> str:
    """Apply the v62 lookup contract for one field.

    Tier 1: source value (per-row, from customer's data)
    Tier 2: session override (set by SME via Decisions)
    Tier 3: HML built-in default (hard-coded SAP defaults)
    Tier 4: empty string

    Args:
        state: session state dict
        sap_field: SAP code, e.g. "BWKEY"
        source_value: the per-row value from the source file, if any
        hml_default: the legacy hard-coded default for this field

    Returns:
        Resolved value as string; "" if all tiers are blank.
    """
    # Tier 1: source value wins
    if source_value is not None:
        s = str(source_value).strip()
        if s and s.lower() not in ("none", "nan"):
            # Normalize float-as-int (Excel quirk: numeric cells often
            # come through as 90189099.0 instead of "90189099")
            try:
                f = float(s)
                if f.is_integer():
                    return str(int(f))
            except (ValueError, TypeError):
                pass
            return s

    # Tier 2: session override
    overrides = get_overrides(state)
    if sap_field in overrides:
        v = overrides[sap_field]
        if v:
            return str(v)

    # Tier 3: HML default
    if hml_default:
        return str(hml_default)

    # Tier 4: empty
    return ""

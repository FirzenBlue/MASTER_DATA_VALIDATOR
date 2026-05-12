"""Team-based mandatory-field policy for MM (v58).

History
-------
v56 introduced a 73-field mandatory list pulled from the customer's
Color Guide sheet. That turned out to be over-aggressive — the Color
Guide's blue/purple/green/orange highlights were "team ownership for
visual organisation", not "every field is mandatory". v58 narrows the
mandatory set to the SME-confirmed 16-field minimum (List 2 in their
v58 brief).

The team color scheme (green Production / purple Sales / blue Planning
/ orange Finance) IS still useful and STAYS — it powers the review
xlsx's per-team coloring so SMEs can visually route fixes to the right
team. What changed is just *which fields* fire mandatory errors.

The 16 mandatory fields (List 2 from the SME's brief, mapped to SAP codes):

  Production                | Sales/CSD       | Planning  | Finance
  --------------------------|-----------------|-----------|-----------
  MTART  Matl Type          | SPART  Division | PLIFZ     | SALES_PRCTR
  MATKL  Matl Group         | LADGR  LoadingGrp| Plnd Delivery
  DWERK  Dlv.plant          |                 |           | BKLAS  Val. Class
  RGEKZ  Backflush          |                 |           | VPRSV  Price ctrl
  FEVOR  Pr.Superv.         |                 |           | EKALR  With QS (Costing)
  SFCPF  Prod.Prof.         |                 |           |
  QUALITY_ACTIVE (Z field)  |                 |           |

Plus two CONDITIONAL rules already enforced via the existing
`mm_price_missing_for_vprsv` handler (Sl 30):
  VPRSV='S' → STPRS required
  VPRSV='V' → VERPR required

Behavior
--------
- Field is BLANK → fires `mm_team_mandatory_<TAG>_<FIELD>` error.
- Field is FILLED with an invalid value → existing catalog/format
  rules fire (e.g. MATKL not in KDS, FEVOR not in KDS). Both kinds of
  error flow through Decisions and the colored review xlsx.
- The LTMC export passes these fields through verbatim. SAP's
  per-field code mapping is the same as before (no friendly-label
  detour — the LTMC generator reads SAP codes directly from the
  loaded materials).

The QUALITY_ACTIVE field is a custom Z field, not a standard SAP
attribute. We register a mandatory rule against it but the LTMC
generator handles it by skipping unknown SAP codes — a pre-existing
behavior that's safe.
"""
from __future__ import annotations

# Team labels — used as identifiers AND human-readable strings in
# error messages and the review xlsx Team column.
TEAM_PRODUCTION = "Production"
TEAM_SALES      = "Sales/CSD"
TEAM_PLANNING   = "Planning"
TEAM_FINANCE    = "Finance"

# Hex header colors per team (from the Color Guide). The review xlsx
# uses these on header row 1 of the Materials sheet.
TEAM_HEADER_COLOR = {
    TEAM_PRODUCTION: "70AD47",
    TEAM_SALES:      "7030A0",
    TEAM_PLANNING:   "2E75B6",
    TEAM_FINANCE:    "E26B0A",
}

# Lighter tint for data cells where a mandatory-but-blank field belongs
# to a known team. Header color mixed with white.
TEAM_TINT_COLOR = {
    TEAM_PRODUCTION: "E2EFDA",   # green tint
    TEAM_SALES:      "E2CFED",   # purple tint
    TEAM_PLANNING:   "BDD7EE",   # blue tint
    TEAM_FINANCE:    "FCE4D6",   # orange tint
}

# Critical-error fill (overrides team tint when value is INVALID rather
# than just missing — pink highlight from the Color Guide row 13).
INVALID_VALUE_FILL = "FFC7CE"

# v58: the SME-confirmed 16-field minimum.
# Field order is Production-first (most fields), then Sales, Planning,
# Finance. Within a team, order matches the SME's brief.
MANDATORY_FIELDS_BY_TEAM: dict[str, list[str]] = {
    TEAM_PRODUCTION: [
        "MTART",          # Matl Type
        "MATKL",          # Matl Group
        "DWERK",          # Dlv.plant
        "RGEKZ",          # Backflush
        "FEVOR",          # Pr.Superv. (Production Supervisor)
        "SFCPF",          # Prod.Prof. (Production Scheduling Profile)
        "QUALITY_ACTIVE", # Custom Z field — mandatory per SME spec
    ],
    TEAM_SALES: [
        "SPART",          # Division
        "LADGR",          # LoadingGrp
    ],
    TEAM_PLANNING: [
        "PLIFZ",          # Plnd Deliv (Planned Delivery time)
    ],
    TEAM_FINANCE: [
        "SALES_PRCTR",    # Profit Ctr
        "BKLAS",          # Val. Class
        "VPRSV",          # Price ctrl (S or V — STPRS/VERPR conditionally
                          # required by the existing mm_price_missing_for_vprsv
                          # rule; not duplicated here)
        "EKALR",          # With QS (Costing relevance)
        # NOTE: STPRS and VERPR are not in this flat list because they're
        # CONDITIONALLY mandatory based on VPRSV. The existing rule
        # `mm_price_missing_for_vprsv` (Sl 30) handles that without
        # duplicate coverage.
    ],
}


def field_to_team() -> dict[str, str]:
    """Reverse lookup: SAP field → team name.

    First-team-wins if a field appears in two teams' lists (shouldn't
    happen given the Color Guide, but defensive).
    """
    out: dict[str, str] = {}
    for team, fields in MANDATORY_FIELDS_BY_TEAM.items():
        for f in fields:
            out.setdefault(f, team)
    # Conditional Finance fields also belong to Finance team for coloring
    out.setdefault("STPRS", TEAM_FINANCE)
    out.setdefault("VERPR", TEAM_FINANCE)
    return out


# Build ChecklistRules for every mandatory field.
def build_team_mandatory_rules() -> list:
    """Return a list of ChecklistRule entries — one per (team, field).

    Rule shape:
      rule_id   = mm_team_mandatory_<TEAM_TAG>_<FIELD>
      rule_name = "<Field label> required for <team>"
      kind      = "mandatory"
      severity  = "error"

    The rule_id includes the TEAM_TAG so the review xlsx can identify
    which team owns the missing cell and color it accordingly.
    """
    from .mm_checklist import ChecklistRule
    rules = []
    team_tag = {
        TEAM_PRODUCTION: "PROD",
        TEAM_SALES:      "SALES",
        TEAM_PLANNING:   "PLAN",
        TEAM_FINANCE:    "FIN",
    }
    for team, fields in MANDATORY_FIELDS_BY_TEAM.items():
        tag = team_tag[team]
        for f in fields:
            rules.append(ChecklistRule(
                rule_id=f"mm_team_mandatory_{tag}_{f}",
                sl_no=f"team-{tag}-{f}",
                description=f"{f} required ({team})",
                sap_field=f,
                kind="mandatory",
                severity="error",
            ))
    return rules


def build_vprsv_conditional_rules() -> list:
    """VPRSV='S' → STPRS required; VPRSV='V' → VERPR required.

    Two rules. The conditional_mandatory handler already exists; we just
    feed it the right when_field/when_value_in/sap_field combinations.
    """
    from .mm_checklist import ChecklistRule
    return [
        ChecklistRule(
            rule_id="mm_stprs_required_when_vprsv_S",
            sl_no="vprsv-S",
            description="Standard Price (STPRS) required when Price Control (VPRSV)='S'",
            sap_field="STPRS",
            kind="conditional_mandatory",
            params={"when_field": "VPRSV", "when_value_in": ["S"]},
            severity="error",
        ),
        ChecklistRule(
            rule_id="mm_verpr_required_when_vprsv_V",
            sl_no="vprsv-V",
            description="Moving Price (VERPR) required when Price Control (VPRSV)='V'",
            sap_field="VERPR",
            kind="conditional_mandatory",
            params={"when_field": "VPRSV", "when_value_in": ["V"]},
            severity="error",
        ),
    ]

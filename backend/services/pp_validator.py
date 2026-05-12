"""
PP/Routing validator — runs declarative rules against loaded data.

Inputs:
  - LoadedBom (or LoadedRouting) from the loader
  - PpCatalogs from pp_kds.load_pp_catalogs()
  - Rulebook from pp_rulebook.get_rulebook() (built from LTMC templates)

Output:
  - list[PpError] compatible with the existing Error Grid + Records
    editor + downloadable error xlsx.

Routing sheet-name prefixing
----------------------------
The rulebook keys Routing's "Global Dependency" / "Local Dependency"
etc. with a "Routing · " prefix to avoid colliding with the BOM
sheets of the same names. The Routing validator resolves the rule for
a sheet by trying the prefixed name first, then falling back to the
unprefixed name (for sheets unique to Routing like "Operations").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pp_loader import LoadedBom
from .routing_loader import LoadedRouting
from .pp_kds import PpCatalogs
from .pp_rulebook import Rulebook, FieldRule
from .duplicate_detector import (
    find_duplicate_groups, format_duplicate_message,
    PP_KEYS_BY_SHEET, ROUTING_KEYS_BY_SHEET,
)


@dataclass
class PpError:
    """One validation error. Compatible shape with MM Error so the
    existing Error Grid + Records editor + downloadable xlsx all
    consume them without changes."""
    sheet: str
    excel_row: int
    matnr: str              # MATNR or PLNNR — for grouping in the decision engine
    sap_field: str
    value: str
    rule_id: str
    rule_name: str
    severity: str           # "error" | "warning"
    message: str
    friendly_label: str = ""
    description: str = ""

    def as_dict(self) -> dict:
        """Match the SD/MM Error.as_dict shape so the existing
        /api/session/errors endpoint can serialize PP errors without
        per-module branching. col_idx/xml_row/row_idx are derived for
        the UI's row navigator: row_idx is 0-based row in the sheet
        (excel_row - 9 since data starts at row 9), xml_row mirrors
        excel_row."""
        return {
            "sheet": self.sheet,
            "row_idx": max(0, self.excel_row - 9),
            "xml_row": self.excel_row,
            "col_idx": -1,                 # PP doesn't use col_idx; UI keys on sap_field
            "column_label": self.friendly_label,
            "sap_field": self.sap_field,
            "value": self.value,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "message": self.message,
            "suggested_value": "",
            "suggested_options": [],
            "char_issues": [],
            "matnr": self.matnr,
        }


# ─── Per-rule check functions ──────────────────────────────────────────

def _check_mandatory(rule: FieldRule, value: Any) -> str | None:
    if not rule.is_mandatory:
        return None
    if value is None or (isinstance(value, str) and not value.strip()):
        return f"{rule.friendly_label} is blank — please fill in this column."
    return None


def _norm_for_length(value: Any) -> str:
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _check_max_length(rule: FieldRule, value: Any) -> str | None:
    if rule.max_length is None or value is None:
        return None
    # Date fields: don't measure datetime objects against the template's
    # 8-char limit. The ETE spec for dates says length 8 (YYYYMMDD), but
    # at this point in the pipeline the value is still a datetime object
    # — it won't be stringified until export. Skip the length check
    # entirely; date format validation belongs in a separate check.
    if rule.field_kind == "date":
        return None
    s = _norm_for_length(value)
    if len(s) > rule.max_length:
        return (f"{rule.friendly_label} is {len(s)} characters; "
                f"SAP allows up to {rule.max_length}. Trim the value to fit.")
    return None


def _check_catalog(rule: FieldRule, value: Any, catalogs: PpCatalogs) -> tuple[str | None, str]:
    """Catalog-presence check. Returns (message, severity) or (None, '')."""
    if rule.catalog is None or value is None:
        return None, ""
    s = _norm_for_length(value)
    if not s:
        return None, ""

    cat = getattr(catalogs, rule.catalog, None)
    if not cat:
        # Catalog wasn't loaded → don't flag (avoids warning-spam when
        # KDS files are absent).
        return None, ""

    if isinstance(cat, set):
        if s.upper() not in cat:
            sev = "error" if rule.is_mandatory else "warning"
            return (
                f"{rule.friendly_label} value '{s}' is not in the "
                f"{rule.catalog} catalog. Either fix the value or have "
                f"your SAP team add it to KDS."
            ), sev
    return None, ""


# ─── Per-row validator ────────────────────────────────────────────────

def _validate_row(
    rule_lookup_key: str,    # the key into rulebook.rule_index for this sheet
    sheet_for_error: str,    # the sheet name to put on PpError (may differ)
    row,
    sheet_rules: list[FieldRule],
    catalogs: PpCatalogs,
    matnr_for_row: str,
    errors: list[PpError],
) -> None:
    for rule in sheet_rules:
        value = row.values.get(rule.sap_field)

        msg = _check_mandatory(rule, value)
        if msg:
            errors.append(PpError(
                sheet=sheet_for_error,
                excel_row=row.excel_row,
                matnr=matnr_for_row,
                sap_field=rule.sap_field,
                value="",
                rule_id=rule.rule_id + "_mandatory",
                rule_name=f"{rule.friendly_label} is mandatory",
                severity="error",
                message=msg,
                friendly_label=rule.friendly_label,
                description=rule.description,
            ))
            continue  # skip length/catalog if mandatory failed

        if value is None or (isinstance(value, str) and not value.strip()):
            continue

        msg = _check_max_length(rule, value)
        if msg:
            errors.append(PpError(
                sheet=sheet_for_error,
                excel_row=row.excel_row,
                matnr=matnr_for_row,
                sap_field=rule.sap_field,
                value=str(value),
                rule_id=rule.rule_id + "_length",
                rule_name=f"{rule.friendly_label} too long",
                severity="error",
                message=msg,
                friendly_label=rule.friendly_label,
                description=rule.description,
            ))

        msg, sev = _check_catalog(rule, value, catalogs)
        if msg:
            errors.append(PpError(
                sheet=sheet_for_error,
                excel_row=row.excel_row,
                matnr=matnr_for_row,
                sap_field=rule.sap_field,
                value=str(value),
                rule_id=rule.rule_id + "_catalog",
                rule_name=f"{rule.friendly_label} not in catalog",
                severity=sev,
                message=msg,
                friendly_label=rule.friendly_label,
                description=rule.description,
            ))


# ─── ID extraction helpers ────────────────────────────────────────────

def _matnr_of(row, fallback: str = "") -> str:
    v = row.values.get("MATNR") or row.values.get("PLNNR") or fallback
    if v is None:
        return fallback
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


# ─── Public entry points ──────────────────────────────────────────────

def validate_bom(
    bom: LoadedBom,
    catalogs: PpCatalogs,
    rulebook: Rulebook,
) -> list[PpError]:
    """Run all rules against a loaded BOM. Returns errors sorted by
    (sheet, excel_row, sap_field) so the Error Grid renders predictably."""
    errors: list[PpError] = []

    # Per-cell rules. BOM sheets in the rulebook are keyed by their
    # natural names (no prefix) since "BOM Header" etc. don't collide
    # with anything.
    for sheet_name, sheet in bom.sheets.items():
        rules = rulebook.rule_index.get(sheet_name, {})
        if not rules:
            continue
        sheet_rules = list(rules.values())
        for row in sheet.rows:
            matnr = _matnr_of(row)
            _validate_row(sheet_name, sheet_name, row, sheet_rules,
                          catalogs, matnr, errors)

    # Composite-key duplicates
    sheets_for_dup_check = [(name, sh.rows) for name, sh in bom.sheets.items()]
    dup_groups = find_duplicate_groups(sheets_for_dup_check, PP_KEYS_BY_SHEET)
    for group in dup_groups:
        msg = format_duplicate_message(group)
        first_key_col = group["key_columns"][0]
        for occ in group["rows"]:
            matnr = str(occ["values"].get("MATNR") or "").strip()
            if matnr.endswith(".0") and matnr[:-2].lstrip("-").isdigit():
                matnr = matnr[:-2]
            errors.append(PpError(
                sheet=group["sheet"],
                excel_row=occ["excel_row"],
                matnr=matnr or "(blank)",
                sap_field=first_key_col,
                value=str(occ["values"].get(first_key_col) or ""),
                rule_id="pp_duplicate_row",
                rule_name="Duplicate row (composite key)",
                severity="error",
                message=msg,
            ))

    errors.sort(key=lambda e: (e.sheet, e.excel_row, e.sap_field))
    return errors


def validate_routing(
    routing: LoadedRouting,
    catalogs: PpCatalogs,
    rulebook: Rulebook,
) -> list[PpError]:
    """Run all rules against a loaded Routing.

    Routing sheet-name resolution: dependency sheets ("Global Dependency"
    etc.) are stored in the rulebook with a "Routing · " prefix to
    avoid colliding with the BOM dependency sheets. The validator
    resolves rules by trying the prefixed name first, then the natural
    name. The Error Grid sees the natural sheet name (no prefix).
    """
    errors: list[PpError] = []

    for sheet_name, sheet in routing.sheets.items():
        # Try prefixed first (for sheets shared with BOM). Fall back to
        # natural name (for Operations, Sequences, etc., unique to
        # Routing).
        prefixed = f"Routing · {sheet_name}"
        rules = rulebook.rule_index.get(prefixed) or rulebook.rule_index.get(sheet_name) or {}
        if not rules:
            continue
        sheet_rules = list(rules.values())
        for row in sheet.rows:
            plnnr = _matnr_of(row)
            _validate_row(prefixed if prefixed in rulebook.rule_index else sheet_name,
                          sheet_name, row, sheet_rules, catalogs, plnnr, errors)

    # Composite-key duplicates. The duplicate detector keys are stored
    # both with and without the prefix so we just pass the natural
    # sheet name.
    sheets_for_dup_check = []
    for name, sh in routing.sheets.items():
        sheets_for_dup_check.append((name, sh.rows))
        # Also try the prefixed variant in case the natural name
        # collides with a BOM sheet (ROUTING_KEYS_BY_SHEET stores both).
        prefixed = f"Routing · {name}"
        if prefixed in ROUTING_KEYS_BY_SHEET:
            sheets_for_dup_check.append((prefixed, sh.rows))
    dup_groups = find_duplicate_groups(sheets_for_dup_check, ROUTING_KEYS_BY_SHEET)

    # De-duplicate when both prefixed and unprefixed forms of the same
    # sheet matched: the per-row identity (excel_row) is the same, so
    # we collapse on (sheet_natural, excel_row) tuples.
    seen_keys: set[tuple] = set()
    for group in dup_groups:
        sheet = group["sheet"]
        natural = sheet.split(" · ", 1)[1] if sheet.startswith("Routing · ") else sheet
        msg = format_duplicate_message(group)
        first_key_col = group["key_columns"][0]
        for occ in group["rows"]:
            dedup_key = (natural, occ["excel_row"], group["key_values"])
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            plnnr = str(occ["values"].get("PLNNR") or "").strip()
            if plnnr.endswith(".0") and plnnr[:-2].lstrip("-").isdigit():
                plnnr = plnnr[:-2]
            errors.append(PpError(
                sheet=natural,
                excel_row=occ["excel_row"],
                matnr=plnnr or "(blank)",
                sap_field=first_key_col,
                value=str(occ["values"].get(first_key_col) or ""),
                rule_id="rt_duplicate_row",
                rule_name="Duplicate row (composite key)",
                severity="error",
                message=msg,
            ))

    errors.sort(key=lambda e: (e.sheet, e.excel_row, e.sap_field))
    return errors

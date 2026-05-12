"""
Validation Engine — produces structured errors ready for grouping.

Design: every error is an atomic event that can be grouped into a decision.
An error includes enough context to render a "decision card" without extra lookups.

All rules are data-driven from FieldSpec + a small rule registry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .xml_engine import Workbook, SheetData, FieldSpec
from . import kds_reference as _kds_ref

# Pull individual catalogs from the unified CATALOG_BY_RULE so the
# validator uses whatever was loaded (xlsx at startup, or hardcoded
# fallback). This means replacing the KDS file on disk actually takes
# effect after a service restart — no code changes needed.
def _cat(rule_id: str) -> dict[str, str]:
    return _kds_ref.CATALOG_BY_RULE.get(rule_id, {})

# Hardcoded fallbacks are still imported for rules that want to pin
# a subset (e.g. Incoterms is small and stable; we always want those
# exact 30 codes). But the runtime lookup goes through _cat() so new
# codes added to the xlsx are picked up automatically.
from .kds_reference import INCOTERMS
DIVISIONS            = _cat("division_not_in_kds")            or _kds_ref.DIVISIONS
DISTRIBUTION_CHANNELS = _cat("distribution_channel_not_in_kds") or _kds_ref.DISTRIBUTION_CHANNELS
CUSTOMER_GROUPS      = _cat("customer_group_not_in_kds")      or _kds_ref.CUSTOMER_GROUPS
SALES_ORGS           = _cat("sales_org_not_in_kds")           or _kds_ref.SALES_ORGS
SALES_OFFICES        = _cat("sales_office_not_in_kds")        or _kds_ref.SALES_OFFICES
SALES_GROUPS         = _cat("sales_group_not_in_kds")         or {}
SHIPPING_CONDITIONS  = _cat("shipping_condition_not_in_kds")  or _kds_ref.SHIPPING_CONDITIONS

# ────────────────────────────────────────────────────────────────────────────
# Error model
# ────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Error:
    sheet: str
    row_idx: int          # 0-based index into data_rows (internal)
    xml_row: int          # 1-based Excel row number (for user display = row_idx + 9)
    col_idx: int          # 1-based column
    column_label: str     # human-readable
    sap_field: str
    value: str            # current value (string form)
    rule_id: str          # e.g. "length_exceeded", "invalid_gstin"
    rule_name: str        # e.g. "GSTIN length"
    severity: str         # "error" | "warning"
    message: str          # user-friendly
    # When the validator knows the "right" answer from a reference table
    # (e.g. KDS says Incoterms 'CIF' should map to 'Costs Insurance and
    # Freight'), we attach the canonical value here. The UI shows it as
    # a one-click "Apply KDS value" button — zero-typing fix.
    suggested_value: str | None = None
    # Full set of valid values for this field in this context. Populated
    # for KDS-lookup rules so the Fix Individually editor can render a
    # searchable dropdown instead of a free-text input. Each entry is
    # {"value": "PE01", "label": "HEALTHIUM MEDTECH LIMITED"}. For
    # unscoped rules (Plant, Material Group) this is the whole catalog;
    # for scoped rules (Storage Location under a specific Plant) it's
    # just the subset valid in that context. Capped at 500 entries
    # defensively — above that, the UI should fall back to free-text
    # with autocomplete rather than rendering 2000 option elements.
    suggested_options: list[dict] = field(default_factory=list)
    # For format rules (PAN/GSTIN/etc.) where specific character positions
    # fail validation, we list the bad positions so the UI can underline
    # them in red. Each entry: {"pos": 0-based index, "expected": human
    # description, "got": actual character}. Empty list if not applicable.
    char_issues: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sheet": self.sheet,
            "row_idx": self.row_idx,
            "xml_row": self.xml_row,
            "col_idx": self.col_idx,
            "column_label": self.column_label,
            "sap_field": self.sap_field,
            "value": self.value,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "message": self.message,
            "suggested_value": self.suggested_value,
            "suggested_options": self.suggested_options,
            "char_issues": self.char_issues,
        }


# ────────────────────────────────────────────────────────────────────────────
# Patterns
# ────────────────────────────────────────────────────────────────────────────

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE   = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _diagnose_pan(value: str) -> list[dict]:
    """Position-level issues for a PAN value. Format: AAAAA9999A (10 chars).
    Positions 0-4 letter A-Z · 5-8 digit 0-9 · 9 letter A-Z.
    Returns list of {pos, expected, got}. Empty list = valid.
    If length wrong, returns single entry with pos=-1.
    """
    v = value.upper().replace(" ", "")
    if len(v) != 10:
        return [{"pos": -1, "expected": "10 chars", "got": f"{len(v)} chars"}]
    issues: list[dict] = []
    for i in range(10):
        ch = v[i]
        if i < 5 or i == 9:
            if not (ch.isalpha() and ch.isascii()):
                issues.append({"pos": i, "expected": "letter", "got": ch})
        else:
            if not ch.isdigit():
                issues.append({"pos": i, "expected": "digit", "got": ch})
    return issues


def _gstin_checksum(first14: str) -> str:
    """Compute the 15th check character for a GSTIN given its first 14 chars.
    Standard algorithm per GSTN spec:
      - alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ' (base-36)
      - for each char position i (0-indexed), factor = (i%2)+1 (alternates 1,2)
      - product = char_value * factor
      - digit_sum = product/36 + product%36  (sum of "digits" in base 36)
      - total = sum of all digit_sums
      - checksum_value = (36 - (total % 36)) % 36
      - return alphabet[checksum_value]
    """
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for i, ch in enumerate(first14):
        if ch not in alphabet:
            return ""   # can't compute; non-base36 char
        v = alphabet.index(ch) * ((i % 2) + 1)
        total += (v // 36) + (v % 36)
    return alphabet[(36 - (total % 36)) % 36]


def _diagnose_gstin(value: str) -> list[dict]:
    """Position-level issues for a GSTIN value. Format 15 chars:
      0-1 digit · 2-6 letter · 7-10 digit · 11 letter · 12 1-9/A-Z
      13 'Z' · 14 checksum (digit/letter).

    Returns per-position issues so the UI can render the chip-grid
    highlighter (same pattern as PAN). For short GSTINs, the FIRST
    missing position is flagged as "missing digit/letter" so the user
    sees where data was cut off; for long GSTINs, the overflow
    positions are flagged as "extra char".

    If length/format are OK, also verifies the 15th checksum character
    against the computed value from positions 0-13.
    """
    v = value.upper().replace(" ", "")
    # Expected type at each of the 15 canonical positions. Used both for
    # per-position checks and for the "missing" marker on short inputs.
    expected_at_pos = [
        "digit", "digit",
        "letter", "letter", "letter", "letter", "letter",
        "digit", "digit", "digit", "digit",
        "letter",
        "1-9 or A-Z",
        "'Z'",
        "digit or letter",
    ]

    issues: list[dict] = []

    if len(v) < 15:
        # Walk existing positions first — a 14-char GSTIN may have issues
        # before position 14 as well as being short. SME needs to see both.
        for i in range(len(v)):
            exp = expected_at_pos[i]
            ch = v[i]
            bad = False
            if exp == "digit": bad = not ch.isdigit()
            elif exp == "letter": bad = not (ch.isalpha() and ch.isascii())
            elif exp == "1-9 or A-Z":
                bad = not ((ch.isdigit() and ch != '0') or (ch.isalpha() and ch.isascii()))
            elif exp == "'Z'": bad = ch != 'Z'
            elif exp == "digit or letter": bad = not (ch.isalnum() and ch.isascii())
            if bad:
                issues.append({"pos": i, "expected": exp, "got": ch})
        # Then mark the FIRST missing position so the chip grid shows
        # where the SME should add content. Showing all missing positions
        # as chips would clutter; one clear arrow is enough.
        if len(v) < 15:
            issues.append({
                "pos": len(v),
                "expected": expected_at_pos[len(v)],
                "got": "(missing)",
            })
        return issues

    if len(v) > 15:
        # Extra characters — flag each one beyond position 14
        for i in (0, 1):
            if not v[i].isdigit(): issues.append({"pos": i, "expected": "digit", "got": v[i]})
        # Check the valid 15-char prefix first so the SME can see both
        # the structural problems (if any) and the extra tail.
        for i in range(2, 7):
            if not (v[i].isalpha() and v[i].isascii()):
                issues.append({"pos": i, "expected": "letter", "got": v[i]})
        for i in range(7, 11):
            if not v[i].isdigit(): issues.append({"pos": i, "expected": "digit", "got": v[i]})
        if not (v[11].isalpha() and v[11].isascii()):
            issues.append({"pos": 11, "expected": "letter", "got": v[11]})
        ok_12 = (v[12].isdigit() and v[12] != '0') or (v[12].isalpha() and v[12].isascii())
        if not ok_12: issues.append({"pos": 12, "expected": "1-9 or A-Z", "got": v[12]})
        if v[13] != 'Z': issues.append({"pos": 13, "expected": "'Z'", "got": v[13]})
        if not (v[14].isalnum() and v[14].isascii()):
            issues.append({"pos": 14, "expected": "digit or letter", "got": v[14]})
        # Flag first extra char — enough to communicate "too long"
        issues.append({"pos": 15, "expected": "(end)", "got": f"extra: {v[15:][:3]}"})
        return issues

    # Length == 15: standard structural check + checksum
    def bad(i: int, label: str):
        issues.append({"pos": i, "expected": label, "got": v[i]})
    for i in (0, 1):
        if not v[i].isdigit(): bad(i, "digit")
    for i in (2, 3, 4, 5, 6):
        if not (v[i].isalpha() and v[i].isascii()): bad(i, "letter")
    for i in (7, 8, 9, 10):
        if not v[i].isdigit(): bad(i, "digit")
    if not (v[11].isalpha() and v[11].isascii()): bad(11, "letter")
    ok_12 = (v[12].isdigit() and v[12] != '0') or (v[12].isalpha() and v[12].isascii())
    if not ok_12: bad(12, "1-9 or A-Z")
    if v[13] != 'Z': bad(13, "'Z'")
    if not (v[14].isalnum() and v[14].isascii()): bad(14, "digit or letter")
    # Only check checksum if positions 0-13 are structurally OK (else
    # a bad checksum alarm just adds noise to a bigger problem).
    if not issues:
        expected_check = _gstin_checksum(v[:14])
        if expected_check and v[14] != expected_check:
            issues.append({
                "pos": 14,
                "expected": f"checksum '{expected_check}'",
                "got": v[14],
            })
    return issues


def _gstin_checksum_ok(value: str) -> bool:
    """True if a 15-char GSTIN passes the Mod-36 checksum check."""
    v = value.upper().replace(" ", "")
    if len(v) != 15:
        return False
    expected = _gstin_checksum(v[:14])
    return bool(expected) and v[14] == expected


# Values that should bypass format checks (valid business values)
BYPASS_TAX_VALUES = {"URP"}

# Junk markers — strings that look like data but clearly aren't
JUNK_VALUES = {"NAN", "NULL", "N/A", "NA", "TBD", "XX", "XXX", "NIL", "NONE", "-", "--"}

# Canonical SAP Indian state codes (T005S / GST State Code).
# Source: GST state code list used by SAP J_1ISTATECDM.
INDIA_STATE_CODE_NAMES = {
    "01": "Jammu & Kashmir",        "02": "Himachal Pradesh",
    "03": "Punjab",                 "04": "Chandigarh",
    "05": "Uttarakhand",            "06": "Haryana",
    "07": "Delhi",                  "08": "Rajasthan",
    "09": "Uttar Pradesh",          "10": "Bihar",
    "11": "Sikkim",                 "12": "Arunachal Pradesh",
    "13": "Nagaland",               "14": "Manipur",
    "15": "Mizoram",                "16": "Tripura",
    "17": "Meghalaya",              "18": "Assam",
    "19": "West Bengal",            "20": "Jharkhand",
    "21": "Odisha",                 "22": "Chhattisgarh",
    "23": "Madhya Pradesh",         "24": "Gujarat",
    "25": "Daman & Diu",            "26": "Dadra & Nagar Haveli",
    "27": "Maharashtra",            "28": "Andhra Pradesh (old)",
    "29": "Karnataka",              "30": "Goa",
    "31": "Lakshadweep",            "32": "Kerala",
    "33": "Tamil Nadu",             "34": "Puducherry",
    "35": "Andaman & Nicobar",      "36": "Telangana",
    "37": "Andhra Pradesh (new)",   "38": "Ladakh",
}
# Legacy 2-letter codes some files may still use
INDIA_STATE_CODES_LEGACY = {
    "AP","AR","AS","BR","CG","DL","GA","GJ","HR","HP","JK","JH","KA","KL",
    "MP","MH","MN","ML","MZ","NL","OD","PB","RJ","SK","TN","TS","TR","UP",
    "UK","UT","WB","AN","CH","DN","DD","LD","PY","LA",
}
INDIA_STATE_CODES = set(INDIA_STATE_CODE_NAMES.keys()) | INDIA_STATE_CODES_LEGACY


# ────────────────────────────────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────────────────────────────────

def _get_row_field(row: dict, specs: list[FieldSpec], label: str) -> Any:
    """Get a value from a row by column label."""
    for s in specs:
        if s.label == label:
            return row.get(s.col_idx)
    return None


def _str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _is_blank(v: Any) -> bool:
    return v is None or _str(v) == ""


def _check_catalog_value(errors: list["Error"], sheet: "SheetData", idx: int, xml_row: int,
                         row: dict, spec: "FieldSpec | None",
                         catalog: dict[str, str], rule_id: str, rule_name: str,
                         pad_to: int | None = None) -> None:
    """If `row[spec]` is non-blank but not in `catalog`, append an error.

    The catalog is a code→description dict from kds_reference. For coded
    numeric fields we try BOTH the padded and unpadded forms — XML files
    from SAP exports are inconsistent: Division may come as "1" while
    Shipping Conditions come as "01", and the KDS may have stored them
    the other way. Accept either representation to avoid false positives.

    Blank values are ignored here — mandatory-field checks handle those.
    No `suggested_value` because there are many valid codes; the SME picks
    from a dropdown via the smart editor rather than getting an auto-guess.
    """
    if spec is None:
        return
    raw = _str(row.get(spec.col_idx))
    if not raw:
        return
    key = raw.upper()
    # Try: as-is, padded, unpadded (stripped leading zeros).
    if key in catalog:
        return
    if key.isdigit():
        if pad_to and key.zfill(pad_to) in catalog:
            return
        # Try unpadded (strip leading zeros but keep "0" if all zeros)
        unpadded = key.lstrip("0") or "0"
        if unpadded in catalog:
            return
    # Still unmatched — flag
    sample = ", ".join(list(catalog.keys())[:4])
    errors.append(Error(
        sheet=sheet.name, row_idx=idx, xml_row=xml_row,
        col_idx=spec.col_idx, column_label=spec.label,
        sap_field=spec.sap_field,
        value=raw,
        rule_id=rule_id,
        rule_name=rule_name,
        severity="error",
        message=(f"'{raw}' is not a valid {spec.label} per KDS. "
                 f"Expected one of {len(catalog)} KDS codes (e.g. {sample}…)."),
    ))


# ────────────────────────────────────────────────────────────────────────────
# Per-sheet validators
# ────────────────────────────────────────────────────────────────────────────

def _validate_general_data(sheet: SheetData, sales_sheet: SheetData | None) -> list[Error]:
    errors: list[Error] = []
    specs = sheet.specs

    # Find important columns once
    def find_col(label: str) -> FieldSpec | None:
        return next((s for s in specs if s.label == label), None)

    country_spec = find_col("Country/Region")
    state_spec = None
    postal_spec = find_col("Postal Code")
    pan_spec = find_col("Permanent Account Number")
    cust_spec = find_col("Customer Number")

    # Disambiguate "State" column — use one AFTER Country/Region
    if country_spec:
        for s in specs:
            if s.label == "State" and s.col_idx > country_spec.col_idx:
                state_spec = s
                break

    # ── Per-customer Sales Org index ──────────────────────────────
    # Build cust_num → set of sales org codes from Sales Data. Used to
    # suppress India-only validations (PAN, GSTIN, DL) for UK02 customers
    # and any other non-India sales orgs. A customer extended to multiple
    # sales orgs only gets India rules if AT LEAST ONE of them is India.
    # That matters because the same PAN field is shared across sales orgs.
    NON_INDIA_SALES_ORGS = {"UK02"}   # extend here as more non-India orgs exist
    cust_to_sorgs: dict[str, set[str]] = {}
    if sales_sheet is not None:
        sd_specs = sales_sheet.specs
        sd_cust = next((s for s in sd_specs if s.label == "Customer Number"), None)
        sd_sorg = next((s for s in sd_specs if s.label == "Sales Organization"), None)
        if sd_cust and sd_sorg:
            for sdrow in sales_sheet.data_rows:
                cn = _str(sdrow.get(sd_cust.col_idx))
                so = _str(sdrow.get(sd_sorg.col_idx)).upper()
                if cn and so:
                    cust_to_sorgs.setdefault(cn, set()).add(so)

    def _is_india_only_customer(cust_num: str) -> bool:
        """A customer is 'India-only' if all its sales orgs are Indian (IN*).
        If they're extended to UK02 and no IN org, we skip India rules."""
        sorgs = cust_to_sorgs.get(cust_num)
        if not sorgs:
            return True  # default: India rules apply (current file is IN02)
        # If ANY sales org is India, we still validate India fields
        return any(so not in NON_INDIA_SALES_ORGS for so in sorgs)

    # DL columns
    dl_num_spec = next((s for s in specs if "Drug Licence Number" in s.label or "DRUGLICENCENUMBER" in s.sap_field.upper()), None)
    dl_exp_spec = next((s for s in specs
                        if "Drug Licence Expiry" in s.label
                        or "Drug License Expiry" in s.label
                        or "Drug Licence Expiration" in s.label
                        or "Drug License Expiration" in s.label
                        or "DL Expiry" in s.label
                        or "DL Expiration" in s.label
                        or "Drug Expiry" in s.label
                        or "DRUGLICENCEEXPIRY" in s.sap_field.upper()
                        or "DLEXPIRY" in s.sap_field.upper().replace("_", "")
                        or "DRUGLICEXPIRY" in s.sap_field.upper().replace("_", "")
                        or "DRUGLIC" in s.sap_field.upper() and "EXPIR" in s.sap_field.upper()), None)
    # One-time log so customer can verify on their real file whether column was detected
    if dl_exp_spec:
        print(f"[validator] Drug Licence Expiry column detected: col {dl_exp_spec.col_idx} '{dl_exp_spec.label}' (sap={dl_exp_spec.sap_field})")
    else:
        # List columns that LOOKED like DL to help diagnose mismatch
        candidates = [s for s in specs if any(k in s.label.lower() for k in ['drug','licence','license','expir','dl ']) or any(k in s.sap_field.upper() for k in ['DRUG','LICENCE','LICENSE','EXPIR'])]
        if candidates:
            print(f"[validator] Drug Licence Expiry column NOT matched. Candidates in this file:")
            for c in candidates:
                print(f"  col {c.col_idx}: label='{c.label}' sap='{c.sap_field}'")
        else:
            print(f"[validator] No Drug Licence columns of any kind in this file.")

    # Build customer → distribution channel lookup from sales sheet
    dc_lookup: dict[str, str] = {}
    if sales_sheet:
        cust_s = next((s for s in sales_sheet.specs if s.label == "Customer Number"), None)
        dc_s = next((s for s in sales_sheet.specs if s.label == "Distribution Channel"), None)
        if cust_s and dc_s:
            for r in sales_sheet.data_rows:
                cn = _str(r.get(cust_s.col_idx))
                dc = _str(r.get(dc_s.col_idx))
                if cn:
                    dc_lookup[cn] = dc

    for idx, row in enumerate(sheet.data_rows):
        xml_row = idx + 9
        customer_num = _str(_get_row_field(row, specs, "Customer Number"))
        country = _str(_get_row_field(row, specs, "Country/Region")).upper()
        dc = dc_lookup.get(customer_num, "")

        # Heuristic: domestic if country=IN AND no export DC marker
        is_export = country and country != "IN"
        is_domestic = not is_export

        # UK02 (and other non-India sales orgs) → skip India-only rules
        # even if Country/Region is blank or set to IN by default. The
        # sales-org linkage is the authoritative signal, not the address.
        is_india_rules = is_domestic and _is_india_only_customer(customer_num)

        # Rule: all text fields — length check
        for s in specs:
            val = row.get(s.col_idx)
            if _is_blank(val):
                if s.mandatory:
                    errors.append(Error(
                        sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                        col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                        value="",
                        rule_id="mandatory_missing",
                        rule_name="Mandatory field missing",
                        severity="error",
                        message=f"'{s.label}' is marked mandatory but is empty.",
                    ))
                continue

            sval = _str(val)

            # Length check against ETE declared length
            if s.ete_type == "C" and s.ete_length > 0 and len(sval) > s.ete_length:
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                    value=sval[:100],
                    rule_id="length_exceeded",
                    rule_name=f"{s.label} length exceeds {s.ete_length}",
                    severity="error",
                    message=f"'{s.label}' is {len(sval)} chars — max allowed {s.ete_length}.",
                ))

            # Junk value detection
            if sval.upper() in JUNK_VALUES:
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                    value=sval,
                    rule_id="junk_value",
                    rule_name=f"Junk value in {s.label}",
                    severity="warning",
                    message=f"Value '{sval}' looks like a placeholder, not real data.",
                ))

        # India state code — if user entered a single digit (e.g. "8" or "6"),
        # propose the zero-padded two-digit version as a suggestion. Common
        # mistake when data is entered from Excel without text formatting
        # (Excel strips leading zeros). Also covers 2-letter legacy codes
        # like "DL" → suggest "07" if the mapping is obvious.
        if state_spec and country == "IN":
            state_val = _str(row.get(state_spec.col_idx))
            if state_val and state_val.upper() not in INDIA_STATE_CODES:
                # Suggest zero-pad if single digit 1-9
                suggestion = None
                if state_val.isdigit() and 1 <= int(state_val) <= 9:
                    padded = state_val.zfill(2)
                    if padded in INDIA_STATE_CODE_NAMES:
                        suggestion = padded
                # Build message with concrete suggestion when we have one
                base_msg = (f"'{state_val}' is not a valid SAP India state code. "
                            f"Use the GST State Code (e.g., 07=Delhi, 24=Gujarat, "
                            f"27=Maharashtra, 29=Karnataka, 33=Tamil Nadu).")
                if suggestion:
                    state_name = INDIA_STATE_CODE_NAMES[suggestion]
                    base_msg = (f"'{state_val}' is not a valid state code. "
                                f"Did you mean '{suggestion}' ({state_name})? "
                                f"Excel often strips leading zeros from state codes.")
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=state_spec.col_idx, column_label=state_spec.label,
                    sap_field=state_spec.sap_field,
                    value=state_val,
                    rule_id="invalid_state_in",
                    rule_name="Invalid India state code",
                    severity="error",
                    message=base_msg,
                    suggested_value=suggestion,
                ))

        # Export postal code max 10
        if is_export and postal_spec:
            pc = _str(row.get(postal_spec.col_idx))
            if pc and len(pc) > 10:
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=postal_spec.col_idx, column_label=postal_spec.label,
                    sap_field=postal_spec.sap_field,
                    value=pc,
                    rule_id="export_postal_long",
                    rule_name="Export postal code > 10",
                    severity="error",
                    message=f"Export customer postal code '{pc}' is {len(pc)} chars; SAP max = 10.",
                ))

        # PAN format (India customers only — UK02 et al. don't use PAN)
        if is_india_rules and pan_spec:
            pan = _str(row.get(pan_spec.col_idx))
            if pan and pan.upper() not in BYPASS_TAX_VALUES and not PAN_RE.match(pan.upper()):
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=pan_spec.col_idx, column_label=pan_spec.label,
                    sap_field=pan_spec.sap_field,
                    value=pan,
                    rule_id="invalid_pan",
                    rule_name="Invalid PAN format",
                    severity="error",
                    message=f"PAN '{pan}' does not match AAAAA9999A format.",
                    char_issues=_diagnose_pan(pan),
                ))

        # Drug Licence — flag if EXPIRED.
        # NOTE: domestic/export gate removed. DL is an India-specific field, so its
        # mere presence on the row means it applies. If column exists and value
        # parses as a past date, flag it.
        # Drug Licence — India-only. Skip entirely for UK02 customers.
        if is_india_rules and dl_exp_spec:
            dl_raw = row.get(dl_exp_spec.col_idx)
            if not _is_blank(dl_raw):
                from datetime import datetime
                dl_s = _str(dl_raw)
                parsed = None
                # Comprehensive format list — handles Excel date cells, SAP exports, and manual entries
                for fmt in (
                    "%Y-%m-%dT%H:%M:%S.%f",   # 2025-10-28T00:00:00.000  (from screenshot)
                    "%Y-%m-%dT%H:%M:%S",       # 2025-10-28T00:00:00
                    "%Y-%m-%d %H:%M:%S",       # 2025-10-28 00:00:00
                    "%Y-%m-%d",                # 2025-10-28
                    "%d-%b-%Y",                # 28-Oct-2025
                    "%d-%B-%Y",                # 28-October-2025
                    "%d/%m/%Y",                # 28/10/2025
                    "%d-%m-%Y",                # 28-10-2025
                    "%m/%d/%Y",                # 10/28/2025 (US style fallback)
                    "%d.%m.%Y",                # 28.10.2025 (SAP German style)
                ):
                    try:
                        parsed = datetime.strptime(dl_s, fmt)
                        break
                    except (ValueError, TypeError):
                        continue
                # Also try to handle Excel serial date numbers (e.g. "45929" for 2025-10-28)
                if parsed is None:
                    try:
                        serial = float(dl_s)
                        if 20000 < serial < 80000:  # sanity range: ~1954 to ~2119
                            from datetime import timedelta
                            parsed = datetime(1899, 12, 30) + timedelta(days=serial)
                    except (ValueError, TypeError):
                        pass

                if parsed and parsed < datetime.now():
                    errors.append(Error(
                        sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                        col_idx=dl_exp_spec.col_idx, column_label=dl_exp_spec.label,
                        sap_field=dl_exp_spec.sap_field,
                        value=parsed.strftime("%d-%b-%Y"),
                        rule_id="dl_expired",
                        rule_name="Drug Licence expired",
                        severity="error",
                        message=f"DL expired on {parsed.strftime('%d-%b-%Y')}. Renew before LTMC upload.",
                    ))

    return errors


def _validate_sales_data(sheet: SheetData) -> list[Error]:
    errors: list[Error] = []
    specs = sheet.specs

    # Find columns
    def find_col(label: str) -> FieldSpec | None:
        return next((s for s in specs if s.label == label), None)

    inco_spec = find_col("Incoterms")
    loc_spec = next((s for s in specs if "Location" in s.label and "Inco" in s.label), None)
    cust_spec = find_col("Customer Number")
    sorg_spec = find_col("Sales Organization")
    dc_spec = find_col("Distribution Channel")
    div_spec = find_col("Division")

    seen_keys: dict[tuple, int] = {}

    for idx, row in enumerate(sheet.data_rows):
        xml_row = idx + 9

        # Duplicate key detection
        if cust_spec and sorg_spec and dc_spec and div_spec:
            key = (
                _str(row.get(cust_spec.col_idx)),
                _str(row.get(sorg_spec.col_idx)),
                _str(row.get(dc_spec.col_idx)),
                _str(row.get(div_spec.col_idx)),
            )
            if all(key):
                if key in seen_keys:
                    errors.append(Error(
                        sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                        col_idx=cust_spec.col_idx, column_label=cust_spec.label,
                        sap_field=cust_spec.sap_field,
                        value=key[0],
                        rule_id="duplicate_record",
                        rule_name="Duplicate Sales Data record",
                        severity="error",
                        message=f"Customer {key[0]} already extended to SOrg={key[1]} / DC={key[2]} / Div={key[3]} (row {seen_keys[key]}).",
                    ))
                else:
                    seen_keys[key] = xml_row

        # ── KDS catalog validation for coded Sales Data fields ──
        # Each coded field (Division, Distribution Channel, Customer Group,
        # Sales Org, etc.) must be a value in the KDS reference. This
        # catches typos, outdated codes, and free-text entries in what
        # should be a dropdown. No auto-suggestion — too many valid
        # options to guess; SME picks from the catalog via dropdown.
        _check_catalog_value(errors, sheet, idx, xml_row, row, sorg_spec,
                             SALES_ORGS, "sales_org_not_in_kds",
                             "Sales Organization not in KDS")
        _check_catalog_value(errors, sheet, idx, xml_row, row, dc_spec,
                             DISTRIBUTION_CHANNELS, "distribution_channel_not_in_kds",
                             "Distribution Channel not in KDS")
        _check_catalog_value(errors, sheet, idx, xml_row, row, div_spec,
                             DIVISIONS, "division_not_in_kds",
                             "Division not in KDS",
                             pad_to=2)
        _check_catalog_value(errors, sheet, idx, xml_row, row,
                             find_col("Customer Group"),
                             CUSTOMER_GROUPS, "customer_group_not_in_kds",
                             "Customer Group not in KDS")
        _check_catalog_value(errors, sheet, idx, xml_row, row,
                             find_col("Sales Office"),
                             SALES_OFFICES, "sales_office_not_in_kds",
                             "Sales Office (state/country) not in KDS",
                             pad_to=2)
        _check_catalog_value(errors, sheet, idx, xml_row, row,
                             find_col("Sales Group"),
                             SALES_GROUPS, "sales_group_not_in_kds",
                             "Sales Group not in KDS",
                             pad_to=2)
        _check_catalog_value(errors, sheet, idx, xml_row, row,
                             find_col("Shipping Conditions"),
                             SHIPPING_CONDITIONS, "shipping_condition_not_in_kds",
                             "Shipping Condition not in KDS")

        # If the Incoterms code is a valid KDS entry, the Inco. Location1
        # field is EXPECTED to hold the KDS description (e.g. CIF →
        # "Costs Insurance and Freight"). Anything else is flagged as
        # an error with the canonical KDS value as a one-click fix.
        if inco_spec and loc_spec:
            inco_val = _str(row.get(inco_spec.col_idx)).strip().upper()
            loc_val = _str(row.get(loc_spec.col_idx))
            if inco_val and inco_val in INCOTERMS:
                expected = INCOTERMS[inco_val]
                if loc_val.strip() != expected:
                    errors.append(Error(
                        sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                        col_idx=loc_spec.col_idx, column_label=loc_spec.label,
                        sap_field=loc_spec.sap_field,
                        value=loc_val,
                        rule_id="inco_location_description",
                        rule_name="Incoterms Location does not match KDS",
                        severity="error",
                        message=(f"Incoterm '{inco_val}' maps to '{expected}' per KDS. "
                                 f"Current value: '{loc_val or '(blank)'}'."),
                        suggested_value=expected,
                    ))

        # Generic length check on all fields
        for s in specs:
            val = row.get(s.col_idx)
            if _is_blank(val):
                if s.mandatory:
                    errors.append(Error(
                        sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                        col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                        value="",
                        rule_id="mandatory_missing",
                        rule_name="Mandatory field missing",
                        severity="error",
                        message=f"'{s.label}' is marked mandatory but is empty.",
                    ))
                continue
            sval = _str(val)
            if s.ete_type == "C" and s.ete_length > 0 and len(sval) > s.ete_length:
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                    value=sval[:100],
                    rule_id="length_exceeded",
                    rule_name=f"{s.label} length exceeds {s.ete_length}",
                    severity="error",
                    message=f"'{s.label}' is {len(sval)} chars — max allowed {s.ete_length}.",
                ))

    return errors


def _validate_tax_numbers(sheet: SheetData, general_sheet: SheetData | None,
                          sales_sheet: SheetData | None = None) -> list[Error]:
    errors: list[Error] = []
    specs = sheet.specs

    def find_col(label: str) -> FieldSpec | None:
        return next((s for s in specs if s.label == label), None)

    cat_spec = find_col("Tax Number Category")
    tn_spec = find_col("Tax Number")
    cust_spec = find_col("Customer Number")

    # Country lookup
    ctry_lookup: dict[str, str] = {}
    if general_sheet:
        gc = next((s for s in general_sheet.specs if s.label == "Customer Number"), None)
        gctry = next((s for s in general_sheet.specs if s.label == "Country/Region"), None)
        if gc and gctry:
            for r in general_sheet.data_rows:
                cn = _str(r.get(gc.col_idx))
                co = _str(r.get(gctry.col_idx)).upper()
                if cn:
                    ctry_lookup[cn] = co

    # Per-customer Sales Org index — mirrors the one in _validate_general_data
    # so that UK02 (and other non-India sales orgs) can skip GSTIN/PAN checks.
    NON_INDIA_SALES_ORGS = {"UK02"}
    cust_to_sorgs: dict[str, set[str]] = {}
    if sales_sheet is not None:
        sd_cust = next((s for s in sales_sheet.specs if s.label == "Customer Number"), None)
        sd_sorg = next((s for s in sales_sheet.specs if s.label == "Sales Organization"), None)
        if sd_cust and sd_sorg:
            for sdrow in sales_sheet.data_rows:
                cn = _str(sdrow.get(sd_cust.col_idx))
                so = _str(sdrow.get(sd_sorg.col_idx)).upper()
                if cn and so:
                    cust_to_sorgs.setdefault(cn, set()).add(so)

    def _is_india_customer(cust_num: str) -> bool:
        sorgs = cust_to_sorgs.get(cust_num)
        if not sorgs:
            return True   # default: apply India rules if unknown
        return any(so not in NON_INDIA_SALES_ORGS for so in sorgs)

    if not (tn_spec and cat_spec):
        return errors

    for idx, row in enumerate(sheet.data_rows):
        xml_row = idx + 9
        tn = _str(row.get(tn_spec.col_idx))
        cat = _str(row.get(cat_spec.col_idx)).upper()

        if not tn:
            errors.append(Error(
                sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                col_idx=tn_spec.col_idx, column_label=tn_spec.label,
                sap_field=tn_spec.sap_field,
                value="",
                rule_id="tax_number_blank",
                rule_name="Tax Number blank",
                severity="error",
                message="Tax Number cannot be blank. Use GSTIN, PAN, or 'URP' for unregistered customers.",
            ))
            continue

        # Bypass URP
        if tn.upper() in BYPASS_TAX_VALUES:
            continue

        # Skip India tax format rules for non-India customers (e.g. UK02).
        # The row is still KEPT on the Tax Numbers sheet, just not format-
        # checked against GSTIN/PAN patterns which don't apply to them.
        cust_num = _str(row.get(cust_spec.col_idx)) if cust_spec else ""
        if not _is_india_customer(cust_num):
            continue

        if cat == "IN3":  # GSTIN
            g = tn.upper().replace(" ", "")
            if len(g) != 15:
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=tn_spec.col_idx, column_label=tn_spec.label,
                    sap_field=tn_spec.sap_field,
                    value=tn,
                    rule_id="gstin_length",
                    rule_name="GSTIN length invalid",
                    severity="error",
                    message=f"GSTIN must be 15 chars (actual: {len(g)}). If unregistered, use 'URP'.",
                    char_issues=_diagnose_gstin(tn),
                ))
            elif not GSTIN_RE.match(g):
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=tn_spec.col_idx, column_label=tn_spec.label,
                    sap_field=tn_spec.sap_field,
                    value=tn,
                    rule_id="gstin_format",
                    rule_name="GSTIN format invalid",
                    severity="error",
                    message=f"GSTIN '{tn}' format invalid. Expected: SS+5 letters+4 digits+1 letter+1 alnum+Z+1 alnum.",
                    char_issues=_diagnose_gstin(tn),
                ))
            elif not _gstin_checksum_ok(g):
                # Format passes the regex, but the final check-digit is wrong.
                # Common cause: typo in 14th position, or the user typed 14
                # characters and one was auto-corrected. The expected check
                # digit comes out of the Mod-36 algorithm so we can propose
                # the correction directly.
                expected_check = _gstin_checksum(g[:14])
                corrected = g[:14] + expected_check if expected_check else None
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=tn_spec.col_idx, column_label=tn_spec.label,
                    sap_field=tn_spec.sap_field,
                    value=tn,
                    rule_id="gstin_checksum",
                    rule_name="GSTIN check digit invalid",
                    severity="error",
                    message=(f"GSTIN '{tn}' last character (check digit) is wrong. "
                             f"Expected '{expected_check}' based on the first 14 characters. "
                             f"Verify the GSTIN against the GST portal."),
                    suggested_value=corrected,
                    char_issues=_diagnose_gstin(tn),
                ))

        elif cat == "IN1":  # PAN
            if not PAN_RE.match(tn.upper().replace(" ", "")):
                errors.append(Error(
                    sheet=sheet.name, row_idx=idx, xml_row=xml_row,
                    col_idx=tn_spec.col_idx, column_label=tn_spec.label,
                    sap_field=tn_spec.sap_field,
                    value=tn,
                    rule_id="pan_format",
                    rule_name="PAN format invalid",
                    severity="error",
                    message=f"PAN '{tn}' does not match AAAAA9999A format.",
                    char_issues=_diagnose_pan(tn),
                ))

    return errors


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────

def validate(wb: Workbook) -> list[Error]:
    errors: list[Error] = []

    general = wb.sheets.get("General Data")
    sales = wb.sheets.get("Sales Data")
    tax = wb.sheets.get("Tax Numbers")

    if general:
        errors.extend(_validate_general_data(general, sales))
    if sales:
        errors.extend(_validate_sales_data(sales))
    if tax:
        errors.extend(_validate_tax_numbers(tax, general, sales))

    # Generic length checks on other sheets
    for name, sheet in wb.sheets.items():
        if name in ("General Data", "Sales Data", "Tax Numbers"):
            continue
        if not sheet.data_rows:
            continue
        for idx, row in enumerate(sheet.data_rows):
            xml_row = idx + 9
            for s in sheet.specs:
                val = row.get(s.col_idx)
                if _is_blank(val):
                    if s.mandatory:
                        errors.append(Error(
                            sheet=name, row_idx=idx, xml_row=xml_row,
                            col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                            value="",
                            rule_id="mandatory_missing",
                            rule_name="Mandatory field missing",
                            severity="error",
                            message=f"'{s.label}' is marked mandatory but is empty.",
                        ))
                    continue
                sval = _str(val)
                if s.ete_type == "C" and s.ete_length > 0 and len(sval) > s.ete_length:
                    errors.append(Error(
                        sheet=name, row_idx=idx, xml_row=xml_row,
                        col_idx=s.col_idx, column_label=s.label, sap_field=s.sap_field,
                        value=sval[:100],
                        rule_id="length_exceeded",
                        rule_name=f"{s.label} length exceeds {s.ete_length}",
                        severity="error",
                        message=f"'{s.label}' is {len(sval)} chars — max allowed {s.ete_length}.",
                    ))
    return errors

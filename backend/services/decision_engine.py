"""
Decision Engine — the heart of the product.

Transforms flat error lists into grouped "Decisions" that users can act on in bulk.

Grouping strategy:
  PATTERN decisions: many rows with IDENTICAL (rule, column, value)
    → offer bulk-accept/bulk-replace
  INDIVIDUAL decisions: rows with same rule but DIFFERENT values
    → reviewed case-by-case

Each decision carries enough context for a card UI without further queries.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .validator import Error


# Threshold — if N+ rows share the same value, treat as pattern
PATTERN_THRESHOLD = 2


@dataclass(slots=True)
class Decision:
    decision_id: str           # stable ID: rule_id + sheet + col + value hash
    kind: str                  # "pattern" | "individual"
    sheet: str
    rule_id: str
    rule_name: str
    severity: str
    column_label: str
    sap_field: str
    col_idx: int               # 1-based column index (used by applier)
    affected_count: int
    sample_value: str          # representative value
    all_values: list[str]      # unique values (capped for size)
    error_row_indexes: list[int]  # row indexes in data_rows
    xml_rows: list[int]        # XML row numbers (row_idx + 9)
    suggested_actions: list[dict]

    def as_dict(self) -> dict:
        # A decision is "categorical" (worth Group & Replace UI) when its
        # errors cluster into few distinct values OR when the rule has a
        # KDS catalog backing it. The list here mirrors CATALOG_BY_RULE
        # in services/kds_reference.py; kept in sync so the UI doesn't
        # need an extra round-trip to figure out which decisions should
        # show the Group & Replace button.
        _catalog_rules = {
            "inco_location_description",
            # sales_group_not_in_kds stays categorical (it IS a small set of
            # distinct bad values) even though we don't hand it a catalog —
            # SMEs still benefit from group-replace; they just type the
            # correct code instead of picking a suggestion.
            "sales_group_not_in_kds",
            "sales_office_not_in_kds", "customer_group_not_in_kds",
            "division_not_in_kds", "distribution_channel_not_in_kds",
            "sales_org_not_in_kds", "sales_district_not_in_kds",
            "shipping_condition_not_in_kds",
        }
        # Threshold matches GROUP_CATEGORICAL_MAX_DISTINCT in main.py
        _categorical_distinct_threshold = 20
        is_cat = (
            self.rule_id in _catalog_rules
            or (len(self.all_values) > 0 and len(self.all_values) <= _categorical_distinct_threshold)
        )
        # Group & Replace only useful if there are multiple cells to
        # replace. Skip it when every error is unique (e.g. gstin_length
        # with 2 errors → 2 distinct values → ratio 1:1, pointless).
        if self.affected_count > 0 and len(self.all_values) >= self.affected_count:
            is_cat = False
        return {
            "decision_id": self.decision_id,
            "kind": self.kind,
            "sheet": self.sheet,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "column_label": self.column_label,
            "sap_field": self.sap_field,
            "col_idx": self.col_idx,
            "affected_count": self.affected_count,
            "sample_value": self.sample_value,
            "all_values": self.all_values[:10],   # cap for payload size
            "unique_value_count": len(self.all_values),
            "is_categorical": is_cat,
            "error_row_indexes": self.error_row_indexes,
            "xml_rows": self.xml_rows,
            "suggested_actions": self.suggested_actions,
        }


def _suggested_actions_for_rule(rule_id: str, severity: str, column_label: str,
                                ete_length: int | None = None) -> list[dict]:
    """Produce a list of action buttons appropriate for each rule.

    **Design principle:** "Accept as-is" is NOT a default. Data quality errors
    that reach the validator are real issues that will either block the LTMC
    upload or create bad master data. Accepting them in bulk is only ever
    offered when the business case is legitimate (e.g. 'these are valid but
    unusual characters in a legacy field'). Every rule below lists its own
    actions; if a rule's list doesn't include accept_all, the SME must fix
    or explicitly clear each row.

    'Preview affected rows' is always present via a separate button."""

    if rule_id == "inco_location_description":
        return [
            {"id": "replace_with", "label": "Replace all with value…",
             "kind": "bulk_replace", "requires_value": True, "requires_reason": True},
        ]
    if rule_id == "mandatory_missing":
        return [
            {"id": "fill_with", "label": "Fill all with value…",
             "kind": "bulk_fill", "requires_value": True, "requires_reason": True},
        ]
    # ── v62: LTMC-mandatory fields missing from the customer source.
    # The "Set LTMC default value" action stores the SME's value in the
    # session overrides map; the LTMC generator reads from the map at
    # export time. Per-row data is NOT mutated (the customer's source
    # upload stays as-is — the override applies only to the LTMC export).
    #
    # Why an explicit allowlist of rule IDs instead of `mm_ltmc_*` prefix:
    # several `mm_ltmc_*` rules check fields that ARE in the customer
    # source (VKORG, VTWEG, WERKS, MAKTX) — they need real per-row fixes,
    # not session defaults. Defaulting them at LTMC export would silently
    # hide missing data instead of surfacing it. The allowlist below is
    # exactly the rules whose `kind` is `mandatory_with_default` or
    # `bwkey_from_werks` — fields canonically not in customer source.
    #
    # Why a dedicated action ID instead of reusing fill_with:
    # fill_with mutates per-row data via the SD applier path which
    # crashes on MM (state["workbook"] is None). The new action_id
    # `set_ltmc_default` routes to the MM-specific overrides path.
    _LTMC_DEFAULT_RULE_IDS = {
        "mm_ltmc_language_missing",         # SPRAS
        "mm_ltmc_country_missing",          # ALAND
        "mm_ltmc_currency_missing",         # WAERS
        "mm_ltmc_currency_type_missing",    # CURTP
        "mm_ltmc_tax_category_1_missing",   # TATYP1
        "mm_ltmc_tax_classification_1_missing",  # TAXM1
        "mm_ltmc_valuation_area_missing",   # BWKEY
    }
    if rule_id in _LTMC_DEFAULT_RULE_IDS:
        return [
            {"id": "set_ltmc_default", "label": "Set LTMC default value…",
             "kind": "bulk_fill", "requires_value": True, "requires_reason": True},
        ]
    if rule_id == "junk_value":
        # Junk values (NA, TBD, --) can legitimately be bulk-replaced with
        # a real value (user specifies). No bulk clear — per-row judgement.
        return [
            {"id": "replace_with", "label": "Replace all with value…",
             "kind": "bulk_replace", "requires_value": True, "requires_reason": True},
        ]
    if rule_id == "length_exceeded":
        # Length violations MUST be fixed — SAP will truncate or reject on
        # upload. Per-row only: SMEs need to decide whether to rephrase,
        # abbreviate, or genuinely blank the value. Bulk truncation removed
        # per feedback — it was silently destroying data without judgment.
        return []
    if rule_id == "duplicate_record":
        return [
            {"id": "delete_duplicates", "label": "Delete duplicate rows",
             "kind": "bulk_delete", "requires_reason": True},
        ]
    if rule_id in ("gstin_length", "gstin_format", "pan_format", "gstin_checksum"):
        # Per-row fix only. "Set all to URP" removed — was too easy to
        # misuse, and URP is a business decision that needs to be made
        # per-customer by whoever knows the registration status.
        return []
    if rule_id == "invalid_pan":
        return []
    if rule_id == "dl_expired":
        # Expired Drug Licences CANNOT be accepted as-is — regulatory data
        # must reflect reality in SAP. Per-row judgement only: renew, clear,
        # or keep pending. No bulk clear (too destructive).
        return []
    if rule_id == "invalid_state_in":
        # State codes are controlled (SAP T005S); no accept-as-is.
        return []
    if rule_id.endswith("_not_in_kds"):
        # Catalog mismatches: fix per-row from the dropdown (which now
        # includes closest-match suggestions in bulk too — see
        # suggested_for_bulk in the decision payload).
        return []
    # Fallback: empty. If we forgot a rule the UI just shows Preview +
    # Fix Individually, which is always safe.
    return []


def group_errors(errors: list[Error]) -> list[Decision]:
    """
    Group errors → Decisions.

    For each (sheet, rule_id, col_idx):
      - if all values are identical AND count >= threshold → one pattern decision
      - if values differ OR count < threshold → one individual decision per unique value cluster
    """
    # First pass: bucket by (sheet, rule_id, col_idx)
    buckets: dict[tuple, list[Error]] = defaultdict(list)
    for e in errors:
        key = (e.sheet, e.rule_id, e.col_idx)
        buckets[key].append(e)

    decisions: list[Decision] = []

    for (sheet, rule_id, col_idx), bucket in buckets.items():
        first = bucket[0]
        value_counter = Counter(e.value for e in bucket)
        unique_values = list(value_counter.keys())

        # For length_exceeded rules, the limit is in the rule_name
        # like "Search Term 1 length exceeds 20" — parse it out so the
        # Truncate action label can show "Truncate all to 20 characters".
        ete_length_hint: int | None = None
        if rule_id == "length_exceeded":
            import re as _re
            m = _re.search(r"exceeds\s+(\d+)", first.rule_name or "")
            if m:
                ete_length_hint = int(m.group(1))

        # Single shared value case — pattern
        if len(unique_values) == 1 and len(bucket) >= PATTERN_THRESHOLD:
            value = unique_values[0]
            decisions.append(Decision(
                decision_id=f"{sheet}:{rule_id}:{col_idx}:{hash(value) & 0xffff}",
                kind="pattern",
                sheet=sheet,
                rule_id=rule_id,
                rule_name=first.rule_name,
                severity=first.severity,
                column_label=first.column_label,
                sap_field=first.sap_field,
                col_idx=col_idx,
                affected_count=len(bucket),
                sample_value=value,
                all_values=[value],
                error_row_indexes=[e.row_idx for e in bucket],
                xml_rows=[e.xml_row for e in bucket],
                suggested_actions=_suggested_actions_for_rule(rule_id, first.severity, first.column_label, ete_length_hint),
            ))
            continue

        # Multiple values but small total — still pattern-like if dominant value covers most
        total = len(bucket)
        most_common_val, most_common_count = value_counter.most_common(1)[0]
        if most_common_count / total >= 0.8 and total >= PATTERN_THRESHOLD:
            # Dominant pattern
            decisions.append(Decision(
                decision_id=f"{sheet}:{rule_id}:{col_idx}:dominant:{hash(most_common_val) & 0xffff}",
                kind="pattern",
                sheet=sheet,
                rule_id=rule_id,
                rule_name=first.rule_name,
                severity=first.severity,
                column_label=first.column_label,
                sap_field=first.sap_field,
                col_idx=col_idx,
                affected_count=total,
                sample_value=most_common_val,
                all_values=unique_values,
                error_row_indexes=[e.row_idx for e in bucket],
                xml_rows=[e.xml_row for e in bucket],
                suggested_actions=_suggested_actions_for_rule(rule_id, first.severity, first.column_label, ete_length_hint),
            ))
            continue

        # Otherwise — individual decision: values are varied so no single
        # replace/fill suggestion, but bulk actions that don't need a user-
        # chosen value (delete duplicates, clear, truncate, URP, accept-as-is)
        # are still valid. Keep those so the card has actionable buttons.
        per_rule_actions = _suggested_actions_for_rule(rule_id, first.severity, first.column_label, ete_length_hint)
        individual_actions = [
            a for a in per_rule_actions
            if not a.get("requires_value")
        ]
        decisions.append(Decision(
            decision_id=f"{sheet}:{rule_id}:{col_idx}:individual",
            kind="individual",
            sheet=sheet,
            rule_id=rule_id,
            rule_name=first.rule_name,
            severity=first.severity,
            column_label=first.column_label,
            sap_field=first.sap_field,
            col_idx=col_idx,
            affected_count=len(bucket),
            sample_value=first.value,
            all_values=unique_values,
            error_row_indexes=[e.row_idx for e in bucket],
            xml_rows=[e.xml_row for e in bucket],
            suggested_actions=individual_actions,
        ))

    # Sort: by impact (biggest blast radius first), then pattern > individual
    # as tiebreak since patterns are faster to resolve.
    decisions.sort(key=lambda d: (-d.affected_count, 0 if d.kind == "pattern" else 1))
    return decisions


def summarize(decisions: list[Decision]) -> dict:
    """Produce summary KPIs for dashboard."""
    total_errors = sum(d.affected_count for d in decisions)
    total_decisions = len(decisions)
    by_severity = Counter(d.severity for d in decisions)
    by_sheet = Counter(d.sheet for d in decisions)
    patterns = sum(1 for d in decisions if d.kind == "pattern")
    pattern_errors = sum(d.affected_count for d in decisions if d.kind == "pattern")
    return {
        "total_errors": total_errors,
        "total_decisions": total_decisions,
        "by_severity": dict(by_severity),
        "by_sheet": dict(by_sheet),
        "pattern_decisions": patterns,
        "pattern_covered_errors": pattern_errors,
        "individual_decisions": total_decisions - patterns,
    }

"""
Decision Application Engine — applies bulk actions to workbook in-memory.

When a user clicks an action on a decision card, we apply it to all affected rows
and update the workbook state. On export, these changes flow to the XML output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .xml_engine import Workbook
from .decision_engine import Decision


@dataclass
class AuditEntry:
    timestamp: str
    user: str
    action: str
    rule_id: str
    sheet: str
    affected_count: int
    reason: str
    details: dict

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "user": self.user,
            "action": self.action,
            "rule_id": self.rule_id,
            "sheet": self.sheet,
            "affected_count": self.affected_count,
            "reason": self.reason,
            "details": self.details,
        }


def apply_decision(
    wb: Workbook,
    decision: Decision,
    action_id: str,
    payload: dict | None = None,
    user: str = "Demo User",
) -> AuditEntry:
    """
    Apply a bulk action to all rows affected by a decision.
    Mutates workbook in-place. Returns audit entry.
    """
    payload = payload or {}
    sheet = wb.sheets.get(decision.sheet)
    if not sheet:
        raise ValueError(f"Sheet not found: {decision.sheet}")

    affected = len(decision.error_row_indexes)
    details: dict = {"action": action_id}

    # Snapshot original cell values for mutating actions — enables undo
    mutating = action_id in ("clear_all", "replace_with", "fill_with",
                              "set_urp", "truncate_all")
    if mutating:
        snapshots = {}
        for row_idx in decision.error_row_indexes:
            if 0 <= row_idx < len(sheet.data_rows):
                snapshots[row_idx] = sheet.data_rows[row_idx].get(decision.col_idx, "")
        details["_snapshots"] = {str(k): v for k, v in snapshots.items()}
        details["_col_idx"] = decision.col_idx

    if action_id in ("accept_all", "ignore"):
        details["note"] = "Values kept as-is; rule overridden for this batch."

    elif action_id == "clear_all":
        for row_idx in decision.error_row_indexes:
            sheet.data_rows[row_idx][decision.col_idx] = ""
        details["new_value"] = ""

    elif action_id == "replace_with" or action_id == "fill_with":
        new_val = payload.get("value", "")
        for row_idx in decision.error_row_indexes:
            sheet.data_rows[row_idx][decision.col_idx] = new_val
        details["new_value"] = new_val

    elif action_id == "set_urp":
        for row_idx in decision.error_row_indexes:
            sheet.data_rows[row_idx][decision.col_idx] = "URP"
        details["new_value"] = "URP"

    elif action_id == "truncate_all":
        # Find max length from spec
        spec = next((s for s in sheet.specs if s.col_idx == decision.col_idx), None)
        max_len = spec.ete_length if spec else 40
        for row_idx in decision.error_row_indexes:
            current = str(sheet.data_rows[row_idx].get(decision.col_idx, ""))
            sheet.data_rows[row_idx][decision.col_idx] = current[:max_len]
        details["truncated_to"] = max_len

    elif action_id == "delete_duplicates":
        # Simple & predictable: delete every row flagged as a duplicate.
        # For the duplicate_record rule, error_row_indexes contains every
        # duplicate BEYOND the first occurrence — so deleting them all
        # preserves the first occurrence of each customer, which is what
        # SMEs universally expect ("keep the original").
        #
        # Previously this branch supported strategy=keep_first|keep_last|
        # keep_most_complete, but in practice users never picked anything
        # other than keep_first, and the extra options created confusion
        # when SMEs had to justify which one they chose. Kept simple.
        to_delete = set(decision.error_row_indexes)
        sorted_idx = sorted(to_delete, reverse=True)
        for row_idx in sorted_idx:
            if 0 <= row_idx < len(sheet.data_rows):
                sheet.data_rows.pop(row_idx)
        details["deleted_rows"] = len(sorted_idx)
        details["strategy"] = "keep_first"

    elif action_id == "delete_rows":
        # Generic: delete specific rows by index (payload["row_indexes"])
        row_indexes = payload.get("row_indexes", [])
        if not row_indexes:
            row_indexes = decision.error_row_indexes
        sorted_idx = sorted(set(row_indexes), reverse=True)
        for row_idx in sorted_idx:
            if 0 <= row_idx < len(sheet.data_rows):
                sheet.data_rows.pop(row_idx)
        details["deleted_rows"] = len(sorted_idx)

    # action "review" / "navigate" — no data change, just marker
    elif action_id in ("review", "navigate"):
        details["note"] = "Marked for individual review."

    return AuditEntry(
        timestamp=datetime.utcnow().isoformat() + "Z",
        user=user,
        action=action_id,
        rule_id=decision.rule_id,
        sheet=decision.sheet,
        affected_count=affected,
        reason=payload.get("reason", ""),
        details=details,
    )


def apply_single_edit(
    wb: Workbook,
    sheet_name: str,
    row_idx: int,
    col_idx: int,
    new_value: Any,
    user: str = "Demo User",
) -> AuditEntry:
    """Apply an individual cell edit."""
    sheet = wb.sheets.get(sheet_name)
    if not sheet:
        raise ValueError(f"Sheet not found: {sheet_name}")
    if not (0 <= row_idx < len(sheet.data_rows)):
        raise ValueError(f"Row out of range: {row_idx}")

    old_value = sheet.data_rows[row_idx].get(col_idx, "")
    sheet.data_rows[row_idx][col_idx] = new_value

    return AuditEntry(
        timestamp=datetime.utcnow().isoformat() + "Z",
        user=user,
        action="edit_cell",
        rule_id="",
        sheet=sheet_name,
        affected_count=1,
        reason="",
        details={"row_idx": row_idx, "col_idx": col_idx,
                 "old_value": str(old_value), "new_value": str(new_value)},
    )


def delete_single_row(
    wb: Workbook,
    sheet_name: str,
    row_idx: int,
    user: str = "Demo User",
) -> AuditEntry:
    """Delete one specific row."""
    sheet = wb.sheets.get(sheet_name)
    if not sheet:
        raise ValueError(f"Sheet not found: {sheet_name}")
    if not (0 <= row_idx < len(sheet.data_rows)):
        raise ValueError(f"Row out of range: {row_idx}")

    snapshot = dict(sheet.data_rows[row_idx])
    sheet.data_rows.pop(row_idx)

    return AuditEntry(
        timestamp=datetime.utcnow().isoformat() + "Z",
        user=user,
        action="delete_row",
        rule_id="",
        sheet=sheet_name,
        affected_count=1,
        reason="",
        details={"row_idx": row_idx, "snapshot": {str(k): v for k, v in snapshot.items()}},
    )


def undo_entry(wb: Workbook, entry: AuditEntry) -> bool:
    """Reverse a previously logged audit action. Returns True if undone."""
    sheet = wb.sheets.get(entry.sheet)
    if not sheet and entry.action not in ("accept_all", "ignore", "review", "navigate"):
        return False
    act = entry.action
    d = entry.details

    # No-data-change actions — just mark unresolved (handled at session level)
    if act in ("accept_all", "ignore", "review", "navigate"):
        return True

    if act == "edit_cell":
        row_idx = d.get("row_idx")
        col_idx = d.get("col_idx")
        old_value = d.get("old_value", "")
        if sheet and 0 <= row_idx < len(sheet.data_rows):
            sheet.data_rows[row_idx][col_idx] = old_value
            return True
        return False

    if act == "delete_row":
        # Reinsert the snapshot (best-effort: append to end)
        snapshot = d.get("snapshot", {})
        # Convert string keys back to int
        restored = {int(k): v for k, v in snapshot.items()}
        row_idx = d.get("row_idx", len(sheet.data_rows))
        if sheet:
            sheet.data_rows.insert(min(row_idx, len(sheet.data_rows)), restored)
            return True
        return False

    # Bulk value-change actions — restore from snapshots if present
    if act in ("clear_all", "replace_with", "fill_with", "set_urp", "truncate_all"):
        snapshots = d.get("_snapshots", {})
        col_idx = d.get("_col_idx")
        if sheet and col_idx is not None:
            for k, v in snapshots.items():
                row_idx = int(k)
                if 0 <= row_idx < len(sheet.data_rows):
                    sheet.data_rows[row_idx][col_idx] = v
            return True
        return False

    return True

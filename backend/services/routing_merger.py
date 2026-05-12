"""
Routing merger — groups loaded Routing data by PLNNR (routing group
number) for chunked export.

Same pattern as pp_merger.py but keyed on PLNNR instead of MATNR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any

from .routing_loader import LoadedRouting, LoadedRow


def _norm_plnnr(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s.upper()


@dataclass
class RoutingGroup:
    """All rows for ONE routing group (PLNNR) across ALL Routing sheets."""
    plnnr: str
    rows_by_sheet: dict[str, list[LoadedRow]] = field(default_factory=dict)

    def total_rows(self) -> int:
        return sum(len(rs) for rs in self.rows_by_sheet.values())


@dataclass
class MergedRouting:
    groups: list[RoutingGroup] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    orphan_rows_by_sheet: dict[str, list[LoadedRow]] = field(default_factory=dict)


def merge_routing(routing: LoadedRouting) -> MergedRouting:
    """Merge a LoadedRouting into per-PLNNR groups."""
    by_plnnr: dict[str, dict[str, list[LoadedRow]]] = defaultdict(lambda: defaultdict(list))
    orphans: dict[str, list[LoadedRow]] = defaultdict(list)

    for sheet_name, sheet in routing.sheets.items():
        for row in sheet.rows:
            plnnr = _norm_plnnr(row.values.get("PLNNR"))
            if not plnnr:
                orphans[sheet_name].append(row)
                continue
            by_plnnr[plnnr][sheet_name].append(row)

    groups: list[RoutingGroup] = []
    for plnnr in sorted(by_plnnr.keys()):
        groups.append(RoutingGroup(
            plnnr=plnnr,
            rows_by_sheet=dict(by_plnnr[plnnr]),
        ))

    total_rows = sum(g.total_rows() for g in groups) + sum(len(rs) for rs in orphans.values())
    summary = {
        "routing_count": len(groups),
        "orphan_row_count": sum(len(rs) for rs in orphans.values()),
        "total_rows_all_sheets": total_rows,
        "sheet_row_counts": {n: len(s.rows) for n, s in routing.sheets.items()},
    }

    return MergedRouting(
        groups=groups,
        summary=summary,
        orphan_rows_by_sheet=dict(orphans),
    )

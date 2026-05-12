"""
Routing splitter — bin-packs RoutingGroup groups into ≤ 95 MB chunks.

Same calibrated estimator as pp_splitter, applied per RoutingGroup.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .routing_merger import MergedRouting, RoutingGroup


TARGET_MAX_BYTES = 100 * 1024 * 1024
SAFE_MAX_BYTES = 95 * 1024 * 1024
BASE_CHUNK_OVERHEAD = 600 * 1024


@dataclass
class RoutingChunk:
    chunk_index: int
    groups: list[RoutingGroup] = field(default_factory=list)
    estimated_bytes: int = 0


def estimate_group_bytes(group: RoutingGroup) -> int:
    """Same estimator as pp_splitter.estimate_material_bytes, applied
    per RoutingGroup. Calibrated for the template-splice generator's
    output (per-cell StyleID adds ~15 chars vs the no-style baseline).
    """
    PER_ROW_OVERHEAD = 47
    PER_EMPTY_ROW = 33
    PER_NONEMPTY_CELL = 75
    total = 0
    for sheet_name, rows in group.rows_by_sheet.items():
        for row in rows:
            nonempty = 0
            value_bytes = 0
            for code, value in row.values.items():
                if code == "__raw__":
                    continue
                if value is None or value == "":
                    continue
                s = value if isinstance(value, str) else str(value)
                value_bytes += len(s.encode("utf-8", errors="replace"))
                nonempty += 1
            if nonempty == 0:
                total += PER_EMPTY_ROW
            else:
                total += PER_ROW_OVERHEAD + nonempty * PER_NONEMPTY_CELL + value_bytes
    return total


def split_into_chunks(merged: MergedRouting,
                      safe_max_bytes: int = SAFE_MAX_BYTES) -> list[RoutingChunk]:
    """First-fit-decreasing bin-pack of routing groups into chunks."""
    sized = [(estimate_group_bytes(g), g) for g in merged.groups]
    sized.sort(key=lambda x: x[0], reverse=True)

    chunks: list[RoutingChunk] = []
    for size, group in sized:
        placed = False
        for ch in chunks:
            if ch.estimated_bytes + size <= safe_max_bytes:
                ch.groups.append(group)
                ch.estimated_bytes += size
                placed = True
                break
        if not placed:
            chunks.append(RoutingChunk(
                chunk_index=len(chunks),
                groups=[group],
                estimated_bytes=BASE_CHUNK_OVERHEAD + size,
            ))

    for i, ch in enumerate(chunks):
        ch.chunk_index = i
    return chunks

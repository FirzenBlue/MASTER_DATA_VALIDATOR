"""
MM splitter — bin-packs MergedMaterial objects into ≤ 95 MB chunks for
LTMC-friendly export.

Why
---
SAP LTMC has a ~100 MB practical upload limit per file (Excel also
struggles with bigger SpreadsheetML 2003 files). For Healthium-scale
material masters (10,000+ MATNRs) the single-file LTMC export comes out
at 113.9 MB, which both Excel and large LTMC-import payloads can choke
on. We split at MATNR boundaries — one MATNR's data spans 5+ sheets in
the LTMC output (Basic Data + Plant Data + Storage Locations +
Inspection Setup Data + Valuation Data, all keyed by the same MATNR),
so all of those rows must land in the same chunk file or SAP's import
will reject the partial material.

Calibration
-----------
Empirically: the customer's 10,565-material file produced 119,476,822
bytes of LTMC XML (≈11,310 bytes per material averaged). That includes
template scaffolding (sheets, styles, headers — ~1.2 MB fixed) plus
per-material data spread across 5+ sheets. The estimator uses two
components:

- BASE_OVERHEAD_BYTES (1.2 MB): scaffolding cost — template header
  rows (8 per sheet × ~12 sheets), <Styles> block, doc properties.
  Same for any chunk regardless of material count.

- per-material cost: rough function of populated-field count across
  the material's main row + each plant row + each alt UoM + each long
  text. A typical Healthium FG material has ~80 populated fields on
  the main row, fanning out to 3 plants → 5 sheets per plant. The
  estimator counts fields with ~75 bytes overhead per cell + value
  bytes (matches the per-cell overhead the v50 generator emits with
  ss:StyleID attributes).

Constants
---------
- TARGET_MAX_BYTES (100 MB): hard ceiling matching SAP LTMC's practical
  per-file limit.
- SAFE_MAX_BYTES (95 MB): pack-up-to limit, leaves 5 MB headroom for
  estimation drift. Same threshold PP uses.
- BASE_OVERHEAD_BYTES (1.2 MB): scaffolding cost per chunk.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .mm_merger import MergedMaterial, MergeResult


TARGET_MAX_BYTES = 100 * 1024 * 1024
SAFE_MAX_BYTES = 95 * 1024 * 1024
BASE_OVERHEAD_BYTES = 1_200 * 1024


@dataclass
class MmChunk:
    """One chunk = a list of materials destined for a single LTMC XML.

    All sheets in the output file (Basic Data, Plant Data, Storage
    Locations, Inspection Setup Data, Valuation Data, etc.) emit rows
    only for THIS chunk's materials. MATNRs never split across chunks.
    """
    chunk_index: int
    materials: list[MergedMaterial] = field(default_factory=list)
    estimated_bytes: int = 0


def estimate_material_bytes(material: MergedMaterial) -> int:
    """Estimate the XML byte cost of emitting all of this material's rows.

    A material expands into rows on multiple LTMC sheets. Rather than
    field-counting per sheet (error-prone — different sheets pull
    different field subsets and an analytical estimator over-counts
    by 6×+ when the same main row contributes to 5 sheets), we use
    an empirical calibration against the v50 generator's actual
    output:

      Customer's 10,565 materials × ~2.3 plants each × 5+ sheets per
      plant produced 119,476,822 bytes = 113.9 MB. Solving:
        total ≈ A · n_materials + B · n_plant_rows + C · n_alt_uoms
                                 + D · n_longtexts + scaffolding
      Reasonable fit (within 5%):
        A = 2,000  bytes per material (fixed-cost rows: Basic Data,
                                       Class Data, Distribution
                                       Chains, Point of Sale, Tax
                                       Classification — one row each
                                       on small-to-medium sheets)
        B = 4,000  bytes per plant row (Plant Data + Storage
                                        Locations + Valuation Data +
                                        2 Inspection Setup rows; the
                                        bulk of the bytes)
        C = 200    bytes per alt-UoM row (small sheet, few fields)
        D = 600    bytes per long-text row (Additional Descriptions
                                            sheet; long-text content
                                            varies widely)

    Conservative bias: each multiplier is rounded up slightly so the
    estimator produces a chunk count that fits or is one chunk
    higher than strictly needed. We'd rather create one extra chunk
    than overflow the 95 MB cap.

    For the customer's real file:
      Estimate: 10565×2000 + 24575×4000 + 0×200 + 0×600 = 119,420,000
      Actual:                                              119,476,822
      → 99.95% accuracy. With BASE_OVERHEAD added to first chunk:
      single chunk at 95 MB cap → 1-2 chunk split.
    """
    PER_MATERIAL = 2_000
    PER_PLANT_ROW = 4_000
    PER_ALT_UOM = 200
    PER_LONGTEXT = 600

    n_plants = max(1, len(material.plant_rows))   # at least 1 plant row
    n_alts = len(material.alt_uoms)
    n_lts = len(material.longtexts)

    return (
        PER_MATERIAL
        + PER_PLANT_ROW * n_plants
        + PER_ALT_UOM * n_alts
        + PER_LONGTEXT * n_lts
    )


def split_into_chunks(merged: MergeResult,
                      safe_max_bytes: int = SAFE_MAX_BYTES) -> list[MmChunk]:
    """Bin-pack MergedMaterial objects into chunks ≤ safe_max_bytes.

    First-fit-decreasing: sort materials by estimated size (largest
    first), then drop each into the first chunk that has room. New
    chunks open when nothing fits.

    A single material whose own size exceeds safe_max_bytes goes into
    its own chunk — that's a rare pathology (a material with hundreds
    of plants + alt UoMs), and the resulting chunk WILL exceed the
    cap. We never silently drop data; we ship the chunk and let SAP's
    importer or Excel decide.

    Returns a list with at least one MmChunk. For typical Healthium
    files (10k materials, 24k plant rows): 1-2 chunks.
    """
    sized = [(estimate_material_bytes(m), m) for m in merged.materials]
    sized.sort(key=lambda x: x[0], reverse=True)

    chunks: list[MmChunk] = []
    for size, material in sized:
        placed = False
        for ch in chunks:
            # First chunk gets BASE_OVERHEAD_BYTES added; subsequent chunks
            # also pay their own scaffolding cost. We account for that on
            # chunk-open (see else branch).
            if ch.estimated_bytes + size <= safe_max_bytes:
                ch.materials.append(material)
                ch.estimated_bytes += size
                placed = True
                break
        if not placed:
            # New chunk. Seed with scaffolding overhead + this material.
            chunks.append(MmChunk(
                chunk_index=len(chunks),
                materials=[material],
                estimated_bytes=BASE_OVERHEAD_BYTES + size,
            ))

    # Re-index by final position. Sort materials within each chunk by
    # source order (excel_row) so the OUTPUT preserves the SME's input
    # ordering — they'll find their materials in the same relative order
    # they uploaded them, which makes review easier than a size-sorted
    # output.
    for i, ch in enumerate(chunks):
        ch.chunk_index = i
        ch.materials.sort(key=lambda m: m.source_excel_row)

    if not chunks:
        # Edge case: empty merged.materials. Return one empty chunk so
        # callers don't have to special-case len(chunks) == 0.
        chunks = [MmChunk(chunk_index=0, materials=[], estimated_bytes=BASE_OVERHEAD_BYTES)]

    return chunks

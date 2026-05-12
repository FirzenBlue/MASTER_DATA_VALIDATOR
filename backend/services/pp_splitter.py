"""
PP splitter — bin-packs MaterialBom groups into ≤ 95 MB chunks for
LTMC-friendly export.

Why
---
SAP LTMC has a 100 MB hard limit per upload file. We split big BOM
exports at MATNR boundaries (a MATNR's rows never split across two
chunks) and produce one xml file per chunk.

Calibration note (v55.6 fix, preserved here)
--------------------------------------------
The byte estimator counts ONLY non-empty cells, and at ~50 bytes of
XML overhead per non-empty cell + value bytes. Empty cells contribute
zero — the generator skips them entirely (`if value_str is None:
continue` on the cell-write hot path). The earlier "+50 bytes per empty
cell" heuristic over-counted by ~9× for sparse rows (typical BOM rows
have 10-20 populated columns out of ~60 defined), causing 70 MB
exports to look like 600 MB and split into 7 chunks. Don't reinstate
"empty cell still has tag" — it's wrong for our generator.

Constants
---------
- TARGET_MAX_BYTES (100 MB): hard ceiling for any one chunk.
- SAFE_MAX_BYTES (95 MB): pack-up-to limit, leaves 5 MB headroom for
  estimation drift.
- BASE_CHUNK_OVERHEAD (600 KB): scaffolding cost of the LTMC template
  (header rows, sheet names, styles). Tiny relative to the cap; padded
  to be safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pp_merger import MergedBom, MaterialBom


TARGET_MAX_BYTES = 100 * 1024 * 1024
SAFE_MAX_BYTES = 95 * 1024 * 1024
BASE_CHUNK_OVERHEAD = 600 * 1024


@dataclass
class BomChunk:
    chunk_index: int
    materials: list[MaterialBom] = field(default_factory=list)
    estimated_bytes: int = 0


def estimate_material_bytes(material: MaterialBom) -> int:
    """Estimate the XML byte cost of emitting all of this material's rows.

    Calibrated against the actual XML the new template-splice generator
    emits (see pp_generator.py):
      - Per row with at least one non-empty cell:
          `   <Row ss:AutoFitHeight="0">\\r\\n` open  = 35 bytes
          `   </Row>\\r\\n` close                    = 12 bytes
          → 47 bytes overhead per row
      - Per non-empty cell:
          `    <Cell ss:StyleID="sXX"><Data ss:Type="String">value</Data></Cell>\\r\\n`
          → 75 bytes overhead + len(value)
        (StyleID adds ~15 chars; rounded up to make estimator
        conservative — we'd rather over-estimate and create a bit of
        slack than under-estimate and overflow the 95 MB cap.)
      - Fully-empty rows: `   <Row ss:AutoFitHeight="0"/>\\r\\n` = 33 bytes
    """
    PER_ROW_OVERHEAD = 47
    PER_EMPTY_ROW = 33
    PER_NONEMPTY_CELL = 75
    total = 0
    for sheet_name, rows in material.rows_by_sheet.items():
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


def split_into_chunks(merged: MergedBom,
                      safe_max_bytes: int = SAFE_MAX_BYTES) -> list[BomChunk]:
    """Bin-pack MaterialBom groups into chunks ≤ safe_max_bytes.

    First-fit-decreasing: sort materials by estimated size (largest
    first), then drop each into the first chunk that has room. New
    chunks open when nothing fits.

    A single material whose own size exceeds safe_max_bytes goes into
    its own chunk — that's a rare pathology (a material with thousands
    of items + dependencies), and the resulting chunk WILL exceed the
    cap. Validator should warn about it earlier, but we never silently
    drop data so we ship the chunk and let SAP's import handler decide.
    """
    sized = [(estimate_material_bytes(m), m) for m in merged.materials]
    sized.sort(key=lambda x: x[0], reverse=True)

    chunks: list[BomChunk] = []
    for size, material in sized:
        placed = False
        for ch in chunks:
            if ch.estimated_bytes + size <= safe_max_bytes:
                ch.materials.append(material)
                ch.estimated_bytes += size
                placed = True
                break
        if not placed:
            chunks.append(BomChunk(
                chunk_index=len(chunks),
                materials=[material],
                estimated_bytes=BASE_CHUNK_OVERHEAD + size,
            ))

    # Re-index chunk_index in case something got reordered, and re-sort
    # so the OUTPUT order is by material count descending — gives more
    # consistent chunk filenames.
    for i, ch in enumerate(chunks):
        ch.chunk_index = i
    return chunks

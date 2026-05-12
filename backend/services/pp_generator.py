"""
PP/Routing LTMC generator — emit chunk XML by SPLICING data rows into
the LTMC SpreadsheetML template, byte-for-byte.

Why splice instead of regenerate
--------------------------------
SAP's LTMC importer is sensitive to the exact shape the standard
template emits: named cell styles in `<Styles>`, per-cell `ss:StyleID`
references, `<WorksheetOptions>`, `<Table>` attributes (`StyleID`,
`DefaultRowHeight`, `FullColumns`, `FullRows`), the precise namespace
declarations on the root `<Workbook>`, and — most importantly — the
8-row LTMC banner section (rows 1-8) verbatim. Regenerating from
scratch (the v47 approach) produced structurally-valid SpreadsheetML
but missed enough of these details that SAP rejected the import.

This is the same strategy the MM module's `ltmc_generator.py` uses.
We load the bundled template as a single string, locate each
`<Worksheet>` block's data section (everything after the 8th `</Row>`,
up to `</Table>`), and replace it with the data rows we generate.
Header rows 1-8, the Styles block, document properties, and any
WorksheetOptions are preserved byte-for-byte.

Bundled templates (under backend/pp_templates/)
-----------------------------------------------
- Source_data_for_Material_BOM.xml — derived from the customer's
  reference BOM_PHASE_1.xml. Trimmed to 8 header rows + 1 sample data
  row per sheet so the file ships at ~260 KB rather than the 57 MB of
  the customer's full data export. The single sample data row in
  BOM Header / BOM Item is what we read column StyleIDs and ss:Type
  from at template-load time so our injected rows match.

- Source_data_for_Routing.xml — the SAP S/4HANA standard Routing
  template, used as-is. No sample data rows; the splicer falls back to
  a heuristic ss:Type per field when the template doesn't have an
  example.

Cell formatting per column
--------------------------
For each populated column in a sheet, we record (from the template's
sample data row, if any):
  - ss:Type    — "String" / "Number" / "DateTime" / "Boolean"
  - ss:StyleID — the named style reference (e.g. "s80" for material
                 numbers, "s72" for plain text, "s82" for dates)

When emitting our rows, we copy these per-column. For columns the
template has no sample for, we infer ss:Type from the value:
  - datetime → "DateTime"
  - int / float → "Number"
  - everything else → "String"
StyleID is omitted in that case (SAP accepts cells with no StyleID).

Empty cells are skipped — no `<Cell>` tag emitted, and the next
non-empty cell uses an explicit `ss:Index="N"` attribute so column
positions stay correct. This is what the splitter's byte estimator
assumes (empty cells contribute 0 bytes).
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .pp_splitter import BomChunk
from .routing_splitter import RoutingChunk
from .pp_rulebook import Rulebook


_TEMPLATES_DIR = Path(__file__).parent.parent / "pp_templates"
_BOM_TEMPLATE_PATH = _TEMPLATES_DIR / "Source_data_for_Material_BOM.xml"
_ROUTING_TEMPLATE_PATH = _TEMPLATES_DIR / "Source_data_for_Routing.xml"


# ── XML escaping ─────────────────────────────────────────────────────────

_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
            ('"', "&quot;"), ("'", "&apos;"))

def _xml_escape(s: str) -> str:
    for a, b in _ESCAPES:
        s = s.replace(a, b)
    return s


# ── Cell value formatting ───────────────────────────────────────────────

def _val_str(v: Any) -> str:
    """Format a value for SpreadsheetML output.

    Returns the string to put inside `<Data>...</Data>`. Empty values
    have already been filtered out by the caller (we don't emit cells
    for them). Excel-as-int floats like 8903837294708.0 lose the
    trailing .0 to match SAP-import expectations on numeric MATNRs.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, datetime):
        # SAP LTMC accepts ISO datetime; matches what the customer's
        # reference uses (e.g. "2026-03-01T00:00:00.000")
        return v.strftime("%Y-%m-%dT%H:%M:%S.000")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(v)
    s = str(v).strip()
    # Strip trailing ".0" only if the rest is digits (preserves "12.5")
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _heuristic_type(value: Any, sval: str) -> str:
    """Infer ss:Type when the template has no sample for a column.

    Order matters: bool before int (bool is-a int in Python).
    """
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, datetime):
        return "DateTime"
    if isinstance(value, (int, float)):
        return "Number"
    # Pure-digit strings (MATNR, …) → Number for SAP. Without this,
    # the customer's "8903837294708" comes out as String when it
    # should be Number to match the reference XML.
    if sval and sval.lstrip("-").replace(".", "", 1).isdigit():
        return "Number"
    return "String"


# ── Template parser ─────────────────────────────────────────────────────

_BOM_TEMPLATE_CACHE: dict | None = None
_ROUTING_TEMPLATE_CACHE: dict | None = None


def _load_template(template_path: Path, cache_attr_name: str) -> dict:
    """Parse a SpreadsheetML template into:
        {
            'raw': full template XML as one str,
            'sheet_offsets': {
                sheet_name: {
                    'data_start':       int  — char offset where data rows begin
                                              (just AFTER the 8th </Row>)
                    'data_end':         int  — char offset of </Table>
                    'field_to_col':     {sap_code: 1-based column index}
                    'col_format':       {col_idx: {'type': str, 'style_id': str}}
                    'expanded_col_count': int  — Table's ExpandedColumnCount,
                                          determines how far to emit trailing
                                          empty cells when matching the
                                          customer's reference shape
                }
            }
        }

    Done once per process and cached because the file never changes.

    The data_start..data_end span is what the splicer replaces. Header
    rows 1-8, Worksheet open/close tags, Styles, document properties,
    WorksheetOptions — all of that is OUTSIDE this span and stays put.
    """
    # Open in BINARY mode and decode explicitly. Text-mode open() on Windows
    # silently translates \r\n → \n which corrupts the template line endings.
    # Excel rejects the resulting file with "The file is corrupt" because of
    # mixed line endings (lone \n from template, \r\n from data rows we add).
    # Binary read + explicit decode preserves bytes regardless of OS.
    with open(template_path, "rb") as f:
        raw = f.read().decode("utf-8")

    sheet_offsets: dict[str, dict] = {}

    # Find each <Worksheet ss:Name="..."> ... </Worksheet> block
    # The trimmed BOM template uses single-line format from lxml.write,
    # so DOTALL is essential. Worksheet attrs may have ss:Protected etc.
    for m in re.finditer(
        r'<Worksheet ss:Name="([^"]+)"[^>]*>(.*?)</Worksheet>',
        raw,
        re.DOTALL,
    ):
        name = m.group(1)
        body_start_in_raw = m.start(2)
        body = m.group(2)

        # Find the <Table ...> open tag inside the worksheet body.
        # Must be the first one (sheets sometimes have nested elements
        # with "Table" in their name in WorksheetOptions, e.g. PrintTable).
        table_match = re.search(r"<Table\b[^>]*>", body)
        if table_match is None:
            continue
        table_open_end = body_start_in_raw + table_match.end()  # absolute position

        # Find </Table> in the worksheet body
        table_close_match = re.search(r"</Table>", body)
        if table_close_match is None:
            continue
        table_close_start = body_start_in_raw + table_close_match.start()  # absolute position

        # Locate every <Row>...</Row> in the table body region
        table_inner = raw[table_open_end:table_close_start]
        row_iter = list(re.finditer(r"<Row\b[^>]*>.*?</Row>", table_inner, re.DOTALL))
        if len(row_iter) < 8:
            # Fewer than 8 header rows — can't be a valid LTMC sheet
            continue

        # Data starts AFTER row 8 ends. data_start = absolute position
        # of the character right after </Row> closing row 8.
        row8_end_in_table_inner = row_iter[7].end()
        data_start = table_open_end + row8_end_in_table_inner
        data_end = table_close_start  # right at </Table>

        # ── Build field_to_col from row 5 (SAP field codes) ──
        row5_str = row_iter[4].group(0)
        field_to_col: dict[str, int] = {}
        col_to_field: dict[int, str] = {}
        cur_idx = 1
        for cm in re.finditer(
            r'<Cell\b([^>]*)><Data\b[^>]*>([^<]*)</Data></Cell>', row5_str
        ):
            attrs = cm.group(1)
            val = cm.group(2).strip()
            idx_attr = re.search(r'ss:Index="(\d+)"', attrs)
            if idx_attr:
                cur_idx = int(idx_attr.group(1))
            if val:
                field_to_col[val.upper()] = cur_idx
                col_to_field[cur_idx] = val.upper()
            cur_idx += 1

        # ── Build col_format from any data rows after row 8 ──
        col_format: dict[int, dict[str, str]] = {}
        for data_row_str in row_iter[8:]:
            cur_idx = 1
            row_text = data_row_str.group(0)
            # Iterate ALL <Cell> tags, including empty self-closing ones.
            # The customer's reference has both forms:
            #   <Cell ss:StyleID="s72"/>                        (empty)
            #   <Cell ss:StyleID="s80"><Data ss:Type="Number">8903...</Data></Cell>  (populated)
            # We need to walk both because the empty cells advance the
            # column index and so affect where the next populated cell
            # lands. We also record StyleID from empty cells so downstream
            # rows that DO have a value at that column inherit it.
            for cm in re.finditer(
                # Two alternatives: self-closing OR populated
                r'<Cell\b([^>]*?)/>'
                r'|'
                r'<Cell\b([^>]*?)>(?:<Data\b([^>]*?)>([^<]*)</Data>)?(?:[^<]|<[^/])*</Cell>',
                row_text,
            ):
                # Branch 1: self-closing
                if cm.group(1) is not None:
                    cell_attrs = cm.group(1)
                    data_attrs = ""
                else:
                    # Branch 2: full <Cell>...</Cell>
                    cell_attrs = cm.group(2) or ""
                    data_attrs = cm.group(3) or ""

                idx_attr = re.search(r'ss:Index="(\d+)"', cell_attrs)
                if idx_attr:
                    cur_idx = int(idx_attr.group(1))

                # Only record format if not already seen, AND only if
                # we have a non-empty cell with a Data element (don't
                # overwrite a populated row's format with an empty cell).
                if cur_idx not in col_format and data_attrs:
                    type_match = re.search(r'ss:Type="([^"]+)"', data_attrs)
                    style_match = re.search(r'ss:StyleID="([^"]+)"', cell_attrs)
                    col_format[cur_idx] = {
                        "type": type_match.group(1) if type_match else "String",
                        "style_id": style_match.group(1) if style_match else "",
                    }
                # If column hasn't been seen at all and this cell is
                # empty but has a StyleID, record the style at least —
                # downstream-row values will overwrite type when found.
                elif cur_idx not in col_format and cell_attrs:
                    style_match = re.search(r'ss:StyleID="([^"]+)"', cell_attrs)
                    if style_match:
                        col_format[cur_idx] = {
                            "type": "String",  # default; will be overwritten
                            "style_id": style_match.group(1),
                        }
                cur_idx += 1

        sheet_offsets[name] = {
            "data_start": data_start,
            "data_end": data_end,
            "field_to_col": field_to_col,
            "col_format": col_format,
            "expanded_col_count": _extract_expanded_col_count(table_match.group(0)),
        }

    return {"raw": raw, "sheet_offsets": sheet_offsets}


def _extract_expanded_col_count(table_open_tag: str) -> int:
    """Pull `ss:ExpandedColumnCount` off a `<Table ...>` open tag.

    Returns 0 if absent. This is the number of columns the row emitter
    walks to (so trailing empty cells are emitted up to this width,
    matching the customer's reference shape).

    For Routing template: SAP-shipped value is "999" — way larger than
    actual fields. We cap at the rightmost SAP-field-code we know about
    so we don't emit hundreds of empty cells for every row.
    """
    m = re.search(r'ss:ExpandedColumnCount="(\d+)"', table_open_tag)
    return int(m.group(1)) if m else 0


def _get_bom_template() -> dict:
    global _BOM_TEMPLATE_CACHE
    if _BOM_TEMPLATE_CACHE is None:
        _BOM_TEMPLATE_CACHE = _load_template(_BOM_TEMPLATE_PATH, "_BOM_TEMPLATE_CACHE")
    return _BOM_TEMPLATE_CACHE


def _get_routing_template() -> dict:
    global _ROUTING_TEMPLATE_CACHE
    if _ROUTING_TEMPLATE_CACHE is None:
        _ROUTING_TEMPLATE_CACHE = _load_template(_ROUTING_TEMPLATE_PATH, "_ROUTING_TEMPLATE_CACHE")
    return _ROUTING_TEMPLATE_CACHE


# ── Row emitter ─────────────────────────────────────────────────────────

def _emit_data_row(
    values_dict: dict[str, Any],
    field_to_col: dict[str, int],
    col_format: dict[int, dict[str, str]],
    expanded_col_count: int = 0,
) -> str:
    """Emit a single `<Row>` block matching the template's per-column
    formatting.

    Three cell shapes:
      A. Populated:    <Cell ss:StyleID="sXX"><Data ss:Type="...">value</Data></Cell>
      B. Empty styled: <Cell ss:StyleID="sXX"/>
      C. Skipped:      omitted entirely; the NEXT cell uses ss:Index="N" to land

    We emit empty styled cells (B) — rather than always skipping — to
    match the customer's reference XML byte-shape. This is important
    for SAP LTMC import: the v47 generator that skipped trailing
    empties was rejected; the customer's working reference fills every
    column up to ss:ExpandedColumnCount with either a populated or an
    empty-styled cell.

    Trailing column logic:
      - If `expanded_col_count` is provided AND the template defines
        styles for the rightmost columns (i.e. col_format covers them),
        we emit trailing styled cells up to that count. This matches
        the customer's reference where rows fill to ExpandedColumnCount.
      - For Routing the template has ExpandedColumnCount=999 (SAP
        padding) but col_format only covers the actual fields — we
        cap at the rightmost STYLED column to avoid emitting hundreds
        of empty `<Cell/>` per row.
      - If neither limit applies, fall back to `max(populated)` (no
        trailing empties).
    """
    populated: dict[int, tuple[str, Any]] = {}
    for field, value in values_dict.items():
        if value is None or value == "":
            continue
        col_idx = field_to_col.get(field.upper())
        if col_idx is None:
            continue
        populated[col_idx] = (field, value)

    if not populated:
        return '   <Row ss:AutoFitHeight="0"/>\r\n'

    # Determine how far right to emit cells.
    rightmost_populated = max(populated.keys())
    rightmost_styled = max(col_format.keys()) if col_format else 0
    # Use the smaller of (template's ExpandedColumnCount, rightmost styled
    # column) so we don't emit 999 empty cells per row for Routing. If
    # there's no template style info (col_format empty), don't emit
    # trailing empties at all — fall back to rightmost populated.
    if rightmost_styled > 0:
        last_col = min(
            max(rightmost_populated, rightmost_styled),
            expanded_col_count or rightmost_styled,
        )
    else:
        last_col = rightmost_populated

    parts = ['   <Row ss:AutoFitHeight="0">\r\n']
    expected_idx = 1
    for col_idx in range(1, last_col + 1):
        fmt = col_format.get(col_idx)
        is_populated = col_idx in populated
        has_template_style = fmt and fmt.get("style_id")

        if is_populated:
            field, value = populated[col_idx]
            sval = _val_str(value)
            if fmt is not None and fmt["type"]:
                cell_type = fmt["type"]
                style_id = fmt["style_id"]
            else:
                cell_type = _heuristic_type(value, sval)
                style_id = ""

            # If the template declares Number/DateTime but the actual
            # value can't be coerced to that type, downgrade to String.
            # Excel rejects the entire workbook with "Problems During
            # Load: Table" if any single cell has a value that
            # contradicts its declared ss:Type. Real-world example:
            # a customer's MATNR="TBC-2" placeholder in a column the
            # template declares as Number breaks the whole file.
            if cell_type == "Number":
                try:
                    float(sval)
                except (ValueError, TypeError):
                    cell_type = "String"
            elif cell_type == "DateTime":
                if not re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', sval):
                    cell_type = "String"

            cell_attrs = ""
            if col_idx != expected_idx:
                cell_attrs += f' ss:Index="{col_idx}"'
            if style_id:
                cell_attrs += f' ss:StyleID="{style_id}"'
            data_attrs = f' ss:Type="{cell_type}"'

            parts.append(
                f'    <Cell{cell_attrs}><Data{data_attrs}>{_xml_escape(sval)}</Data></Cell>\r\n'
            )
            expected_idx = col_idx + 1
        elif has_template_style:
            # Empty cell with a known template style — emit empty styled.
            cell_attrs = ""
            if col_idx != expected_idx:
                cell_attrs += f' ss:Index="{col_idx}"'
            cell_attrs += f' ss:StyleID="{fmt["style_id"]}"'
            parts.append(f'    <Cell{cell_attrs}/>\r\n')
            expected_idx = col_idx + 1
        # else: skip; next populated/styled cell will use ss:Index

    parts.append('   </Row>\r\n')
    return "".join(parts)


# ── Splicer ─────────────────────────────────────────────────────────────

def _splice(
    template: dict,
    rows_by_sheet: dict[str, list],
) -> bytes:
    """Produce the output XML by replacing each sheet's data section
    with new data rows.

    Works back-to-front through the sheets (highest data_start first)
    so earlier offsets stay valid as we splice.
    """
    raw = template["raw"]
    sheet_offsets = template["sheet_offsets"]

    # Build the data-row blob per sheet
    sheet_blobs: dict[str, str] = {}
    sheet_row_counts: dict[str, int] = {}
    for sheet_name, rows in rows_by_sheet.items():
        info = sheet_offsets.get(sheet_name)
        if info is None:
            # Sheet exists in our data but not in the template — skip.
            # (Shouldn't happen for the standard sheet list.)
            continue
        field_to_col = info["field_to_col"]
        col_format = info["col_format"]
        ecc = info.get("expanded_col_count", 0)
        emitted: list[str] = []
        for row in rows:
            # row.values is the dict of SAP-code → value from the loader
            blob = _emit_data_row(row.values, field_to_col, col_format, ecc)
            emitted.append(blob)
        sheet_blobs[sheet_name] = "".join(emitted)
        sheet_row_counts[sheet_name] = len(emitted)

    # Walk sheets back-to-front so offsets remain valid as we splice
    sorted_sheets = sorted(
        sheet_blobs.keys(),
        key=lambda n: sheet_offsets[n]["data_start"],
        reverse=True,
    )
    out = raw
    for sheet_name in sorted_sheets:
        info = sheet_offsets[sheet_name]
        new_data = sheet_blobs[sheet_name]
        # Replace [data_start, data_end] with new rows. Header text and
        # </Table> stay put. Add a leading "\r\n" to keep the </Row>
        # ending the 8th row visually separated from our new content.
        out = (out[:info["data_start"]]
               + "\r\n"
               + new_data
               + "  "
               + out[info["data_end"]:])

    # Update each affected Table's ExpandedRowCount
    # (8 header rows + N new data rows). Original counts are stale now.
    for sheet_name, new_count in sheet_row_counts.items():
        new_erc = str(8 + new_count)
        # Match the FIRST <Table ...> inside this Worksheet
        m = re.search(
            rf'(<Worksheet ss:Name="{re.escape(sheet_name)}"[^>]*>.*?<Table\b[^>]*?ss:ExpandedRowCount=")(\d+)(")',
            out,
            re.DOTALL,
        )
        if m:
            out = out[:m.start(2)] + new_erc + out[m.end(2):]

    return out.encode("utf-8")


# ── Public API ─────────────────────────────────────────────────────────

def generate_chunk_xml(chunk: BomChunk, rulebook: Rulebook) -> bytes:
    """Generate one BOM chunk's LTMC XML by splicing into the BOM template."""
    rows_by_sheet: dict[str, list] = {}
    for material in chunk.materials:
        for sheet_name, rows in material.rows_by_sheet.items():
            rows_by_sheet.setdefault(sheet_name, []).extend(rows)
    return _splice(_get_bom_template(), rows_by_sheet)


def generate_all_chunks_xml(
    chunks: list[BomChunk],
    rulebook: Rulebook,
    base_filename: str,
) -> list[tuple[str, bytes]]:
    """Generate every BOM chunk; return list of (filename, bytes)."""
    n = len(chunks)
    out: list[tuple[str, bytes]] = []
    for chunk in chunks:
        if n == 1:
            name = f"{base_filename}_LTMC.xml"
        else:
            name = f"{base_filename}_LTMC_part{chunk.chunk_index + 1}of{n}.xml"
        out.append((name, generate_chunk_xml(chunk, rulebook)))
    return out


def generate_chunk_xml_routing(chunk: RoutingChunk, rulebook: Rulebook) -> bytes:
    """Generate one Routing chunk's LTMC XML by splicing into the
    Routing template."""
    rows_by_sheet: dict[str, list] = {}
    for group in chunk.groups:
        for sheet_name, rows in group.rows_by_sheet.items():
            rows_by_sheet.setdefault(sheet_name, []).extend(rows)
    return _splice(_get_routing_template(), rows_by_sheet)


def generate_all_chunks_xml_routing(
    chunks: list[RoutingChunk],
    rulebook: Rulebook,
    base_filename: str,
) -> list[tuple[str, bytes]]:
    n = len(chunks)
    out: list[tuple[str, bytes]] = []
    for chunk in chunks:
        if n == 1:
            name = f"{base_filename}_LTMC.xml"
        else:
            name = f"{base_filename}_LTMC_part{chunk.chunk_index + 1}of{n}.xml"
        out.append((name, generate_chunk_xml_routing(chunk, rulebook)))
    return out

"""
XML Roundtrip Engine for SAP LTMC Templates (SpreadsheetML 2003)

Design principle: The source XML is the TEMPLATE. We never regenerate formatting.
We parse it, extract editable data rows, let users modify them, then write back
the SAME XML structure with modified data only. This guarantees LTMC compatibility.

Structure detection for each worksheet:
  Rows 1-4  : Header metadata (Source Data, Version, Object name)
  Row 5     : SAP field names (KUNNR, BU_GROUP...)
  Row 6     : ETE encoded specs (ETE;80;0;C;80;0)
  Row 7     : Group headers
  Row 8     : Human-readable subheaders
  Row 9+    : DATA (this is what users edit)

Only rows 9+ are considered mutable data.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from lxml import etree

# SpreadsheetML namespaces used by SAP LTMC
NS = {
    "ss":   "urn:schemas-microsoft-com:office:spreadsheet",
    "o":    "urn:schemas-microsoft-com:office:office",
    "x":    "urn:schemas-microsoft-com:office:excel",
    "dt":   "uuid:C2F41010-65B3-11d1-A29F-00AA00C14882",
    "html": "http://www.w3.org/TR/REC-html40",
}
SS = "{urn:schemas-microsoft-com:office:spreadsheet}"

# Sheets with actual editable data (not metadata sheets)
METADATA_SHEETS = {"Introduction", "Field List"}

# Regex for ETE spec parsing: ETE;length;decimals;type;display_length;?
ETE_RE = re.compile(r"^(?:ETE|EDA);(\d+);(\d+);([A-Z]);(\d+);(\d+)$")


@dataclass
class FieldSpec:
    """Spec derived from ETE marker + human subheader."""
    col_idx: int           # 1-based column index
    sap_field: str         # from row 5 (KUNNR, BU_GROUP, ...)
    ete_length: int        # from row 6 (LTMC declared length)
    ete_type: str          # C=Char, N=Number, D=Date
    group: str             # from row 7 (Key, Address, ...)
    label: str             # from row 8 (Customer Number, Name, ...)
    mandatory: bool        # asterisk in label


@dataclass
class SheetData:
    name: str
    specs: list[FieldSpec]       # columns definition
    data_rows: list[dict]        # list of {col_idx: value} — 1-based col indices
    start_row: int = 9            # first data row in XML (inclusive)


@dataclass
class Workbook:
    path_or_bytes: Any            # original source for reference
    xml_bytes: bytes              # full original XML
    sheets: dict[str, SheetData]  # name -> SheetData
    customer_count: int = 0       # total customers detected
    object_name: str = "Customer"


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _clean_label(raw: str | None) -> tuple[str, bool]:
    """Strip the first line, detect mandatory (*)."""
    if not raw:
        return "", False
    first = str(raw).split("\n")[0].strip()
    mand = first.endswith("*")
    return first.rstrip("*").strip(), mand


def _cell_value(cell_el) -> Any:
    """Extract Data text content and type from a <Cell>."""
    data = cell_el.find(f"{SS}Data")
    if data is None or data.text is None:
        return None
    t = data.get(f"{SS}Type", "String")
    txt = data.text
    if t == "Number":
        try:
            return int(txt) if "." not in txt else float(txt)
        except ValueError:
            return txt
    if t == "Boolean":
        return txt in ("1", "true", "True")
    return txt


def _cell_index(cell_el, current_col: int) -> int:
    """Return effective 1-based column index of a cell (handles ss:Index jumps)."""
    idx = cell_el.get(f"{SS}Index")
    if idx:
        return int(idx)
    return current_col + 1


# ────────────────────────────────────────────────────────────────────────────
# Parse
# ────────────────────────────────────────────────────────────────────────────

def parse(xml_bytes: bytes) -> Workbook:
    """Parse SpreadsheetML → Workbook object with editable data only."""
    # Preserve entities, don't resolve
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(xml_bytes, parser=parser)

    wb = Workbook(path_or_bytes=None, xml_bytes=xml_bytes, sheets={})

    # Extract object name from custom properties
    cdp = root.find("{urn:schemas-microsoft-com:office:office}CustomDocumentProperties")
    if cdp is not None:
        obj = cdp.find("OBJECT_NAME")
        if obj is not None and obj.text:
            wb.object_name = obj.text.strip()

    for ws in root.findall(f"{SS}Worksheet"):
        name = ws.get(f"{SS}Name", "")
        if name in METADATA_SHEETS:
            continue

        table = ws.find(f"{SS}Table")
        if table is None:
            continue

        # Collect all rows in order (respecting ss:Index on rows too)
        rows: dict[int, Any] = {}
        current = 0
        for r in table.findall(f"{SS}Row"):
            idx_attr = r.get(f"{SS}Index")
            current = int(idx_attr) if idx_attr else current + 1
            rows[current] = r

        # Row 6 must have ETE marker to qualify
        row6 = rows.get(6)
        if row6 is None:
            continue
        first_cell = row6.find(f"{SS}Cell")
        if first_cell is None or not str(_cell_value(first_cell) or "").startswith("ETE;"):
            continue

        # Build column specs from rows 5, 6, 7, 8
        specs = _build_specs(rows.get(5), rows.get(6), rows.get(7), rows.get(8))

        # Extract data rows (row 9 onwards)
        data_rows: list[dict] = []
        for row_num in sorted(rows):
            if row_num < 9:
                continue
            r = rows[row_num]
            col = 0
            row_data: dict[int, Any] = {}
            for c in r.findall(f"{SS}Cell"):
                col = _cell_index(c, col)
                v = _cell_value(c)
                if v is not None:
                    row_data[col] = v
            if row_data:  # skip empty rows
                data_rows.append(row_data)

        wb.sheets[name] = SheetData(name=name, specs=specs, data_rows=data_rows)

    # Count unique customers (General Data row count)
    gen = wb.sheets.get("General Data")
    if gen:
        wb.customer_count = len(gen.data_rows)

    return wb


def _build_specs(row5, row6, row7, row8) -> list[FieldSpec]:
    """Build per-column specs from the 4 header rows."""
    specs: list[FieldSpec] = []

    def row_to_dict(r) -> dict[int, Any]:
        out: dict[int, Any] = {}
        if r is None:
            return out
        col = 0
        for c in r.findall(f"{SS}Cell"):
            col = _cell_index(c, col)
            out[col] = _cell_value(c)
        return out

    r5 = row_to_dict(row5)
    r6 = row_to_dict(row6)
    r7 = row_to_dict(row7)
    r8 = row_to_dict(row8)

    # find max column
    max_col = max(
        (max(d.keys(), default=0) for d in (r5, r6, r7, r8)),
        default=0,
    )

    current_group = ""
    for c in range(1, max_col + 1):
        ete = str(r6.get(c, "") or "")
        m = ETE_RE.match(ete)
        if not m:
            continue
        ete_length, _dec, ete_type, _disp, _disp2 = m.groups()

        group_val = r7.get(c)
        if group_val:
            current_group = str(group_val).strip()

        label, mand = _clean_label(r8.get(c))
        if not label:
            continue

        sap_field = str(r5.get(c, "") or "").strip()

        specs.append(FieldSpec(
            col_idx=c,
            sap_field=sap_field,
            ete_length=int(ete_length),
            ete_type=ete_type,
            group=current_group,
            label=label,
            mandatory=mand,
        ))
    return specs


# ────────────────────────────────────────────────────────────────────────────
# Write (roundtrip)
# ────────────────────────────────────────────────────────────────────────────

def write(wb: Workbook, changes: dict[str, list[dict]] | None = None) -> bytes:
    """
    Write Workbook back as SpreadsheetML preserving exact structure.

    `changes` is optional: {sheet_name: [{col_idx: new_value, ...}, ...]}
    where the list index matches the data_row index. If None, uses wb.sheets
    data as-is.
    """
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(wb.xml_bytes, parser=parser)

    # For each editable worksheet, replace data rows
    for ws in root.findall(f"{SS}Worksheet"):
        name = ws.get(f"{SS}Name", "")
        if name in METADATA_SHEETS:
            continue
        if name not in wb.sheets:
            continue

        sheet = wb.sheets[name]
        table = ws.find(f"{SS}Table")
        if table is None:
            continue

        # Capture original row 9 template (preserves styling attributes)
        template_row = _find_first_data_row(table)

        # Collect all rows from 1 to 8 (keep as-is) and row 9+ template props
        rows_to_keep = []
        row_idx = 0
        for r in list(table):
            if r.tag != f"{SS}Row":
                continue
            idx = r.get(f"{SS}Index")
            row_idx = int(idx) if idx else row_idx + 1
            if row_idx <= 8:
                rows_to_keep.append(r)

        # Remove all existing Row elements
        for r in list(table.findall(f"{SS}Row")):
            table.remove(r)

        # Re-append rows 1-8
        for r in rows_to_keep:
            table.append(r)

        # Determine data rows to write (apply changes if provided)
        data_to_write = list(sheet.data_rows)
        if changes and name in changes:
            for i, override in enumerate(changes[name]):
                if i < len(data_to_write):
                    merged = {**data_to_write[i], **override}
                    data_to_write[i] = merged
                else:
                    data_to_write.append(override)

        # Write each data row
        for row_data in data_to_write:
            new_row = _build_data_row(template_row, row_data, sheet.specs)
            table.append(new_row)

        # Update ExpandedRowCount on Table
        total = 8 + len(data_to_write)
        table.set(f"{SS}ExpandedRowCount", str(total))

    # Serialize with original XML declarations + processing instructions
    out = BytesIO()
    # Re-emit processing instructions manually (lxml loses them from root)
    pis = _extract_pis(wb.xml_bytes)
    out.write(b'<?xml version="1.0"?>\r\n')
    for pi in pis:
        if not pi.startswith('<?xml'):
            out.write(pi.encode("utf-8") + b"\r\n")
    body = etree.tostring(root, pretty_print=False, xml_declaration=False, encoding="utf-8")
    out.write(body)
    return out.getvalue()


def _extract_pis(xml_bytes: bytes) -> list[str]:
    """Extract processing instructions like <?mso-application progid="Excel.Sheet"?>"""
    pis = []
    # Simple regex on leading bytes (PIs are before <Workbook>)
    head = xml_bytes[:2000].decode("utf-8", errors="ignore")
    for m in re.finditer(r"<\?[^>]+\?>", head):
        pi = m.group()
        if not pi.startswith("<?xml"):
            pis.append(pi)
    return pis


def _find_first_data_row(table) -> Any | None:
    """Find first Row with row index >= 9 (for style template)."""
    row_idx = 0
    for r in table.findall(f"{SS}Row"):
        idx = r.get(f"{SS}Index")
        row_idx = int(idx) if idx else row_idx + 1
        if row_idx >= 9:
            return r
    return None


def _build_data_row(template_row, row_data: dict, specs: list[FieldSpec]):
    """Build a new <Row> using template_row's styling but with fresh cell values."""
    # Start with a template-only row clone to inherit StyleID attributes on Cells
    if template_row is not None:
        new_row = etree.Element(f"{SS}Row", nsmap={})
        # Copy Row attributes (StyleID, Height, etc.)
        for k, v in template_row.attrib.items():
            if k != f"{SS}Index":  # Let XML natural flow handle index
                new_row.set(k, v)
        # Build a map: col_idx -> template_cell for style copying
        template_cells: dict[int, Any] = {}
        col = 0
        for c in template_row.findall(f"{SS}Cell"):
            col = _cell_index(c, col)
            template_cells[col] = c
    else:
        new_row = etree.Element(f"{SS}Row")
        template_cells = {}

    # Emit cells in ascending column order
    last_written = 0
    for col in sorted(row_data.keys()):
        value = row_data[col]
        if value is None or value == "":
            continue

        cell = etree.SubElement(new_row, f"{SS}Cell")

        # Copy style from template cell if available
        template_cell = template_cells.get(col)
        if template_cell is not None:
            for k, v in template_cell.attrib.items():
                if k != f"{SS}Index":
                    cell.set(k, v)

        # If there's a gap in columns, use ss:Index
        if col != last_written + 1:
            cell.set(f"{SS}Index", str(col))

        # Build <Data>
        data_el = etree.SubElement(cell, f"{SS}Data")

        # Determine type from spec
        spec = next((s for s in specs if s.col_idx == col), None)
        dtype = "String"
        if isinstance(value, bool):
            dtype = "Boolean"
            value = "1" if value else "0"
        elif isinstance(value, (int, float)):
            dtype = "Number"
            value = str(value)
        elif spec and spec.ete_type == "N":
            try:
                float(str(value))
                dtype = "Number"
                value = str(value)
            except (ValueError, TypeError):
                pass

        data_el.set(f"{SS}Type", dtype)
        data_el.text = str(value)
        last_written = col

    return new_row


# ────────────────────────────────────────────────────────────────────────────
# Serialization for frontend (JSON-friendly)
# ────────────────────────────────────────────────────────────────────────────

def to_dict(wb: Workbook) -> dict:
    """Convert workbook to JSON-friendly dict for API response."""
    sheets_out = {}
    for name, sheet in wb.sheets.items():
        sheets_out[name] = {
            "name": name,
            "row_count": len(sheet.data_rows),
            "column_count": len(sheet.specs),
            "specs": [
                {
                    "col_idx": s.col_idx,
                    "sap_field": s.sap_field,
                    "label": s.label,
                    "group": s.group,
                    "ete_length": s.ete_length,
                    "ete_type": s.ete_type,
                    "mandatory": s.mandatory,
                }
                for s in sheet.specs
            ],
            # Convert col_idx keys (int) to string for JSON
            "rows": [
                {str(k): v for k, v in r.items()}
                for r in sheet.data_rows
            ],
        }
    return {
        "object_name": wb.object_name,
        "customer_count": wb.customer_count,
        "sheet_count": len(wb.sheets),
        "sheets": sheets_out,
    }

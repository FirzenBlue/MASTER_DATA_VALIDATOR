"""
LTMC XML generator for the MM (Material Master / Product) migration object.

This generator loads HML's LTMC SpreadsheetML template verbatim, replaces
each sheet's data section with rows generated from the merged MM data,
and writes the modified XML back. The non-data parts of the template
(Styles, CustomDocumentProperties, header rows 1-8 of every sheet, sheets
we don't populate) are preserved byte-for-byte.

This is the same strategy SD uses for its XML output: the original file's
shape is the contract, we just substitute values.

INJECTION MODEL
===============

For each sheet we populate, the template has:
  Row 1-8: banners, SAP structure name, field codes, ETE format, group
           labels, long help text — all preserved as-is.
  Row 9 onwards: data rows.

The replacement is byte-level on the raw XML string:

  <Worksheet ss:Name="Basic Data">
    <Table ss:ExpandedColumnCount="136" ss:ExpandedRowCount="9" ...>
      <Row>...</Row>  ← row 1
      ...
      <Row>...</Row>  ← row 8
      <Row ss:AutoFitHeight="0">  ← row 9 (existing data we replace)
        ...
      </Row>
    </Table>
  </Worksheet>

We:
  1. Locate the </Row>...</Table> boundary that ends row 8.
  2. Drop everything from there to </Table>.
  3. Inject our generated <Row> blocks.
  4. Update ExpandedRowCount on the Table to (8 + new_data_row_count).

For sheets we do NOT populate (Seasons, Article Hierarchy, etc.), we
leave them as the template has them — header rows only, no data rows.

DATA ROW SHAPE
==============

Data rows match the template's row-9 format exactly:
  <Row ss:AutoFitHeight="0">
    <Cell ss:Index="N" ss:StyleID="..."><Data ss:Type="..">value</Data></Cell>
    ...
  </Row>

We use sparse cells (`ss:Index` jumps) so blank columns between populated
fields are not emitted. ss:Type is "Number" for numeric values, "DateTime"
for dates, "String" otherwise. We omit ss:StyleID — LTMC's import doesn't
care about styling, and skipping it keeps our output cleaner.

FIELD MAPPING
=============

The mapping from source MM data → LTMC columns is derived from HML's
filled-in sample (a single FG material at PE01, VKORG=IN02, VTWEG=30).
Defaults like DATAB=2026-04-01, GEWEI=KGM, BRGEW=1, etc. are HML-wide
and live in HML_DEFAULTS below.
"""
from __future__ import annotations
import re
from pathlib import Path
from datetime import datetime
from typing import Any
from io import StringIO


# ─── Constants ─────────────────────────────────────────────────────────────

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "ltmc_template.xml"

# Healthium-specific defaults derived from the user's filled sample.
# These are not in the source xlsx but ARE in the LTMC ground truth —
# meaning HML wants them populated for FG materials.
HML_DEFAULTS = {
    # Basic Data (S_MARA)
    "DATAB": "2026-04-01T00:00:00.000",   # Valid From — DateTime type
    "GEWEI": "KGM",
    "BRGEW": "1",
    "NTGEW": "1",
    "XGCHP": "X",
    "SLED_BBD": "B",
    "RDMHD": "D",
    "SPRAS": "EN",
    # Plant Data (S_MARC)
    "MTVFP": "02",
    "PRCTR": "10020402",
    "TAXIM": "X ",   # trailing space matches HML's filled LTMC sample
    "GI_PR_TIME": "2",
    # Point of Sale Data (S_WLK2)
    "RBZUL": "X",
    # Tax Classification (S_MLAN)
    "ALAND": "IN",
    # Valuation Data (S_MBEW)
    "WAERS": "INR",
    # Class Data (S_CLASS)
    "CLASS": "SUTURES_CLASS",
    "CLASSTYPE": "023",
}

# India GST tax category scheme. TAXM (classification) values are 0/1 —
# defaulted to 0 except TATYP4=JOUG which HML's sample sets to 1.
#
# v61: trimmed from 8 entries (TATYP1..TATYP8) down to 5 (TATYP1..TATYP5)
# per SME spec (May 2026):
#   "Tax Category 6, Tax Classification 6, Tax Category 7, Tax
#    Classification 7, Tax Category 8, Tax Classification 8, Tax
#    Category 9, Tax Classification 9 — tax related things u can
#    remove from the column in LTMC standard format."
#
# Keeping TATYP1..TATYP5 because that's the canonical India GST output
# scheme:
#   JOCG  - CGST output
#   JOSG  - SGST output
#   JOIG  - IGST output
#   JOUG  - UTGST output (defaulted to TAXM=1 per HML sample)
#   JTC1  - tax code category placeholder
# Categories 6-8 (JTC2/JTC3/JTC4) were generic placeholders that the
# customer doesn't use; emitting them produced extra columns in the
# Tax Classification LTMC sheet that SAP would either ignore or
# reject as undefined for the Healthium tax determination config.
INDIA_TAX_CATEGORIES = [
    # v69: SME spec change — Tax Classification 5 (JTC1) is the "active"
    # classification with TAXM5='1'; all other categories (TAXM1-4) emit
    # '0'. Previously (v60-v68) TAXM4='1' for JOUG. The shift to TAXM5='1'
    # was confirmed by the SME on 2026-05-11 and aligns with HML's GST
    # configuration where JTC1 carries the active tax flag.
    ("TATYP1", "JOCG", "TAXM1", "0"),
    ("TATYP2", "JOSG", "TAXM2", "0"),
    ("TATYP3", "JOIG", "TAXM3", "0"),
    ("TATYP4", "JOUG", "TAXM4", "0"),
    ("TATYP5", "JTC1", "TAXM5", "1"),
]

# Inspection Setup — HML uses ART=04 (GR for FG) + ART=01 (in-process)
# per material per plant. Both APA=X (preferred) AKTIV=X (active).
INSPECTION_TYPES = [("04", "X", "X"), ("01", "X", "X")]

# Fields whose value in our generator is naturally numeric. Used to set
# ss:Type="Number" on the cell instead of "String". Numeric LTMC fields
# include lengths, weights, prices, periods, quantities, etc.
NUMERIC_FIELDS = {
    "BRGEW", "NTGEW", "VOLUM", "INHAL", "INHBR", "VPREH",
    "LAENG", "BREIT", "HOEHE", "CAPAUSE",
    "ERGEW", "GEWTO", "ERVOL", "VOLTO",
    "WESCH", "MHDHB", "MHDRZ", "MHDLP",
    "QQTIME", "MAXC", "MAXC_TOL", "MAXL", "MAXB", "MAXH",
    "WBWSP",
    # Plant Data
    "PRFRQ", "MINBE", "FXHOR", "VINT1", "VINT2",
    "BSTMI", "BSTMA", "BSTFE", "LOSFX", "AUSSS",
    "MABST", "BSTRF", "TAKZT", "EISBE", "EISLO", "SHZET",
    "LGRAD", "KAUSF", "VRBFK", "BASMG", "UEETO", "UNETO",
    "RUEZT", "TRANZ", "BEARZ", "LOSGR", "MAXLZ",
    "VRVEZ", "VBEAZ", "VBAMG", "GI_PR_TIME",
    "DZEIT", "PLIFZ", "WEBAZ", "WZEIT",
    "SCM_TARGET_DUR", "SCM_REORD_DUR", "SCM_GRPRT", "SCM_CONHAP",
    "SCM_GIPRT", "SCM_CONHAP_OUT",
    # Valuation Data
    "VERPR", "STPRS", "PEINH", "ZKPRS", "ZPLP1", "ZPLP2", "ZPLP3",
    "BWPRS", "BWPS1", "VJBWS", "BWPEI", "BWPRH", "BWPH1", "VJBWH",
    "ABWKZ", "BWSPA",
    # Alt UoM
    "UMREN", "UMREZ",
    # MRP/Forecasting
    "PERAN", "ANZPR", "PERIO", "PERIN", "FIMON", "SIGGR",
    "ALPHA", "BETA1", "GAMMA", "DELTA",
    # Receipt Texts
    "LFDNR",
    # Store Replenishment
    "SOBST", "TRCOV", "PRWUG", "PRWOG",
    # Distribution Chains
    "LFMNG", "LFMAX", "AUMNG", "SCMNG",
    "CTR_TERM_DEF", "CTR_TERM_ALT1", "CTR_TERM_ALT2",
    "EXT_PERIOD_DEF", "EXT_PERIOD_ALT1", "EXT_PERIOD_ALT2",
}

# Fields that are dates (ss:Type="DateTime")
DATE_FIELDS = {
    "DATAB", "LIQDT", "MSTDE", "MSTDV", "MMSTD", "AUSDT",
    "VRBDT", "ZKDAT", "ZPLD1", "ZPLD2", "ZPLD3",
    "LDVZL", "LDBZL", "LDVFL", "LDBFL",
    "VDVZL", "VDBZL", "VDVFL", "VDBFL",
    "VKDAB", "VKBIS",
}


# ─── Cached template parsing ────────────────────────────────────────────────

_TEMPLATE_CACHE: dict[str, Any] | None = None


def _load_template() -> dict[str, Any]:
    """Load and parse the LTMC template. Returns:
        {
            'raw': full template XML as a single str,
            'sheet_offsets': {sheet_name: {data_start, data_end,
                                           field_to_col,
                                           col_format: {col_idx: {type, style_id, x_ticked, raw_value}}}}
        }
    Done once and cached because the template is 1 MB and never changes.

    The `col_format` map is built from the template's existing data rows
    (rows 9+ of each sheet). It records the exact ss:Type, ss:StyleID,
    and x:Ticked attributes the template uses for each populated column,
    so when we inject our own rows we can mirror those attributes
    byte-for-byte. For columns the template never populated, we default
    to ss:Type="String" with no styling.
    """
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None:
        return _TEMPLATE_CACHE

    # Open in BINARY mode and decode explicitly. Text-mode `open(..., encoding="utf-8")`
    # on Windows silently translates \r\n → \n on read, which corrupts the
    # template's line endings. Excel rejects the resulting file with
    # "The file is corrupt and cannot be opened" because it gets mixed
    # line endings (lone \n from template-derived sections, \r\n from data
    # rows we generate). Using "rb" + .decode() preserves bytes verbatim
    # regardless of OS.
    with open(TEMPLATE_PATH, "rb") as f:
        raw = f.read().decode("utf-8")

    sheet_offsets: dict[str, dict] = {}

    for m in re.finditer(
        # Some sheets have ss:Protected="1" or other attributes between
        # Name's close-quote and the `>`. The original `>` literal here
        # excluded those — match any non-`>` attributes too.
        r'<Worksheet ss:Name="([^"]+)"[^>]*>(.*?)</Worksheet>',
        raw,
        re.DOTALL,
    ):
        name = m.group(1)
        body_start = m.start(2)
        body = m.group(2)

        # Find the <Table ...> open tag
        table_match = re.search(r"<Table\b[^>]*>", body)
        if table_match is None:
            continue
        table_open_abs_start = body_start + table_match.start()

        # Need ss:ExpandedRowCount to update later
        erc_match = re.search(r'ss:ExpandedRowCount="(\d+)"', table_match.group(0))
        if erc_match is None:
            continue

        # Find row 5 (SAP-field-codes row) — indexes per code
        rows_in_body = re.findall(r"<Row\b[^>]*>(.*?)</Row>", body, re.DOTALL)
        if len(rows_in_body) < 5:
            continue
        row5 = rows_in_body[4]
        field_to_col: dict[str, int] = {}
        col_to_field: dict[int, str] = {}
        cur_idx = 1
        for cm in re.finditer(
            r'<Cell\b([^>]*)><Data\b[^>]*>([^<]*)</Data></Cell>', row5
        ):
            attrs = cm.group(1)
            val = cm.group(2).strip()
            idx_attr = re.search(r'ss:Index="(\d+)"', attrs)
            if idx_attr:
                cur_idx = int(idx_attr.group(1))
            if val:
                field_to_col[val] = cur_idx
                col_to_field[cur_idx] = val
            cur_idx += 1

        # Build col_format from rows 9+ of the template (existing data
        # examples). Template rows 9+ come AFTER the 8th </Row>.
        col_format: dict[int, dict[str, str]] = {}
        if len(rows_in_body) >= 9:
            for data_row_str in rows_in_body[8:]:
                # Parse cells of this row, tracking ss:Index jumps
                cur_idx = 1
                cell_iter = re.finditer(
                    r'<Cell\b([^>]*)><Data\b([^>]*)>([^<]*)</Data></Cell>',
                    data_row_str,
                )
                for cm in cell_iter:
                    cell_attrs = cm.group(1)
                    data_attrs = cm.group(2)
                    val = cm.group(3)
                    idx_attr = re.search(r'ss:Index="(\d+)"', cell_attrs)
                    if idx_attr:
                        cur_idx = int(idx_attr.group(1))
                    # Only record format for columns the template populated
                    # (skip cells whose value is empty/whitespace).
                    if val.strip() and cur_idx not in col_format:
                        type_match = re.search(r'ss:Type="([^"]+)"', data_attrs)
                        style_match = re.search(r'ss:StyleID="([^"]+)"', cell_attrs)
                        ticked_match = re.search(r'x:Ticked="([^"]+)"', data_attrs)
                        col_format[cur_idx] = {
                            "type": type_match.group(1) if type_match else "String",
                            "style_id": style_match.group(1) if style_match else "",
                            "x_ticked": ticked_match.group(1) if ticked_match else "",
                        }
                    cur_idx += 1

        # Find data section start = end of 8th </Row>; end = position of </Table>
        if len(rows_in_body) < 8:
            data_section_start = body_start + table_match.end()
            data_section_end = body_start + body.rfind("</Table>")
        else:
            row_close_iter = list(re.finditer(r"</Row>\s*", body))
            if len(row_close_iter) < 8:
                continue
            after_row8 = row_close_iter[7].end()
            data_section_start = body_start + after_row8
            data_section_end = body_start + body.rfind("</Table>")

        sheet_offsets[name] = {
            "data_start": data_section_start,
            "data_end": data_section_end,
            "field_to_col": field_to_col,
            "col_format": col_format,
        }

    _TEMPLATE_CACHE = {"raw": raw, "sheet_offsets": sheet_offsets}
    return _TEMPLATE_CACHE


# ─── Cell construction helpers ─────────────────────────────────────────────

def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


def _val_str(v: Any) -> str:
    """Coerce a Python value to its LTMC string representation. Does NOT
    strip whitespace — some HML source cells have trailing spaces (e.g.
    XCHPF="X ") which the template preserves; we mirror that exactly."""
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return str(v)
    return str(v)


def _data_type_for(field_code: str, value: str) -> str:
    """Pick ss:Type for a cell based on the field's domain."""
    if not value:
        return "String"
    if field_code in DATE_FIELDS:
        return "DateTime"
    if field_code in NUMERIC_FIELDS:
        # Only mark as Number if the value parses as numeric. If a number
        # field is somehow holding text (rare), fall back to String to
        # avoid LTMC rejecting the cell.
        try:
            float(value)
            return "Number"
        except (ValueError, TypeError):
            return "String"
    return "String"


def _emit_data_row(values_dict: dict[str, str],
                   field_to_col: dict[str, int],
                   col_format: dict[int, dict[str, str]]) -> str:
    """Emit a single <Row> using sparse cells with ss:Index, mirroring
    the template's per-column formatting (ss:Type, ss:StyleID, x:Ticked).

    Cells appear in column order; gaps are skipped (next cell gets an
    explicit ss:Index attribute).
    """
    populated = [(field_to_col[f], f, v) for f, v in values_dict.items()
                 if f in field_to_col and v not in (None, "")]
    populated.sort(key=lambda x: x[0])

    parts = ['  <Row ss:AutoFitHeight="0">\r\n']
    expected_idx = 1
    for col_idx, field, value in populated:
        sval = _val_str(value)
        # Look up template format for this column. If not in template
        # (column never populated in the template's example rows),
        # fall back to our heuristic from the field-code domain.
        fmt = col_format.get(col_idx)
        if fmt is not None:
            cell_type = fmt["type"]
            style_id = fmt["style_id"]
            x_ticked = fmt["x_ticked"]
        else:
            cell_type = _data_type_for(field, sval)
            style_id = ""
            x_ticked = ""

        # If the template declares ss:Type="Number" or "DateTime" but
        # the actual value can't be coerced to that type, downgrade to
        # "String". Excel rejects entire workbooks with "Problems
        # During Load: Table" if any single cell has a value that
        # contradicts its declared ss:Type.
        #
        # Real-world example from a customer file: a material with
        # MATNR="TBC-2" (literal "to be confirmed" placeholder) in a
        # column the LTMC template declares as Number — Excel refuses
        # to open the entire 100+ MB file because of this one cell.
        # Validation should catch these upfront, but the generator
        # must be defensive too: shipping a file Excel won't open is
        # worse than shipping a file with one cell typed as String
        # instead of Number (SAP's importer is permissive on this).
        if cell_type == "Number":
            try:
                float(sval)
            except (ValueError, TypeError):
                cell_type = "String"
                x_ticked = ""  # x:Ticked only valid on Number cells
        elif cell_type == "DateTime":
            # DateTime values must be ISO format like 2026-03-01T00:00:00.000
            import re as _re
            if not _re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', sval):
                cell_type = "String"

        # Build cell attributes
        cell_attrs = ""
        if col_idx != expected_idx:
            cell_attrs += f' ss:Index="{col_idx}"'
        if style_id:
            cell_attrs += f' ss:StyleID="{style_id}"'

        # Build data attributes
        data_attrs = f' ss:Type="{cell_type}"'
        if x_ticked:
            data_attrs += f' x:Ticked="{x_ticked}"'

        parts.append(
            f'    <Cell{cell_attrs}><Data{data_attrs}>{_xml_escape(sval)}</Data></Cell>\r\n'
        )
        expected_idx = col_idx + 1
    parts.append('   </Row>\r\n')
    return "".join(parts)


# ─── Per-sheet builders ────────────────────────────────────────────────────

def _build_basic_data(material) -> dict[str, str]:
    main = material.main
    return {
        "PRODUCT": _val_str(material.matnr),
        "MTART": _val_str(main.get("MTART")),
        "MATKL": _val_str(main.get("MATKL")),
        "MBRSH": _val_str(main.get("MBRSH")),
        "MAKTX": _val_str(main.get("MAKTX")),
        # v62: SPRAS uses session override → HML default ('EN') → ''
        "SPRAS": _resolve_ltmc("SPRAS", main.get("SPRAS"), HML_DEFAULTS["SPRAS"]),
        "MEINS": _val_str(main.get("MEINS")),
        "SPART": _val_str(main.get("SPART")),
        "BISMT": _val_str(main.get("BISMT")),
        "XCHPF": _val_str(main.get("XCHPF") or "X"),
        "DATAB": HML_DEFAULTS["DATAB"],
        "MTPOS_MARA": _val_str(main.get("MTPOS_MARA")),
        "BRGEW": HML_DEFAULTS["BRGEW"],
        "NTGEW": HML_DEFAULTS["NTGEW"],
        "GEWEI": HML_DEFAULTS["GEWEI"],
        "TRAGR": _val_str(main.get("TRAGR")),
        "XGCHP": HML_DEFAULTS["XGCHP"],
        "MHDHB": _val_str(main.get("MHDHB")),
        "MHDRZ": _val_str(main.get("MHDRZ")),
        "SLED_BBD": HML_DEFAULTS["SLED_BBD"],
        "RDMHD": HML_DEFAULTS["RDMHD"],
    }


def _build_additional_descriptions(material) -> list[dict[str, str]]:
    rows = []
    for lt in material.longtexts:
        text = lt.get("BASE_TEXT") or ""
        if not text:
            continue
        rows.append({
            "PRODUCT": _val_str(material.matnr),
            # v62: SPRAS uses override → HML default ('EN')
            "SPRAS": _resolve_ltmc("SPRAS", None, HML_DEFAULTS["SPRAS"]),
            "MAKTX": text[:40],
        })
    return rows


def _build_alt_units(material) -> list[dict[str, str]]:
    rows = []
    for au in material.alt_uoms:
        rows.append({
            "PRODUCT": _val_str(material.matnr),
            "MEINH": _val_str(au.get("MEINH")),
            "UMREN": _val_str(au.get("UMREN")),
            "UMREZ": _val_str(au.get("UMREZ")),
        })
    return rows


def _build_class_data(material) -> dict[str, str]:
    return {
        "PRODUCT": _val_str(material.matnr),
        "CLASS": HML_DEFAULTS["CLASS"],
        "CLASSTYPE": HML_DEFAULTS["CLASSTYPE"],
    }


def _build_distribution_chains(material) -> dict[str, str]:
    main = material.main
    return {
        "PRODUCT": _val_str(material.matnr),
        "VKORG": _val_str(main.get("VKORG")),
        "VTWEG": _val_str(main.get("VTWEG")),
        "SKTOF": _val_str(main.get("SKTOF")),
        "MTPOS": _val_str(main.get("MTPOS")),
        "KTGRM": _val_str(main.get("KTGRM")),
        # v58: DWERK (Delivering Plant) is part of the customer's
        # mandatory-field list. Sales-area-level field, lives on the
        # Distribution Chains LTMC sheet alongside VKORG/VTWEG.
        "DWERK": _val_str(main.get("DWERK")),
    }


def _build_point_of_sale(material) -> dict[str, str]:
    main = material.main
    return {
        "PRODUCT": _val_str(material.matnr),
        "VKORG": _val_str(main.get("VKORG")),
        "VTWEG": _val_str(main.get("VTWEG")),
        "RBZUL": HML_DEFAULTS["RBZUL"],
    }


def _build_tax_classification(material) -> dict[str, str]:
    # v62: ALAND uses session override → HML default ('IN') → ''
    # SME can change this to e.g. a different country code via the
    # "Set LTMC default value" Decision card on the ALAND row.
    main = material.main
    out = {
        "PRODUCT": _val_str(material.matnr),
        "ALAND": _resolve_ltmc("ALAND", main.get("ALAND"), HML_DEFAULTS["ALAND"]),
    }
    # India GST tax categories (TATYP1..TATYP5) — override-aware so
    # SMEs can change the default 0/0/0/0/1 classification pattern
    # for materials with non-standard tax treatment if needed.
    for ty_field, ty_val, tx_field, tx_val in INDIA_TAX_CATEGORIES:
        out[ty_field] = _resolve_ltmc(ty_field, main.get(ty_field), ty_val)
        out[tx_field] = _resolve_ltmc(tx_field, main.get(tx_field), tx_val)
    return out


def _build_plant_data(material, plant_row) -> dict[str, str]:
    main = material.main
    return {
        "PRODUCT": _val_str(material.matnr),
        "WERKS": _val_str(plant_row.get("WERKS")),
        "DISMM": _val_str(plant_row.get("DISMM") or main.get("DISMM")),
        "DISPO": _val_str(plant_row.get("DISPO") or main.get("DISPO")),
        "MTVFP": HML_DEFAULTS["MTVFP"],
        # v58: PRCTR (Profit Center). When the customer provides
        # SALES_PRCTR (mandatory per List 2), use that. Else fall back
        # to PLNT2_PRCTR (some templates use this name) or the bundled
        # HML default.
        "PRCTR": _val_str(
            main.get("SALES_PRCTR")
            or main.get("PLNT2_PRCTR")
            or HML_DEFAULTS["PRCTR"]
        ),
        "XCHPF": _val_str(main.get("XCHPF") or "X"),
        "LADGR": _val_str(main.get("LADGR")),
        "STEUC": _val_str(main.get("STEUC")),
        "TAXIM": HML_DEFAULTS["TAXIM"],
        "MAABC": _val_str(main.get("MAABC")),
        "STRGR": _val_str(main.get("STRGR")),
        "FXHOR": _val_str(main.get("FXHOR")),
        "VRMOD": _val_str(main.get("VRMOD")),
        "VINT1": _val_str(main.get("VINT1")),
        "VINT2": _val_str(main.get("VINT2")),
        "DISLS": _val_str(main.get("DISLS")),
        "EISBE": _val_str(main.get("EISBE")),
        "SBDKZ": _val_str(main.get("SBDKZ")),
        "BESKZ": _val_str(main.get("BESKZ")),
        "LGPRO": _val_str(plant_row.get("LGPRO") or main.get("LGPRO")),
        "DZEIT": _val_str(main.get("DZEIT")),
        "WEBAZ": _val_str(main.get("WEBAZ")),
        "FHORI": _val_str(main.get("FHORI")),
        "FEVOR": _val_str(plant_row.get("FEVOR") or main.get("FEVOR")),
        "SFCPF": _val_str(main.get("SFCPF")),
        "AWSLS": _val_str(main.get("AWSLS")),
        "LOSGR": _val_str(main.get("LOSGR")),
        "GI_PR_TIME": HML_DEFAULTS["GI_PR_TIME"],
        # v58: PLIFZ (Planned Delivery time) — Planning team mandatory.
        "PLIFZ": _val_str(main.get("PLIFZ")),
        # v58: RGEKZ (Backflush indicator) — Production team mandatory.
        # Plant-scoped in SAP (each plant can have its own backflush
        # setting), so check plant_row first.
        "RGEKZ": _val_str(plant_row.get("RGEKZ") or main.get("RGEKZ")),
    }


def _build_storage_locations(material, plant_row) -> dict[str, str]:
    return {
        "PRODUCT": _val_str(material.matnr),
        "WERKS": _val_str(plant_row.get("WERKS")),
        "LGORT": _val_str(
            plant_row.get("LGPRO") or plant_row.get("LGORT")
            or material.main.get("LGPRO")
        ),
    }


def _build_inspection_setup(material, plant_row) -> list[dict[str, str]]:
    rows = []
    for art, apa, aktiv in INSPECTION_TYPES:
        rows.append({
            "PRODUCT": _val_str(material.matnr),
            "WERKS": _val_str(plant_row.get("WERKS")),
            "ART": art,
            "APA": apa,
            "AKTIV": aktiv,
        })
    return rows


def _build_valuation_data(material, plant_row, bwkey_map: dict[str, str]) -> dict[str, str]:
    werks = _val_str(plant_row.get("WERKS"))
    # v62: BWKEY resolution order:
    #   1) Per-row BWKEY in plant_row (SME edited via Records editor)
    #   2) Session override (SME set "Set LTMC default value")
    #   3) WERKS value itself (v60 default — plant-level valuation)
    #   4) bwkey_map fallback (KDS)
    bwkey = (_val_str(plant_row.get("BWKEY"))
             or _ACTIVE_OVERRIDES.get("BWKEY")
             or werks
             or bwkey_map.get(werks, ""))
    main = material.main
    # v58: VPRSV controls which price column SAP wants populated.
    #   VPRSV = 'S' → STPRS (standard price) is the active price
    #   VPRSV = 'V' → VERPR (moving avg price) is the active price
    # The validator's mm_price_missing_for_vprsv rule fires if the
    # required-side column is blank. The LTMC export emits both
    # columns regardless (SAP tolerates blanks on the inactive side),
    # but the customer's value flows through unchanged either way.
    vprsv = _val_str(main.get("VPRSV"))
    stprs = main.get("STPRS")
    verpr = main.get("VERPR")
    return {
        "PRODUCT": _val_str(material.matnr),
        "BWKEY": bwkey,
        "MLAST": _val_str(main.get("MLAST")),
        "BKLAS": _val_str(main.get("BKLAS")),
        "VPRSV": vprsv,
        # v62: WAERS uses session override → HML default ('INR') → ''
        "WAERS": _resolve_ltmc("WAERS", main.get("WAERS"), HML_DEFAULTS["WAERS"]),
        "STPRS": _val_str(stprs) if stprs is not None else "",
        # v58: VERPR (moving price) — emitted alongside STPRS so SAP
        # has both columns populated in the LTMC template. Customer
        # provides the value; we don't compute or default it.
        "VERPR": _val_str(verpr) if verpr is not None else "",
        # v58: PEINH (Price Unit) — paired with STPRS/VERPR. Common
        # default is "1" (price per 1 unit) when not specified.
        "PEINH": _val_str(main.get("PEINH") or "1"),
        # v58: EKALR (With QS / Costing relevance flag) — Finance team
        # mandatory. Goes on the Costing view alongside valuation in
        # the LTMC template.
        "EKALR": _val_str(main.get("EKALR")),
    }


# Per-sheet dispatch: (sheet_name, builder, mode)
# mode: "single" | "list_per_material" | "per_plant" | "list_per_plant" | "valuation"
SHEET_BUILDERS = [
    ("Basic Data",                   _build_basic_data,              "single"),
    ("Additional Descriptions",      _build_additional_descriptions, "list_per_material"),
    ("Alternative Units of Measure", _build_alt_units,               "list_per_material"),
    ("Class Data",                   _build_class_data,              "single"),
    ("Distribution Chains",          _build_distribution_chains,     "single"),
    ("Point of Sale Data",           _build_point_of_sale,           "single"),
    ("Tax Classification",           _build_tax_classification,      "single"),
    ("Plant Data",                   _build_plant_data,              "per_plant"),
    ("Storage Locations",            _build_storage_locations,       "per_plant"),
    ("Inspection Setup Data",        _build_inspection_setup,        "list_per_plant"),
    ("Valuation Data",               None,                           "valuation"),
]


# ─── Public API ───────────────────────────────────────────────────────────

def generate_ltmc_xml(materials, bwkey_map: dict[str, str] | None = None,
                      ltmc_overrides: dict[str, str] | None = None) -> bytes:
    """Generate LTMC SpreadsheetML XML bytes by injecting data rows into
    HML's template. Format matches the template byte-for-byte except for
    the data sections we replace.

    Args:
        materials: iterable of MergedMaterial objects from mm_merger.merge.
        bwkey_map: optional WERKS→BWKEY map from KDS for Valuation Data.
        ltmc_overrides: optional session-level field overrides (v62).
            Map of {sap_field: value} set by the SME via "Set LTMC default
            value" Decision actions. Resolution order during emit:
                1. Per-row source value (material.main / plant_row)
                2. ltmc_overrides[sap_field]    ← v62
                3. HML_DEFAULTS[sap_field]
                4. ""
            Lets SMEs set BWKEY, ALAND, WAERS, CURTP etc. via Decisions
            instead of the system silently using HML hard-coded defaults.

    Returns:
        bytes of the complete XML file ready for LTMC upload.
    """
    if bwkey_map is None:
        bwkey_map = {}

    # Module-level slot for ltmc_overrides — same pattern as
    # _ACTIVE_LABELS in mm_validator.py. Avoids threading the overrides
    # dict through every _build_* builder signature (would touch ~12
    # functions) when only a handful actually consult it.
    global _ACTIVE_OVERRIDES
    prev_overrides = _ACTIVE_OVERRIDES
    _ACTIVE_OVERRIDES = dict(ltmc_overrides or {})

    try:
        return _generate_ltmc_xml_inner(materials, bwkey_map)
    finally:
        _ACTIVE_OVERRIDES = prev_overrides


# Module-level slot for the active LTMC overrides map. Set by
# generate_ltmc_xml() at the start of each call and cleared at the end.
# `_resolve_ltmc()` reads it. Same pattern as _ACTIVE_LABELS in
# mm_validator.py — module-level rather than per-builder argument to
# avoid touching every _build_* function.
_ACTIVE_OVERRIDES: dict[str, str] = {}


def _resolve_ltmc(sap_field: str, source_value=None, hml_default: str = "") -> str:
    """Resolve a field's emit value per v62 lookup order.

    1. Source value (per-row from material.main or plant_row)
    2. Session override (set via "Set LTMC default value" Decisions)
    3. HML hard-coded default
    4. ""

    Source value normalisation (Excel quirk: numeric cells often arrive
    as 90189099.0 not "90189099") is handled here so callers don't have
    to repeat the float→int→str dance.
    """
    if source_value is not None:
        s = _val_str(source_value)
        if s and s.lower() not in ("none", "nan"):
            return s
    if sap_field in _ACTIVE_OVERRIDES:
        v = _ACTIVE_OVERRIDES[sap_field]
        if v:
            return str(v)
    if hml_default:
        return str(hml_default)
    return ""


def _generate_ltmc_xml_inner(materials, bwkey_map: dict[str, str]) -> bytes:
    """Internal — separated so the _ACTIVE_OVERRIDES setup wraps cleanly."""

    template = _load_template()
    raw = template["raw"]
    sheet_offsets = template["sheet_offsets"]

    # Build the data-row blocks per sheet
    sheet_data: dict[str, str] = {}
    sheet_row_counts: dict[str, int] = {}

    for sheet_name, builder, mode in SHEET_BUILDERS:
        if sheet_name not in sheet_offsets:
            continue
        field_to_col = sheet_offsets[sheet_name]["field_to_col"]
        col_format = sheet_offsets[sheet_name]["col_format"]
        data_rows: list[str] = []

        for material in materials:
            if mode == "single":
                row = builder(material)
                if any(v for v in row.values()):
                    data_rows.append(_emit_data_row(row, field_to_col, col_format))
            elif mode == "list_per_material":
                for row in builder(material):
                    if any(v for v in row.values()):
                        data_rows.append(_emit_data_row(row, field_to_col, col_format))
            elif mode == "per_plant":
                for plant_row in material.plant_rows:
                    row = builder(material, plant_row)
                    if any(v for v in row.values()):
                        data_rows.append(_emit_data_row(row, field_to_col, col_format))
            elif mode == "list_per_plant":
                for plant_row in material.plant_rows:
                    for row in builder(material, plant_row):
                        if any(v for v in row.values()):
                            data_rows.append(_emit_data_row(row, field_to_col, col_format))
            elif mode == "valuation":
                for plant_row in material.plant_rows:
                    row = _build_valuation_data(material, plant_row, bwkey_map)
                    if any(v for v in row.values()):
                        data_rows.append(_emit_data_row(row, field_to_col, col_format))

        sheet_data[sheet_name] = "".join(data_rows)
        sheet_row_counts[sheet_name] = len(data_rows)

    # Inject — work back-to-front so earlier offsets stay valid.
    sorted_sheets = sorted(
        sheet_data.keys(),
        key=lambda n: sheet_offsets[n]["data_start"],
        reverse=True,
    )
    out = raw
    for sheet_name in sorted_sheets:
        info = sheet_offsets[sheet_name]
        new_data = sheet_data[sheet_name]
        new_row_count = sheet_row_counts[sheet_name]

        # Replace the data section
        out = out[: info["data_start"]] + "\r\n" + new_data + "  " + out[info["data_end"]:]

        # Update ExpandedRowCount = 8 header rows + N data rows.
        # Indices are based on the ORIGINAL raw, so we must recompute from
        # the new `out`. Use the same regex match scoped to this sheet's
        # name (worksheets are uniquely named).
        new_erc = str(8 + new_row_count)
        # Update the FIRST occurrence of the Table tag inside this sheet.
        # Same fix as _load_template: allow ss:Protected="1" etc. after Name.
        ws_match = re.search(
            rf'(<Worksheet ss:Name="{re.escape(sheet_name)}"[^>]*>.*?<Table\b[^>]*?ss:ExpandedRowCount=")(\d+)(")',
            out,
            re.DOTALL,
        )
        if ws_match:
            out = out[: ws_match.start(2)] + new_erc + out[ws_match.end(2):]

    return out.encode("utf-8")

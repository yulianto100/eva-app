"""GDR (Goods Delivery Receipt) service for the web dashboard.

Mirrors the WhatsApp bot's GDR generation logic:
- Fetches next available GDR number from Google Sheets
- Marks it as used
- Writes material data to GDR spreadsheet
- Generates an Excel file from the universal template using openpyxl
- Returns the file for download

Spreadsheet IDs (same as bot):
- GDR_NUMBER_SHEET_ID: Nomor GDR tracking (1XTlHR_QAoXJ-BPE1gOO5OQV-BBPJ48PsL8uPvuPMBRI)
- GDR_SHEET_ID: Material data (1FNKF9e4Q9xlLDq4BG5V_DBCK7vTPVyjMMLUFP_vrkW8)
"""
import json
import math
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import gspread
import openpyxl
from google.oauth2.service_account import Credentials

from config import Config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

GDR_NUMBER_SHEET_ID = os.getenv("GDR_NUMBER_SHEET_ID", "1XTlHR_QAoXJ-BPE1gOO5OQV-BBPJ48PsL8uPvuPMBRI")
GDR_SHEET_ID = os.getenv("GDR_SHEET_ID", "1FNKF9e4Q9xlLDq4BG5V_DBCK7vTPVyjMMLUFP_vrkW8")
SERVICE_ACCOUNT_PATH = Config.BASE_DIR / ".secrets" / "eva-google-service-account.json"
GDR_TEMPLATE_DIR = Config.BASE_DIR / "templates" / "GDR"
GDR_VENDORS_FILE = Config.BASE_DIR / "gdr_vendors.json"
GDR_HISTORY_FILE = Config.BASE_DIR / "gdr_history.json"

# Material column mapping for the GDR spreadsheet (same as bot)
# Column indices in spreadsheet (0-based): E=4, F=5, ... W=22
MATERIAL_COL_MAP = {
    "FO core type SM G.652.D-ADSS 24 cores": 4,
    "FO core type SM G.652.D-ADSS 36 cores": 5,
    "FO core type SM G.652.D-ADSS 48 cores": 6,
    "FO core type SM G.652.D-ADSS 96 cores": 7,
    "FO core type SM G.652.D-ADSS 144 cores": 8,
    "FO core type SM G.652.D-ADSS 288 cores": 9,
    "144 cores capacity ground mounted FDT": 10,
    "72 cores capacity pole mounted FDT": 11,
    "48 cores capacity pole mounted FDT": 12,
    "Pedestal mounted type (16 ports type)": 13,
    "Pole mounted outdoor type (16 ports type)": 14,
    'Pengadaan Tiang 7 meter 4", STEL L-003 1996': 15,
    "Pengadaan Tiang 7 meter 3 inch": 16,
    "Pole / Tiang Udara 7 Meter 2.5": 17,
    "Sub Duct / HDPE 32/26 warna hitam, 3 strip ungu": 18,
    "Sub Duct / HDPE 40/34 warna Ungu, 3 Strip Putih": 19,
    "Precast Hand hole for FDT : 80 x 80 x 130 cm": 20,
    "Precast Hand hole for FAT : 40 x 40 x 60 cm": 21,
    "Precast Hand hole for pulling pit : 20x20x20 cm": 22,
}


def _sheets_client():
    creds = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_PATH), scopes=SCOPES)
    return gspread.authorize(creds)


def private_store():
    from gdr_store import GdrStore
    return GdrStore(Config.BASE_DIR / "data" / "gdr_private.db")


def load_vendors(owner_user_id: str) -> dict:
    return private_store().vendors(owner_user_id)


def load_history(owner_user_id: str) -> list:
    return private_store().history(owner_user_id)


def get_next_gdr_number() -> dict:
    """Fetch next available GDR number from Google Sheets (No > 800, empty col B)."""
    gc = _sheets_client()
    sh = gc.open_by_key(GDR_NUMBER_SHEET_ID)
    ws = sh.worksheet("Nomor GDR 2026")
    rows = ws.get_all_values()

    reserved = private_store().reserved_labels()
    now = datetime.now()
    next_no = None
    row_index = None
    for i, row in enumerate(rows[1:], start=2):  # skip header
        try:
            no = int(row[0])
        except (ValueError, IndexError):
            continue
        used = (row[1] if len(row) > 1 else "").strip()
        label = f"{no}/EMR/NRO-GDR/{now.month}/{now.year}"
        if no > 800 and not used and label not in reserved:
            next_no = no
            row_index = i
            break

    if next_no is None:
        raise ValueError("No available GDR number found (No > 800 with empty col B)")

    gdr_label = f"{next_no}/EMR/NRO-GDR/{now.month}/{now.year}"
    return {"number": next_no, "label": gdr_label, "row_index": row_index}


def allocate_gdr_number(vendor: str, pr_numbers: list[str] | None = None, clusters: list[dict] | None = None, area: str = "", pmo: str = "", *, owner_user: dict, request_hash: str) -> dict:
    """Reserve durably before Sheets; caller holds sheet_write_guard throughout generation."""
    store = private_store()
    info = get_next_gdr_number()
    store.reserve_number(owner_user, info, request_hash)
    try:
        mark_gdr_number(
            number=info["number"],
            gdr_label=info["label"],
            vendor=vendor,
            pr_numbers=pr_numbers or [],
            clusters=clusters or [],
            area=area,
            pmo=pmo,
        )
    except Exception as exc:
        # A timeout has an unknown provider outcome; keep its reservation pending.
        store.update_reservation(info["label"], "pending" if isinstance(exc, TimeoutError) else "failed",
                                 failure_code="NUMBER_MARK_FAILED")
        raise
    store.update_reservation(info["label"], "number_marked")
    return info


def mark_gdr_number(number: int, gdr_label: str, vendor: str, pr_numbers: list[str] | None = None, clusters: list[dict] | None = None, area: str = "", pmo: str = "") -> None:
    """Mark a GDR number as used in the spreadsheet."""
    gc = _sheets_client()
    sh = gc.open_by_key(GDR_NUMBER_SHEET_ID)
    ws = sh.worksheet("Nomor GDR 2026")
    rows = ws.get_all_values()

    for i, row in enumerate(rows[1:], start=2):
        try:
            no = int(row[0])
        except (ValueError, IndexError):
            continue
        if no == number:
            used = (row[1] if len(row) > 1 else "").strip()
            if used:
                raise ValueError(f"GDR number {number} already used: {used}")
            now = datetime.now()
            date_str = now.strftime("%d %B %Y")
            cluster_entries = []
            for item in (clusters or []):
                pr = str(item.get("pr_number", "")).strip()
                cl = str(item.get("cluster", "")).strip()
                if pr:
                    cluster_entries.append(f"PR_{pr}")
                elif cl:
                    cluster_entries.append(cl)
            pr_list = "; ".join(cluster_entries) if cluster_entries else ", ".join(f"PR_{p}" for p in (pr_numbers or []))
            values = [number, date_str, gdr_label, vendor, pr_list, "PROGRESS", area or "", pmo or "", str(now.year)]
            ws.update(f"A{i}:I{i}", [values], value_input_option="RAW")
            return

    raise ValueError(f"GDR number {number} not found in Nomor GDR 2026 sheet")


def write_materials_to_gdr_sheet(
    vendor: str,
    materials: list[dict],
    pr_number: str,
    cluster: str,
    gdr_label: str,
) -> None:
    """Write material data to the GDR spreadsheet as a SINGLE row for this PR.
    All materials fill their own columns (E-W) within that one row ??? one PR
    is always one row, never split across multiple rows."""
    gc = _sheets_client()
    sh = gc.open_by_key(GDR_SHEET_ID)
    ws = sh.worksheet("GDR")

    # Build one row: A=PR, B=Vendor, C=Cluster, D=(empty), E-W=materials, Y=GDR No
    row_data = [""] * 25
    row_data[0] = pr_number  # A: PR Number
    row_data[1] = vendor  # B: Vendor
    row_data[2] = cluster  # C: Roll Out / Cluster
    row_data[24] = gdr_label  # Y: No GDR

    for mat in materials:
        mat_name = mat.get("name", "")
        qty = float(mat.get("qty", 0))
        col_idx = MATERIAL_COL_MAP.get(mat_name)
        if col_idx is None or qty <= 0:
            continue
        row_data[col_idx] = int(qty)  # fill material column in the same row

    ws.append_row(row_data, value_input_option="RAW")


def _vendor_header_values(vendor_code: str, vendor_data: dict) -> dict:
    """Use only the captured creator-owned vendor, never the shared legacy JSON."""
    from gdr_store import vendor_values
    entry = vendor_values(vendor_data)
    return {"F7": entry["vendor_name"], "F8": entry["origin"],
            "F9": entry["destination"], "F10": entry["pic"]}


def _find_template() -> Path | None:
    """Find the universal GDR template file."""
    if not GDR_TEMPLATE_DIR.exists():
        return None
    for f in GDR_TEMPLATE_DIR.iterdir():
        if f.name.upper().startswith("TEMPLATE GDR") and f.suffix.lower() == ".xlsx":
            return f
    return None


def _round_up(value: float, multiple: int) -> int:
    return math.ceil(value / multiple) * multiple


def generate_gdr_excel(
    vendor: str,
    materials: list[dict],
    gdr_label: str,
    pr_number: str = "",
    cluster: str = "",
    clusters: list[dict] | None = None,
    area: str = "",
    pmo: str = "",
    request_name: str = "",
    request_mobile: str = "",
    approve_name: str = "",
    approve_mobile: str = "",
    *,
    vendor_data: dict,
) -> Path:
    """Generate GDR Excel file from template using openpyxl. Returns path to generated file."""
    template = _find_template()
    if template is None:
        raise FileNotFoundError("Template GDR tidak ditemukan")

    # Prepare output
    out_dir = Config.OUTPUT_DIR / "gdr"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_label = gdr_label.replace("/", "-")
    import uuid
    from gdr_store import vendor_code
    vendor = vendor_code(vendor)
    out_file = out_dir / f"GDR {safe_label}, {vendor}, {uuid.uuid4().hex}.xlsx"
    fd = os.open(out_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output, template.open("rb") as source:
        os.fchmod(output.fileno(), 0o600)
        shutil.copyfileobj(source, output)

    now = datetime.now()
    date_str = now.strftime("%d %B %Y")
    req_date = (now + timedelta(days=3)).strftime("%d %B %Y")

    def _gdr_unit_label(name: str, unit_field: str) -> str:
        """Determine remark unit label from material description + unit field."""
        n = (name or "").upper()
        u = (unit_field or "").upper()
        # FDT / FAT / ODF / Splitter / etc → unit (must win before "pole mounted" text)
        if "FDT" in n or "FAT" in n or "ODF" in n or "SPLITTER" in n:
            return "unit"
        # Cable / FO → m
        if "FO" in n or "KABEL" in n or "CABLE" in n or u == "METER":
            return "m"
        # Pole / Tiang → set
        if "POLE" in n or "TIANG" in n or u == "SET":
            return "set"
        return "unit"

    # Prepare items
    items = []
    for m in materials:
        name = m.get("name", "")
        qty = float(m.get("qty", 0))
        original_qty = qty
        unit = m.get("unit", "")
        if qty <= 0:
            continue
        unit_label = _gdr_unit_label(name, unit)
        # Qty column for cables: round up to 3000m blocks.
        # Remark for cables: keep original EVA material input quantity.
        display_qty = qty
        remark_qty = original_qty
        if unit_label == "m":
            display_qty = _round_up(qty, 3000)
        breakdown = m.get("pr_breakdown") or []
        remark_parts = []
        for part in breakdown:
            part_pr = str(part.get("pr_number", "")).strip()
            try:
                part_qty = float(part.get("qty", 0) or 0)
            except (TypeError, ValueError):
                part_qty = 0
            if part_pr and part_qty > 0:
                remark_parts.append(f"PR_{part_pr}, {int(part_qty)} {unit_label}")
        if remark_parts:
            remark = "; ".join(remark_parts)
        else:
            remark = f"PR_{pr_number}, {int(remark_qty)} {unit_label}" if pr_number else ""
        items.append({"description": name, "qty": display_qty, "unit": unit, "remark": remark})

    cluster_entries = []
    for item in (clusters or []):
        pr = str(item.get("pr_number", "")).strip()
        cl = str(item.get("cluster", "")).strip()
        if pr and cl:
            cluster_entries.append(f"PR_{pr} - {cl}")
        elif pr:
            cluster_entries.append(f"PR_{pr}")
        elif cl:
            cluster_entries.append(cl)
    if cluster_entries:
        pr_summary = "\n".join(cluster_entries)
    else:
        pr_summary = f"PR_{pr_number} - {cluster}" if pr_number and cluster else (f"PR_{pr_number}" if pr_number else "")

    # Get vendor header values
    header = _vendor_header_values(vendor, vendor_data)

    # Open workbook with openpyxl
    wb = openpyxl.load_workbook(out_file)
    # Use the sheet named "Contoh GDR" or first sheet
    ws = None
    for name in wb.sheetnames:
        if "contoh" in name.lower() or "gdr" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active

    import re as _re
    from copy import copy as _copy
    from openpyxl.utils import range_boundaries, get_column_letter

    def _read_anchor(row, col):
        """Read a cell value resolving merged cells to their anchor."""
        ref = f"{get_column_letter(col)}{row}"
        for mr in ws.merged_cells.ranges:
            mc, mrow, xc, xrow = range_boundaries(str(mr))
            if mrow <= row <= xrow and mc <= col <= xc:
                return ws.cell(row=mrow, column=mc).value
        return ws.cell(row=row, column=col).value

    # Locate the material header row ("No" in col A or C).
    header_row = 12
    for row in range(1, 41):
        if str(_read_anchor(row, 1) or "").strip() == "No" or str(_read_anchor(row, 3) or "").strip() == "No":
            header_row = row
            break
    data_start_row = header_row + 1

    # Locate the signature section start ("Request by" / "Approve by").
    signature_row = None
    for row in range(data_start_row, data_start_row + 80):
        row_text = " ".join(str(_read_anchor(row, c) or "") for c in range(1, 8))
        if _re.search(r"request by|approve by|pickup by", row_text, _re.I):
            signature_row = row
            break
    if signature_row is None:
        signature_row = data_start_row + 1

    # Template has exactly one material data row at data_start_row (row 13).
    # For N materials, insert N-1 rows directly BELOW that row, copying row 13
    # style/height/merged-cell pattern. This makes output grow like the Excel
    # template instead of leaving blank rows before the signature block.
    template_material_rows = 1
    extra_rows = max(0, len(items) - template_material_rows)

    # Capture single-row column merges within the template data row so they
    # can be replicated on every inserted data row (if template later uses
    # merged cells in row 13).
    data_row_col_merges = []
    for mr in list(ws.merged_cells.ranges):
        mc, mrow, xc, xrow = range_boundaries(str(mr))
        if mrow == xrow == data_start_row and xc > mc:
            data_row_col_merges.append((mc, xc))

    # Locate values beside the template's actual Request/Approve labels.
    # Never overwrite label cells or carry the template's sample identities.
    signature_fields = {}
    labels = {"name": "name", "mobile": "mobile", "date": "date", "dept": "dept"}
    for kind, heading in (("request", "request by"), ("approve", "approve by")):
        col = next((c for c in range(1, 8)
                    if heading in str(_read_anchor(signature_row, c) or "").lower()), None)
        if col is None:
            raise ValueError("Bagian tanda tangan GDR tidak ditemukan: " + kind)
        for row in range(signature_row + 1, ws.max_row + 1):
            label = str(_read_anchor(row, col) or "").lower().strip(" :")
            if label in labels:
                signature_fields[(kind, labels[label])] = (row, col + 1)
        if any((kind, field) not in signature_fields for field in labels):
            raise ValueError("Field tanda tangan GDR tidak lengkap: " + kind)
        for field in ("name", "mobile"):
            row, value_col = signature_fields[(kind, field)]
            ws.cell(row, value_col).value = None

    # openpyxl moves cells, not merge ranges or row dimensions. Unmerge BEFORE
    # insertion, then rebuild the shifted ranges so no orphan MergedCell remains.
    if extra_rows > 0:
        insert_at = data_start_row + 1
        merges = [range_boundaries(str(r)) for r in ws.merged_cells.ranges]
        dimensions = {r: _copy(d) for r, d in ws.row_dimensions.items() if r >= insert_at}
        for r in list(ws.merged_cells.ranges):
            ws.unmerge_cells(str(r))
        ws.insert_rows(insert_at, extra_rows)
        for r in sorted(dimensions, reverse=True):
            del ws.row_dimensions[r]
        for r, dimension in dimensions.items():
            dimension.index = r + extra_rows
            ws.row_dimensions[r + extra_rows] = dimension
        template_height = ws.row_dimensions[data_start_row].height
        for row in range(insert_at, insert_at + extra_rows):
            ws.row_dimensions[row].height = template_height
            for col in range(1, 8):
                ws.cell(row, col)._style = _copy(ws.cell(data_start_row, col)._style)
        for mc, first, xc, last in merges:
            if first >= insert_at:
                first, last = first + extra_rows, last + extra_rows
            elif last >= insert_at:
                last += extra_rows
            ws.merge_cells(start_row=first, start_column=mc, end_row=last, end_column=xc)
        for row in range(insert_at, insert_at + extra_rows):
            for mc, xc in data_row_col_merges:
                ws.merge_cells(start_row=row, start_column=mc, end_row=row, end_column=xc)

    # Rebuild merge anchor map AFTER row insertion (ranges shifted).
    merge_anchor_map = {}
    for merge_range in ws.merged_cells.ranges:
        min_col, min_row, max_col, max_row = range_boundaries(str(merge_range))
        anchor = f"{get_column_letter(min_col)}{min_row}"
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                merge_anchor_map[f"{get_column_letter(c)}{r}"] = anchor

    def safe_set(cell_ref, value):
        """Write literal text at the merge anchor; never interpret user formulas."""
        cell = ws[merge_anchor_map.get(cell_ref, cell_ref)]
        cell.value = value
        if isinstance(value, str):
            cell.data_type = "s"

    def safe_set_rc(row, col, value):
        safe_set(f"{get_column_letter(col)}{row}", value)

    # Write header values
    safe_set("C7", f": {gdr_label}")
    safe_set("C8", f": {date_str}")
    safe_set("C9", f": {req_date}")
    # C10, G7, and G10 intentionally left blank per template requirement.
    safe_set("C10", "")
    safe_set("G7", "")
    safe_set("G9", f": {cluster}" if cluster else ": Bekasi")
    safe_set("G10", "")

    # Every header comes from the creator-owned snapshot, including blanks.
    for ref in ("F7", "F8", "F9", "F10"):
        safe_set(ref, header[ref])

    # Populate the shifted value cells, preserving the printed labels.
    for kind, values in (("request", {"name": request_name or "", "mobile": str(request_mobile or ""),
                                     "date": date_str, "dept": "Roll Out"}),
                         ("approve", {"name": approve_name or "", "mobile": str(approve_mobile or ""),
                                     "date": req_date, "dept": "Roll Out"})):
        for field, value in values.items():
            row, col = signature_fields[(kind, field)]
            safe_set_rc(row + extra_rows, col, value)

    # Write material items
    for idx, item in enumerate(items):
        row = data_start_row + idx
        safe_set_rc(row, 1, idx + 1)  # A: No
        safe_set_rc(row, 2, "")  # B: Part Number
        safe_set_rc(row, 3, item["description"])  # C: Description
        safe_set_rc(row, 5, item["qty"])  # E: Qty
        safe_set_rc(row, 6, item["unit"])  # F: Unit
        safe_set_rc(row, 7, item["remark"])  # G: Remark

    # Write PR summary above the (possibly shifted) signature section.
    if pr_summary:
        from math import ceil
        from openpyxl.styles import Alignment

        sig_after = signature_row + extra_rows
        summary_row = sig_after - 1
        safe_set_rc(summary_row, 7, pr_summary)

        # If summary lands inside a merged range (e.g. G21:H22 after row shifts),
        # keep it readable: wrap text, center vertically, top-align horizontally,
        # and expand row heights based on text length.
        target_merge = None
        for mr in ws.merged_cells.ranges:
            if f"G{summary_row}" in mr:
                target_merge = str(mr)
                break

        if target_merge:
            min_col, min_row, max_col, max_row = range_boundaries(target_merge)
            anchor = ws.cell(min_row, min_col)
            anchor.alignment = _copy(Alignment(horizontal='left', vertical='center', wrap_text=True))

            span_rows = max_row - min_row + 1
            span_cols = max_col - min_col + 1
            text_len = len(str(pr_summary or ''))
            est_lines = max(1, ceil(text_len / max(28, 18 * span_cols)))
            total_height = max(30, min(90, est_lines * 15))
            per_row_height = total_height / span_rows
            for r in range(min_row, max_row + 1):
                current = ws.row_dimensions[r].height or 15
                ws.row_dimensions[r].height = max(current, per_row_height)
        else:
            cell = ws.cell(summary_row, 7)
            cell.alignment = _copy(Alignment(horizontal='left', vertical='center', wrap_text=True))
            current = ws.row_dimensions[summary_row].height or 15
            ws.row_dimensions[summary_row].height = max(current, 36)

    # Keep print/page-break preview covering all generated rows down to the
    # shifted signature/pickup section instead of stopping near the item table.
    try:
        final_print_row = max(ws.max_row, (signature_row + extra_rows + 12))
        ws.print_area = f"A1:G{final_print_row}"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 1
        ws.page_margins.left = 0.25
        ws.page_margins.right = 0.25
        ws.page_margins.top = 0.35
        ws.page_margins.bottom = 0.35
    except Exception:
        pass

    wb.save(out_file)
    wb.close()
    out_file.chmod(0o600)
    return out_file


def delete_gdr_from_sheets(gdr_label: str) -> None:
    """Delete exact label only; caller holds sheet_write_guard and keeps a tombstone."""
    number = int(gdr_label.split("/", 1)[0])
    if number <= 0:
        raise ValueError("Label GDR tidak valid.")
    gc = _sheets_client()

    # Delete bottom-up; provider failures must leave local history intact.
    ws = gc.open_by_key(GDR_SHEET_ID).worksheet("GDR")
    rows = ws.get_all_values()
    for i in reversed(range(2, len(rows) + 1)):
        if len(rows[i - 1]) > 24 and rows[i - 1][24].strip() == gdr_label:
            current = ws.row_values(i)
            if len(current) <= 24 or current[24].strip() != gdr_label:
                raise ValueError("Baris material GDR berubah; perlu rekonsiliasi.")
            ws.delete_rows(i)

    ws = gc.open_by_key(GDR_NUMBER_SHEET_ID).worksheet("Nomor GDR 2026")
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > 2 and row[2].strip() == gdr_label:
            current = ws.row_values(i)
            if len(current) <= 2 or current[2].strip() != gdr_label or int(current[0]) != number:
                raise ValueError("Baris nomor GDR berubah; perlu rekonsiliasi.")
            ws.update(f"B{i}:I{i}", [[""] * 8], value_input_option="RAW")
            break


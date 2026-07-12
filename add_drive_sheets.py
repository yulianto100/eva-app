"""Add sheets from drive-download folder to Excel and upload to Google Sheets."""
from __future__ import annotations

import os
import shutil
import time
from copy import copy
from pathlib import Path

import gspread
import openpyxl
from google.oauth2.service_account import Credentials

from coreman_sheets import SCOPES, service_account_path, spreadsheet_id

EXCEL_PATH = Path(r"C:/Users/Employee/Downloads/Coreman_Ops_Dashboard_Bekasi.xlsx")
TEMP_PATH = Path(r"C:/Users/Employee/Downloads/Coreman_Ops_Dashboard_Bekasi_v2.xlsx")
DRIVE_FOLDER = Path(r"C:\Users\Employee\Downloads\drive-download-20260601T154623Z-3-001")
BATCH_SIZE = 5000
DELAY = 1.0

OLT_FILE_MAP = {
    "OLT BINTARA": "OLT BINTARA",
    "OLT CIKARANG UTARA": "OLT CIKARANG UTARA",
    "OLT DUREN JAYA 2": "OLT DUREN JAYA",
    "OLT HARAPAN JAYA": "OLT HARAPAN JAYA",
    "OLT LAMBANG JAYA": "OLT LAMBANG JAYA",
    "SHELTER NORTH BEKASI": "OLT NORTH BEKASI",
}
SKIP_SHEETS = {"Sheet4", "Sheet5", "Sheet6", "Sheet7"}


def is_sheet_empty(ws, threshold=10):
    count = 0
    for r in range(1, min(ws.max_row + 1, 30)):
        for c in range(1, min(ws.max_column + 1, 30)):
            if ws.cell(row=r, column=c).value is not None:
                count += 1
                if count >= threshold:
                    return False
    return count < threshold


def copy_sheet(src_ws, dst_wb, new_name):
    dst_ws = dst_wb.create_sheet(title=new_name)
    max_col = min(src_ws.max_column, 29)
    for row in src_ws.iter_rows(min_row=1, max_row=src_ws.max_row, max_col=max_col):
        for cell in row:
            if cell.value is not None:
                nc = dst_ws.cell(row=cell.row, column=cell.column, value=cell.value)
                if cell.font:
                    nc.font = copy(cell.font)
                if cell.fill and cell.fill.fill_type:
                    nc.fill = copy(cell.fill)
                if cell.alignment:
                    nc.alignment = copy(cell.alignment)
                if cell.border:
                    nc.border = copy(cell.border)
    for col_letter, dim in src_ws.column_dimensions.items():
        if dim.width:
            dst_ws.column_dimensions[col_letter].width = dim.width
    return dst_ws


def main():
    print("Loading existing Excel...")
    wb = openpyxl.load_workbook(EXCEL_PATH)
    existing = set(wb.sheetnames)
    print(f"  {len(existing)} existing sheets")

    added = 0
    for fname in sorted(os.listdir(DRIVE_FOLDER)):
        if not fname.endswith(".xlsx"):
            continue
        path = DRIVE_FOLDER / fname
        olt_file = os.path.splitext(fname)[0]
        olt_name = OLT_FILE_MAP.get(olt_file, olt_file)
        print(f"\n=== {fname} -> {olt_name} ===")
        src_wb = openpyxl.load_workbook(path, data_only=True)

        for sname in src_wb.sheetnames:
            if sname in SKIP_SHEETS:
                continue
            src_ws = src_wb[sname]
            if is_sheet_empty(src_ws):
                print(f"  Skip (empty): {sname}")
                continue
            new_name = f"{olt_name} - {sname}"
            if len(new_name) > 31:
                new_name = new_name[:31]
            if new_name in existing:
                new_name = new_name[:28] + " v2"
            print(f"  Copy: {sname} -> {new_name}")
            copy_sheet(src_ws, wb, new_name)
            existing.add(new_name)
            added += 1
        src_wb.close()

    print(f"\nSaving to {TEMP_PATH} ({added} new sheets)...")
    wb.save(TEMP_PATH)
    wb.close()
    print(f"Saved! Total sheets: {len(existing)}")

    # Upload new sheets to Google Sheets
    print("\n=== Uploading to Google Sheets ===")
    creds = Credentials.from_service_account_file(str(service_account_path()), scopes=SCOPES)
    gc = gspread.authorize(creds)
    sp = gc.open_by_key(spreadsheet_id())
    gsheets = {ws.title: ws for ws in sp.worksheets()}
    print(f"Current Google sheets: {len(gsheets)}")

    wb2 = openpyxl.load_workbook(TEMP_PATH, data_only=True)
    uploaded = 0
    for sname in wb2.sheetnames:
        if sname in gsheets:
            continue
        ws_excel = wb2[sname]
        data = []
        max_col = min(ws_excel.max_column, 29)
        for row in ws_excel.iter_rows(min_row=1, max_row=ws_excel.max_row, max_col=max_col, values_only=True):
            data.append([str(v) if v is not None else "" for v in row])
        while data and all(c == "" for c in data[-1]):
            data.pop()
        if not data:
            continue

        print(f"  {sname} ({len(data)} rows)")
        for attempt in range(2):
            try:
                new_ws = sp.add_worksheet(title=sname, rows=max(len(data), 100), cols=29)
                time.sleep(0.5)
                cells = []
                for r_idx, row in enumerate(data):
                    for c_idx, val in enumerate(row):
                        if val:
                            cells.append(gspread.Cell(r_idx + 1, c_idx + 1, val))
                for i in range(0, len(cells), BATCH_SIZE):
                    batch = cells[i:i + BATCH_SIZE]
                    new_ws.update_cells(batch, value_input_option="USER_ENTERED")
                    if i + BATCH_SIZE < len(cells):
                        time.sleep(0.5)
                uploaded += 1
                time.sleep(DELAY)
                break
            except gspread.exceptions.APIError as e:
                if "quota" in str(e).lower() or "429" in str(e):
                    print(f"  Rate limited, waiting 60s...")
                    time.sleep(60)
                else:
                    print(f"  FAILED: {e}")
                    break
            except Exception as e:
                print(f"  FAILED: {e}")
                break

    wb2.close()
    print(f"\nDone! {uploaded} new sheets uploaded")
    print(f"URL: https://docs.google.com/spreadsheets/d/{spreadsheet_id()}")


if __name__ == "__main__":
    main()

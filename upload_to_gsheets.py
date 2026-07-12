"""Upload Manajemen Core Excel to Google Spreadsheet."""
from __future__ import annotations

import time
from pathlib import Path

import gspread
import openpyxl
from google.oauth2.service_account import Credentials

from coreman_sheets import (
    COREMAN_HEADERS,
    DEFAULT_SERVICE_ACCOUNT_PATH,
    DEFAULT_SPREADSHEET_ID,
    SCOPES,
    normalize_sheet_title,
    service_account_path,
    spreadsheet_id,
)

EXCEL_PATH = Path(r"C:/Users/Employee/Downloads/Coreman_Ops_Dashboard_Bekasi.xlsx")
TEMPLATE_PATH = Path(r"C:/Users/Employee/Downloads/Template Coreman ASTRI - Bekasi.xlsx")
BATCH_SIZE = 5000  # cells per API call
DELAY_BETWEEN_SHEETS = 1.0  # seconds


def get_gspread_client():
    creds = Credentials.from_service_account_file(str(service_account_path()), scopes=SCOPES)
    return gspread.authorize(creds)


def excel_to_2d(ws, max_col=29):
    """Convert openpyxl worksheet to 2D list for gspread."""
    data = []
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=max_col, values_only=True):
        data.append([str(v) if v is not None else "" for v in row])
    # Remove trailing empty rows
    while data and all(c == "" for c in data[-1]):
        data.pop()
    return data


def upload_sheet_data(worksheet, data):
    """Upload 2D data to a gspread worksheet."""
    if not data:
        return
    # Resize worksheet
    rows_needed = max(len(data), 100)
    cols_needed = max(len(data[0]) if data else 29, 29)
    if worksheet.row_count < rows_needed or worksheet.col_count < cols_needed:
        worksheet.resize(rows=rows_needed, cols=cols_needed)
    # Upload in batches
    all_cells = []
    for r_idx, row in enumerate(data):
        for c_idx, val in enumerate(row):
            if val:
                all_cells.append(gspread.Cell(r_idx + 1, c_idx + 1, val))
    for i in range(0, len(all_cells), BATCH_SIZE):
        batch = all_cells[i:i + BATCH_SIZE]
        worksheet.update_cells(batch, value_input_option="USER_ENTERED")
        if i + BATCH_SIZE < len(all_cells):
            time.sleep(0.5)


def main():
    print("Connecting to Google Sheets...")
    gc = get_gspread_client()
    sp = gc.open_by_key(spreadsheet_id())

    # Read Excel
    print("Reading Excel file...")
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    sheet_names = wb.sheetnames
    print(f"  {len(sheet_names)} sheets to upload")

    # Get existing sheets
    existing = {ws.title: ws for ws in sp.worksheets()}
    print(f"  Current spreadsheet has {len(existing)} sheets")

    # Delete all existing sheets except TEMPLATE MANCORE (we'll update it later)
    for title, ws in existing.items():
        if title != "TEMPLATE MANCORE":
            try:
                sp.del_worksheet(ws)
                print(f"  Deleted: {title}")
                time.sleep(0.3)
            except Exception as e:
                print(f"  Warning: could not delete {title}: {e}")

    # Upload each sheet
    success = 0
    errors = 0
    for idx, sname in enumerate(sheet_names):
        print(f"[{idx+1}/{len(sheet_names)}] Uploading: {sname}")
        try:
            ws_excel = wb[sname]
            data = excel_to_2d(ws_excel)
            if not data:
                print(f"  Skipping (empty)")
                continue

            # Create sheet
            new_ws = sp.add_worksheet(title=sname, rows=max(len(data), 100), cols=29)
            time.sleep(0.5)

            # Upload data
            upload_sheet_data(new_ws, data)
            success += 1
            print(f"  OK ({len(data)} rows)")
            time.sleep(DELAY_BETWEEN_SHEETS)

        except gspread.exceptions.APIError as e:
            if "quota" in str(e).lower() or "429" in str(e):
                print(f"  Rate limited, waiting 60s...")
                time.sleep(60)
                try:
                    new_ws = sp.add_worksheet(title=sname, rows=max(len(data), 100), cols=29)
                    time.sleep(0.5)
                    upload_sheet_data(new_ws, data)
                    success += 1
                    print(f"  OK (retry)")
                except Exception as e2:
                    print(f"  FAILED: {e2}")
                    errors += 1
            else:
                print(f"  FAILED: {e}")
                errors += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            errors += 1

    # Update TEMPLATE MANCORE from template file
    print("\nUpdating TEMPLATE MANCORE...")
    try:
        twb = openpyxl.load_workbook(TEMPLATE_PATH, data_only=True)
        tws = twb["TEMPLATE MANCORE"]
        template_data = excel_to_2d(tws, max_col=16)
        twb.close()

        template_ws = None
        for ws in sp.worksheets():
            if ws.title == "TEMPLATE MANCORE":
                template_ws = ws
                break

        if template_ws:
            # Clear existing data
            template_ws.resize(rows=max(len(template_data), 100), cols=16)
            template_ws.clear()
            # Upload template data
            upload_sheet_data(template_ws, template_data)
            print(f"  TEMPLATE MANCORE updated ({len(template_data)} rows)")
        else:
            print("  TEMPLATE MANCORE not found!")
    except Exception as e:
        print(f"  FAILED: {e}")

    wb.close()
    print(f"\nDone! {success} sheets uploaded, {errors} errors")
    print(f"URL: https://docs.google.com/spreadsheets/d/{spreadsheet_id()}")


if __name__ == "__main__":
    main()

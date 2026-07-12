from __future__ import annotations

import os
import re
import time
import logging
from pathlib import Path
from functools import wraps
from typing import Any, Callable

import gspread
from google.oauth2.service_account import Credentials

from config import Config

logger = logging.getLogger(__name__)

DEFAULT_SPREADSHEET_ID = "1MOb_FOjIktkBZ8x9xh9zL66RFzGwItSMBGJUJKuEnHI"
DEFAULT_SERVICE_ACCOUNT_PATH = Path(".secrets") / "eva-google-service-account.json"
DEFAULT_TEMPLATE_SHEET_NAME = "TEMPLATE"
DEFAULT_GRID_WIDTH = 29
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
COREMAN_HEADERS = [
    "Source Type",
    "Source Code",
    "source name",
    "Source Tube Number",
    "Source Core Number",
    "(Sub)Feeder Code",
    "Tube Number",
    "Core Number",
    "Status",
    "Remarks",
    "Closure Code",
    "Dest. Type",
    "Dest. Code",
    "Dest. Tube Number",
    "Dest. Core Number",
    "Dest. Name",
]


def retry_on_quota_error(max_retries: int = 3, base_delay: float = 30.0):
    """Decorator to retry on Google Sheets 429 quota errors with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    exc_str = str(exc).lower()
                    is_quota = "429" in exc_str or "quota" in exc_str or "rate limit" in exc_str or "resourceexhausted" in type(exc).__name__.lower()
                    if not is_quota or attempt == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.warning("Quota hit on %s (attempt %d/%d), retrying in %.0fs", func.__name__, attempt + 1, max_retries, delay)
                    time.sleep(delay)
                    last_exc = exc
            raise last_exc
        return wrapper
    return decorator


def spreadsheet_id() -> str:
    return os.getenv("COREMAN_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID).strip() or DEFAULT_SPREADSHEET_ID


def template_sheet_name() -> str:
    return os.getenv("COREMAN_TEMPLATE_SHEET_NAME", DEFAULT_TEMPLATE_SHEET_NAME).strip() or DEFAULT_TEMPLATE_SHEET_NAME


def is_template_sheet(title: str) -> bool:
    normalized = " ".join(str(title or "").strip().upper().split())
    configured = " ".join(template_sheet_name().upper().split())
    return normalized in {configured, "TEMPLATE", "TEMPLATE MANCORE", "TEMPLATE COREMAN"}


def service_account_path() -> Path:
    configured = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else Config.BASE_DIR / path
    return Config.BASE_DIR / DEFAULT_SERVICE_ACCOUNT_PATH


def gspread_client():
    path = service_account_path()
    if not Path(path).exists():
        raise FileNotFoundError(
            f"Google service account belum disiapkan di {path}. "
            "Tambahkan file JSON kredensial atau set env GOOGLE_SERVICE_ACCOUNT_FILE ke path lain. "
            "Operasional non-Google-Sheets tetap berjalan."
        )
    creds = Credentials.from_service_account_file(str(path), scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet():
    return gspread_client().open_by_key(spreadsheet_id())


def gspread_unavailable_payload(message=None):
    msg = message or (
        "Google Sheets belum terhubung: file kredensial tidak ditemukan di server. "
        "Sinkronisasi spreadsheet manual dapat dilakukan setelah kredensial ditambahkan."
    )
    return {
        "spreadsheet_id": spreadsheet_id(),
        "sheets": [],
        "rows": [],
        "total": 0,
        "warning": msg,
        "google_sheets_available": False,
    }


def worksheet_or_none(spreadsheet, title: str):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return None


def normalize_sheet_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(title or "").strip())
    if not cleaned:
        raise ValueError("Nama sheet wajib diisi")
    if is_template_sheet(cleaned):
        raise ValueError("Nama sheet tidak boleh memakai nama template")
    return cleaned[:99]


def parse_sheet_title(title: str) -> dict:
    match = re.match(r"^(.*?)\s+-\s+SEG\s+(.+)$", title.strip(), flags=re.IGNORECASE)
    if not match:
        return {"olt_name": title.strip(), "segment_name": "", "is_segment_sheet": False}
    return {
        "olt_name": match.group(1).strip(),
        "segment_name": match.group(2).strip(),
        "is_segment_sheet": True,
    }


def list_sheets() -> list[dict]:
    try:
        spreadsheet = open_spreadsheet()
    except FileNotFoundError as exc:
        logger.warning("Google Sheets belum terhubung: %s", exc)
        return []
    sheets = []
    for worksheet in spreadsheet.worksheets():
        title = worksheet.title
        if is_template_sheet(title):
            continue
        parsed = parse_sheet_title(title)
        sheets.append(
            {
                "title": title,
                "gid": worksheet.id,
                "row_count": worksheet.row_count,
                "col_count": worksheet.col_count,
                **parsed,
            }
        )
    return sheets


def ensure_sheet(sheet_name: str, template_name: str | None = None, row_count: int = 144) -> dict:
    title = normalize_sheet_title(sheet_name)
    try:
        spreadsheet = open_spreadsheet()
    except FileNotFoundError as exc:
        return {"title": title, "created": False, "warning": str(exc), "google_sheets_available": False}
    existing = worksheet_or_none(spreadsheet, title)
    if existing is not None:
        return {
            "title": existing.title,
            "gid": existing.id,
            "row_count": existing.row_count,
            "col_count": existing.col_count,
            "created": False,
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id()}/edit#gid={existing.id}",
        }

    template_title = (template_name or template_sheet_name()).strip() or template_sheet_name()
    template = worksheet_or_none(spreadsheet, template_title)
    if template is not None:
        worksheet = spreadsheet.duplicate_sheet(template.id, new_sheet_name=title)
    else:
        worksheet = spreadsheet.add_worksheet(title=title, rows=max(int(row_count or 144) + 1, 2), cols=len(COREMAN_HEADERS))
        worksheet.update(values=[COREMAN_HEADERS], range_name="A1:P1")

    return {
        "title": worksheet.title,
        "gid": worksheet.id,
        "row_count": worksheet.row_count,
        "col_count": worksheet.col_count,
        "created": True,
        "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id()}/edit#gid={worksheet.id}",
    }


def worksheet_by_title(title: str):
    return open_spreadsheet().worksheet(title)


def column_label(index: int) -> str:
    label = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def pad_row(row: list, width: int = 16) -> list[str]:
    padded = [str(value or "") for value in row[:width]]
    return padded + [""] * max(0, width - len(padded))


def is_grid_template(values: list[list]) -> bool:
    joined = " ".join(str(cell or "").upper() for row in values[:4] for cell in row)
    return "FDT INPUT SPLICING" in joined or "HUB FEEDER" in joined or "HUB FEEDER" in joined


def build_grid_headers(values: list[list], width: int) -> list[str]:
    header_rows = [pad_row(values[index] if index < len(values) else [], width) for index in range(3)]
    headers: list[str] = []
    for col in range(width):
        parts = []
        for row in header_rows:
            value = row[col].strip()
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        headers.append(" / ".join(parts) if parts else f"Column {col + 1}")
    return headers


def infer_layout(values: list[list], col_count: int = 0) -> dict:
    width = max(DEFAULT_GRID_WIDTH, len(COREMAN_HEADERS), int(col_count or 0), *(len(row) for row in values)) if values else max(DEFAULT_GRID_WIDTH, len(COREMAN_HEADERS), int(col_count or 0))
    if is_grid_template(values):
        return {
            "width": width,
            "range": f"A:{column_label(width)}",
            "header_rows": 3,
            "headers": build_grid_headers(values, width),
            "layout": "coreman_grid",
        }
    return {
        "width": max(width, len(COREMAN_HEADERS)),
        "range": f"A:{column_label(max(width, len(COREMAN_HEADERS)))}",
        "header_rows": 1,
        "headers": pad_row(values[0] if values else COREMAN_HEADERS, max(width, len(COREMAN_HEADERS))),
        "layout": "legacy_rows",
    }


def get_rows(sheet_name: str, limit: int = 400, offset: int = 0) -> dict:
    if is_template_sheet(sheet_name):
        raise ValueError("Sheet template tidak ditampilkan di dashboard Coreman")
    try:
        worksheet = worksheet_by_title(sheet_name)
    except FileNotFoundError as exc:
        return {
            "spreadsheet_id": spreadsheet_id(),
            "sheet": sheet_name,
            "rows": [],
            "total": 0,
            "warning": str(exc),
            "google_sheets_available": False,
        }
    except gspread.exceptions.WorksheetNotFound:
        return {
            "spreadsheet_id": spreadsheet_id(),
            "sheet": sheet_name,
            "rows": [],
            "total": 0,
            "warning": f"Sheet '{sheet_name}' belum dibuat di Google Sheets. Buat dulu lewat tombol 'Buat Sheet'.",
            "google_sheets_available": True,
        }
    values = worksheet.get(f"A:{column_label(max(DEFAULT_GRID_WIDTH, worksheet.col_count or 0))}") or []
    layout = infer_layout(values, worksheet.col_count)
    headers = layout["headers"]
    body = values[layout["header_rows"]:]
    total = len(body)
    start = max(0, int(offset or 0))
    end = start + max(1, int(limit or 400))
    rows = [
        {
            "row_number": index + layout["header_rows"] + 1,
            "values": pad_row(row, layout["width"]),
        }
        for index, row in enumerate(body[start:end], start=start)
    ]
    return {
        "sheet": {"title": worksheet.title, "gid": worksheet.id, "row_count": worksheet.row_count, "col_count": worksheet.col_count},
        "headers": headers,
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "layout": layout["layout"],
        "header_rows": layout["header_rows"],
    }


def update_row(sheet_name: str, row_number: int, values: list) -> dict:
    if is_template_sheet(sheet_name):
        raise ValueError("Sheet template tidak bisa diedit dari dashboard Coreman")
    worksheet = worksheet_by_title(sheet_name)
    row_number = int(row_number)
    if row_number < 2:
        raise ValueError("Header row tidak bisa diedit")
    width = max(DEFAULT_GRID_WIDTH, len(values or []), worksheet.col_count or 0, len(COREMAN_HEADERS))
    padded = pad_row(values, width)
    worksheet.update(values=[padded], range_name=f"A{row_number}:{column_label(width)}{row_number}")
    return {"row_number": row_number, "values": padded}


def clear_assignment(sheet_name: str, row_number: int) -> dict:
    if is_template_sheet(sheet_name):
        raise ValueError("Sheet template tidak bisa diedit dari dashboard Coreman")
    worksheet = worksheet_by_title(sheet_name)
    row_number = int(row_number)
    if row_number < 2:
        raise ValueError("Header row tidak bisa dihapus")
    width = max(DEFAULT_GRID_WIDTH, worksheet.col_count or 0, len(COREMAN_HEADERS))
    existing = pad_row((worksheet.get(f"A{row_number}:{column_label(width)}{row_number}") or [[]])[0], width)
    all_values = worksheet.get(f"A:{column_label(width)}") or []
    layout = infer_layout(all_values, worksheet.col_count)
    if layout["layout"] == "coreman_grid":
        cleared = existing[:]
        for index in [18, 23, 24, 25]:
            if index < len(cleared):
                cleared[index] = ""
    else:
        cleared = [""] * width
        cleared[0] = existing[0] or "FEEDER"
        cleared[3] = existing[3]
        cleared[4] = existing[4]
    worksheet.update(values=[cleared], range_name=f"A{row_number}:{column_label(width)}{row_number}")
    return {"row_number": row_number, "values": cleared}


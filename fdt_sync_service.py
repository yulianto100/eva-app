from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from kmz_standardizer import (
    extract_fdt_order,
    find_header_columns,
    format_excel_identifier,
    looks_like_fdt_code,
    normalize_text,
    FdtCodingRecord,
)

import fdt_coding_db

logger = logging.getLogger("fdt_sync_service")

SYNC_SOURCES_ENV = "FDT_SYNC_SOURCES"
SERVICE_ACCOUNT_FILE_ENV = "GOOGLE_SERVICE_ACCOUNT_FILE"
DEFAULT_SERVICE_ACCOUNT_PATH = Path(".secrets") / "eva-google-service-account.json"
HASHES_FILE = Path("data") / "fdt_sheet_hashes.json"
REVISIONS_FILE = Path("data") / "fdt_source_revisions.json"
IGNORED_SHEET_NAMES = {"CREATE EMAIL SENDING"}

GSCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass
class SyncSource:
    name: str
    spreadsheet_id: str
    spreadsheet_label: str = ""


DEFAULT_SOURCES: list[SyncSource] = [
    SyncSource(
        name="bekasi",
        spreadsheet_id="1QUW7iwJRSHKZi4NTdpZrxCMUugeiS4uC8qUFTjLIQWc",
        spreadsheet_label="Coding FTTH - Bekasi Area Update 2021",
    ),
    SyncSource(
        name="jakarta",
        spreadsheet_id="1vajv8JOseMkCVJVHuUG6ZO6ktxZne7K6J6CQAYPnwQY",
        spreadsheet_label="Coding FTTH - Jakarta Area Update 2021",
    ),
]


def resolve_sources_from_env() -> list[SyncSource]:
    raw_sources = os.getenv(SYNC_SOURCES_ENV, "").strip()
    if not raw_sources:
        return DEFAULT_SOURCES

    try:
        parsed = json.loads(raw_sources)
    except json.JSONDecodeError as exc:
        logger.warning("FDT_SYNC_SOURCES invalid JSON, memakai default sources: %s", exc)
        return DEFAULT_SOURCES

    sources: list[SyncSource] = []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        logger.warning("FDT_SYNC_SOURCES harus berupa object/list, memakai default sources.")
        return DEFAULT_SOURCES

    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            continue
        spreadsheet_id = normalize_text(item.get("spreadsheet_id") or item.get("id"))
        if not spreadsheet_id:
            continue
        sources.append(
            SyncSource(
                name=normalize_text(item.get("name")) or f"source-{index}",
                spreadsheet_id=spreadsheet_id,
                spreadsheet_label=normalize_text(item.get("spreadsheet_label") or item.get("label")),
            )
        )

    return sources or DEFAULT_SOURCES

def _resolve_service_account_path() -> Path:
    env_path = os.getenv(SERVICE_ACCOUNT_FILE_ENV, "").strip()
    if env_path:
        return Path(env_path)
    from config import Config
    candidate = Config.BASE_DIR / DEFAULT_SERVICE_ACCOUNT_PATH
    if candidate.exists():
        return candidate
    return DEFAULT_SERVICE_ACCOUNT_PATH


def _build_gspread_client() -> gspread.Client:
    cred_path = _resolve_service_account_path()
    creds = Credentials.from_service_account_file(str(cred_path), scopes=GSCOPES)
    return gspread.authorize(creds)


def _load_sheet_hashes() -> dict[str, str]:
    try:
        from config import Config
        path = Config.BASE_DIR / HASHES_FILE
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_sheet_hashes(hashes: dict[str, str]) -> None:
    try:
        from config import Config
        path = Config.BASE_DIR / HASHES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Gagal menyimpan sheet hashes: %s", exc)


def _load_source_revisions() -> dict[str, str]:
    """Load the last-seen Drive modifiedTime per spreadsheet id."""
    try:
        from config import Config
        path = Config.BASE_DIR / REVISIONS_FILE
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_source_revisions(revisions: dict[str, str]) -> None:
    try:
        from config import Config
        path = Config.BASE_DIR / REVISIONS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(revisions, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Gagal menyimpan source revisions: %s", exc)


def _fetch_drive_modified_time(client: gspread.Client, spreadsheet_id: str) -> str | None:
    """Cheap change-detection: read just the Drive `modifiedTime` for the
    spreadsheet (1 lightweight metadata request) instead of pulling every
    cell value. Returns an ISO timestamp string, or None if it could not be
    determined (caller should then fall back to a full fetch).

    Uses gspread's built-in `get_file_drive_metadata`, which exists
    precisely to read a file's create/update time via the Drive API. The
    service account already holds the Drive scope, so no extra dependency
    or credential is needed."""
    try:
        data = client.get_file_drive_metadata(spreadsheet_id)
        modified = normalize_text((data or {}).get("modifiedTime"))
        return modified or None
    except Exception as exc:
        logger.debug("Drive modifiedTime check gagal untuk %s: %s", spreadsheet_id, exc)
        return None


def _hash_key(spreadsheet_id: str, sheet_name: str) -> str:
    return f"{spreadsheet_id}_{sheet_name}"


def _should_skip_sheet(sheet_name: str) -> bool:
    return normalize_text(sheet_name).upper() in IGNORED_SHEET_NAMES

def _calc_hash(data: list[list[str]]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _looks_like_hub_id(value: object) -> bool:
    text = normalize_text(value).upper()
    return bool(re.fullmatch(r"[A-Z]{2,}\.\d{3}\.\d{2,4}(?:\.\d+)?", text))

def extract_area_from_sheet_name(sheet_name: str) -> str:
    text = normalize_text(sheet_name).upper()
    if "(BEKASI)" in text or "BEKASI" in text.split()[-2:]:  # rough heuristic
        return "Bekasi"
    # More precise: look for parenthesized region or explicit label
    # For spreadsheet labels like 'Coding FTTH - Jakarta Area Update 2021':
    for label in text.split():
        label = label.strip("()|")
        if label.upper() == "BEKASI":
            return "Bekasi"
        if label.upper() == "JAKARTA":
            return "Jakarta"
    # From actual sheet names like 'OLT Cikarang Utara 2' — we fall back to checking source label
    return ""


def extract_sheet_area(sheet_name: str, source_label: str = "") -> str:
    text = normalize_text(sheet_name).upper()
    # Explicit region indicators in sheet name or source label
    if "BEKASI" in text:
        return "Bekasi"
    # Check source spreadsheet label too
    if source_label:
        source_upper = normalize_text(source_label).upper()
        if "BEKASI" in source_upper:
            return "Bekasi"
        if "JAKARTA" in source_upper:
            return "Jakarta"
    # Check parenthesized: (BEKASI) / (JAKARTA) or (BEKASI) / (JAKARTA) in combined text
    combined = text + " " + normalize_text(source_label).upper()
    for region in ["BEKASI", "JAKARTA"]:
        # Match region as a standalone token, optionally in parentheses
        if re.search(rf"(?:^|[^A-Z0-9]){region}(?:[^A-Z0-9]|$)", combined):
            return region.capitalize()
    return ""


def _parse_sheet_data(
    spreadsheet_id: str,
    sheet_name: str,
    data: list[list[str]],
) -> list[FdtCodingRecord]:
    if not data:
        return []

    layout = None
    for index, row in enumerate(data, start=1):
        columns = find_header_columns(row)
        if columns:
            layout = (index, columns)
            break
    if not layout:
        return []

    header_row, columns = layout
    records: list[FdtCodingRecord] = []

    sheet_olt = ""
    sheet_hub = ""
    for scan_idx in range(max(0, header_row - 5), min(header_row + 10, len(data))):
        if scan_idx == header_row - 1:
            continue
        row = data[scan_idx] if scan_idx < len(data) else []
        if columns.get("olt_initial", 0) > 0 and not sheet_olt:
            col = columns["olt_initial"]
            val = normalize_text(row[col - 1] if col <= len(row) else "")
            if val and 2 <= len(val) <= 10:
                sheet_olt = val.upper()
        if columns.get("hub_id", 0) > 0:
            col = columns["hub_id"]
            val = normalize_text(row[col - 1] if col <= len(row) else "")
            if _looks_like_hub_id(val) and (not sheet_hub or len(val) > len(sheet_hub)):
                sheet_hub = val

    for row_idx in range(header_row, len(data)):
        row = data[row_idx]
        site_col = columns["site"]
        fdt_code_col = columns["fdt_code"]
        fdt_no_col = columns.get("fdt_no", 0)
        idx_col = columns.get("index", 0)
        cluster_col = columns.get("cluster", 0)
        olt_col = columns.get("olt_initial", 0)
        hub_col = columns.get("hub_id", 0)
        hostname_col = columns.get("olt_hostname", 0)

        site_name = normalize_text(row[site_col - 1] if site_col <= len(row) else "")
        fdt_code = normalize_text(row[fdt_code_col - 1] if fdt_code_col <= len(row) else "")
        cluster_initial = normalize_text(row[cluster_col - 1] if cluster_col <= len(row) and cluster_col > 0 else "") if cluster_col else ""
        if not site_name:
            continue
        if not looks_like_fdt_code(fdt_code):
            fdt_code = cluster_initial
        if not fdt_code:
            continue

        fdt_id = format_excel_identifier(row[fdt_no_col - 1] if fdt_no_col <= len(row) and fdt_no_col > 0 else "") if fdt_no_col else ""
        index_code = normalize_text(row[idx_col - 1] if idx_col <= len(row) and idx_col > 0 else "") if idx_col else ""
        olt_initial = normalize_text(row[olt_col - 1] if olt_col <= len(row) and olt_col > 0 else "") if olt_col else ""
        hub_id = normalize_text(row[hub_col - 1] if hub_col <= len(row) and hub_col > 0 else "") if hub_col else ""
        olt_hostname = normalize_text(row[hostname_col - 1] if hostname_col <= len(row) and hostname_col > 0 else "") if hostname_col else ""
        if hub_id and not _looks_like_hub_id(hub_id):
            hub_id = ""

        if not olt_initial:
            olt_initial = sheet_olt
        if not hub_id:
            hub_id = sheet_hub

        records.append(FdtCodingRecord(
            sheet_name=sheet_name,
            row_number=row_idx,
            site_name=site_name,
            fdt_order=extract_fdt_order(site_name),
            fdt_id=fdt_id,
            fdt_code=fdt_code.upper(),
            index_code=index_code,
            cluster_initial=cluster_initial,
            olt_initial=olt_initial.upper() if olt_initial else "",
            hub_id=hub_id,
            olt_hostname=olt_hostname.upper() if olt_hostname else "",
            area=extract_sheet_area(sheet_name, spreadsheet_id),
        ))

    return records


def _sync_incremental_sheet(
    spreadsheet_id: str,
    sheet_name: str,
    data: list[list[str]],
    db_path: Path,
    hashes: dict[str, str],
) -> int:
    key = _hash_key(spreadsheet_id, sheet_name)
    current_hash = _calc_hash(data)

    if hashes.get(key) == current_hash:
        return 0

    records = _parse_sheet_data(spreadsheet_id, sheet_name, data)
    if not records:
        hashes[key] = current_hash
        return 0

    count = fdt_coding_db.replace_records_by_source(
        db_path, records,
        source_spreadsheet_id=spreadsheet_id,
        olt_initial=sheet_name,
    )
    hashes[key] = current_hash
    return count


def sync_all_incremental(
    db_path: str | Path | None = None,
    sources: Sequence[SyncSource] | None = None,
    force: bool = False,
) -> dict[str, dict[str, int]]:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sources = list(sources or DEFAULT_SOURCES)

    gs_client = _build_gspread_client()
    hashes = _load_sheet_hashes()
    revisions = _load_source_revisions()
    results: dict[str, dict[str, int]] = {}
    changed_total = 0

    for source in sources:
        changed = 0
        skipped = 0
        label = source.spreadsheet_label or source.spreadsheet_id

        # --- Cheap change-detection gate -------------------------------
        # Read just the Drive modifiedTime (one tiny request) before
        # pulling every cell value. If the spreadsheet has not changed
        # since the last successful sync AND the DB already holds this
        # source's data, skip the expensive values_batch_get entirely.
        # This is what keeps us under the Sheets "read requests per
        # minute" quota: an unchanged spreadsheet costs 1 metadata call
        # per poll instead of a full multi-tab values fetch.
        modified_time = _fetch_drive_modified_time(gs_client, source.spreadsheet_id)
        if (
            not force
            and modified_time is not None
            and revisions.get(source.spreadsheet_id) == modified_time
            and fdt_coding_db.count_records_by_source(db_path, source.spreadsheet_id) > 0
        ):
            results[source.name] = {"changed": 0, "skipped": 0, "unchanged": 1}
            logger.debug("Inbound sync: '%s' tidak berubah (modifiedTime sama), skip fetch.", label)
            continue

        spreadsheet = gs_client.open_by_key(source.spreadsheet_id)
        worksheets = spreadsheet.worksheets()

        ranges = [f"'{ws.title}'" for ws in worksheets if len(ws.title) <= 120 and not _should_skip_sheet(ws.title)]
        value_ranges = spreadsheet.values_batch_get(ranges).get("valueRanges", [])

        sheet_data: dict[str, list[list[str]]] = {}
        for vr in value_ranges:
            raw_range = vr.get("range", "")
            values = vr.get("values", [])
            title = raw_range.split("'")[1] if "'" in raw_range else raw_range.split("!")[0]
            title = title.strip()
            sheet_data[title] = values

        for ws in worksheets:
            title = ws.title
            if _should_skip_sheet(title):
                skipped_olt = normalize_text(title).upper()
                # removed = fdt_coding_db.delete_records_by_source(db_path, source.spreadsheet_id, skipped_olt)  # DISABLED: prevent data loss
                try:
                    fdt_coding_db.delete_olt(db_path, skipped_olt, force=True)
                except Exception:
                    logger.debug("Inbound sync: OLT skip '%s' tidak perlu dihapus dari daftar OLT.", title)
                if removed:
                    logger.info("Inbound sync: sheet '%s' di-skip dan %d record lama dihapus.", title, removed)
                skipped += 1
                continue
            data = sheet_data.get(title, [])
            c = _sync_incremental_sheet(source.spreadsheet_id, title, data, db_path, hashes)
            if c:
                changed += c
                logger.debug("  Sheet '%s' changed: %d records", title, c)
            else:
                skipped += 1

        # Record the modifiedTime we just successfully processed so the
        # next poll can skip if nothing changed.
        if modified_time is not None:
            revisions[source.spreadsheet_id] = modified_time

        results[source.name] = {"changed": changed, "skipped": skipped}
        changed_total += changed

    _save_sheet_hashes(hashes)
    _save_source_revisions(revisions)

    if changed_total > 0:
        logger.info("Incremental sync: %d records changed across %d source(s)", changed_total, len(sources))
    return results




# ---------------------------------------------------------------------------
# OLT sheet creation from template
# ---------------------------------------------------------------------------

FDT_TEMPLATE_SHEET_ENV = "FDT_TEMPLATE_SHEET_NAME"
FDT_TEMPLATE_DEFAULT_SHEET = "Template"

def _source_for_olt_code(olt_code: str) -> SyncSource:
    prefix = normalize_text(olt_code).upper().split(".", 1)[0]
    if prefix == "JKT":
        for source in DEFAULT_SOURCES:
            if source.name.lower() == "jakarta":
                return source
    return DEFAULT_SOURCES[0]

def _worksheet_or_none(spreadsheet, title: str):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return None

def _copy_template_worksheet(spreadsheet, new_sheet_name: str):
    template_name = os.getenv(FDT_TEMPLATE_SHEET_ENV, FDT_TEMPLATE_DEFAULT_SHEET).strip() or FDT_TEMPLATE_DEFAULT_SHEET
    template = _worksheet_or_none(spreadsheet, template_name)
    if template is None:
        worksheets = spreadsheet.worksheets()
        if not worksheets:
            return spreadsheet.add_worksheet(title=new_sheet_name, rows=1000, cols=38)
        template = worksheets[0]
    return spreadsheet.duplicate_sheet(template.id, new_sheet_name=new_sheet_name)

def _hub_parts(olt_code: str) -> tuple[str, str, str]:
    parts = normalize_text(olt_code).upper().split(".")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    return "", "", ""

def delete_olt_sheet(olt_initial: str, olt_code: str = "") -> dict:
    sheet_name = normalize_text(olt_initial).upper()
    if not sheet_name:
        return {"deleted": False, "reason": "missing_sheet_name"}
    sources = []
    if olt_code:
        sources.append(_source_for_olt_code(olt_code))
    sources.extend(source for source in DEFAULT_SOURCES if source not in sources)
    client = _build_gspread_client()
    for source in sources:
        try:
            spreadsheet = client.open_by_key(source.spreadsheet_id)
            worksheet = _worksheet_or_none(spreadsheet, sheet_name)
            if worksheet is None:
                continue
            if normalize_text(worksheet.title).upper() in {"TEMPLATE", "CREATE EMAIL SENDING"}:
                return {"deleted": False, "reason": "protected_sheet", "sheet_name": worksheet.title}
            spreadsheet.del_worksheet(worksheet)
            return {"deleted": True, "spreadsheet": source.name, "sheet_name": worksheet.title}
        except Exception as exc:
            logger.warning("Gagal hapus sheet OLT '%s' dari %s: %s", sheet_name, source.name, exc)
    return {"deleted": False, "reason": "sheet_not_found", "sheet_name": sheet_name}

def create_olt_sheet_from_template(olt: dict) -> dict:
    olt_name = normalize_text(olt.get("olt_initial")).upper()
    olt_code = normalize_text(olt.get("hub_id")).upper()
    olt_initial = normalize_text(olt.get("olt_name_initial")).upper()
    olt_hostname = normalize_text(olt.get("olt_hostname")).upper()
    if not olt_name or not olt_code:
        return {"created": False, "reason": "missing_olt_name_or_id"}

    source = _source_for_olt_code(olt_code)
    client = _build_gspread_client()
    spreadsheet = client.open_by_key(source.spreadsheet_id)
    existing = _worksheet_or_none(spreadsheet, olt_name)
    if existing is not None:
        worksheet = existing
        created = False
    else:
        worksheet = _copy_template_worksheet(spreadsheet, olt_name)
        created = True

    city, hub_no, subhub_no = _hub_parts(olt_code)
    hub_base = f"{city}.{hub_no}" if city and hub_no else ""
    backbone_text = f"{olt_code}-Line A - Cable 96C/8T s/d {olt_code}-Line B - Cable 96C/8T + (increment)"
    updates = [
        {"range": "D5", "values": [[olt_code]]},
        {"range": "F5", "values": [[olt_initial]]},
        {"range": "I5", "values": [[olt_hostname]]},
        {"range": "C14", "values": [[hub_no]]},
        {"range": "D14", "values": [[hub_base]]},
        {"range": "B15", "values": [[olt_name]]},
        {"range": "C15", "values": [[subhub_no]]},
        {"range": "D15", "values": [[olt_code]]},
        {"range": "E15", "values": [[olt_initial]]},
        {"range": "K15", "values": [[backbone_text]]},
    ]
    worksheet.batch_update(updates, value_input_option="RAW")
    worksheet.format("G:G", {"numberFormat": {"type": "TEXT"}})
    return {"created": created, "spreadsheet": source.name, "sheet_name": worksheet.title}


# ---------------------------------------------------------------------------
# Background scheduler for periodic inbound sync
# ---------------------------------------------------------------------------

_INBOUND_SYNC_INTERVAL_ENV = "FDT_INBOUND_SYNC_INTERVAL_SECONDS"
_INBOUND_SYNC_LOCK_FILE_ENV = "FDT_INBOUND_SYNC_LOCK_FILE"
_INBOUND_SYNC_DEFAULT_INTERVAL = 300
_inbound_sync_started = False
_inbound_sync_lock_file_handle = None
_inbound_sync_lock = threading.Lock()
_outbound_create_lock = threading.Lock()


def _is_google_quota_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return (
        "429" in text
        or "quota" in text
        or "rate limit" in text
        or "resourceexhausted" in text
        or "read requests per minute" in text
    )


def _acquire_inbound_sync_process_lock() -> bool:
    global _inbound_sync_lock_file_handle
    if os.name == "nt":
        return True
    try:
        import fcntl
    except ImportError:
        return True
    lock_file = Path(os.getenv(_INBOUND_SYNC_LOCK_FILE_ENV, "/tmp/eva_fdt_inbound_sync.lock"))
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_file.open("w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        _inbound_sync_lock_file_handle = handle
        return True
    except BlockingIOError:
        logger.info("Inbound sync scheduler skipped; process lock already held.")
        return False
    except OSError as exc:
        logger.warning("Inbound sync process lock unavailable, scheduler tetap jalan: %s", exc)
        return True


def _inbound_sync_loop(interval_seconds: int, sources: list[SyncSource]) -> None:
    quota_cooldown_seconds = 0
    while True:
        try:
            if _outbound_create_lock.locked():
                logger.debug("Inbound sync skipped (outbound create masih jalan).")
            elif _inbound_sync_lock.acquire(blocking=False):
                try:
                    sync_all_incremental(sources=sources)
                    quota_cooldown_seconds = 0
                except Exception as exc:
                    if _is_google_quota_error(exc):
                        quota_cooldown_seconds = min(max(quota_cooldown_seconds * 2, 300), 1800)
                        logger.warning(
                            "Scheduled inbound sync kena quota Google Sheets; cooldown %ds: %s",
                            quota_cooldown_seconds,
                            exc,
                        )
                    else:
                        logger.warning("Scheduled inbound sync gagal: %s", exc)
                finally:
                    _inbound_sync_lock.release()
            else:
                logger.debug("Inbound sync skipped (previous run masih jalan).")
        except Exception:
            logger.exception("Scheduled inbound sync error.")
        time.sleep(max(interval_seconds, quota_cooldown_seconds, 5))


def start_inbound_sync_scheduler(
    interval_seconds: int | None = None,
    sources: list[SyncSource] | None = None,
) -> None:
    global _inbound_sync_started
    if _inbound_sync_started:
        return
    interval = interval_seconds if interval_seconds is not None else int(
        os.getenv(_INBOUND_SYNC_INTERVAL_ENV, str(_INBOUND_SYNC_DEFAULT_INTERVAL))
    )
    sources = sources or resolve_sources_from_env()
    if not _acquire_inbound_sync_process_lock():
        _inbound_sync_started = True
        return
    _inbound_sync_started = True
    thread = threading.Thread(
        target=_inbound_sync_loop,
        args=(interval, sources),
        name="fdt-inbound-sync",
        daemon=True,
    )
    thread.start()
    logger.info("Inbound sync scheduler started every %ds.", interval)


# ---------------------------------------------------------------------------
# Outbound sync: push a single DB record change back to its source sheet row
# ---------------------------------------------------------------------------

_DB_TO_SHEET_COLUMN_MAP = {
    "site_name": "site",
    "fdt_id": "fdt_no",
    "fdt_code": "fdt_code",
    "index_code": "index",
    "cluster_initial": "cluster",
    "olt_name_initial": "olt_initial",
    "hub_id": "hub_id",
    "olt_hostname": "olt_hostname",
}


def _gs_write_cell(worksheet, row: int, col: int, value: object) -> None:
    cell_val = "" if value is None else str(value)
    cell = rowcol_to_a1(row, col)
    worksheet.update(cell, [[cell_val]], value_input_option="RAW")


def _outbound_update_row(
    worksheet: gspread.Worksheet,
    header_row: int,
    columns: dict[str, int],
    row_number: int,
    record: dict,
) -> None:
    updates = []
    for db_field, sheet_key in _DB_TO_SHEET_COLUMN_MAP.items():
        col = columns.get(sheet_key, 0)
        if col < 1:
            continue
        value = record.get(db_field, "")
        if db_field == "fdt_id":
            value = format_excel_identifier(value)
        updates.append({"range": rowcol_to_a1(row_number, col), "values": [["" if value is None else str(value)]]})
    if updates:
        worksheet.batch_update(updates, value_input_option="RAW")
    fdt_col = columns.get("fdt_no", 0)
    if fdt_col > 0:
        worksheet.format(rowcol_to_a1(row_number, fdt_col), {"numberFormat": {"type": "TEXT"}})


def _find_header_layout_for_sheet(worksheet: gspread.Worksheet, max_header_scan: int = 25) -> tuple[int, dict[str, int]] | None:
    rows = worksheet.get_values(
        value_render_option="FORMATTED_VALUE",
        major_dimension="ROWS",
    )
    for row_index, row in enumerate(rows[:max_header_scan], start=1):
        columns = find_header_columns(row)
        if columns:
            return row_index, columns
    return None


def _default_source_for_record(db_path: Path, record: dict) -> tuple[str, str]:
    sheet_name = normalize_text(record.get("sheet_name") or record.get("olt_initial")).upper()
    spreadsheet_id = normalize_text(record.get("source_spreadsheet_id"))
    if spreadsheet_id and sheet_name:
        return spreadsheet_id, sheet_name

    olt_initial = normalize_text(record.get("olt_initial")).upper()
    if olt_initial:
        with fdt_coding_db.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT source_spreadsheet_id, sheet_name
                FROM fdt_coding_records
                WHERE olt_initial = ?
                  AND source_spreadsheet_id <> ''
                  AND sheet_name <> ''
                ORDER BY source_row DESC, id DESC
                LIMIT 1
                """,
                (olt_initial,),
            ).fetchone()
            if row:
                return row["source_spreadsheet_id"], row["sheet_name"]

    hub_id = normalize_text(record.get("hub_id"))
    if hub_id:
        return _source_for_olt_code(hub_id).spreadsheet_id, sheet_name or olt_initial

    return DEFAULT_SOURCES[0].spreadsheet_id, sheet_name or olt_initial


def _find_case_insensitive_sheet(spreadsheet, sheet_name: str) -> str | None:
    """Find sheet by case-insensitive comparison."""
    normalized = normalize_text(sheet_name).upper()
    for ws in spreadsheet.worksheets():
        if normalize_text(ws.title).upper() == normalized:
            return ws.title
    return None

def _ensure_worksheet(spreadsheet, sheet_name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        actual_title = _find_case_insensitive_sheet(spreadsheet, sheet_name)
        if actual_title:
            return spreadsheet.worksheet(actual_title)
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=12)
        worksheet.update(
            "A1:G1",
            [[
                "NO FDT ID",
                "AREA NAMA KOMPLEK",
                "CODING FDT",
                "INDEX FDT",
                "INISIAL NAME CLUSTER",
                "INISIAL NAME OLT",
                "HUB ID",
            ]],
        )
        return worksheet

def _next_append_row(worksheet: gspread.Worksheet, header_row: int, columns: dict[str, int]) -> int:
    values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")
    watched_cols = [col for col in columns.values() if col and col > 0]
    last_row = header_row
    for row_index, row in enumerate(values, start=1):
        if row_index <= header_row:
            continue
        if any(col <= len(row) and normalize_text(row[col - 1]) for col in watched_cols):
            last_row = row_index
    return last_row + 1

def sync_outbound_create_record(record_id: int, db_path: str | Path | None = None) -> bool:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    try:
        record = fdt_coding_db.get_record(db_path, record_id)
        if record is None:
            logger.warning("Outbound sync create: record %d tidak ditemukan.", record_id)
            return False

        if normalize_text(record.get("source_spreadsheet_id")) and normalize_text(record.get("sheet_name")) and int(record.get("source_row", 0) or 0) > 0:
            return sync_outbound_update_record(record_id, db_path=db_path)

        spreadsheet_id, sheet_name = _default_source_for_record(db_path, record)
        if not spreadsheet_id or not sheet_name:
            logger.warning("Outbound sync create: record %d tidak punya target spreadsheet/sheet.", record_id)
            return False

        last_exc = None
        for _attempt in range(3):
            try:
                gs_client = _build_gspread_client()
                spreadsheet = gs_client.open_by_key(spreadsheet_id)
                worksheet = _ensure_worksheet(spreadsheet, sheet_name)
                layout = _find_header_layout_for_sheet(worksheet)
                if not layout:
                    logger.warning("Outbound sync create: header tidak ditemukan di sheet '%s'.", sheet_name)
                    return False

                header_row, columns = layout
                data_row = _next_append_row(worksheet, header_row, columns)
                record = fdt_coding_db.set_record_source_metadata(db_path, record_id, spreadsheet_id, sheet_name, data_row) or record
                _outbound_update_row(worksheet, header_row, columns, data_row, record)
                logger.info("Outbound sync create: record %d -> sheet '%s' row %d.", record_id, sheet_name, data_row)
                return True
            except Exception as exc:
                last_exc = exc
                if _is_google_quota_error(exc) and _attempt < 2:
                    wait = 15 * (_attempt + 1)
                    logger.warning("Outbound sync create: record %d quota, retry %d/3 tunggu %ds", record_id, _attempt + 1, wait)
                    time.sleep(wait)
                else:
                    break
        # All retries exhausted — re-raise for outer except
        raise last_exc
    except Exception as exc:
        logger.error("Outbound sync create: record %d gagal: %s", record_id, exc)
        return False

def sync_outbound_create_records(record_ids: Sequence[int], db_path: str | Path | None = None) -> dict[int, bool]:
    results: dict[int, bool] = {}
    with _outbound_create_lock:
        for _idx, record_id in enumerate(record_ids):
            try:
                normalized_id = int(record_id)
            except (TypeError, ValueError):
                continue
            if _idx > 0:
                time.sleep(5)
            results[normalized_id] = sync_outbound_create_record(normalized_id, db_path=db_path)
    return results


def sync_outbound_delete_records(record_snapshots: dict, db_path: str | Path | None = None) -> dict:
    """Batch delete with delay between records to avoid 429 quota."""
    results: dict = {}
    for _idx, (record_id, snapshot) in enumerate(record_snapshots.items()):
        if _idx > 0:
            time.sleep(5)
        results[record_id] = sync_outbound_delete_record(record_id, db_path=db_path, record_snapshot=snapshot)
    return results

def sync_outbound_update_record(record_id: int, db_path: str | Path | None = None) -> bool:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    try:
        record = fdt_coding_db.get_record(db_path, record_id)
        if record is None:
            logger.warning("Outbound sync: record %d tidak ditemukan.", record_id)
            return False

        spreadsheet_id = record.get("source_spreadsheet_id", "").strip()
        sheet_name = record.get("sheet_name", "").strip()
        source_row = int(record.get("source_row", 0))
        if not spreadsheet_id or not sheet_name or source_row < 1:
            logger.info("Outbound sync: record %d tidak punya source metadata, skip.", record_id)
            return False

        gs_client = _build_gspread_client()
        spreadsheet = gs_client.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)

        layout = _find_header_layout_for_sheet(worksheet)
        if not layout:
            logger.warning("Outbound sync: header tidak ditemukan di sheet '%s'.", sheet_name)
            return False

        header_row, columns = layout
        data_row = source_row
        if data_row <= header_row:
            logger.warning("Outbound sync: source_row %d <= header_row %d, skip.", data_row, header_row)
            return False

        _outbound_update_row(worksheet, header_row, columns, data_row, record)
        logger.info("Outbound sync: record %d -> sheet '%s' row %d.", record_id, sheet_name, data_row)
        return True

    except gspread.exceptions.WorksheetNotFound:
        logger.warning("Outbound sync: worksheet '%s' tidak ditemukan.", record.get("sheet_name", ""))
        return False
    except Exception as exc:
        logger.error("Outbound sync: record %d gagal: %s", record_id, exc)
        return False


def _record_matches_sheet_row(row: list[str], columns: dict[str, int], record: dict) -> bool:
    """Return True only for the exact row represented by a DB record.

    Delete sync is destructive. Older logic treated matching fdt_code alone as
    enough and also cleared preferred_row without verifying its content. That
    could clear a neighboring/header row when source_row was stale, or clear
    several rows when similar data existed. Require a stronger composite match.
    """
    site_col = columns.get("site", 0)
    code_col = columns.get("fdt_code", 0)
    fdt_col = columns.get("fdt_no", 0)
    row_site = normalize_text(row[site_col - 1] if site_col and site_col <= len(row) else "").upper()
    row_code = normalize_text(row[code_col - 1] if code_col and code_col <= len(row) else "").upper()
    row_fdt = format_excel_identifier(row[fdt_col - 1] if fdt_col and fdt_col <= len(row) else "")
    rec_site = normalize_text(record.get("site_name")).upper()
    rec_code = normalize_text(record.get("fdt_code")).upper()
    rec_fdt = format_excel_identifier(record.get("fdt_id"))

    if rec_code and row_code != rec_code:
        return False
    if rec_fdt and row_fdt != rec_fdt:
        return False
    if rec_site and row_site != rec_site:
        return False
    return bool(rec_code and (rec_fdt or rec_site))

def _clear_record_rows(worksheet: gspread.Worksheet, columns: dict[str, int], record: dict, preferred_row: int = 0) -> list[int]:
    values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")

    # Prefer source_row only when the row content still matches the record.
    if preferred_row > 0 and preferred_row <= len(values):
        preferred_values = values[preferred_row - 1]
        if _record_matches_sheet_row(preferred_values, columns, record):
            worksheet.delete_rows(preferred_row)
            return [preferred_row]
        logger.warning(
            "Outbound sync delete: source_row %d content mismatch for record %s, will search exact single match instead.",
            preferred_row,
            record.get("id", ""),
        )

    matches = [
        row_idx
        for row_idx, row in enumerate(values, start=1)
        if _record_matches_sheet_row(row, columns, record)
    ]
    if len(matches) == 1:
        worksheet.delete_rows(matches[0])
        return matches
    if len(matches) > 1:
        logger.error(
            "Outbound sync delete: ambiguous matches for record %s in sheet '%s': rows %s. No rows cleared.",
            record.get("id", ""),
            record.get("sheet_name", ""),
            matches,
        )
    return []

def sync_outbound_delete_record(record_id: int, db_path: str | Path | None = None, record_snapshot: dict | None = None) -> bool:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    try:
        record = record_snapshot or fdt_coding_db.get_record(db_path, record_id)
        if record is None:
            return False

        spreadsheet_id = record.get("source_spreadsheet_id", "").strip()
        sheet_name = record.get("sheet_name", "").strip()
        source_row = int(record.get("source_row", 0))
        if not spreadsheet_id or not sheet_name or source_row < 1:
            return False

        last_exc = None
        for _attempt in range(3):
            try:
                gs_client = _build_gspread_client()
                spreadsheet = gs_client.open_by_key(spreadsheet_id)
                ws_title = _find_case_insensitive_sheet(spreadsheet, sheet_name) or sheet_name
                worksheet = spreadsheet.worksheet(ws_title)

                layout = _find_header_layout_for_sheet(worksheet)
                if not layout:
                    return False

                header_row, columns = layout
                data_row = source_row
                preferred_row = data_row if data_row > header_row else 0
                cleared_rows = _clear_record_rows(worksheet, columns, record, preferred_row=preferred_row)
                if not cleared_rows:
                    return False

                logger.info("Outbound sync: record %d deleted -> sheet '%s' rows %s deleted.", record_id, ws_title, cleared_rows)
                return True
            except Exception as exc:
                last_exc = exc
                if _is_google_quota_error(exc) and _attempt < 2:
                    wait = 15 * (_attempt + 1)
                    logger.warning("Outbound delete: record %d quota, retry %d/3 tunggu %ds", record_id, _attempt + 1, wait)
                    time.sleep(wait)
                else:
                    break
        # All retries exhausted — re-raise for outer except
        raise last_exc

    except Exception as exc:
        logger.error("Outbound sync: delete record %d gagal: %s", record_id, exc)
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sources = resolve_sources_from_env()
    logger.info("Starting FDT coding sync from %d source(s):", len(sources))
    for source in sources:
        label = source.spreadsheet_label or source.spreadsheet_id
        logger.info("  - %s (%s)", source.name, label)

    # Manual CLI runs always force a full fetch, bypassing the
    # modifiedTime gate, so an operator can rebuild the DB on demand.
    start = time.time()
    results = sync_all_incremental(sources=sources, force="--force" in sys.argv or True)
    elapsed = time.time() - start

    changed_total = sum(v.get("changed", 0) for v in results.values())
    logger.info("Sync complete in %.1fs: %d records changed.", elapsed, changed_total)
    for name, counts in results.items():
        logger.info("  %s: %d changed, %d skipped", name, counts.get("changed", 0), counts.get("skipped", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())


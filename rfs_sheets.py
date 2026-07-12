from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import gspread

import coreman_sheets

logger = logging.getLogger(__name__)

DEFAULT_RFS_SPREADSHEET_ID = "1DRCnUofaVEkqjKniljy4FPu-sG1BeHGteS-sCISOkyQ"
DATA_SHEET_NAME = os.getenv("RFS_DATA_SHEET_NAME", "RFS_DATA").strip() or "RFS_DATA"
TARGET_SHEET_NAME = os.getenv("RFS_TARGET_SHEET_NAME", "RFS_TARGET").strip() or "RFS_TARGET"
SUMMARY_SHEET_NAME = os.getenv("RFS_SUMMARY_SHEET_NAME", "RFS_SUMMARY").strip() or "RFS_SUMMARY"
YTD_SHEET_NAME = os.getenv("RFS_YTD_SHEET_NAME", "RFS_YTD").strip() or "RFS_YTD"
DEFAULT_REGIONS = [
    item.strip()
    for item in os.getenv("RFS_DEFAULT_REGIONS", "Bekasi,Sukabumi").split(",")
    if item.strip()
]
DEFAULT_BASE_INCENTIVE_IDR = int(os.getenv("RFS_BASE_INCENTIVE_IDR", "2000000"))
DEFAULT_DIFFICULTY_BY_REGION = {
    "BEKASI": 1.25,
    "JAKARTA BARAT": 1.25,
    "JAKARTA PUSAT": 1.25,
    "JAKARTA SELATAN": 1.25,
    "JAKARTA TIMUR": 1.25,
    "JAKARTA UTARA": 1.25,
    "BANDUNG": 1.10,
    "CIREBON": 1.00,
    "SUKABUMI": 1.00,
    "SUMEDANG": 1.00,
}


def _retry_on_quota(func):
    """Wrapper to retry on Google Sheets 429 quota errors with exponential backoff."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                exc_str = str(exc).lower()
                is_quota = "429" in exc_str or "quota" in exc_str or "rate limit" in exc_str or "resourceexhausted" in type(exc).__name__.lower()
                if not is_quota or attempt == 2:
                    raise
                delay = 30.0 * (2 ** attempt)
                logger.warning("Quota hit on %s (attempt %d/3), retrying in %.0fs", func.__name__, attempt + 1, delay)
                time.sleep(delay)
        raise RuntimeError("Retry failed")
    return wrapper

DATA_HEADERS = [
    "Month",
    "Region",
    "Site",
    "HP",
    "Recorded At",
    "Source Chat",
    "Sender",
    "Message ID",
    "Raw Text",
    "Fingerprint",
]
TARGET_HEADERS = ["Month", "Region", "Target HP", "Notes", "Updated At"]
SUMMARY_HEADERS = [
    "Month",
    "Region",
    "Actual HP",
    "Target HP",
    "Progress",
    "Gap HP",
    "Records",
    "National Progress",
    "Base Incentive",
    "Difficulty",
    "YTD Progress",
    "Payout Factor",
    "Incentive IDR",
    "Incentive Status",
]
YTD_HEADERS = ["Year", "Through Month", "Region", "Actual HP YTD", "Target HP YTD", "Progress YTD", "Gap HP YTD", "Records", "Monthly Progress"]
_READY = False
_INFO_CACHE: dict | None = None


def spreadsheet_id() -> str:
    return os.getenv("RFS_SPREADSHEET_ID", DEFAULT_RFS_SPREADSHEET_ID).strip() or DEFAULT_RFS_SPREADSHEET_ID


@_retry_on_quota
def _open_real():
    return coreman_sheets.gspread_client().open_by_key(spreadsheet_id())

def open_spreadsheet():
    try:
        return _open_real()
    except FileNotFoundError as exc:
        raise FileNotFoundError(str(exc))

def _open_or_payload():
    """Return (spreadsheet, payload). One is None."""
    try:
        return _open_real(), None
    except FileNotFoundError as exc:
        return None, _google_unavailable(exc)


def _google_unavailable(exc: Exception | None = None) -> dict:
    return {
        "google_sheets_available": False,
        "warning": str(exc) if exc else "Google Sheets belum terhubung: kredensial service account tidak ditemukan.",
        "spreadsheet_id": spreadsheet_id(),
        "spreadsheet_url": spreadsheet_url(),
    }


def spreadsheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id()}"


def _timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("TIMEZONE", "Asia/Jakarta"))


def month_key(dt: datetime | None = None) -> str:
    current = dt or datetime.now(_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=_timezone())
    return current.astimezone(_timezone()).strftime("%Y-%m")


def _now_iso() -> str:
    return datetime.now(_timezone()).isoformat(timespec="seconds")


def _clean_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def region_label(value: object) -> str:
    raw = _clean_space(value)
    if not raw:
        return ""
    label = raw.lower().title()
    replacements = {"Dki": "DKI", "Rw": "RW", "Rt": "RT"}
    for source, replacement in replacements.items():
        label = re.sub(rf"\b{source}\b", replacement, label)
    return label


def region_key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def site_label(value: object) -> str:
    return _clean_space(value).upper()


def _int(value: object, default: int = 0) -> int:
    raw = re.sub(r"[^0-9-]+", "", str(value or ""))
    try:
        return int(raw)
    except ValueError:
        return default


def _month_parts(value: object) -> tuple[int, int] | None:
    match = re.match(r"^(\d{4})-(\d{2})$", _clean_space(value))
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        return None
    return year, month


def _progress(actual_hp: int, target_hp: int) -> float | None:
    return (actual_hp / target_hp) if target_hp > 0 else None


def _progress_label(progress: float | None) -> str:
    return "-" if progress is None else f"{progress * 100:.1f}%"


def _difficulty_overrides() -> dict[str, float]:
    raw = os.getenv("RFS_DIFFICULTY_BY_REGION", "").strip()
    if not raw:
        return {}
    overrides = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        region, value = item.split(":", 1)
        try:
            overrides[region_key(region)] = float(value.strip())
        except ValueError:
            continue
    return overrides


def difficulty_for_region(region: object) -> float:
    key = region_key(region)
    overrides = _difficulty_overrides()
    if key in overrides:
        return overrides[key]
    return DEFAULT_DIFFICULTY_BY_REGION.get(key, 1.0)


def _incentive_result(
    *,
    region: str,
    area_progress: float | None,
    national_progress: float | None,
    ytd_progress: float | None,
    target_hp: int,
) -> dict:
    base_incentive = DEFAULT_BASE_INCENTIVE_IDR
    difficulty = difficulty_for_region(region)
    payout_factor = 0.0
    status = "NO TARGET"

    if target_hp <= 0 or area_progress is None:
        status = "NO TARGET"
    elif national_progress is None:
        status = "NO NATIONAL TARGET"
    else:
        if area_progress >= 0.70:
            payout_factor = 1.0
            status = "ELIGIBLE"
        else:
            status = "ACH AREA <70%"

    if payout_factor > 0 and ytd_progress is None:
        status = "NO YTD TARGET"
        incentive_idr = 0
    else:
        incentive_idr = round(base_incentive * difficulty * (area_progress or 0) * (ytd_progress or 0) * payout_factor)

    return {
        "base_incentive": base_incentive,
        "difficulty": difficulty,
        "ytd_progress": ytd_progress,
        "national_progress": national_progress,
        "payout_factor": payout_factor,
        "incentive_idr": incentive_idr,
        "status": status,
    }


def _worksheet(spreadsheet, title: str, rows: int, cols: int):
    existing = coreman_sheets.worksheet_or_none(spreadsheet, title)
    if existing is not None:
        return existing, False
    return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols), True


def _header_matches(values: list[list[str]], headers: list[str]) -> bool:
    if not values:
        return False
    return [_clean_space(cell) for cell in values[0][: len(headers)]] == headers


def _ensure_header(worksheet, headers: list[str]) -> None:
    values = worksheet.get(f"A1:{coreman_sheets.column_label(len(headers))}1") or []
    if not _header_matches(values, headers):
        worksheet.update(values=[headers], range_name=f"A1:{coreman_sheets.column_label(len(headers))}1")
    try:
        worksheet.freeze(rows=1)
        worksheet.format(
            f"A1:{coreman_sheets.column_label(len(headers))}1",
            {
                "backgroundColor": {"red": 0.11, "green": 0.18, "blue": 0.31},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
            },
        )
    except Exception:
        pass


@_retry_on_quota
def _all_values(worksheet) -> list[list[str]]:
    return worksheet.get_all_values() or []


@_retry_on_quota
def ensure_rfs_sheets(rebuild: bool = True) -> dict:
    global _READY, _INFO_CACHE
    if _READY and not rebuild and _INFO_CACHE:
        return _INFO_CACHE

    spreadsheet, payload = _open_or_payload()
    if spreadsheet is None:
        payload.update({"ready": False, "created": [], "sheets": [], "data_sheet": DATA_SHEET_NAME, "target_sheet": TARGET_SHEET_NAME, "summary_sheet": SUMMARY_SHEET_NAME, "ytd_sheet": YTD_SHEET_NAME})
        return payload
    data_sheet, data_created = _worksheet(spreadsheet, DATA_SHEET_NAME, 1000, len(DATA_HEADERS))
    target_sheet, target_created = _worksheet(spreadsheet, TARGET_SHEET_NAME, 200, len(TARGET_HEADERS))
    summary_sheet, summary_created = _worksheet(spreadsheet, SUMMARY_SHEET_NAME, 200, len(SUMMARY_HEADERS))
    ytd_sheet, ytd_created = _worksheet(spreadsheet, YTD_SHEET_NAME, 300, len(YTD_HEADERS))

    _ensure_header(data_sheet, DATA_HEADERS)
    _ensure_header(target_sheet, TARGET_HEADERS)
    _ensure_header(summary_sheet, SUMMARY_HEADERS)
    _ensure_header(ytd_sheet, YTD_HEADERS)
    _seed_current_month_targets(target_sheet)
    if rebuild:
        rebuild_summary_sheet(spreadsheet)

    info = {
        "spreadsheet_id": spreadsheet_id(),
        "url": spreadsheet_url(),
        "sheets": {
            "data": {"title": data_sheet.title, "gid": data_sheet.id, "created": data_created},
            "target": {"title": target_sheet.title, "gid": target_sheet.id, "created": target_created},
            "summary": {"title": summary_sheet.title, "gid": summary_sheet.id, "created": summary_created},
            "ytd": {"title": ytd_sheet.title, "gid": ytd_sheet.id, "created": ytd_created},
        },
    }
    _READY = True
    _INFO_CACHE = info
    return info


def _seed_current_month_targets(target_sheet) -> None:
    current_month = month_key()
    existing = {
        (row[0], region_key(row[1] if len(row) > 1 else ""))
        for row in _all_values(target_sheet)[1:]
        if row and _clean_space(row[0] if row else "")
    }
    rows = []
    for region in DEFAULT_REGIONS:
        key = (current_month, region_key(region))
        if key not in existing:
            rows.append([current_month, region_label(region), 0, "", _now_iso()])
    if rows:
        target_sheet.append_rows(rows, value_input_option="USER_ENTERED")


def fingerprint_for(month: str, region: str, site: str, hp: int) -> str:
    raw = "|".join([month, region_key(region), site_label(site), str(int(hp or 0))])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _data_records(values: list[list[str]]) -> list[dict]:
    records = []
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * max(0, len(DATA_HEADERS) - len(row))
        month, region, site, hp, recorded_at, source_chat, sender, message_id, raw_text, fingerprint = padded[: len(DATA_HEADERS)]
        if not month or not region or not site:
            continue
        records.append(
            {
                "row_number": row_number,
                "month": _clean_space(month),
                "region": region_label(region),
                "site": site_label(site),
                "hp": _int(hp),
                "recorded_at": _clean_space(recorded_at),
                "source_chat": _clean_space(source_chat),
                "sender": _clean_space(sender),
                "message_id": _clean_space(message_id),
                "raw_text": _clean_space(raw_text),
                "fingerprint": _clean_space(fingerprint),
            }
        )
    return records


def _target_records(values: list[list[str]]) -> list[dict]:
    targets = []
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * max(0, len(TARGET_HEADERS) - len(row))
        month, region, target_hp, notes, updated_at = padded[: len(TARGET_HEADERS)]
        if not month or not region:
            continue
        targets.append(
            {
                "row_number": row_number,
                "month": _clean_space(month),
                "region": region_label(region),
                "target_hp": _int(target_hp),
                "notes": _clean_space(notes),
                "updated_at": _clean_space(updated_at),
            }
        )
    return targets


@_retry_on_quota
def append_record(payload: dict) -> dict:
    try:
        info = ensure_rfs_sheets(rebuild=False)
        spreadsheet = open_spreadsheet()
    except FileNotFoundError as exc:
        result = _google_unavailable(exc)
        result.update({"ok": False, "duplicate": False, "record": payload})
        return result
    worksheet = spreadsheet.worksheet(DATA_SHEET_NAME)
    target_month = _clean_space(payload.get("month")) or month_key()
    region = region_label(payload.get("region"))
    site = site_label(payload.get("site"))
    hp = _int(payload.get("hp"))
    if not region:
        raise ValueError("Region RFS wajib diisi")
    if not site:
        raise ValueError("Site RFS wajib diisi")
    if hp <= 0:
        raise ValueError("HP RFS harus lebih dari 0")

    fingerprint = fingerprint_for(target_month, region, site, hp)
    existing = None
    for record in _data_records(_all_values(worksheet)):
        if record.get("month") == target_month and record.get("fingerprint") == fingerprint:
            existing = record
            break

    duplicate = existing is not None
    if not duplicate:
        recorded_at = _clean_space(payload.get("recorded_at")) or _now_iso()
        worksheet.append_row(
            [
                target_month,
                region,
                site,
                hp,
                recorded_at,
                _clean_space(payload.get("source_chat")),
                _clean_space(payload.get("sender")),
                _clean_space(payload.get("message_id")),
                _clean_space(payload.get("raw_text")),
                fingerprint,
            ],
            value_input_option="USER_ENTERED",
        )
        rebuild_summary_sheet(spreadsheet)

    summary = summarize(target_month)
    return {
        "duplicate": duplicate,
        "record": existing or {"month": target_month, "region": region, "site": site, "hp": hp, "fingerprint": fingerprint},
        "summary": summary,
        "message_text": format_recorded_response(summary, duplicate=duplicate),
        "spreadsheet": info,
    }


@_retry_on_quota
def set_target(payload: dict) -> dict:
    try:
        ensure_rfs_sheets(rebuild=False)
        spreadsheet = open_spreadsheet()
    except FileNotFoundError as exc:
        result = _google_unavailable(exc)
        result.update({"ok": False, "target": payload, "duplicate": False})
        return result
    if spreadsheet is None:
        result = _google_unavailable()
        result.update({"ok": False, "target": payload, "duplicate": False})
        return result
    worksheet = spreadsheet.worksheet(TARGET_SHEET_NAME)
    target_month = _clean_space(payload.get("month")) or month_key()
    region = region_label(payload.get("region"))
    target_hp = _int(payload.get("target_hp"))
    notes = _clean_space(payload.get("notes"))
    if not region:
        raise ValueError("Region target wajib diisi")
    if target_hp < 0:
        raise ValueError("Target HP tidak boleh negatif")

    values = _all_values(worksheet)
    wanted = (target_month, region_key(region))
    target_row = None
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * max(0, len(TARGET_HEADERS) - len(row))
        key = (_clean_space(padded[0]), region_key(padded[1]))
        if key == wanted:
            target_row = row_number
            break

    row = [target_month, region, target_hp, notes, _now_iso()]
    if target_row:
        worksheet.update(values=[row], range_name=f"A{target_row}:E{target_row}")
    else:
        worksheet.append_row(row, value_input_option="USER_ENTERED")

    rebuild_summary_sheet(spreadsheet)
    return {"target": {"month": target_month, "region": region, "target_hp": target_hp, "notes": notes}, "summary": summarize(target_month)}


def months() -> list[str]:
    try:
        ensure_rfs_sheets(rebuild=False)
        spreadsheet = open_spreadsheet()
    except FileNotFoundError:
        return [month_key()]
    if spreadsheet is None:
        return [month_key()]
    found = {month_key()}
    for worksheet_name, parser in [(DATA_SHEET_NAME, _data_records), (TARGET_SHEET_NAME, _target_records)]:
        worksheet = spreadsheet.worksheet(worksheet_name)
        for record in parser(_all_values(worksheet)):
            if record.get("month"):
                found.add(record["month"])
    return sorted(found, reverse=True)


def default_dashboard_month() -> str:
    try:
        ensure_rfs_sheets(rebuild=False)
        spreadsheet = open_spreadsheet()
        data_months = {
            record["month"]
            for record in _data_records(_all_values(spreadsheet.worksheet(DATA_SHEET_NAME)))
            if record.get("month")
        }
        if data_months:
            return sorted(data_months, reverse=True)[0]
        available_months = months()
        return available_months[0] if available_months else month_key()
    except FileNotFoundError:
        return month_key()


@_retry_on_quota
def summarize(target_month: str | None = None) -> dict:
    if target_month is None:
        target_month = month_key()
    try:
        ensure_rfs_sheets(rebuild=False)
        spreadsheet = open_spreadsheet()
        if spreadsheet is None:
            payload = _build_summary(target_month, [], [])
            payload.update(_google_unavailable())
            return payload
        records = _data_records(_all_values(spreadsheet.worksheet(DATA_SHEET_NAME)))
        targets = _target_records(_all_values(spreadsheet.worksheet(TARGET_SHEET_NAME)))
        return _build_summary(target_month, records, targets)
    except FileNotFoundError as exc:
        payload = _build_summary(target_month, [], [])
        payload.update(_google_unavailable(exc))
        return payload


def _build_summary(target_month: str, all_records: list[dict], all_targets: list[dict]) -> dict:
    records = [record for record in all_records if record["month"] == target_month]
    targets = [target for target in all_targets if target["month"] == target_month]

    grouped: dict[str, dict] = {}
    for target in targets:
        key = region_key(target["region"])
        grouped.setdefault(key, {"region": target["region"], "records": [], "actual_hp": 0, "target_hp": 0, "target_row_number": target["row_number"]})
        grouped[key]["target_hp"] = target["target_hp"]
        grouped[key]["target_row_number"] = target["row_number"]
        grouped[key]["notes"] = target.get("notes", "")

    for record in records:
        key = region_key(record["region"])
        grouped.setdefault(key, {"region": record["region"], "records": [], "actual_hp": 0, "target_hp": 0, "target_row_number": None})
        grouped[key]["records"].append(record)
        grouped[key]["actual_hp"] += record["hp"]

    regions = []
    for item in grouped.values():
        target_hp = int(item.get("target_hp") or 0)
        actual_hp = int(item.get("actual_hp") or 0)
        gap_hp = actual_hp - target_hp
        progress = _progress(actual_hp, target_hp)
        regions.append(
            {
                "region": item["region"],
                "actual_hp": actual_hp,
                "target_hp": target_hp,
                "gap_hp": gap_hp,
                "progress": progress,
                "record_count": len(item.get("records", [])),
                "target_row_number": item.get("target_row_number"),
                "records": sorted(item.get("records", []), key=lambda record: record.get("recorded_at", ""), reverse=True),
                "notes": item.get("notes", ""),
            }
        )
    regions.sort(key=lambda item: item["region"])
    total_actual = sum(item["actual_hp"] for item in regions)
    total_target = sum(item["target_hp"] for item in regions)
    total_progress = _progress(total_actual, total_target)
    recent_records = sorted(records, key=lambda record: record.get("recorded_at", ""), reverse=True)[:50]
    ytd = _build_ytd(target_month, all_records, all_targets)
    ytd_by_region = {region_key(item["region"]): item for item in ytd.get("regions", [])}
    for item in regions:
        ytd_region = ytd_by_region.get(region_key(item["region"]), {})
        item["incentive"] = _incentive_result(
            region=item["region"],
            area_progress=item.get("progress"),
            national_progress=total_progress,
            ytd_progress=ytd_region.get("progress"),
            target_hp=int(item.get("target_hp") or 0),
        )
    incentive_total = sum(int(item.get("incentive", {}).get("incentive_idr") or 0) for item in regions)
    return {
        "month": target_month,
        "regions": regions,
        "records": recent_records,
        "ytd": ytd,
        "totals": {
            "actual_hp": total_actual,
            "target_hp": total_target,
            "gap_hp": total_actual - total_target,
            "progress": total_progress,
            "record_count": len(records),
            "incentive_idr": incentive_total,
        },
        "spreadsheet_url": spreadsheet_url(),
        "sheet_names": {"data": DATA_SHEET_NAME, "target": TARGET_SHEET_NAME, "summary": SUMMARY_SHEET_NAME, "ytd": YTD_SHEET_NAME},
    }


def _build_ytd(target_month: str, all_records: list[dict], all_targets: list[dict]) -> dict:
    parts = _month_parts(target_month) or _month_parts(month_key())
    if parts is None:
        return {"year": "", "through_month": target_month, "regions": [], "total": {"actual_hp": 0, "target_hp": 0, "gap_hp": 0, "progress": None, "record_count": 0, "monthly": []}}

    year, through_month = parts
    through_key = f"{year}-{through_month:02d}"
    records = [
        record for record in all_records
        if (record_parts := _month_parts(record.get("month"))) and record_parts[0] == year and record_parts[1] <= through_month
    ]
    targets = [
        target for target in all_targets
        if (target_parts := _month_parts(target.get("month"))) and target_parts[0] == year and target_parts[1] <= through_month
    ]

    month_keys = sorted({
        item["month"]
        for item in [*records, *targets]
        if _month_parts(item.get("month"))
    })

    grouped: dict[str, dict] = {}
    total_by_month: dict[str, dict] = {key: {"month": key, "actual_hp": 0, "target_hp": 0, "record_count": 0} for key in month_keys}

    for target in targets:
        key = region_key(target["region"])
        month = target["month"]
        grouped.setdefault(key, {"region": target["region"], "monthly": {}, "actual_hp": 0, "target_hp": 0, "record_count": 0})
        grouped[key]["monthly"].setdefault(month, {"month": month, "actual_hp": 0, "target_hp": 0, "record_count": 0})
        grouped[key]["monthly"][month]["target_hp"] += int(target.get("target_hp") or 0)

    for record in records:
        key = region_key(record["region"])
        month = record["month"]
        grouped.setdefault(key, {"region": record["region"], "monthly": {}, "actual_hp": 0, "target_hp": 0, "record_count": 0})
        grouped[key]["monthly"].setdefault(month, {"month": month, "actual_hp": 0, "target_hp": 0, "record_count": 0})
        grouped[key]["monthly"][month]["actual_hp"] += int(record.get("hp") or 0)
        grouped[key]["monthly"][month]["record_count"] += 1

    regions = []
    for item in grouped.values():
        monthly = []
        for month in month_keys:
            row = item["monthly"].get(month, {"month": month, "actual_hp": 0, "target_hp": 0, "record_count": 0})
            actual_hp = int(row.get("actual_hp") or 0)
            target_hp = int(row.get("target_hp") or 0)
            record_count = int(row.get("record_count") or 0)
            monthly_row = {
                "month": month,
                "actual_hp": actual_hp,
                "target_hp": target_hp,
                "gap_hp": actual_hp - target_hp,
                "progress": _progress(actual_hp, target_hp),
                "record_count": record_count,
            }
            monthly.append(monthly_row)
            item["actual_hp"] += actual_hp
            item["target_hp"] += target_hp
            item["record_count"] += record_count
            total_by_month.setdefault(month, {"month": month, "actual_hp": 0, "target_hp": 0, "record_count": 0})
            total_by_month[month]["actual_hp"] += actual_hp
            total_by_month[month]["target_hp"] += target_hp
            total_by_month[month]["record_count"] += record_count

        actual_hp = int(item.get("actual_hp") or 0)
        target_hp = int(item.get("target_hp") or 0)
        regions.append({
            "region": item["region"],
            "actual_hp": actual_hp,
            "target_hp": target_hp,
            "gap_hp": actual_hp - target_hp,
            "progress": _progress(actual_hp, target_hp),
            "record_count": int(item.get("record_count") or 0),
            "monthly": monthly,
        })

    regions.sort(key=lambda item: item["region"])
    total_monthly = []
    for month in month_keys:
        row = total_by_month.get(month, {"month": month, "actual_hp": 0, "target_hp": 0, "record_count": 0})
        actual_hp = int(row.get("actual_hp") or 0)
        target_hp = int(row.get("target_hp") or 0)
        total_monthly.append({
            "month": month,
            "actual_hp": actual_hp,
            "target_hp": target_hp,
            "gap_hp": actual_hp - target_hp,
            "progress": _progress(actual_hp, target_hp),
            "record_count": int(row.get("record_count") or 0),
        })

    total_actual = sum(item["actual_hp"] for item in regions)
    total_target = sum(item["target_hp"] for item in regions)
    return {
        "year": str(year),
        "through_month": through_key,
        "regions": regions,
        "total": {
            "region": "TOTAL ALL AREAS",
            "actual_hp": total_actual,
            "target_hp": total_target,
            "gap_hp": total_actual - total_target,
            "progress": _progress(total_actual, total_target),
            "record_count": sum(item["record_count"] for item in regions),
            "monthly": total_monthly,
        },
    }


def _summaries_for_all_months(all_records: list[dict], all_targets: list[dict]) -> list[list]:
    values_by_month = set()
    for record in all_records:
        values_by_month.add(record["month"])
    for target in all_targets:
        values_by_month.add(target["month"])

    rows = []
    for key in sorted(values_by_month, reverse=True):
        summary = _build_summary(key, all_records, all_targets)
        for region in summary["regions"]:
            progress = "" if region["progress"] is None else round(region["progress"], 4)
            incentive = region.get("incentive") or {}
            rows.append([
                key,
                region["region"],
                region["actual_hp"],
                region["target_hp"],
                progress,
                region["gap_hp"],
                region["record_count"],
                "" if incentive.get("national_progress") is None else round(incentive["national_progress"], 4),
                incentive.get("base_incentive", DEFAULT_BASE_INCENTIVE_IDR),
                incentive.get("difficulty", difficulty_for_region(region["region"])),
                "" if incentive.get("ytd_progress") is None else round(incentive["ytd_progress"], 4),
                incentive.get("payout_factor", 0),
                incentive.get("incentive_idr", 0),
                incentive.get("status", ""),
            ])
    return rows


def _monthly_progress_text(monthly: list[dict]) -> str:
    parts = []
    for row in monthly:
        if int(row.get("actual_hp") or 0) == 0 and int(row.get("target_hp") or 0) == 0:
            continue
        parts.append(f"{row['month']}: {_progress_label(row.get('progress'))}")
    return "; ".join(parts)


def _ytd_rows_for_all_months(all_records: list[dict], all_targets: list[dict]) -> list[list]:
    values_by_month = set()
    for record in all_records:
        if _month_parts(record.get("month")):
            values_by_month.add(record["month"])
    for target in all_targets:
        if _month_parts(target.get("month")):
            values_by_month.add(target["month"])

    rows = []
    for key in sorted(values_by_month, reverse=True):
        ytd = _build_ytd(key, all_records, all_targets)
        total = ytd["total"]
        all_rows = [total, *ytd["regions"]]
        for item in all_rows:
            rows.append([
                ytd["year"],
                ytd["through_month"],
                item["region"],
                item["actual_hp"],
                item["target_hp"],
                "" if item["progress"] is None else round(item["progress"], 4),
                item["gap_hp"],
                item["record_count"],
                _monthly_progress_text(item.get("monthly") or []),
            ])
    return rows


@_retry_on_quota
@_retry_on_quota
def rebuild_summary_sheet(spreadsheet=None) -> None:
    spreadsheet = spreadsheet or open_spreadsheet()
    summary_sheet = spreadsheet.worksheet(SUMMARY_SHEET_NAME)
    ytd_sheet = spreadsheet.worksheet(YTD_SHEET_NAME)
    all_records = _data_records(_all_values(spreadsheet.worksheet(DATA_SHEET_NAME)))
    all_targets = _target_records(_all_values(spreadsheet.worksheet(TARGET_SHEET_NAME)))
    rows = [SUMMARY_HEADERS] + _summaries_for_all_months(all_records, all_targets)
    summary_sheet.clear()
    summary_sheet.update(values=rows, range_name=f"A1:{coreman_sheets.column_label(len(SUMMARY_HEADERS))}{max(len(rows), 1)}")
    ytd_rows = [YTD_HEADERS] + _ytd_rows_for_all_months(all_records, all_targets)
    ytd_sheet.clear()
    ytd_sheet.update(values=ytd_rows, range_name=f"A1:{coreman_sheets.column_label(len(YTD_HEADERS))}{max(len(ytd_rows), 1)}")
    try:
        summary_sheet.freeze(rows=1)
        ytd_sheet.freeze(rows=1)
        summary_sheet.format(
            f"A1:{coreman_sheets.column_label(len(SUMMARY_HEADERS))}1",
            {
                "backgroundColor": {"red": 0.11, "green": 0.18, "blue": 0.31},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
            },
        )
        ytd_sheet.format(
            "A1:I1",
            {
                "backgroundColor": {"red": 0.11, "green": 0.18, "blue": 0.31},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
            },
        )
        if len(rows) > 1:
            summary_sheet.format("C2:D", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
            summary_sheet.format("F2:G", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
            summary_sheet.format("I2:I", {"numberFormat": {"type": "CURRENCY", "pattern": '"Rp"#,##0'}})
            summary_sheet.format("J2:J", {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}})
            summary_sheet.format("M2:M", {"numberFormat": {"type": "CURRENCY", "pattern": '"Rp"#,##0'}})
            summary_sheet.format("E2:E", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
            summary_sheet.format("H2:H", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
            summary_sheet.format("K2:L", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
        if len(ytd_rows) > 1:
            ytd_sheet.format("D2:G", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
            ytd_sheet.format("F2:F", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
    except Exception:
        pass


def format_recorded_response(summary: dict, duplicate: bool = False) -> str:
    first_line = "RFS sudah pernah tercatat bulan ini." if duplicate else "RFS tercatat."
    return f"{first_line}\n\n{format_month_summary(summary)}"


def format_month_summary(summary: dict) -> str:
    parts = []
    for region in summary.get("regions", []):
        records = region.get("records") or []
        rows = [f"- {record['site']} : {record['hp']} HP" for record in records]
        if not rows:
            rows = ["- Belum ada data"]
        parts.append("\n".join([f"{region['region']} :", *rows, f"Total HP RFS : {region['actual_hp']}"]))
    return "\n\n".join(parts) if parts else f"Belum ada data RFS bulan {summary.get('month') or month_key()}."


def search_rfs(query: str, max_results: int = 10) -> str:
    q = query.lower().strip()
    if not q or len(q) < 2:
        return ""

    try:
        spreadsheet = open_spreadsheet()
        ws = spreadsheet.worksheet(DATA_SHEET_NAME)
        all_vals = ws.get_all_values() or []
        records = _data_records(all_vals)[:max_results]
    except Exception:
        return ""

    matched = []
    for r in records:
        if q in r["month"].lower() or q in r["region"].lower() or q in r["site"].lower():
            matched.append(f"{r['month']} | {r['region']} | {r['site']} | {r['hp']} HP")

    if not matched:
        return ""

    return "RFS:\n" + "\n".join(matched[:max_results])


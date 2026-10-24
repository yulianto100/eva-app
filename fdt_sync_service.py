from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from functools import wraps
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
    normalize_header,
    normalize_text,
    FdtCodingRecord,
)

import fdt_coding_db

logger = logging.getLogger("fdt_sync_service")

_sync_operation_lock = threading.RLock()
_sync_operation_state = threading.local()


def synchronized_sync_operation(function):
    """Serialize inbound reconciliation and Google Sheet mutations."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with fdt_sync_operation_lock():
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def fdt_sync_operation_lock():
    """Serialize DB reconciliation and Google Sheet mutations across processes."""
    with _sync_operation_lock:
        depth = int(getattr(_sync_operation_state, "depth", 0))
        handle = None
        if depth == 0 and os.name != "nt":
            import fcntl

            default_lock_path = (
                "/app/data/fdt_sync_operation.lock"
                if Path("/app/data").exists()
                else str(Path("data") / "fdt_sync_operation.lock")
            )
            lock_path = Path(os.getenv("FDT_SYNC_OPERATION_LOCK_FILE", default_lock_path))
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _sync_operation_state.depth = depth + 1
        try:
            yield
        finally:
            _sync_operation_state.depth = depth
            if handle is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

SYNC_SOURCES_ENV = "FDT_SYNC_SOURCES"
SERVICE_ACCOUNT_FILE_ENV = "GOOGLE_SERVICE_ACCOUNT_FILE"
SERVICE_ACCOUNT_MAP_FILE_ENV = "FDT_SERVICE_ACCOUNT_MAP_FILE"
DEFAULT_SERVICE_ACCOUNT_PATH = Path(".secrets") / "eva-google-service-account.json"
HASHES_FILE = Path("data") / "fdt_sheet_hashes.json"
COUNTS_FILE = Path("data") / "fdt_sheet_counts.json"
REVISIONS_FILE = Path("data") / "fdt_source_revisions.json"
IGNORED_SHEET_NAMES = {"CREATE EMAIL SENDING"}
RETIRED_SOURCE_IDS = {"1eN6WVXxUtMkJQRNlKMHvRNUgfzs4VMdMm5yc6Wku2bs"}

GSCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass
class SyncSource:
    name: str
    spreadsheet_id: str
    spreadsheet_label: str = ""


LEGACY_FDT_SPREADSHEET_ID = "1PNnmDDFgmks0bEkGF4jF8c3Vlv9-hzF3YushyP9xQWs"
SUKABUMI_FDT_SPREADSHEET_ID = "1jBoLTe0psl5G8PoQTLXzRUdS9kpQq7NWah-69Z4CWhQ"
TANGERANG_FDT_SPREADSHEET_ID = "1I5RR2Ow631aRgm9oOBfi5rhOMU-fnMOKGCUXWLSi6U4"
TANGERANG_RETIRED_SPREADSHEET_ID = "1a89l5TmTlZiICSSvvRwCB6lONDOCQgpd"
RETIRED_SOURCE_IDS.add(TANGERANG_RETIRED_SPREADSHEET_ID)

# Exact physical tabs verified in the replacement Tangerang workbook. Parent
# ownership follows the tab; each data row can still select another unit from
# its evaluated full index (for example TNG.100.0206.* inside OLT CIPUTAT).
TANGERANG_FDT_TABS = {
    "OLT CIPUTAT": ("TNG.100.0201", "CPT"),
    "OLT PAMULANG": ("TNG.100.1001", "PML"),
    "OLT JOMBANG": ("TNG.100.0301", "JBG"),
    "OLT SMART BSD": ("TNG.100.0101", "BSD"),
    "OLT JOMBANG RAYA 02": ("TNG.100.0305", "JB2"),
    "OLT CURUG": ("TNG.100.0601", "CRG"),
    "OLT SERPONG UTARA": ("TNG.100.1301", "SPU"),
    "Mini OLT Paragon Village": ("TNG.200.0201", "PRV"),
    "OLT KELAPA DUA": ("TNG.100.0801", "KLD"),
    "OLT PASAR KEMIS": ("TNG.100.1501", "PKS"),
    "OLT RAJEG": ("TNG.100.1601", "RJG"),
    "OLT TIGA RAKSA": ("TNG.100.2701", "TGR"),
    "OLT SAWAH LAMA": ("TNG.100.0901", "SWL"),
    "OLT CILEDUG": ("TNG.100.1801", "CDG"),
    "OLT MODERNLAND": ("TNG.100.0401", "MDL"),
    "OLT GUNUNG SINDUR": ("TNG.100.1101", "GNS"),
    "OLT CISOKA": ("TNG.100.2501", "CSA"),
    "OLT SAWANGAN": ("TNG.100.1201", "SWN"),
    "OLT ATMA ASNAWI": ("TNG.100.2801", "ANW"),
    "OLT BANTAR PANJANG": ("TNG.100.2401", "BPJ"),
    "OLT CISAUK": ("TNG.100.1401", "CSK"),
    "OLT PARUNG PANJANG": ("TNG.100.1701", "PRG"),
    "OLT SEPATAN": ("TNG.100.3101", "SPT"),
    "OLT TELUK NAGA": ("TNG.100.3201", "TNG"),
    "OLT TENJO": ("TNG.100.3601", "TJO"),
    "OLT LIPPO KARAWACI UTARA": ("TNG.100.2901", "LVU"),
    "OLT LIPPO KARAWACI SENTRAL": ("TNG.100.3001", "LVS"),
    "OLT LIPPO KARAWACI BARAT": ("TNG.100.3501", "LVB"),
    "OLT PARK SERPONG": ("TNG.100.4201", "PSP"),
}
TANGERANG_FDT_SHEET_IDS = {
    "Create Email sending": 1168385334,
    "Tangerang": 475205580,
    "SMT": 1651263549,
    "CODING POLE MAIN FEEDER": 1452465669,
    "OLT CIPUTAT": 1716784894,
    "OLT PAMULANG": 1668684649,
    "OLT JOMBANG": 1053879593,
    "OLT SMART BSD": 1514184663,
    "OLT JOMBANG RAYA 02": 470982407,
    "OLT CURUG": 980308778,
    "OLT SERPONG UTARA": 1980655130,
    "Mini OLT Paragon Village": 1010359624,
    "OLT KELAPA DUA": 1634042801,
    "OLT TIGA RAKSA": 1476677780,
    "OLT SAWAH LAMA": 1059376677,
    "OLT CILEDUG": 608577053,
    "OLT MODERNLAND": 1633013760,
    "OLT GUNUNG SINDUR": 94800899,
    "OLT CISOKA": 1686472562,
    "OLT SAWANGAN": 1001071131,
    "OLT ATMA ASNAWI": 1644689343,
    "OLT PASAR KEMIS": 550974726,
    "OLT RAJEG": 716230771,
    "OLT BANTAR PANJANG": 436463786,
    "OLT CISAUK": 453237759,
    "OLT PARUNG PANJANG": 1467818242,
    "OLT SEPATAN": 959202438,
    "OLT TELUK NAGA": 1100976792,
    "OLT TENJO": 1662417368,
    "OLT LIPPO KARAWACI UTARA": 442293245,
    "OLT LIPPO KARAWACI SENTRAL": 423443985,
    "OLT LIPPO KARAWACI BARAT": 970828665,
    "OLT PARK SERPONG": 157175988,
}
TANGERANG_FDT_IGNORED_TABS = frozenset(
    set(TANGERANG_FDT_SHEET_IDS) - set(TANGERANG_FDT_TABS)
)
TANGERANG_FDT_WORKBOOK_TITLE = "Coding FTTH - Tangerang Area update 2021"
TANGERANG_FDT_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]*\.\d{3}$", re.IGNORECASE)
TANGERANG_HUB_PATTERN = re.compile(r"^(TNG\.\d{3}\.\d{4})(?:\.|$)", re.IGNORECASE)

# Spreadsheet-local OLT identity. Parent follows the physical tab. Hostnames are
# verified against the ASTRI Sukabumi OLT list; Gunungendut is intentionally
# fail-closed because it is absent there and its copied metadata is Parakansalak.
SUKABUMI_FDT_TABS = {
    "OLT CIKOLE": ("SKB.100.0101", "CKL", "SMI-CKL-OLT1-ZTE"),
    "OLT KEBONPEDES": ("SKB.100.0201", "KBS", "SMI-KBS-OLT1-ZTE"),
    "OLT CISAAT": ("SKB.100.0301", "CST", "SMI-CSA-OLT1-ZTE"),
    "OLT CIBADAK": ("SMI.100.0401", "CBD", "SMI-CBD-OLT1-ZTE"),
    "OLT CIKEMBAR": ("SMI.100.0501", "CKB", "SMI-CKB-OLT1-ZTE"),
    "OLT CIRENGHAS": ("SMI.100.0601", "CRS", "SMI-CRH-OLT1-ZTE"),
    "OLT CICURUG": ("SMI.100.0701", "CCG", "SMI-CCG-OLT1-ZTE"),
    "OLT PELABUHAN RATU": ("SMI.100.0801", "PRT", "SMI-PRT-OLT1-ZTE"),
    "OLT POLE PARAKANSALAK": ("SMI.100.0901", "PRL", "SMI-PRL-OLP1-ZTE"),
    "OLT GUNUNGENDUT": ("", "GNE", ""),
}
SUKABUMI_FDT_CODE_PATTERN = re.compile(
    r"^[A-Z0-9][A-Z0-9-]*(?:\.[A-Z0-9-]+)*\.\d{3}$",
    re.IGNORECASE,
)
LEGACY_FDT_TABS = {
    "OLT INDRAMAYU": "Indramayu",
    "OLT LOHBENER": "Indramayu",
    "KUNINGAN": "Kuningan",
}
# exact hub routing. CWG.100.0201 deliberately absent: user requested skip.
LEGACY_FDT_HUBS = {
    "IDM.100.0101": ("OLT INDRAMAYU", "IDM-IDM-OLT1-FH", "IDM", "Indramayu", "OLT INDRAMAYU", "IDM"),
    "IDM.100.0102": ("OLT INDRAMAYU 02", "IDM-IDM-OLT2-FH", "IDM", "Indramayu", "OLT INDRAMAYU", "IDM"),
    "IDM.100.0103": ("OLT INDRAMAYU 03", "IDM-IDM-OLT3-FH", "IDM", "Indramayu", "OLT INDRAMAYU", "IDM"),
    "IDM.100.0201": ("OLT LOHBENER", "IDM-LBN-OLT1-FH", "LBN", "Indramayu", "OLT LOHBENER", "LBN"),
    "KNG.100.0101": ("OLT KUNINGAN", "KNG-KNG-OLT1-FH", "KNG", "Kuningan", "KUNINGAN", "KNG"),
    "KNG.100.0201": ("OLT CIAWIGEBANG", "KNG-CWG-OLT1-FH", "CWG", "Kuningan", "KUNINGAN", "KNG"),
}


def is_tangerang_read_only(record: dict | None) -> bool:
    """Return True for every immutable Tangerang ownership signal."""
    payload = record or {}
    source_id = normalize_text(payload.get("source_spreadsheet_id"))
    area = normalize_text(payload.get("area")).upper()
    hub_id = normalize_text(payload.get("hub_id")).upper()
    sheet_name = normalize_text(payload.get("sheet_name")).upper()
    olt_initial = normalize_text(payload.get("olt_initial")).upper()
    protected_titles = {normalize_text(title).upper() for title in TANGERANG_FDT_SHEET_IDS}
    return (
        source_id in {TANGERANG_FDT_SPREADSHEET_ID, TANGERANG_RETIRED_SPREADSHEET_ID}
        or area == "TANGERANG"
        or hub_id.startswith("TNG.")
        or sheet_name in protected_titles
        or olt_initial in protected_titles
    )


def is_inbound_read_only(record: dict | None) -> bool:
    return fdt_coding_db.is_balikpapan_read_only_record(record) or fdt_coding_db.is_tangerang_read_only_record(record)


def _legacy_hub(value: object) -> str:
    match = re.match(r"^([A-Z]{2,}\.\d{3}\.\d{4})(?:\.|$)", normalize_text(value).upper())
    return match.group(1) if match else ""


def _legacy_hub_details(hub_id: object):
    return LEGACY_FDT_HUBS.get(normalize_text(hub_id).upper())


def _legacy_hub_allowed_for_sheet(hub_id: object, sheet_name: str) -> bool:
    details = _legacy_hub_details(hub_id)
    return bool(details and normalize_text(details[4]).upper() == normalize_text(sheet_name).upper())


def _legacy_sheet_allowed(spreadsheet_id: str, sheet_name: str) -> bool:
    if normalize_text(spreadsheet_id) != LEGACY_FDT_SPREADSHEET_ID:
        return True
    return normalize_text(sheet_name).upper() in LEGACY_FDT_TABS


def _legacy_catalog_units() -> list[dict]:
    return [
        {
            "olt_code": hub_id,
            "olt_initial": parent,
            "olt_hostname": hostname,
            "olt_name_initial": initial,
            "olt_area": area,
        }
        for hub_id, (parent, hostname, initial, area, _tab, _formula_initial) in LEGACY_FDT_HUBS.items()
    ]


DEFAULT_SOURCES: list[SyncSource] = [
    SyncSource(name="balikpapan", spreadsheet_id=fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID,
               spreadsheet_label=fdt_coding_db.BALIKPAPAN_FDT_WORKBOOK_TITLE),
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
    SyncSource(
        name="indramayu-kuningan",
        spreadsheet_id=LEGACY_FDT_SPREADSHEET_ID,
        spreadsheet_label="Coding FTTH - Indramayu 2026",
    ),
    SyncSource(
        name="sukabumi",
        spreadsheet_id=SUKABUMI_FDT_SPREADSHEET_ID,
        spreadsheet_label="Coding FTTH - Sukabumi Area Update 2026",
    ),
    SyncSource(
        name="tangerang",
        spreadsheet_id=TANGERANG_FDT_SPREADSHEET_ID,
        spreadsheet_label="Coding FTTH - Tangerang Area update 2021",
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
        if not spreadsheet_id or spreadsheet_id in RETIRED_SOURCE_IDS:
            if spreadsheet_id in RETIRED_SOURCE_IDS:
                logger.warning("FDT source retired, diabaikan: %s", spreadsheet_id)
            continue
        sources.append(
            SyncSource(
                name=normalize_text(item.get("name")) or f"source-{index}",
                spreadsheet_id=spreadsheet_id,
                spreadsheet_label=normalize_text(item.get("spreadsheet_label") or item.get("label")),
            )
        )

    return sources or DEFAULT_SOURCES

def _service_account_map_path() -> Path:
    env_path = os.getenv(SERVICE_ACCOUNT_MAP_FILE_ENV, "").strip()
    if env_path:
        return Path(env_path)
    from config import Config
    candidate = Config.BASE_DIR / Path(".secrets") / "fdt-service-account-map.json"
    if candidate.exists():
        return candidate
    return Path(".secrets") / "fdt-service-account-map.json"


def _load_service_account_map() -> dict[str, Path]:
    path = _service_account_map_path()
    if not path.exists():
        return {}
    try:
        def unique_entries(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate credential mapping key")
                if normalize_text(key) == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID and key != fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID:
                    raise ValueError("Balikpapan credential mapping alias diblok")
                result[key] = value
            return result
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_entries)
        if isinstance(payload, dict) and isinstance(payload.get("spreadsheets"), dict):
            payload = payload["spreadsheets"]
        if not isinstance(payload, dict):
            logger.warning("FDT service-account map bukan object: %s", path)
            return {}
        result: dict[str, Path] = {}
        for spreadsheet_id, credential_path in payload.items():
            if not isinstance(credential_path, str) or not credential_path.strip():
                continue
            resolved = Path(credential_path.strip())
            if not resolved.is_absolute():
                resolved = path.parent / resolved
            result[normalize_text(spreadsheet_id)] = resolved
        return result
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("FDT service-account map gagal dibaca: %s", exc)
        return {}


def _resolve_service_account_path(spreadsheet_id: str | None = None) -> Path:
    if spreadsheet_id:
        source_id = normalize_text(spreadsheet_id)
        if source_id == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID and spreadsheet_id != source_id:
            raise ValueError("Balikpapan source ID alias diblok")
        if source_id in RETIRED_SOURCE_IDS:
            raise FileNotFoundError(f"FDT source retired; credential access diblok: {source_id}")
        mapped = _load_service_account_map().get(source_id)
        if mapped is not None:
            return mapped
        if source_id == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID:
            raise FileNotFoundError("Credential mapping khusus spreadsheet Balikpapan wajib dikonfigurasi")
        if source_id == TANGERANG_FDT_SPREADSHEET_ID:
            raise FileNotFoundError(
                "Credential mapping khusus spreadsheet Tangerang wajib dikonfigurasi"
            )
    env_path = os.getenv(SERVICE_ACCOUNT_FILE_ENV, "").strip()
    if env_path:
        return Path(env_path)
    from config import Config
    candidate = Config.BASE_DIR / DEFAULT_SERVICE_ACCOUNT_PATH
    if candidate.exists():
        return candidate
    return DEFAULT_SERVICE_ACCOUNT_PATH


def _build_gspread_client(spreadsheet_id: str | None = None) -> gspread.Client:
    cred_path = _resolve_service_account_path(spreadsheet_id)
    if not cred_path.exists():
        scope = f" for spreadsheet {spreadsheet_id}" if spreadsheet_id else ""
        raise FileNotFoundError(f"Google service-account file tidak ditemukan{scope}: {cred_path}")
    protected = normalize_text(spreadsheet_id) == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly",
              "https://www.googleapis.com/auth/drive.metadata.readonly"] if protected else GSCOPES
    creds = Credentials.from_service_account_file(str(cred_path), scopes=scopes)
    if protected and creds.service_account_email != "fdt-balikpapan@coding-balikpapan.iam.gserviceaccount.com":
        raise ValueError("Dedicated Balikpapan service-account identity mismatch")
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


def _load_sheet_counts() -> dict[str, int]:
    try:
        from config import Config
        path = Config.BASE_DIR / COUNTS_FILE
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {str(key): int(value) for key, value in raw.items()}
    except Exception:
        pass
    return {}


def _save_sheet_counts(counts: dict[str, int]) -> None:
    try:
        from config import Config
        path = Config.BASE_DIR / COUNTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(counts, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Gagal menyimpan sheet counts: %s", exc)


def _source_db_matches_sheet_counts(db_path: Path, spreadsheet_id: str, counts: dict[str, int]) -> bool:
    prefix = f"{spreadsheet_id}_"
    expected = {key: int(value) for key, value in counts.items() if key.startswith(prefix)}
    if not expected:
        return False
    with fdt_coding_db.connect(db_path) as conn:
        source_total, pending = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(source_row < 0), 0) FROM fdt_coding_records WHERE source_spreadsheet_id = ?",
            (spreadsheet_id,),
        ).fetchone()
        if pending or source_total != sum(expected.values()):
            return False
        for key, expected_count in expected.items():
            sheet_name = key[len(prefix):]
            actual = int(conn.execute(
                "SELECT COUNT(*) FROM fdt_coding_records WHERE source_spreadsheet_id = ? AND sheet_name = ?",
                (spreadsheet_id, sheet_name),
            ).fetchone()[0])
            if actual != expected_count:
                return False
    return True


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


def _should_sync_sheet(spreadsheet_id: str, sheet_name: str) -> bool:
    source_id = normalize_text(spreadsheet_id)
    if source_id in RETIRED_SOURCE_IDS:
        return False
    if source_id == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID:
        return str(sheet_name or "") in fdt_coding_db.BALIKPAPAN_FDT_TABS
    if source_id == TANGERANG_FDT_SPREADSHEET_ID:
        return str(sheet_name or "") in TANGERANG_FDT_TABS
    if _should_skip_sheet(sheet_name):
        return False
    normalized_sheet = normalize_text(sheet_name).upper()
    if source_id == SUKABUMI_FDT_SPREADSHEET_ID:
        return normalized_sheet in SUKABUMI_FDT_TABS
    return _legacy_sheet_allowed(spreadsheet_id, sheet_name)

def _validate_balikpapan_workbook_identity(spreadsheet, worksheets) -> None:
    actual = {}
    for ws in worksheets:
        if ws.title in actual:
            raise RuntimeError("Duplicate Balikpapan physical worksheet")
        actual[ws.title] = int(ws.id)
    if (str(spreadsheet.id) != fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID
            or spreadsheet.title != fdt_coding_db.BALIKPAPAN_FDT_WORKBOOK_TITLE
            or actual != fdt_coding_db.BALIKPAPAN_FDT_SHEET_IDS):
        raise RuntimeError("Balikpapan workbook/title/sheetId identity drift")


def _publish_balikpapan_state(hashes, counts, revisions) -> None:
    # Publish revision last: failed count/hash publication must force a retry.
    from config import Config
    import tempfile
    for filename, value in ((HASHES_FILE, hashes), (COUNTS_FILE, counts), (REVISIONS_FILE, revisions)):
        path = Config.BASE_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as out:
                temporary = Path(out.name)
                json.dump(value, out, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _validate_tangerang_workbook_identity(spreadsheet, worksheets: Sequence[gspread.Worksheet]) -> None:
    title = str(getattr(spreadsheet, "title", "") or "")
    if title != TANGERANG_FDT_WORKBOOK_TITLE:
        raise RuntimeError(
            f"Workbook Tangerang tidak sesuai: expected {TANGERANG_FDT_WORKBOOK_TITLE!r}, got {title!r}"
        )
    actual: dict[str, int] = {}
    for worksheet in worksheets:
        worksheet_title = str(getattr(worksheet, "title", "") or "")
        if worksheet_title in actual:
            raise RuntimeError(f"Duplicate worksheet title Tangerang: {worksheet_title!r}")
        actual[worksheet_title] = int(getattr(worksheet, "id"))
    if actual != TANGERANG_FDT_SHEET_IDS:
        missing = sorted(set(TANGERANG_FDT_SHEET_IDS) - set(actual))
        extra = sorted(set(actual) - set(TANGERANG_FDT_SHEET_IDS))
        changed = sorted(
            title for title in set(actual) & set(TANGERANG_FDT_SHEET_IDS)
            if actual[title] != TANGERANG_FDT_SHEET_IDS[title]
        )
        raise RuntimeError(
            f"Worksheet identity Tangerang drift: missing={missing}, extra={extra}, sheetId_changed={changed}"
        )


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
        if "SUKABUMI" in source_upper:
            return "Sukabumi"
    # Check parenthesized: (BEKASI) / (JAKARTA) or (BEKASI) / (JAKARTA) in combined text
    combined = text + " " + normalize_text(source_label).upper()
    for region in ["BEKASI", "JAKARTA", "SUKABUMI"]:
        # Match region as a standalone token, optionally in parentheses
        if re.search(rf"(?:^|[^A-Z0-9]){region}(?:[^A-Z0-9]|$)", combined):
            return region.capitalize()
    return ""


def _legacy_header_columns(row_values: list[str]) -> dict[str, int] | None:
    headers = {index: normalize_header(value) for index, value in enumerate(row_values, start=1)}
    site = next((index for index, header in headers.items() if header == "SITE"), 0)
    fdt_code = next((index for index, header in headers.items() if header == "FDT ID"), 0)
    olt_code = next((index for index, header in headers.items() if header == "OLT CODE"), 0)
    if not (site and fdt_code and olt_code):
        return None
    return {
        "site": site,
        "fdt_code": fdt_code,
        "olt_code": olt_code,
        "fdt_no": next((index for index, header in headers.items() if header == "NO"), 0),
        "segment": next((index for index, header in headers.items() if header == "SEGMENT"), 0),
        "legacy_template": 1,
        "legacy_last_column": len(row_values),
    }


def _legacy_fdt_code(value: object) -> str:
    code = normalize_text(value).upper()
    return code if re.fullmatch(r"[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*\.\d{3}", code) else ""


def _parse_legacy_sheet_data(sheet_name: str, data: list[list[str]]) -> list[FdtCodingRecord]:
    layout = next(((index, columns) for index, row in enumerate(data, start=1) if (columns := _legacy_header_columns(row))), None)
    if not layout:
        return []
    header_row, columns = layout
    records: list[FdtCodingRecord] = []
    for row_index, row in enumerate(data[header_row:], start=header_row + 1):
        def cell(key: str) -> str:
            column = columns.get(key, 0)
            return normalize_text(row[column - 1] if column and column <= len(row) else "")

        site_name = cell("site")
        fdt_code = _legacy_fdt_code(cell("fdt_code"))
        hub_id = _legacy_hub(cell("olt_code"))
        details = _legacy_hub_details(hub_id)
        # Parent follows the base OLT CODE, not tab title. A source tab can contain
        # a row for another valid hub; CWG remains skipped because it has no mapping.
        if not site_name or not fdt_code or not details:
            continue
        parent, hostname, initial, area, _target_tab, _formula_initial = details
        fdt_id = format_excel_identifier(cell("fdt_no")) or fdt_code.rsplit(".", 1)[-1]
        prefix = fdt_code.rsplit(".", 1)[0]
        records.append(FdtCodingRecord(
            sheet_name=sheet_name,
            row_number=row_index,
            site_name=site_name,
            fdt_order=int(fdt_id) if fdt_id.isdigit() else None,
            fdt_id=fdt_id,
            fdt_code=fdt_code,
            # Legacy has no index column. Preserve segment in this unused core field
            # so outbound edits can change only B/C/E and leave formulas intact.
            index_code=cell("segment"),
            cluster_initial=prefix,
            area=area,
            olt_initial=parent,
            hub_id=hub_id,
            olt_hostname=hostname,
            olt_name_initial=initial,
        ))
    return records


def _parse_sukabumi_sheet_data(sheet_name: str, data: list[list[str]]) -> list[FdtCodingRecord]:
    tab_key = normalize_text(sheet_name).upper()
    olt = SUKABUMI_FDT_TABS.get(tab_key)
    if not olt:
        return []
    layout = next(
        ((row_number, columns) for row_number, row in enumerate(data, start=1)
         if (columns := find_header_columns(row))),
        None,
    )
    if not layout:
        return []
    header_row, columns = layout
    parent, hub_id, olt_name_initial, olt_hostname = sheet_name, *olt
    records: list[FdtCodingRecord] = []
    for row_number, row in enumerate(data[header_row:], start=header_row + 1):
        def cell(key: str) -> str:
            column = columns.get(key, 0)
            return normalize_text(row[column - 1] if column and column <= len(row) else "")

        site_name = cell("site")
        cluster_initial = cell("cluster").upper()
        fdt_id = format_excel_identifier(cell("fdt_no"))
        fdt_code = cell("fdt_code").upper()
        # Preserve evaluated source truth byte-for-byte. Cluster/FDT number are
        # still required so partial rows (.084/.081) stay excluded.
        if (
            not site_name
            or not cluster_initial
            or not fdt_id
            or not SUKABUMI_FDT_CODE_PATTERN.fullmatch(fdt_code)
        ):
            continue
        records.append(FdtCodingRecord(
            sheet_name=sheet_name,
            row_number=row_number,
            site_name=site_name,
            fdt_order=extract_fdt_order(site_name),
            fdt_id=fdt_id,
            fdt_code=fdt_code,
            index_code="",
            cluster_initial=cluster_initial,
            olt_initial=parent,
            hub_id=hub_id,
            olt_hostname=olt_hostname,
            olt_name_initial=olt_name_initial,
            area="Sukabumi",
        ))
    return records


def _parse_tangerang_sheet_data(sheet_name: str, data: list[list[str]]) -> list[FdtCodingRecord]:
    """Parse only verified physical Tangerang OLT tabs.

    A physical tab can contain several OLT units. The evaluated full index is
    authoritative for the row unit; malformed/missing index values fall back
    to the verified primary unit of that exact tab without rewriting source
    values.
    """
    tab_key = str(sheet_name or "")
    tab_identity = TANGERANG_FDT_TABS.get(tab_key)
    if not tab_identity:
        return []
    layout = next(
        ((row_number, columns) for row_number, row in enumerate(data, start=1)
         if (columns := find_header_columns(row))),
        None,
    )
    if not layout:
        return []
    header_row, columns = layout
    primary_hub, olt_name_initial = tab_identity
    records: list[FdtCodingRecord] = []
    for row_number, row in enumerate(data[header_row:], start=header_row + 1):
        def cell(key: str) -> str:
            column = columns.get(key, 0)
            return normalize_text(row[column - 1] if column and column <= len(row) else "")

        site_name = cell("site")
        cluster_initial = cell("cluster").upper()
        fdt_id = format_excel_identifier(cell("fdt_no"))
        fdt_code = cell("fdt_code").upper()
        index_code = cell("index")
        if (
            not site_name
            or not cluster_initial
            or not fdt_id
            or not TANGERANG_FDT_CODE_PATTERN.fullmatch(fdt_code)
        ):
            continue
        index_hub = TANGERANG_HUB_PATTERN.match(index_code.upper())
        hub_id = index_hub.group(1).upper() if index_hub else primary_hub
        records.append(FdtCodingRecord(
            sheet_name=sheet_name,
            row_number=row_number,
            site_name=site_name,
            fdt_order=extract_fdt_order(site_name),
            fdt_id=fdt_id,
            fdt_code=fdt_code,
            index_code=index_code,
            cluster_initial=cluster_initial,
            olt_initial=sheet_name,
            hub_id=hub_id,
            olt_hostname="",
            olt_name_initial=olt_name_initial,
            area="Tangerang",
        ))
    return records


def _parse_balikpapan_sheet_data(sheet_name: str, data: list[list[str]]) -> list[FdtCodingRecord]:
    identity = fdt_coding_db.BALIKPAPAN_FDT_TABS.get(sheet_name)
    if identity is None:
        return []
    layout = next(((n, cols) for n, row in enumerate(data[:25], 1)
                   if (cols := find_header_columns(row))), None)
    if not layout:
        raise RuntimeError(f"Sheet {sheet_name!r} tidak punya header FDT yang valid")
    header_row, columns = layout
    initial, shift = identity
    records = []
    for number, row in enumerate(data[header_row:], header_row + 1):
        def cell(key):
            col = columns.get(key, 0)
            if key in {"cluster", "fdt_no", "fdt_code", "index"}:
                col -= shift
            return normalize_text(row[col-1] if 0 < col <= len(row) else "")
        site, cluster = cell("site"), cell("cluster").upper()
        code, index, raw_number = cell("fdt_code").upper(), cell("index"), cell("fdt_no")
        if not site or not cluster or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*\.[0-9]{1,3}", code):
            continue
        hub = re.fullmatch(r"((?:BPN|TRG)\.\d{3}\.\d{4})\." + re.escape(code), index.upper())
        if hub is None:
            continue
        suffix = int(code.rsplit(".", 1)[1])
        if raw_number and (not raw_number.isdigit() or int(raw_number) != suffix):
            continue
        # Evaluated code remains source truth; blank sharing numbers inherit
        # only a complete validated code/index suffix, never a generated code.
        records.append(FdtCodingRecord(sheet_name=sheet_name, row_number=number,
            site_name=site, area="Balikpapan", fdt_order=extract_fdt_order(site),
            fdt_id=f"{suffix:03d}", fdt_code=code, index_code=index,
            cluster_initial=cluster, olt_initial=sheet_name, hub_id=hub[1],
            olt_hostname="", olt_name_initial=initial))
    return records


def _parse_sheet_data(
    spreadsheet_id: str,
    sheet_name: str,
    data: list[list[str]],
    source_label: str = "",
) -> list[FdtCodingRecord]:
    source_id = normalize_text(spreadsheet_id)
    if source_id == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID:
        return _parse_balikpapan_sheet_data(sheet_name, data)
    if not data:
        return []
    if source_id == LEGACY_FDT_SPREADSHEET_ID:
        return _parse_legacy_sheet_data(sheet_name, data)
    if source_id == SUKABUMI_FDT_SPREADSHEET_ID:
        return _parse_sukabumi_sheet_data(sheet_name, data)
    if source_id == TANGERANG_FDT_SPREADSHEET_ID:
        return _parse_tangerang_sheet_data(sheet_name, data)

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
            # `row_idx` is a zero-based list index; Sheets row numbers are 1-based.
            row_number=row_idx + 1,
            site_name=site_name,
            fdt_order=extract_fdt_order(site_name),
            fdt_id=fdt_id,
            fdt_code=fdt_code.upper(),
            index_code=index_code,
            cluster_initial=cluster_initial,
            olt_initial=olt_initial.upper() if olt_initial else "",
            hub_id=hub_id,
            olt_hostname=olt_hostname.upper() if olt_hostname else "",
            area=extract_sheet_area(sheet_name, source_label),
        ))

    return records


def _sync_incremental_sheet(
    spreadsheet_id: str,
    sheet_name: str,
    data: list[list[str]],
    db_path: Path,
    hashes: dict[str, str],
    source_label: str = "",
    sheet_counts: dict[str, int] | None = None,
    force: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    if _is_tangerang_sheet_identity(spreadsheet_id, sheet_name):
        raise ValueError("Area Tangerang read-only; hanya sync_all_incremental terotorisasi")
    key = _hash_key(spreadsheet_id, sheet_name)
    current_hash = _calc_hash(data)
    records = _parse_sheet_data(spreadsheet_id, sheet_name, data, source_label=source_label)
    if not records:
        has_header = any(
            _legacy_header_columns(row) or find_header_columns(row)
            for row in data[:25]
        )
        if not has_header:
            # Abort so source modifiedTime is not persisted; next poll retries.
            raise RuntimeError(f"Sheet '{sheet_name}' tidak punya header FDT yang valid")

    if sheet_counts is not None:
        sheet_counts[key] = len(records)

    if not force and hashes.get(key) == current_hash:
        with fdt_coding_db.connect(db_path) if conn is None else nullcontext(conn) as check_conn:
            actual = int(check_conn.execute(
                "SELECT COUNT(*) FROM fdt_coding_records WHERE source_spreadsheet_id = ? AND sheet_name = ?",
                (spreadsheet_id, sheet_name),
            ).fetchone()[0])
            pending = check_conn.execute(
                "SELECT 1 FROM fdt_coding_records WHERE source_spreadsheet_id=? AND sheet_name=? AND source_row<0 LIMIT 1",
                (spreadsheet_id, sheet_name),
            ).fetchone()
        if actual == len(records) and not pending:
            return 0
    if not records:
        # A valid header with no data rows is a deliberate empty sheet.
        fdt_coding_db.replace_records_by_source(
            db_path, [],
            source_spreadsheet_id=spreadsheet_id,
            source_sheet_name=sheet_name,
            conn=conn,
        )
        hashes[key] = current_hash
        return 0

    count = fdt_coding_db.replace_records_by_source(
        db_path, records,
        source_spreadsheet_id=spreadsheet_id,
        source_sheet_name=sheet_name,
        conn=conn,
    )
    hashes[key] = current_hash
    return count


def _assert_pending_sharing_matches_workbook(db_path: Path, spreadsheet_id: str,
                                             sheet_data: dict[str, list[list[str]]],
                                             source_label: str) -> None:
    """Validate every pending sharing row before any tab is replaced."""
    with fdt_coding_db.read_connect(db_path) as conn:
        pending = conn.execute("SELECT * FROM fdt_coding_records WHERE source_spreadsheet_id=? AND source_row<0",
                               (spreadsheet_id,)).fetchall()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='fdt_coding_sharing' AND type='table'").fetchone():
            pending += conn.execute("""SELECT r.* FROM fdt_coding_records r JOIN fdt_coding_sharing s ON s.record_id=r.id
                WHERE r.source_spreadsheet_id=? AND r.source_row>0""", (spreadsheet_id,)).fetchall()
        parsed: dict[str, dict[int, dict]] = {}
        for row in pending:
            title = row['sheet_name']
            physical = abs(int(row['source_row']))
            if title not in sheet_data:
                raise ValueError(f"Sharing FDT pending record {row['id']} at {title} row {physical}; worksheet changed")
            if title not in parsed:
                items = _parse_sheet_data(spreadsheet_id, title, sheet_data[title], source_label=source_label)
                parsed[title] = {}
                for item in items:
                    payload = fdt_coding_db.record_to_db_payload(item, source_spreadsheet_id=spreadsheet_id)
                    raw = fdt_coding_db.normalize_record_payload(payload)
                    payload = fdt_coding_db.preserve_shared_record_payload(conn, raw)
                    if payload is raw and spreadsheet_id != SUKABUMI_FDT_SPREADSHEET_ID:
                        payload = fdt_coding_db.normalize_record_payload(
                            fdt_coding_db.apply_catalog_to_payload(conn, payload))
                    if item.row_number in parsed[title]:
                        raise ValueError(f"Duplicate physical FDT row {item.row_number} in {title}")
                    parsed[title][item.row_number] = payload
            payload = parsed[title].get(physical)
            identity = ('area', 'olt_initial', 'site_name', 'fdt_id', 'fdt_code',
                        'hub_id', 'olt_hostname', 'index_code', 'cluster_initial')
            if not payload or any(normalize_text(row[key]).upper() != normalize_text(payload[key]).upper()
                                  for key in identity):
                raise ValueError(f"Sharing FDT pending record {row['id']} at {title} row {physical}; perlu rekonsiliasi")


@synchronized_sync_operation
def sync_all_incremental(
    db_path: str | Path | None = None,
    sources: Sequence[SyncSource] | None = None,
    force: bool = False,
) -> dict[str, dict[str, int]]:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sources = list(sources or DEFAULT_SOURCES)
    seen_source_ids: set[str] = set()
    for source in sources:
        source_id = normalize_text(source.spreadsheet_id)
        source_name = normalize_text(source.name).lower()
        source_label = str(source.spreadsheet_label or "")
        if not source_id:
            raise ValueError("FDT source ID kosong")
        if source_id in RETIRED_SOURCE_IDS:
            raise ValueError(f"FDT source retired; sync diblok: {source_id}")
        if source_id in seen_source_ids:
            raise ValueError(f"FDT source duplicate: {source_id}")
        seen_source_ids.add(source_id)
        claims_balikpapan = source_name == "balikpapan" or "BALIKPAPAN" in source_label.upper()
        if claims_balikpapan and source_id != fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID:
            raise ValueError("Source Balikpapan wajib memakai workbook terverifikasi")
        if source_id == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID and (
                source.spreadsheet_id != source_id or source.name != "balikpapan" or source_label != fdt_coding_db.BALIKPAPAN_FDT_WORKBOOK_TITLE):
            raise ValueError("Nama/label source Balikpapan tidak sesuai workbook terverifikasi")
        claims_tangerang = source_name == "tangerang" or "TANGERANG" in source_label.upper()
        if claims_tangerang and source_id != TANGERANG_FDT_SPREADSHEET_ID:
            raise ValueError("Source Tangerang wajib memakai replacement workbook terverifikasi")
        if source_id == TANGERANG_FDT_SPREADSHEET_ID and source_label != TANGERANG_FDT_WORKBOOK_TITLE:
            raise ValueError("Label source Tangerang tidak sesuai workbook terverifikasi")
    # Retired cleanup is a write too: reject unresolved reservations before
    # touching any active source, not after its tabs have committed.
    ordinary_retired_ids = RETIRED_SOURCE_IDS - {TANGERANG_RETIRED_SPREADSHEET_ID}
    with fdt_coding_db.read_connect(db_path) as preflight_conn:
        if ordinary_retired_ids:
            placeholders = ','.join('?' for _ in ordinary_retired_ids)
            retired_pending = preflight_conn.execute(
                f"SELECT id FROM fdt_coding_records WHERE source_row<0 "
                f"AND source_spreadsheet_id IN ({placeholders}) LIMIT 1",
                sorted(ordinary_retired_ids),
            ).fetchone()
            if retired_pending:
                raise ValueError(f"Sharing FDT pending record {retired_pending['id']} pada retired source; perlu rekonsiliasi")
    if any(source.spreadsheet_id == LEGACY_FDT_SPREADSHEET_ID for source in sources):
        fdt_coding_db.upsert_olt_catalog_units(db_path, _legacy_catalog_units())

    hashes = _load_sheet_hashes()
    sheet_counts = _load_sheet_counts()
    revisions = _load_source_revisions()
    results: dict[str, dict[str, int]] = {}
    changed_total = 0

    for source in sources:
        gs_client = _build_gspread_client(source.spreadsheet_id)
        if source.spreadsheet_id == fdt_coding_db.BALIKPAPAN_FDT_SPREADSHEET_ID:
            # Exclusive protected mutator: lexical to this validating orchestrator.
            spreadsheet = gs_client.open_by_key(source.spreadsheet_id)
            worksheets = spreadsheet.worksheets()
            _validate_balikpapan_workbook_identity(spreadsheet, worksheets)
            modified = _fetch_drive_modified_time(gs_client, source.spreadsheet_id)
            active = [ws for ws in worksheets if ws.title in fdt_coding_db.BALIKPAPAN_FDT_TABS]
            expected_keys = {_hash_key(source.spreadsheet_id, ws.title) for ws in active}
            source_counts = {k: v for k, v in sheet_counts.items() if k.startswith(source.spreadsheet_id + "_")}
            with fdt_coding_db.read_connect(db_path) as check:
                owned = [dict(row) for row in check.execute("SELECT * FROM fdt_coding_records")
                         if fdt_coding_db.is_balikpapan_read_only_record(row)]
                counts = {ws.title: sum(row["sheet_name"] == ws.title for row in owned) for ws in active}
                complete = (set(source_counts) == expected_keys and len(owned) == sum(source_counts.values())
                    and all(row["source_spreadsheet_id"] == source.spreadsheet_id and row["source_row"] > 0 for row in owned)
                    and all(counts[ws.title] == source_counts.get(_hash_key(source.spreadsheet_id, ws.title)) for ws in active))
            if not force and modified and revisions.get(source.spreadsheet_id) == modified and complete:
                results[source.name] = {"changed": 0, "skipped": 0, "unchanged": 1}
                if len(sources) == 1:
                    return results
                continue
            ranges = ["'" + ws.title.replace("'", "''") + "'" for ws in active]
            values = spreadsheet.values_batch_get(ranges).get("valueRanges", [])
            if len(values) != len(active):
                raise RuntimeError("Balikpapan incomplete batch response")
            payloads, pending_hashes, pending_counts, locators = [], {}, {}, set()
            for ws, value in zip(active, values):
                data = value.get("values", [])
                records = _parse_balikpapan_sheet_data(ws.title, data)
                for record in records:
                    payload = fdt_coding_db.normalize_record_payload(fdt_coding_db.record_to_db_payload(
                        record, source_spreadsheet_id=source.spreadsheet_id))
                    fdt_coding_db.validate_payload(payload)
                    if payload["sheet_name"] != ws.title or payload["olt_initial"] != ws.title or payload["area"] != "Balikpapan":
                        raise ValueError("Balikpapan inbound ownership mismatch")
                    locator = (payload["sheet_name"], payload["source_row"])
                    if locator in locators:
                        raise ValueError("Duplicate physical Balikpapan locator")
                    locators.add(locator); payloads.append(payload)
                key = _hash_key(source.spreadsheet_id, ws.title)
                pending_hashes[key] = _calc_hash(data); pending_counts[key] = len(records)
            _validate_balikpapan_workbook_identity(spreadsheet, spreadsheet.worksheets())
            if not modified or _fetch_drive_modified_time(gs_client, source.spreadsheet_id) != modified:
                raise RuntimeError("Balikpapan revision race/unavailable before transaction")
            fields = fdt_coding_db.FIELDS
            now = fdt_coding_db.utc_now()
            with fdt_coding_db.connect(db_path) as conn:
                fdt_coding_db.begin_immediate(conn)
                owned = [dict(row) for row in conn.execute("SELECT * FROM fdt_coding_records")
                         if fdt_coding_db.is_balikpapan_read_only_record(row)]
                marked = {row[0] for row in conn.execute("SELECT record_id FROM fdt_coding_sharing")}
                shared_titles = fdt_coding_db.TANGERANG_PROTECTED_OLT_KEYS & {
                    normalize_text(title).upper() for title in fdt_coding_db.BALIKPAPAN_FDT_SHEET_IDS}
                for row in owned:
                    if row["source_row"] < 0 or row["id"] in marked:
                        raise ValueError("Balikpapan pending/sharing record requires reconciliation")
                    other_owner = dict(row)
                    # Shared helper names protect generic writes, not exclusive regional ownership.
                    for field in ("sheet_name", "olt_initial"):
                        if normalize_text(other_owner[field]).upper() in shared_titles:
                            other_owner[field] = ""
                    if fdt_coding_db.is_tangerang_read_only_record(other_owner):
                        raise ValueError("Balikpapan ownership collides with protected Tangerang")
                existing = {(row["sheet_name"], row["source_row"]): row for row in owned
                            if row["source_spreadsheet_id"] == source.spreadsheet_id}
                retain = {existing[loc]["id"] for loc in locators if loc in existing}
                for row in owned:
                    if row["id"] not in retain:
                        conn.execute("DELETE FROM fdt_coding_records WHERE id=?", (row["id"],))
                for payload in payloads:
                    previous = existing.get((payload["sheet_name"], payload["source_row"]))
                    if previous:
                        if any(previous[field] != payload[field] for field in fields):
                            conn.execute("UPDATE fdt_coding_records SET " + ",".join(field + "=?" for field in fields)
                                + ",updated_at=? WHERE id=?", [*[payload[field] for field in fields], now, previous["id"]])
                    else:
                        columns = (*fields, "created_at", "updated_at")
                        conn.execute("INSERT INTO fdt_coding_records (" + ",".join(columns) + ") VALUES ("
                            + ",".join("?" for field in columns) + ")", [*[payload[field] for field in fields], now, now])
            for state in (hashes, sheet_counts):
                for key in list(state):
                    if key.startswith(source.spreadsheet_id + "_"):
                        del state[key]
            hashes.update(pending_hashes); sheet_counts.update(pending_counts)
            revisions[source.spreadsheet_id] = modified
            _publish_balikpapan_state(hashes, sheet_counts, revisions)
            results[source.name] = {"changed": len(payloads), "skipped": len(worksheets) - len(active)}
            if len(sources) == 1:
                return results  # No ordinary-source retirement/manifest mutation.
            changed_total += len(payloads)
            continue
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
            and _source_db_matches_sheet_counts(db_path, source.spreadsheet_id, sheet_counts)
        ):
            results[source.name] = {"changed": 0, "skipped": 0, "unchanged": 1}
            logger.debug("Inbound sync: '%s' tidak berubah (modifiedTime sama), skip fetch.", label)
            continue

        spreadsheet = gs_client.open_by_key(source.spreadsheet_id)
        worksheets = spreadsheet.worksheets()
        if source.spreadsheet_id == TANGERANG_FDT_SPREADSHEET_ID:
            _validate_tangerang_workbook_identity(spreadsheet, worksheets)
        sync_worksheets = [
            ws for ws in worksheets
            if len(ws.title) <= 120 and _should_sync_sheet(source.spreadsheet_id, ws.title)
        ]
        if not sync_worksheets:
            raise RuntimeError(f"Tidak ada worksheet FDT aktif untuk {label}")
        normalized_titles: dict[str, str] = {}
        for ws in sync_worksheets:
            normalized = normalize_text(ws.title).upper()
            previous = normalized_titles.get(normalized)
            if previous is not None and previous != ws.title:
                raise RuntimeError(
                    f"Worksheet FDT ambigu untuk {label}: '{previous}' dan '{ws.title}'"
                )
            normalized_titles[normalized] = ws.title

        ranges = [f"'{ws.title.replace(chr(39), chr(39) * 2)}'" for ws in sync_worksheets]
        value_ranges = spreadsheet.values_batch_get(ranges).get("valueRanges", [])
        if len(value_ranges) != len(sync_worksheets):
            raise RuntimeError(
                f"Sheets batch response tidak lengkap untuk {label}: "
                f"expected {len(sync_worksheets)}, got {len(value_ranges)}"
            )

        # BatchGet preserves request order. Pair with the requested Worksheet
        # object so titles with significant trailing spaces remain exact.
        sheet_data = {
            ws.title: vr.get("values", [])
            for ws, vr in zip(sync_worksheets, value_ranges)
        }

        if source.spreadsheet_id == TANGERANG_FDT_SPREADSHEET_ID:
            # Validate and parse every physical data tab before opening the write
            # transaction. No per-sheet partial state can reach production.
            payloads: list[dict] = []
            pending_hashes: dict[str, str] = {}
            pending_counts: dict[str, int] = {}
            physical_locators: set[tuple[str, str, int]] = set()
            for ws in sync_worksheets:
                title = ws.title
                data = sheet_data[title]
                records = _parse_tangerang_sheet_data(title, data)
                has_header = any(find_header_columns(row) for row in data[:25])
                if not has_header:
                    raise RuntimeError(f"Sheet '{title}' tidak punya header FDT yang valid")
                for record in records:
                    payload = fdt_coding_db.normalize_record_payload(
                        fdt_coding_db.record_to_db_payload(
                            record,
                            source_spreadsheet_id=TANGERANG_FDT_SPREADSHEET_ID,
                        )
                    )
                    if (
                        payload["source_spreadsheet_id"] != TANGERANG_FDT_SPREADSHEET_ID
                        or payload["sheet_name"] != title
                        or normalize_text(payload["area"]).upper() != "TANGERANG"
                        or not normalize_text(payload["hub_id"]).upper().startswith("TNG.")
                        or int(payload["source_row"] or 0) < 1
                    ):
                        raise ValueError("Payload inbound Tangerang tidak konsisten")
                    fdt_coding_db.validate_payload(payload)
                    locator = (
                        payload["source_spreadsheet_id"],
                        payload["sheet_name"],
                        int(payload["source_row"]),
                    )
                    if locator in physical_locators:
                        raise ValueError(
                            "Duplicate physical FDT source locator: "
                            f"{locator[0]} / {locator[1]} / row {locator[2]}"
                        )
                    physical_locators.add(locator)
                    payloads.append(payload)
                key = _hash_key(source.spreadsheet_id, title)
                pending_hashes[key] = _calc_hash(data)
                pending_counts[key] = len(records)

            revision_before_write = _fetch_drive_modified_time(
                gs_client, TANGERANG_FDT_SPREADSHEET_ID
            )
            if not modified_time or revision_before_write != modified_time:
                raise RuntimeError(
                    "Source Tangerang berubah atau revision tidak tersedia sebelum transaksi"
                )

            now = fdt_coding_db.utc_now()
            fields = fdt_coding_db.FIELDS
            columns = ", ".join((*fields, "created_at", "updated_at"))
            placeholders = ", ".join("?" for _ in (*fields, "created_at", "updated_at"))
            protected_titles = sorted(
                normalize_text(title).upper() for title in TANGERANG_FDT_TABS
            )
            title_placeholders = ",".join("?" for _ in protected_titles)
            with fdt_coding_db.connect(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        f"""
                        DELETE FROM fdt_coding_records
                        WHERE source_spreadsheet_id IN (?, ?)
                           OR UPPER(TRIM(area)) = 'TANGERANG'
                           OR UPPER(TRIM(hub_id)) LIKE 'TNG.%'
                           OR UPPER(TRIM(sheet_name)) IN ({title_placeholders})
                           OR UPPER(TRIM(olt_initial)) IN ({title_placeholders})
                        """,
                        [
                            TANGERANG_FDT_SPREADSHEET_ID,
                            TANGERANG_RETIRED_SPREADSHEET_ID,
                            *protected_titles,
                            *protected_titles,
                        ],
                    )
                    for payload in payloads:
                        conn.execute(
                            f"INSERT INTO fdt_coding_records ({columns}) VALUES ({placeholders})",
                            [payload[field] for field in fields] + [now, now],
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            for state in (hashes, sheet_counts):
                for key in list(state):
                    if key.startswith(f"{TANGERANG_FDT_SPREADSHEET_ID}_"):
                        state.pop(key, None)
                    if key.startswith(f"{TANGERANG_RETIRED_SPREADSHEET_ID}_"):
                        state.pop(key, None)
            hashes.update(pending_hashes)
            sheet_counts.update(pending_counts)
            revisions.pop(TANGERANG_RETIRED_SPREADSHEET_ID, None)
            if modified_time is not None:
                revisions[source.spreadsheet_id] = modified_time
            changed = len(payloads)
            skipped = len(worksheets) - len(sync_worksheets)
            results[source.name] = {"changed": changed, "skipped": skipped}
            changed_total += changed
            continue

        # Fail before the first per-tab commit when a later tab has an
        # unresolved sharing write. Exact titles matter, including spaces.
        _assert_pending_sharing_matches_workbook(
            db_path, source.spreadsheet_id, sheet_data,
            source.spreadsheet_label or source.name,
        )
        active_sheet_keys = {
            _hash_key(source.spreadsheet_id, ws.title)
            for ws in sync_worksheets
        }
        for key in list(sheet_counts):
            if key.startswith(f"{source.spreadsheet_id}_") and key not in active_sheet_keys:
                sheet_counts.pop(key, None)
        for key in list(hashes):
            if key.startswith(f"{source.spreadsheet_id}_") and key not in active_sheet_keys:
                hashes.pop(key, None)

        skipped += len(worksheets) - len(sync_worksheets)
        # All tabs of one workbook commit together; a late malformed tab cannot
        # leave reconciled reservations with stale state manifests.
        with fdt_coding_db.connect(db_path) as inbound_conn:
            fdt_coding_db.begin_immediate(inbound_conn)
            for ws in sync_worksheets:
                title = ws.title
                data = sheet_data.get(title, [])
                c = _sync_incremental_sheet(
                    source.spreadsheet_id,
                    title,
                    data,
                    db_path,
                    hashes,
                    source_label=source.spreadsheet_label or source.name,
                    sheet_counts=sheet_counts,
                    force=force,
                    conn=inbound_conn,
                )
                if c:
                    changed += c
                    logger.debug("  Sheet '%s' changed: %d records", title, c)
                else:
                    skipped += 1

            removed_orphans = fdt_coding_db.delete_source_records_not_in_sheets(
                db_path,
                source.spreadsheet_id,
                [ws.title for ws in sync_worksheets],
                conn=inbound_conn,
            )
            if removed_orphans:
                logger.info("Inbound sync: pruned %d orphan row(s) for %s", removed_orphans, label)

        # Record the modifiedTime we just successfully processed so the
        # next poll can skip if nothing changed.
        if modified_time is not None:
            revisions[source.spreadsheet_id] = modified_time

        results[source.name] = {"changed": changed, "skipped": skipped}
        changed_total += changed

    # Generic cleanup handles only ordinary retired sources. The protected
    # Tangerang predecessor is removed inside Tangerang's validated transaction.
    ordinary_retired_ids = RETIRED_SOURCE_IDS - {TANGERANG_RETIRED_SPREADSHEET_ID}
    retired_removed = fdt_coding_db.delete_records_by_spreadsheet_ids(
        db_path,
        ordinary_retired_ids,
    )
    if retired_removed:
        logger.info("Inbound sync: pruned %d retired-source row(s)", retired_removed)
    for retired_id in ordinary_retired_ids:
        retired_prefix = f"{retired_id}_"
        for state in (hashes, sheet_counts):
            for key in list(state):
                if key.startswith(retired_prefix):
                    state.pop(key, None)
        revisions.pop(retired_id, None)

    _save_sheet_hashes(hashes)
    _save_sheet_counts(sheet_counts)
    _save_source_revisions(revisions)

    if changed_total > 0:
        logger.info("Incremental sync: %d records changed across %d source(s)", changed_total, len(sources))
    return results




# ---------------------------------------------------------------------------
# OLT sheet creation from template
# ---------------------------------------------------------------------------

FDT_TEMPLATE_SHEET_ENV = "FDT_TEMPLATE_SHEET_NAME"
FDT_TEMPLATE_DEFAULT_SHEET = "Template"


class OltSheetRollbackError(RuntimeError):
    """Raised when a failed OLT-sheet mutation may need reconciliation."""


def _legacy_shared_sheet_target(olt_code: str) -> tuple[str, str] | None:
    details = _legacy_hub_details(olt_code)
    if not details:
        return None
    return LEGACY_FDT_SPREADSHEET_ID, details[4]


def _source_for_olt_code(olt_code: str) -> SyncSource:
    if _legacy_hub_details(olt_code):
        return next(source for source in DEFAULT_SOURCES if source.spreadsheet_id == LEGACY_FDT_SPREADSHEET_ID)
    prefix = normalize_text(olt_code).upper().split(".", 1)[0]
    if prefix == "JKT":
        for source in DEFAULT_SOURCES:
            if source.name.lower() == "jakarta":
                return source
    if prefix == "SMI":
        return _source_named("sukabumi")
    if prefix == "TNG":
        return _source_named("tangerang")
    if prefix == "BKS":
        return _source_named("bekasi")
    # SKB exists in both the Sukabumi source and historical Bekasi catalog.
    # Callers without area/source ownership must not guess.
    raise ValueError(f"FDT OLT prefix tidak punya source mapping: {prefix or '(kosong)'}")

def _worksheet_or_none(spreadsheet, title: str):
    try:
        worksheet = spreadsheet.worksheet(title)
        if _is_tangerang_worksheet(worksheet):
            raise ValueError("Inbound-only worksheet read-only; returned identity mismatch")
        return worksheet
    except gspread.exceptions.WorksheetNotFound:
        return None

def _copy_template_worksheet(spreadsheet, new_sheet_name: str):
    if _is_tangerang_sheet_identity(getattr(spreadsheet, "id", ""), new_sheet_name):
        raise ValueError("Spreadsheet Tangerang read-only; copy worksheet diblok")
    template_name = os.getenv(FDT_TEMPLATE_SHEET_ENV, FDT_TEMPLATE_DEFAULT_SHEET).strip() or FDT_TEMPLATE_DEFAULT_SHEET
    template = _worksheet_or_none(spreadsheet, template_name)
    if template is None:
        worksheets = spreadsheet.worksheets()
        if not worksheets:
            return spreadsheet.add_worksheet(title=new_sheet_name, rows=1000, cols=38)
        template = worksheets[0]
    if _is_tangerang_worksheet(template):
        raise ValueError("Inbound-only template read-only; duplicate diblok")
    return spreadsheet.duplicate_sheet(template.id, new_sheet_name=new_sheet_name)

def _hub_parts(olt_code: str) -> tuple[str, str, str]:
    parts = normalize_text(olt_code).upper().split(".")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    return "", "", ""

def delete_olt_sheet(olt_initial: str, olt_code: str = "") -> dict:
    if is_inbound_read_only({"olt_initial": olt_initial, "hub_id": olt_code}):
        return {"deleted": False, "reason": "protected_source_read_only"}
    raw_sheet_name = str(olt_initial or "")
    sheet_name = normalize_text(raw_sheet_name).upper()
    if (
        normalize_text(olt_code).upper().startswith("TNG.")
        or sheet_name in {normalize_text(name).upper() for name in TANGERANG_FDT_SHEET_IDS}
    ):
        return {
            "deleted": False,
            "reason": "protected_tangerang_read_only",
            "sheet_name": raw_sheet_name,
        }
    legacy_target = _legacy_shared_sheet_target(olt_code)
    if legacy_target:
        # One shared source tab carries several legacy OLTs. Never delete it.
        return {"deleted": False, "reason": "protected_legacy_shared_sheet", "sheet_name": legacy_target[1]}
    sheet_name = normalize_text(olt_initial).upper()
    if not sheet_name:
        return {"deleted": False, "reason": "missing_sheet_name"}
    sources = []
    if olt_code:
        sources.append(_source_for_olt_code(olt_code))
    sources.extend(
        source for source in DEFAULT_SOURCES
        if source not in sources and source.spreadsheet_id != TANGERANG_FDT_SPREADSHEET_ID
    )
    for source in sources:
        client = _build_gspread_client(source.spreadsheet_id)
        try:
            spreadsheet = client.open_by_key(source.spreadsheet_id)
            worksheet = _worksheet_or_none(spreadsheet, sheet_name)
            if worksheet is None:
                continue
            if _is_tangerang_worksheet(worksheet):
                return {
                    "deleted": False,
                    "reason": "protected_tangerang_read_only",
                    "sheet_name": worksheet.title,
                }
            if normalize_text(worksheet.title).upper() in {"TEMPLATE", "CREATE EMAIL SENDING"}:
                return {"deleted": False, "reason": "protected_sheet", "sheet_name": worksheet.title}
            spreadsheet.del_worksheet(worksheet)
            return {"deleted": True, "spreadsheet": source.name, "sheet_name": worksheet.title}
        except Exception as exc:
            logger.warning("Gagal hapus sheet OLT '%s' dari %s: %s", sheet_name, source.name, exc)
    return {"deleted": False, "reason": "sheet_not_found", "sheet_name": sheet_name}

def _source_for_manual_olt(olt: dict) -> SyncSource:
    """Validate explicit Area ownership, then use the shared target router."""
    area = normalize_text(olt.get("area")).upper()
    prefix = normalize_text(olt.get("hub_id")).upper().split(".", 1)[0]
    routes = {
        "BEKASI": ("bekasi", {"BKS"}),
        "JAKARTA": ("jakarta", {"JKT"}),
        "SUKABUMI": ("sukabumi", {"SKB", "SMI"}),
        "TANGERANG": ("tangerang", {"TNG"}),
        "INDRAMAYU": ("indramayu-kuningan", {"IDM"}),
        "KUNINGAN": ("indramayu-kuningan", {"KNG"}),
    }
    route = routes.get(area)
    if route is None:
        raise ValueError(f"Area OLT tidak didukung: {area or '(kosong)'}")
    expected_source, allowed_prefixes = route
    if prefix not in allowed_prefixes:
        allowed = "/".join(sorted(allowed_prefixes))
        raise ValueError(f"OLT ID tidak sesuai Area {area.title()}; gunakan prefix {allowed}")
    source = _source_for_target_record(olt)
    if source.name.lower() != expected_source:
        raise ValueError(f"Area {area.title()} tidak sesuai source OLT ID {olt.get('hub_id')}")
    return source


def _olt_sheet_expected_values(olt: dict) -> dict[str, str]:
    olt_name = normalize_text(olt.get("olt_initial")).upper()
    olt_code = normalize_text(olt.get("hub_id")).upper()
    olt_initial = normalize_text(olt.get("olt_name_initial")).upper()
    olt_hostname = normalize_text(olt.get("olt_hostname")).upper()
    city, hub_no, subhub_no = _hub_parts(olt_code)
    hub_base = f"{city}.{hub_no}" if city and hub_no else ""
    return {
        "D5": olt_code,
        "F5": olt_initial,
        "I5": olt_hostname,
        "C14": hub_no,
        "D14": hub_base,
        "B15": olt_name,
        "C15": subhub_no,
        "D15": olt_code,
        "E15": olt_initial,
        "K15": f"{olt_code}-Line A - Cable 96C/8T s/d {olt_code}-Line B - Cable 96C/8T + (increment)",
    }


def _olt_sheet_read_values(worksheet, ranges: list[str]) -> dict[str, str]:
    values = worksheet.batch_get(ranges)
    if len(values) != len(ranges):
        raise RuntimeError("Read-back metadata OLT tidak lengkap")
    return {
        cell: normalize_text(item[0][0] if item and item[0] else "")
        for cell, item in zip(ranges, values)
    }


def _verify_olt_sheet_values(worksheet, expected: dict[str, str]) -> None:
    actual = _olt_sheet_read_values(worksheet, list(expected))
    mismatches = [
        cell for cell, value in expected.items()
        if normalize_text(value) != actual.get(cell, "")
    ]
    if mismatches:
        raise RuntimeError(f"Read-back OLT gagal pada {mismatches}")


def create_olt_sheet_from_template(olt: dict) -> dict:
    if is_inbound_read_only(olt):
        raise ValueError("Inbound-only source read-only; OLT mutation diblok")
    """Create/read-back one OLT worksheet using explicit Area ownership."""
    olt_name = normalize_text(olt.get("olt_initial")).upper()
    olt_code = normalize_text(olt.get("hub_id")).upper()
    olt_initial = normalize_text(olt.get("olt_name_initial")).upper()
    olt_hostname = normalize_text(olt.get("olt_hostname")).upper()
    area = normalize_text(olt.get("area"))
    if not olt_name or not olt_code or not olt_initial or not area:
        raise ValueError("Area, Nama OLT, OLT ID, dan Inisial OLT wajib diisi")
    if is_inbound_read_only({
        "area": area,
        "olt_initial": olt_name,
        "hub_id": olt_code,
        "sheet_name": olt_name,
    }):
        if area.upper() == "TANGERANG":
            raise ValueError("Tambah OLT Tangerang belum didukung karena workbook baru tidak punya tab Template resmi")
        raise ValueError("Spreadsheet Tangerang read-only; Tambah OLT diblok")

    source = _source_for_manual_olt(olt)
    expected_source = {
        "BEKASI": "bekasi",
        "JAKARTA": "jakarta",
        "SUKABUMI": "sukabumi",
        "TANGERANG": "tangerang",
        "INDRAMAYU": "indramayu-kuningan",
        "KUNINGAN": "indramayu-kuningan",
    }.get(area.upper())
    if not expected_source or source.name.lower() != expected_source:
        raise ValueError(f"Area {area} tidak sesuai source OLT ID {olt_code}")
    if area.upper() == "TANGERANG":
        raise ValueError("Tambah OLT Tangerang belum didukung karena workbook baru tidak punya tab Template resmi")

    legacy_details = _legacy_hub_details(olt_code)
    if area.upper() in {"INDRAMAYU", "KUNINGAN"}:
        if not legacy_details:
            raise ValueError(f"OLT ID {olt_code} belum punya mapping tab legacy")
        parent, mapped_hostname, mapped_initial, mapped_area, target_tab, _formula_initial = legacy_details
        if (
            mapped_area.upper() != area.upper()
            or fdt_coding_db.comparable_olt_name(parent) != fdt_coding_db.comparable_olt_name(olt_name)
            or normalize_text(mapped_initial).upper() != olt_initial
            or (olt_hostname and normalize_text(mapped_hostname).upper() != olt_hostname)
        ):
            raise ValueError(f"Data OLT {olt_code} tidak sesuai mapping {mapped_area} / {parent}")
        client = _build_gspread_client(source.spreadsheet_id)
        spreadsheet = client.open_by_key(source.spreadsheet_id)
        worksheet = _worksheet_or_none(spreadsheet, target_tab)
        if worksheet is None:
            raise ValueError(f"Tab legacy '{target_tab}' tidak ditemukan")
        layout = _find_header_layout_for_sheet(worksheet)
        if not layout or not layout[1].get("legacy_template"):
            raise ValueError(f"Header legacy '{target_tab}' tidak valid")
        header_row, columns = layout
        formula_row = _legacy_formula_source_row(worksheet, columns, olt_code)
        if formula_row <= header_row:
            raise ValueError(f"OLT ID {olt_code} tidak terverifikasi pada tab legacy '{target_tab}'")
        return {
            "created": False,
            "verified": True,
            "reason": "protected_legacy_shared_sheet",
            "spreadsheet": source.name,
            "spreadsheet_id": source.spreadsheet_id,
            "sheet_name": worksheet.title,
            "verified_source_row": formula_row,
            "rollback": None,
        }
    if legacy_details:
        raise ValueError(f"OLT ID {olt_code} adalah OLT legacy {legacy_details[3]}, bukan Area {area}")

    client = _build_gspread_client(source.spreadsheet_id)
    spreadsheet = client.open_by_key(source.spreadsheet_id)
    expected = _olt_sheet_expected_values(olt)
    existing = _worksheet_or_none(spreadsheet, olt_name)
    if existing is not None:
        # Existing worksheets are verification-only; never rewrite them here.
        _verify_olt_sheet_values(existing, expected)
        return {
            "created": False,
            "verified": True,
            "reason": "sheet_exists",
            "spreadsheet": source.name,
            "spreadsheet_id": source.spreadsheet_id,
            "sheet_name": existing.title,
            "rollback": None,
        }

    worksheet = None
    try:
        worksheet = _copy_template_worksheet(spreadsheet, olt_name)
        if _is_tangerang_worksheet(worksheet):
            raise ValueError("Inbound-only worksheet read-only; returned copy mismatch")
        updates = [{"range": cell, "values": [[value]]} for cell, value in expected.items()]
        worksheet.batch_update(updates, value_input_option="RAW")
        worksheet.format("G:G", {"numberFormat": {"type": "TEXT"}})
        _verify_olt_sheet_values(worksheet, expected)
    except Exception as exc:
        try:
            rollback_worksheet = worksheet or _worksheet_or_none(spreadsheet, olt_name)
            if rollback_worksheet is not None:
                if _is_tangerang_worksheet(rollback_worksheet):
                    raise ValueError("Inbound-only worksheet read-only; compensation diblok")
                spreadsheet.del_worksheet(rollback_worksheet)
            if _worksheet_or_none(spreadsheet, olt_name) is not None:
                raise RuntimeError("rollback verification failed: worksheet masih ada")
        except Exception as rollback_exc:
            raise OltSheetRollbackError(
                f"Pembuatan worksheet OLT gagal dan rollback perlu diperiksa: {rollback_exc}"
            ) from exc
        raise

    return {
        "created": True,
        "verified": True,
        "reason": "created",
        "spreadsheet": source.name,
        "spreadsheet_id": source.spreadsheet_id,
        "sheet_name": worksheet.title,
        "rollback": {
            "created": True,
            "spreadsheet_id": source.spreadsheet_id,
            "sheet_name": worksheet.title,
        },
    }


def rollback_olt_sheet_creation(result: dict) -> bool:
    """Delete and verify only a worksheet created by this Add OLT request."""
    rollback = dict((result or {}).get("rollback") or {})
    if rollback.get("created") is not True:
        return True
    spreadsheet_id = normalize_text(rollback.get("spreadsheet_id"))
    sheet_name = str(rollback.get("sheet_name") or "")
    if not spreadsheet_id or not sheet_name:
        raise ValueError("Snapshot rollback OLT tidak lengkap")
    if _is_tangerang_sheet_identity(spreadsheet_id, sheet_name):
        raise ValueError("Spreadsheet Tangerang read-only; rollback OLT diblok")
    client = _build_gspread_client(spreadsheet_id)
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = _worksheet_or_none(spreadsheet, sheet_name)
    if worksheet is not None:
        spreadsheet.del_worksheet(worksheet)
    if _worksheet_or_none(spreadsheet, sheet_name) is not None:
        raise RuntimeError("rollback verification failed: worksheet masih ada")
    return True


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
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Spreadsheet Tangerang read-only; cell write diblok")
    cell_val = "" if value is None else str(value)
    cell = rowcol_to_a1(row, col)
    worksheet.update(cell, [[cell_val]], value_input_option="RAW")


def _legacy_formula_source_row(worksheet: gspread.Worksheet, columns: dict[str, int], hub_id: str) -> int:
    """Find same-hub formula row to clone for a legacy create."""
    hub = normalize_text(hub_id).upper()
    column = columns.get("olt_code", 0)
    if not hub or not column:
        return 0
    values = worksheet.get_values(value_render_option="FORMULA", major_dimension="ROWS")
    for row_number, row in enumerate(values, start=1):
        formula = normalize_text(row[column - 1] if column <= len(row) else "").upper()
        if hub in formula:
            return row_number
    return 0


def _legacy_empty_formula_row(
    worksheet: gspread.Worksheet,
    header_row: int,
    columns: dict[str, int],
    hub_id: str,
) -> int:
    """Return an emptied legacy row for this hub, preserving its formulas."""
    hub = normalize_text(hub_id).upper()
    hub_column = columns.get("olt_code", 0)
    user_columns = (columns.get("site", 0), columns.get("fdt_code", 0), columns.get("segment", 0))
    if not hub or not hub_column or not all(user_columns):
        return 0
    formulas = worksheet.get_values(value_render_option="FORMULA", major_dimension="ROWS")
    values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")
    for row_number, row in enumerate(values, start=1):
        if row_number <= header_row:
            continue
        formula_row = formulas[row_number - 1] if row_number <= len(formulas) else []
        formula_hub = normalize_text(
            formula_row[hub_column - 1] if hub_column <= len(formula_row) else ""
        ).upper()
        if hub not in formula_hub:
            continue
        if all(not normalize_text(row[column - 1] if column <= len(row) else "") for column in user_columns):
            return row_number
    return 0


def _append_legacy_formula_row(
    worksheet: gspread.Worksheet,
    header_row: int,
    columns: dict[str, int],
    record: dict,
) -> int:
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Spreadsheet Tangerang read-only; formula-row write diblok")
    hub_id = normalize_text(record.get("hub_id")).upper()
    target_row = _legacy_empty_formula_row(worksheet, header_row, columns, hub_id)
    if target_row > header_row:
        _outbound_update_row(worksheet, header_row, columns, target_row, record)
        return target_row

    source_row = _legacy_formula_source_row(worksheet, columns, hub_id)
    if source_row <= header_row:
        raise ValueError(f"Template formula untuk OLT ID {hub_id} tidak ditemukan.")
    target_row = _next_append_row(worksheet, header_row, columns)
    last_column = int(columns.get("legacy_last_column") or 10)
    operator_columns = {
        int(columns.get(key) or 0)
        for key in ("site", "fdt_code", "segment")
    }
    formula_columns = [
        column for column in range(1, last_column + 1)
        if column not in operator_columns
    ]
    ranges: list[tuple[int, int]] = []
    for column in formula_columns:
        if ranges and column == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], column)
        else:
            ranges.append((column, column))
    for start_column, end_column in ranges:
        source_start = rowcol_to_a1(source_row, start_column)
        source_end = rowcol_to_a1(source_row, end_column)
        target_start = rowcol_to_a1(target_row, start_column)
        target_end = rowcol_to_a1(target_row, end_column)
        worksheet.copy_range(
            f"{source_start}:{source_end}",
            f"{target_start}:{target_end}",
            paste_type="PASTE_NORMAL",
        )
    _outbound_update_row(worksheet, header_row, columns, target_row, record)
    return target_row


def _is_sukabumi_worksheet(worksheet: gspread.Worksheet) -> bool:
    return normalize_text(getattr(worksheet, "spreadsheet_id", "")) == SUKABUMI_FDT_SPREADSHEET_ID


def _is_tangerang_sheet_identity(spreadsheet_id: object, sheet_name: object) -> bool:
    if fdt_coding_db.is_balikpapan_read_only_record({"source_spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name}):
        return True
    source_id = normalize_text(spreadsheet_id)
    title = normalize_text(sheet_name).upper()
    protected_titles = {normalize_text(value).upper() for value in TANGERANG_FDT_SHEET_IDS}
    return (
        source_id in {
            TANGERANG_FDT_SPREADSHEET_ID,
            TANGERANG_RETIRED_SPREADSHEET_ID,
        }
        or title in protected_titles
    )


def _is_tangerang_worksheet(worksheet: gspread.Worksheet) -> bool:
    if _is_tangerang_sheet_identity(getattr(getattr(worksheet, "spreadsheet", None), "id", ""), getattr(worksheet, "title", "")):
        return True
    return _is_tangerang_sheet_identity(
        getattr(worksheet, "spreadsheet_id", "") or getattr(getattr(worksheet, "spreadsheet", None), "id", ""),
        getattr(worksheet, "title", ""),
    )


def _sukabumi_managed_columns(columns: dict[str, int]) -> tuple[int, ...]:
    return tuple(
        column for column in (
            columns.get("site", 0),
            columns.get("cluster", 0),
            columns.get("fdt_no", 0),
        ) if column > 0
    )


def _outbound_update_row(
    worksheet: gspread.Worksheet,
    header_row: int,
    columns: dict[str, int],
    row_number: int,
    record: dict,
) -> None:
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Spreadsheet Tangerang read-only; outbound write diblok")
    if columns.get("legacy_template"):
        # Segment is source-owned now that it is absent from the operator UI.
        field_columns = (("site_name", "site"), ("fdt_code", "fdt_code"))
    elif _is_sukabumi_worksheet(worksheet) or _is_tangerang_worksheet(worksheet):
        # Source formulas own generated FDT/full-index cells. Operators only
        # write the three raw inputs that drive those formulas.
        field_columns = (("site_name", "site"), ("cluster_initial", "cluster"), ("fdt_id", "fdt_no"))
    else:
        field_columns = tuple(_DB_TO_SHEET_COLUMN_MAP.items())
    updates = []
    for db_field, sheet_key in field_columns:
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
    if fdt_col > 0 and not columns.get("legacy_template"):
        worksheet.format(rowcol_to_a1(row_number, fdt_col), {"numberFormat": {"type": "TEXT"}})


def _source_named(name: str) -> SyncSource:
    wanted = normalize_text(name).lower()
    for source in DEFAULT_SOURCES:
        if source.name.lower() == wanted:
            return source
    raise ValueError(f"FDT source tidak ditemukan: {name}")


def _source_for_target_record(record: dict) -> SyncSource:
    """Resolve the destination spreadsheet from the edited target OLT."""
    if fdt_coding_db.is_balikpapan_read_only_record(record):
        return _source_named("balikpapan")
    hub_id = normalize_text(record.get("hub_id")).upper()
    area = normalize_text(record.get("area")).upper()
    hub_prefix = hub_id.split(".", 1)[0] if hub_id else ""
    if area == "TANGERANG" and hub_prefix and hub_prefix != "TNG":
        raise ValueError(f"OLT ID {hub_id} tidak sesuai Area Tangerang; gunakan prefix TNG")
    if hub_prefix == "TNG" and area and area != "TANGERANG":
        raise ValueError(f"OLT ID {hub_id} tidak sesuai Area {area.title()}; source TNG hanya untuk Tangerang")
    # A valid OLT ID is stronger than a stale/manual area label. This prevents
    # a BKS unit with an accidentally retained Jakarta area from crossing files.
    if _legacy_hub_details(hub_id):
        return _source_named("indramayu-kuningan")
    # SKB.* also exists in historical Bekasi data, so area/source ownership is
    # authoritative for Sukabumi instead of globally routing the SKB prefix.
    if area == "SUKABUMI":
        return _source_named("sukabumi")
    if area == "TANGERANG":
        return _source_named("tangerang")
    if hub_id.startswith("SMI."):
        return _source_named("sukabumi")
    if hub_id.startswith("TNG."):
        return _source_named("tangerang")
    if hub_id.startswith("JKT."):
        return _source_named("jakarta")
    if hub_id.startswith("BKS."):
        return _source_named("bekasi")
    if area in {"INDRAMAYU", "KUNINGAN"}:
        return _source_named("indramayu-kuningan")
    if area == "JAKARTA":
        return _source_named("jakarta")
    if area == "BEKASI":
        return _source_named("bekasi")
    return _source_for_olt_code(hub_id)


def _target_sheet_name_for_record(record: dict, source: SyncSource) -> str:
    hub_id = normalize_text(record.get("hub_id")).upper()
    if source.spreadsheet_id == LEGACY_FDT_SPREADSHEET_ID:
        details = _legacy_hub_details(hub_id)
        if not details:
            raise ValueError(f"OLT legacy tidak punya mapping sheet: {hub_id or '(kosong)'}")
        return details[4]
    raw_sheet_name = str(record.get("olt_initial") or "")
    if source.spreadsheet_id == TANGERANG_FDT_SPREADSHEET_ID:
        if raw_sheet_name not in TANGERANG_FDT_TABS:
            raise ValueError(f"Sheet Tangerang tidak terdaftar: {raw_sheet_name!r}")
        if not hub_id.startswith("TNG."):
            raise ValueError(f"OLT ID {hub_id or '(kosong)'} tidak sesuai source Tangerang")
        return raw_sheet_name
    sheet_name = normalize_text(raw_sheet_name).upper()
    if not sheet_name:
        raise ValueError("OLT target kosong; sheet tujuan tidak dapat ditentukan")
    return sheet_name


def _find_target_worksheet(spreadsheet, requested_name: str):
    """Resolve common OLT-title variants without silently picking duplicates."""
    exact = _find_case_insensitive_sheet(spreadsheet, requested_name)
    if exact:
        return spreadsheet.worksheet(exact)
    key = fdt_coding_db.comparable_olt_name(requested_name)
    if key:
        matches = [
            worksheet
            for worksheet in spreadsheet.worksheets()
            if fdt_coding_db.comparable_olt_name(worksheet.title) == key
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Sheet target ambigu untuk OLT '{requested_name}': "
                f"{[worksheet.title for worksheet in matches]}"
            )
        if matches:
            return matches[0]
    return None


def _record_matches_target_sheet_row(
    row: list[str],
    formula_row: list[str],
    columns: dict[str, int],
    record: dict,
    *,
    strict: bool = True,
) -> bool:
    if not _record_matches_physical_row(row, formula_row, columns, record, strict=strict):
        return False
    return True


def _record_matches_physical_row(
    row: list[str],
    formula_row: list[str],
    columns: dict[str, int],
    record: dict,
    *,
    strict: bool = False,
) -> bool:
    """Match row payload plus its OLT unit, without trusting sheet position."""
    if not _record_matches_sheet_row(row, columns, record):
        return False

    if columns.get("sukabumi_template"):
        # Parent identity is the selected physical tab; data rows deliberately
        # leave OLT metadata cells blank and derive OLT ID through formulas.
        return True

    if columns.get("legacy_template"):
        expected_hub = normalize_text(record.get("hub_id")).upper()
        hub_col = columns.get("olt_code", 0)
        row_hub = normalize_text(row[hub_col - 1] if hub_col and hub_col <= len(row) else "").upper()
        formula_hub = normalize_text(
            formula_row[hub_col - 1] if hub_col and hub_col <= len(formula_row) else ""
        ).upper()
        return bool(expected_hub and (expected_hub in row_hub or expected_hub in formula_hub))

    # Regular sheets can store a base hub ID or a generated FDT index such as
    # BKS.100.0201.001. Accept that same-unit suffix, never another prefix.
    for column_key, record_key in (("olt_initial", "olt_name_initial"), ("hub_id", "hub_id")):
        column = columns.get(column_key, 0)
        expected = normalize_text(record.get(record_key)).upper()
        if not column or not expected or column > len(row):
            continue
        actual = normalize_text(row[column - 1]).upper()
        if actual and actual != expected and not actual.startswith(f"{expected}."):
            return False
        if strict and not actual:
            return False
    return True


def record_requires_sheet_move(record: dict, identity: dict) -> bool:
    """Return True when an edited record has a different physical sheet target."""
    old_spreadsheet_id = normalize_text(identity.get("source_spreadsheet_id"))
    old_sheet_name = str(identity.get("sheet_name") or "")
    old_source_row = int(identity.get("source_row", 0) or 0)
    if not old_spreadsheet_id or not old_sheet_name.strip() or old_source_row < 1:
        return False

    target_source = _source_for_target_record(record)
    target_sheet = _target_sheet_name_for_record(record, target_source)
    if old_spreadsheet_id != target_source.spreadsheet_id:
        return True

    old_sheet_key = fdt_coding_db.comparable_olt_name(old_sheet_name)
    target_sheet_key = fdt_coding_db.comparable_olt_name(target_sheet)
    if old_sheet_key != target_sheet_key:
        return True

    # Shared tabs can carry several OLT units. Changing unit is a physical
    # formula-row move even when the parent tab remains the same.
    if target_source.spreadsheet_id in {
        LEGACY_FDT_SPREADSHEET_ID,
        TANGERANG_FDT_SPREADSHEET_ID,
    }:
        return normalize_text(identity.get("hub_id")).upper() != normalize_text(record.get("hub_id")).upper()
    return False


def _restore_record_row_values(
    worksheet: gspread.Worksheet,
    columns: dict[str, int],
    row_number: int,
    original_values: list[str],
) -> None:
    """Restore one source row without shifting any other source_row values."""
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Spreadsheet Tangerang read-only; restore row diblok")
    if not original_values:
        raise RuntimeError(f"Snapshot row kosong: {row_number}")
    if columns.get("legacy_template") or _is_sukabumi_worksheet(worksheet):
        managed_columns = (
            (
                columns.get("site", 0),
                columns.get("fdt_code", 0),
                columns.get("segment", 0),
            )
            if columns.get("legacy_template")
            else _sukabumi_managed_columns(columns)
        )
        updates = [
            {
                "range": rowcol_to_a1(row_number, column),
                "values": [[original_values[column - 1] if column <= len(original_values) else ""]],
            }
            for column in managed_columns
            if column > 0
        ]
        if updates:
            worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        return
    worksheet.update(
        f"{rowcol_to_a1(row_number, 1)}:{rowcol_to_a1(row_number, len(original_values))}",
        [original_values],
        value_input_option="USER_ENTERED",
    )


def _move_record_between_sheets(
    record_id: int,
    record: dict,
    identity: dict,
    db_path: str | Path,
) -> bool:
    """Move one edited record with verify-before-clear and compensation."""
    if is_inbound_read_only(record) or is_inbound_read_only(identity):
        raise ValueError("Inbound-only source read-only; pindah row diblok")
    old_spreadsheet_id = normalize_text(identity.get("source_spreadsheet_id"))
    old_sheet_name = str(identity.get("sheet_name") or "")
    old_source_row = int(identity.get("source_row", 0) or 0)
    if not old_spreadsheet_id or not old_sheet_name.strip() or old_source_row < 1:
        raise ValueError("Source identity lengkap wajib untuk memindahkan FDT antar-sheet")
    if old_spreadsheet_id == TANGERANG_FDT_SPREADSHEET_ID:
        raise ValueError(
            "Pindah OLT/unit Tangerang belum didukung; edit data tanpa mengganti OLT/unit."
        )
    if _is_tangerang_sheet_identity(old_spreadsheet_id, old_sheet_name):
        raise ValueError("Spreadsheet Tangerang read-only; pindah row diblok")

    target_source = _source_for_target_record(record)
    target_sheet_name = _target_sheet_name_for_record(record, target_source)
    if _is_tangerang_sheet_identity(target_source.spreadsheet_id, target_sheet_name):
        raise ValueError("Spreadsheet Tangerang read-only; pindah row diblok")
    source_client = _build_gspread_client(old_spreadsheet_id)

    source_spreadsheet = source_client.open_by_key(old_spreadsheet_id)
    source_title = _find_case_insensitive_sheet(source_spreadsheet, old_sheet_name)
    if not source_title:
        raise ValueError(f"Sheet sumber tidak ditemukan: {old_sheet_name}")
    source_worksheet = source_spreadsheet.worksheet(source_title)
    source_layout = _find_header_layout_for_sheet(source_worksheet)
    if not source_layout:
        raise ValueError(f"Header FDT tidak ditemukan di sheet sumber: {source_title}")
    _source_header_row, source_columns = source_layout
    source_values = source_worksheet.get_values(
        value_render_option="FORMATTED_VALUE", major_dimension="ROWS"
    )
    source_formulas = source_worksheet.get_values(
        value_render_option="FORMULA", major_dimension="ROWS"
    )
    if old_source_row > len(source_values):
        raise ValueError(
            f"Source identity mismatch; record {record_id} row {old_source_row} di luar sheet {source_title}"
        )
    source_row_values = source_values[old_source_row - 1]
    source_row_formulas = (
        source_formulas[old_source_row - 1]
        if old_source_row <= len(source_formulas)
        else []
    )
    if not _record_matches_physical_row(
        source_row_values,
        source_row_formulas,
        source_columns,
        identity,
        strict=False,
    ):
        raise ValueError(
            f"Source identity mismatch; record {record_id} row {old_source_row} di {source_title}"
        )
    duplicate_source_matches = [
        row_number
        for row_number, row in enumerate(source_values, start=1)
        if row_number != old_source_row
        and _record_matches_physical_row(
            row,
            source_formulas[row_number - 1] if row_number <= len(source_formulas) else [],
            source_columns,
            identity,
            strict=False,
        )
    ]
    if duplicate_source_matches:
        raise ValueError(
            f"Baris sumber ambigu untuk record {record_id}: {[old_source_row, *duplicate_source_matches]}"
        )
    source_row = old_source_row
    source_original_values = (
        source_formulas[source_row - 1]
        if source_row <= len(source_formulas) and source_formulas[source_row - 1]
        else source_values[source_row - 1]
    )

    target_client = (
        source_client
        if target_source.spreadsheet_id == old_spreadsheet_id
        else _build_gspread_client(target_source.spreadsheet_id)
    )
    target_spreadsheet = target_client.open_by_key(target_source.spreadsheet_id)
    if target_source.spreadsheet_id == LEGACY_FDT_SPREADSHEET_ID:
        target_worksheet = _worksheet_or_none(target_spreadsheet, target_sheet_name)
        if target_worksheet is None:
            raise ValueError(f"Sheet legacy tujuan tidak ditemukan: {target_sheet_name}")
    else:
        target_worksheet = _find_target_worksheet(target_spreadsheet, target_sheet_name)
        if target_worksheet is None:
            raise ValueError(f"Sheet target tidak ditemukan: {target_sheet_name}")
    target_title = target_worksheet.title
    target_layout = _find_header_layout_for_sheet(target_worksheet)
    if not target_layout:
        raise ValueError(f"Header FDT tidak ditemukan di sheet tujuan: {target_title}")
    target_header_row, target_columns = target_layout
    target_values = target_worksheet.get_values(
        value_render_option="FORMATTED_VALUE", major_dimension="ROWS"
    )
    target_formulas = target_worksheet.get_values(
        value_render_option="FORMULA", major_dimension="ROWS"
    )
    same_worksheet = (
        old_spreadsheet_id == target_source.spreadsheet_id
        and source_title == target_title
    )
    target_matches = [
        row_number
        for row_number, row in enumerate(target_values, start=1)
        if not (same_worksheet and row_number == source_row)
        and _record_matches_target_sheet_row(
            row,
            target_formulas[row_number - 1] if row_number <= len(target_formulas) else [],
            target_columns,
            record,
            strict=False,
        )
    ]
    if len(target_matches) > 1:
        raise ValueError(f"Baris target ambigu untuk record {record_id}: {target_matches}")

    target_row = target_matches[0] if target_matches else 0
    target_created = False
    target_width = 0
    source_cleared = False
    try:
        if not target_row:
            target_created = True
            if target_columns.get("legacy_template"):
                target_row = _append_legacy_formula_row(
                    target_worksheet, target_header_row, target_columns, record
                )
            else:
                target_row = _next_append_row(target_worksheet, target_header_row, target_columns)
                _outbound_update_row(target_worksheet, target_header_row, target_columns, target_row, record)

        target_values_after = target_worksheet.get_values(
            value_render_option="FORMATTED_VALUE", major_dimension="ROWS"
        )
        target_formulas_after = target_worksheet.get_values(
            value_render_option="FORMULA", major_dimension="ROWS"
        )
        if target_row > len(target_values_after) or not _record_matches_target_sheet_row(
            target_values_after[target_row - 1],
            target_formulas_after[target_row - 1] if target_row <= len(target_formulas_after) else [],
            target_columns,
            record,
        ):
            raise RuntimeError(f"Verifikasi write target gagal: {target_title} row {target_row}")
        # A newly appended target row has no prior business data; on failure it
        # must be blanked, not restored from its post-write contents.

        # Regular-sheet rows are blanked in place. Deleting them would shift the
        # immutable source_row identity of every later DB record in that sheet.
        _clear_record_row_in_place(
            source_worksheet,
            source_columns,
            source_row,
            width=len(source_values[source_row - 1]),
            formula_values=source_formulas[source_row - 1] if source_row <= len(source_formulas) else (),
        )
        source_cleared = True
        source_values_after = source_worksheet.get_values(
            value_render_option="FORMATTED_VALUE", major_dimension="ROWS"
        )
        if source_row <= len(source_values_after) and _record_matches_sheet_row(
            source_values_after[source_row - 1],
            source_columns,
            identity,
        ):
            raise RuntimeError(f"Verifikasi clear source gagal: {source_title} row {source_row}")

        updated = fdt_coding_db.set_record_source_metadata(
            db_path,
            record_id,
            target_source.spreadsheet_id,
            target_title,
            target_row,
        )
        if not updated:
            raise RuntimeError(f"Metadata source FDT gagal diperbarui: {record_id}")
        logger.info(
            "Outbound move: record %d '%s' row %d -> '%s' row %d.",
            record_id,
            source_title,
            source_row,
            target_title,
            target_row,
        )
        return True
    except Exception:
        # Compensate both external sides. This keeps a failed move from leaving
        # the DB, source sheet, and target sheet in three different states.
        if source_cleared:
            try:
                _restore_record_row_values(
                    source_worksheet, source_columns, source_row, source_original_values
                )
            except Exception:
                logger.exception("Outbound move restore source gagal untuk record %d.", record_id)
        if target_created and target_row > 0:
            try:
                _clear_record_row_in_place(target_worksheet, target_columns, target_row)
            except Exception:
                logger.exception("Outbound move cleanup target gagal untuk record %d.", record_id)
        raise


def _find_header_layout_for_sheet(worksheet: gspread.Worksheet, max_header_scan: int = 25) -> tuple[int, dict[str, int]] | None:
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Inbound-only worksheet read-only; outbound read diblok")
    rows = worksheet.get_values(
        value_render_option="FORMATTED_VALUE",
        major_dimension="ROWS",
    )
    for row_index, row in enumerate(rows[:max_header_scan], start=1):
        columns = _legacy_header_columns(row) or find_header_columns(row)
        if columns:
            if _is_sukabumi_worksheet(worksheet):
                columns["sukabumi_template"] = 1
            return row_index, columns
    return None


def _default_source_for_record(db_path: Path, record: dict) -> tuple[str, str]:
    sheet_name = normalize_text(record.get("sheet_name") or record.get("olt_initial")).upper()
    spreadsheet_id = normalize_text(record.get("source_spreadsheet_id"))
    if spreadsheet_id and sheet_name:
        return spreadsheet_id, sheet_name

    legacy_details = _legacy_hub_details(record.get("hub_id"))
    if legacy_details:
        return LEGACY_FDT_SPREADSHEET_ID, legacy_details[4]

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
    area = normalize_text(record.get("area")).upper()
    if hub_id or area:
        source = _source_for_target_record(record)
        return source.spreadsheet_id, sheet_name or olt_initial

    raise ValueError("FDT record tidak punya source spreadsheet, area, atau OLT ID yang valid")


def _find_case_insensitive_sheet(spreadsheet, sheet_name: str) -> str | None:
    """Prefer exact physical title; fall back for legacy normalized metadata."""
    for ws in spreadsheet.worksheets():
        if ws.title == sheet_name:
            return ws.title
    normalized = normalize_text(sheet_name).upper()
    for ws in spreadsheet.worksheets():
        if normalize_text(ws.title).upper() == normalized:
            return ws.title
    return None

def _ensure_worksheet(spreadsheet, sheet_name: str) -> gspread.Worksheet:
    if _is_tangerang_sheet_identity(getattr(spreadsheet, "id", ""), sheet_name):
        raise ValueError("Spreadsheet Tangerang read-only; create worksheet diblok")
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        if _is_tangerang_worksheet(worksheet):
            raise ValueError("Inbound-only worksheet read-only; returned identity mismatch")
        return worksheet
    except gspread.exceptions.WorksheetNotFound:
        actual_title = _find_case_insensitive_sheet(spreadsheet, sheet_name)
        if actual_title:
            worksheet = spreadsheet.worksheet(actual_title)
            if _is_tangerang_worksheet(worksheet):
                raise ValueError("Inbound-only worksheet read-only; returned identity mismatch")
            return worksheet
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=12)
        if _is_tangerang_worksheet(worksheet):
            raise ValueError("Inbound-only worksheet read-only; returned identity mismatch")
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
    if columns.get("sukabumi_template"):
        formulas = worksheet.get_values(value_render_option="FORMULA", major_dimension="ROWS")
        fdt_code_col = columns.get("fdt_code", 0)
        managed_columns = _sukabumi_managed_columns(columns)
        # Rows 13-16 are template metadata/examples; data starts five rows below header.
        for row_number in range(header_row + 5, max(len(values), len(formulas)) + 1):
            row = values[row_number - 1] if row_number <= len(values) else []
            formula_row = formulas[row_number - 1] if row_number <= len(formulas) else []
            managed_empty = all(
                not normalize_text(row[column - 1] if column <= len(row) else "")
                for column in managed_columns
            )
            formula_cells = (
                formula_row[fdt_code_col - 1] if fdt_code_col and fdt_code_col <= len(formula_row) else "",
                formula_row[fdt_code_col] if fdt_code_col and fdt_code_col < len(formula_row) else "",
            )
            if managed_empty and all(isinstance(value, str) and value.startswith("=") for value in formula_cells):
                return row_number
        raise ValueError(f"Slot formula kosong Sukabumi tidak tersedia di sheet '{worksheet.title}'.")
    watched_cols = [col for key, col in columns.items() if key != "sukabumi_template" and col and col > 0]
    last_row = header_row
    for row_index, row in enumerate(values, start=1):
        if row_index <= header_row:
            continue
        if any(col <= len(row) and normalize_text(row[col - 1]) for col in watched_cols):
            last_row = row_index
    return last_row + 1

@synchronized_sync_operation
def sync_outbound_create_record(record_id: int, db_path: str | Path | None = None) -> bool:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    try:
        record = fdt_coding_db.get_record(db_path, record_id)
        if record is None:
            logger.warning("Outbound sync create: record %d tidak ditemukan.", record_id)
            return False
        if is_inbound_read_only(record):
            logger.warning("Outbound sync create: record %d Tangerang read-only, diblok.", record_id)
            return False

        with fdt_coding_db.read_connect(db_path) as check:
            fdt_coding_db.reject_tangerang_write(record, check)
        if normalize_text(record.get("source_spreadsheet_id")) and normalize_text(record.get("sheet_name")) and int(record.get("source_row", 0) or 0) > 0:
            return sync_outbound_update_record(record_id, db_path=db_path)

        spreadsheet_id, sheet_name = _default_source_for_record(db_path, record)
        if not spreadsheet_id or not sheet_name:
            logger.warning("Outbound sync create: record %d tidak punya target spreadsheet/sheet.", record_id)
            return False

        last_exc = None
        for _attempt in range(3):
            try:
                gs_client = _build_gspread_client(spreadsheet_id)
                spreadsheet = gs_client.open_by_key(spreadsheet_id)
                worksheet = _ensure_worksheet(spreadsheet, sheet_name)
                layout = _find_header_layout_for_sheet(worksheet)
                if not layout:
                    logger.warning("Outbound sync create: header tidak ditemukan di sheet '%s'.", sheet_name)
                    return False

                header_row, columns = layout
                data_row = 0
                wrote_row = False
                try:
                    if columns.get("legacy_template"):
                        data_row = _append_legacy_formula_row(worksheet, header_row, columns, record)
                    else:
                        data_row = _next_append_row(worksheet, header_row, columns)
                        _outbound_update_row(worksheet, header_row, columns, data_row, record)
                    wrote_row = True

                    values_after = worksheet.get_values(
                        value_render_option="FORMATTED_VALUE", major_dimension="ROWS"
                    )
                    formulas_after = worksheet.get_values(
                        value_render_option="FORMULA", major_dimension="ROWS"
                    )
                    if data_row > len(values_after) or not _record_matches_target_sheet_row(
                        values_after[data_row - 1],
                        formulas_after[data_row - 1] if data_row <= len(formulas_after) else [],
                        columns,
                        record,
                        strict=False,
                    ):
                        raise RuntimeError(f"Verifikasi write target gagal: {sheet_name} row {data_row}")

                    updated = fdt_coding_db.set_record_source_metadata(
                        db_path, record_id, spreadsheet_id, worksheet.title, data_row
                    )
                    if not updated:
                        raise RuntimeError(f"Metadata source FDT gagal diperbarui: {record_id}")
                except Exception:
                    if wrote_row and data_row > header_row:
                        try:
                            _clear_record_row_in_place(worksheet, columns, data_row)
                        except Exception:
                            logger.exception("Outbound create cleanup target gagal untuk record %d.", record_id)
                    raise
                logger.info("Outbound sync create: record %d -> sheet '%s' row %d.", record_id, worksheet.title, data_row)
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

def _sharing_sukabumi_slot(worksheet: gspread.Worksheet, header_row: int,
                           columns: dict[str, int], values: list[list[str]],
                           formulas: list[list[str]]) -> int:
    """Use a blank tail row; DB owns Sharing OLT, not Sheet metadata B16/D16."""
    if header_row != 12 or columns.get("hub_id") != 4 or columns.get("fdt_code") not in (8, 9):
        raise ValueError("Layout Sukabumi berubah; sharing dibatalkan")
    target = max(len(values), len(formulas)) + 1
    if target > worksheet.row_count:
        raise ValueError("Baris kosong Sukabumi habis; sharing dibatalkan")
    return target


def _sharing_business_row_matches(row: list[str], columns: dict[str, int], record: dict,
                                  index_col: int = 0) -> bool:
    """Same business identity before write and on read-back; OLT is DB-owned."""
    if not _record_matches_sheet_row(row, columns, record):
        return False
    for key, field in (("cluster", "cluster_initial"), ("segment", "index_code")):
        col = int(columns.get(key, 0) or 0)
        if col and normalize_text(row[col - 1] if col <= len(row) else "").upper() != normalize_text(record[field]).upper():
            return False
    if index_col and not columns.get("legacy_template"):
        index = normalize_text(row[index_col - 1] if index_col <= len(row) else "").upper()
        if re.match(r"^[A-Z]{2,}\.\d{3}\.\d{4}\.", index):
            return index.split(".", 3)[-1] in (
                normalize_text(record["fdt_code"]).upper(), format_excel_identifier(record["fdt_id"]))
        return index == normalize_text(record["index_code"]).upper()
    return True


class SharedFdtSyncError(RuntimeError):
    def __init__(self, message: str, *, reconciliation_required: bool = False):
        super().__init__(message)
        self.reconciliation_required = reconciliation_required


@synchronized_sync_operation
def sync_outbound_create_shared_record(record_id: int, source_id: int, db_path: str | Path | None = None) -> bool:
    """Append a new cluster to the selected FDT's exact physical worksheet.

    No generic create here: regular sheets own formula cells that RAW writes destroy.
    A write with an ambiguous outcome keeps the DB row for explicit reconciliation.
    """
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    write_started = False
    try:
        record = fdt_coding_db.get_record(db_path, record_id)
        source = fdt_coding_db.get_record(db_path, source_id)
        if not record or not source:
            raise ValueError("Record sharing atau sumber FDT tidak ditemukan")
        if is_inbound_read_only(record) or is_inbound_read_only(source):
            raise ValueError("FDT Tangerang read-only")
        if (
            source["area"], source["olt_initial"], normalize_text(source["hub_id"]).upper(),
            normalize_text(source["olt_hostname"]).upper(),
            source["fdt_id"], source["fdt_code"], source["sheet_name"], source["olt_name_initial"],
            source["index_code"], source["cluster_initial"], source["fdt_order"],
        ) != (
            record["area"], record["olt_initial"], normalize_text(record["hub_id"]).upper(),
            normalize_text(record["olt_hostname"]).upper(),
            record["fdt_id"], record["fdt_code"], record["sheet_name"], record["olt_name_initial"],
            record["index_code"], record["cluster_initial"], record["fdt_order"],
        ) or record["source_spreadsheet_id"] or record["source_row"]:
            raise ValueError("Identitas sharing FDT berubah; simpan dibatalkan")
        source_book = normalize_text(source["source_spreadsheet_id"])
        source_tab = str(source["sheet_name"] or "")  # Physical title; spaces are significant.
        source_row = int(source["source_row"] or 0)
        if not source_book or not source_tab.strip() or source_row < 1:
            raise ValueError("Lokasi FDT sumber tidak valid")
        book = _build_gspread_client(source_book).open_by_key(source_book)
        worksheet = book.worksheet(source_tab)
        if (worksheet.title != source_tab or getattr(worksheet, "spreadsheet_id", None) != source_book
                or _is_tangerang_worksheet(worksheet)):
            raise ValueError("Worksheet FDT sumber tidak sesuai")
        layout = _find_header_layout_for_sheet(worksheet)
        if not layout:
            raise ValueError("Header worksheet FDT sumber tidak ditemukan")
        header_row, columns = layout
        if source_row <= header_row:
            raise ValueError("Baris FDT sumber berada di header")
        before_values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")
        before_formulas = worksheet.get_values(value_render_option="FORMULA", major_dimension="ROWS")
        physical = before_values[source_row - 1] if source_row <= len(before_values) else []
        physical_formulas = before_formulas[source_row - 1] if source_row <= len(before_formulas) else []
        if not _record_matches_sheet_row(physical, columns, source):
            raise ValueError("Baris FDT sumber berubah; refresh Coding FDT sebelum mencoba lagi")
        # Canonical OLT labels can differ from the physical tab's old initial.
        # Copy raw source cells/formulas, never write the canonicalized label back.
        hub_col = columns.get("hub_id") or columns.get("olt_code") or 0
        actual_hub = normalize_text(physical[hub_col-1] if hub_col and hub_col <= len(physical) else "").upper()
        index_col = columns.get("index", 0)
        actual_index = normalize_text(physical[index_col-1] if index_col and index_col <= len(physical) else "").upper()
        # Some Jakarta tabs call column J "Coding Number" so the generic
        # header parser misses it. Locate the exact saved index on that row.
        if not index_col and source.get("index_code"):
            for col, value in enumerate(physical, 1):
                if normalize_text(value).upper() == normalize_text(source["index_code"]).upper():
                    index_col, actual_index = col, normalize_text(value).upper()
                    break
        if not index_col and columns.get("fdt_code") and not columns.get("legacy_template"):
            candidate_col = columns["fdt_code"] + 1
            candidate = normalize_text(physical[candidate_col-1] if candidate_col <= len(physical) else "").upper()
            if re.match(r"^[A-Z]{2,}\.\d{3}\.\d{4}\.", candidate):
                index_col, actual_index = candidate_col, candidate
        if not _sharing_business_row_matches(physical, columns, source, index_col):
            raise ValueError("Identitas bisnis FDT sumber berubah; sharing dibatalkan")
        # Selected DB record owns the OLT tuple; Sheet reads only protect row/formulas.
        target_row = (_sharing_sukabumi_slot(
            worksheet, header_row, columns, before_values, before_formulas,
        ) if columns.get("sukabumi_template") else
            max(len(before_values), len(before_formulas)) + 1 if columns.get("legacy_template") else
            _next_append_row(worksheet, header_row, columns))
        if target_row > worksheet.row_count:
            raise ValueError("Slot worksheet habis; tambah baris sebelum sharing")
        with fdt_coding_db.read_connect(db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM fdt_coding_records WHERE source_spreadsheet_id=? AND sheet_name=? AND source_row IN (?, ?) LIMIT 1",
                (source_book, source_tab, target_row, -target_row),
            ).fetchone()
        if existing:
            raise ValueError("Baris tujuan sudah punya locator DB; sharing dibatalkan")
        target_values = before_values[target_row - 1] if target_row <= len(before_values) else []
        target_formulas = before_formulas[target_row - 1] if target_row <= len(before_formulas) else []
        # Reserve the physical locator durably before any external write. A
        # negative row means pending, not an imported/successful source row.
        # Inbound will reconcile the exact row or stop, never duplicate it.
        def reserve_target() -> None:
            if not fdt_coding_db.set_record_source_metadata(db_path, record_id, source_book, source_tab, -target_row):
                raise RuntimeError("Locator pending sharing FDT tidak tersimpan")
        if columns.get("legacy_template") and target_row > header_row:
            # An Indramayu shared tab can have several hubs; clone the exact
            # source row's formulas, then restore only its operator-owned cells.
            if any(normalize_text(value) for value in target_values + target_formulas):
                raise ValueError("Baris tujuan tidak kosong; sharing dibatalkan")
            site_col, code_col, segment_col = (columns.get(key, 0) for key in ("site", "fdt_code", "segment"))
            if not site_col or not code_col or not segment_col:
                raise ValueError("Template legacy tidak punya kolom sharing lengkap")
            if any(int(col) > worksheet.col_count for col in (site_col, code_col, segment_col)):
                raise ValueError("Lebar template legacy tidak sesuai")
            last_col = int(columns.get("legacy_last_column") or len(physical_formulas))
            # D is a literal source-owned dependency for H/J in this workbook.
            dependency_col = 4
            dependency = normalize_text(physical[dependency_col - 1] if len(physical) >= dependency_col else "")
            if (dependency != normalize_text(record["fdt_code"]) or
                    (len(physical_formulas) >= dependency_col and
                     str(physical_formulas[dependency_col - 1]).startswith("="))):
                raise ValueError("Literal FDT legacy D tidak sesuai; sharing dibatalkan")
            prior_formulas = {col: value for col, value in enumerate(physical_formulas, 1)
                              if isinstance(value, str) and value.startswith("=")}
            write_started = True
            reserve_target()
            worksheet.copy_range(
                f"A{source_row}:{rowcol_to_a1(source_row, last_col)}",
                f"A{target_row}:{rowcol_to_a1(target_row, last_col)}", paste_type="PASTE_FORMULA"
            )
            worksheet.batch_update([
                {"range": rowcol_to_a1(target_row, col), "values": [[value]]}
                for col, value in ((site_col, record["site_name"]), (code_col, record["fdt_code"]),
                                   (dependency_col, dependency), (segment_col, record["index_code"]))
            ], value_input_option="RAW")
        elif columns.get("sukabumi_template"):
            # A Sukabumi slot already contains formulas; write only its 3 inputs.
            managed = _sukabumi_managed_columns(columns)
            if any(normalize_text(target_values[col-1] if col <= len(target_values) else "") for col in managed):
                raise ValueError("Slot formula Sukabumi tidak kosong")
            source_formulas = {col: value for col, value in enumerate(physical_formulas, 1)
                               if isinstance(value, str) and value.startswith("=")}
            if not source_formulas or any(col in managed for col in source_formulas):
                raise ValueError("Formula sumber Sukabumi tidak dapat disalin aman")
            prior_formulas = source_formulas
            write_started = True
            reserve_target()
            # Copy only formula-owned cells. B/F/G are the three operator inputs.
            for col in sorted(source_formulas):
                worksheet.copy_range(rowcol_to_a1(source_row, col), rowcol_to_a1(target_row, col),
                                     paste_type="PASTE_NORMAL")
            _outbound_update_row(worksheet, header_row, columns, target_row, record)
        else:
            # A blank tail row may be cloned; never overwrite an occupied/formula row.
            if any(normalize_text(value) for value in target_values + target_formulas):
                raise ValueError("Baris tujuan tidak kosong; sharing dibatalkan")
            site_column = columns.get("site", 0)
            if site_column < 1 or (site_column <= len(physical_formulas)
                    and str(physical_formulas[site_column - 1]).startswith("=")):
                raise ValueError("Kolom nama cluster sumber memakai formula")
            last_col = max(len(physical_formulas), len(physical), max(columns.values()))
            if last_col > worksheet.col_count:
                raise ValueError("Lebar template worksheet tidak sesuai")
            prior_formulas = {col: value for col, value in enumerate(physical_formulas, 1)
                              if isinstance(value, str) and value.startswith("=")}
            literal_cols = {int(columns[key]) for key in (
                "site", "fdt_no", "fdt_code", "index", "cluster", "olt_initial", "hub_id", "olt_hostname", "segment"
            ) if int(columns.get(key, 0) or 0) > 0}
            if index_col:
                literal_cols.add(index_col)  # Jakarta's full index has a nonstandard header.
            if site_column in prior_formulas:
                raise ValueError("Nama cluster dari formula tidak dapat diubah")
            if not prior_formulas and (not columns.get("fdt_code") or not index_col):
                raise ValueError("Template literal tidak punya FDT code/unit yang terverifikasi")
            write_started = True
            reserve_target()
            if prior_formulas:
                worksheet.copy_range(
                    f"A{source_row}:{rowcol_to_a1(source_row, last_col)}",
                    f"A{target_row}:{rowcol_to_a1(target_row, last_col)}",
                    paste_type="PASTE_FORMULA",
                )
            # Only FDT-owned literals: do not clone the old row's No./status cells.
            worksheet.batch_update([
                {"range": rowcol_to_a1(target_row, col), "values": [[
                    record["site_name"] if col == site_column else
                    (physical[col-1] if col <= len(physical) else "")
                ]]}
                for col in sorted(literal_cols) if col not in prior_formulas
            ], value_input_option="RAW")
        after_values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")
        after_formulas = worksheet.get_values(value_render_option="FORMULA", major_dimension="ROWS")
        target = after_values[target_row - 1] if target_row <= len(after_values) else []
        target_formula = after_formulas[target_row - 1] if target_row <= len(after_formulas) else []
        if not _sharing_business_row_matches(target, columns, record, index_col):
            raise RuntimeError("Read-back sharing FDT tidak sesuai sumber yang dipilih")
        actual_target_hub = normalize_text(target[hub_col-1] if hub_col and hub_col <= len(target) else "").upper()
        actual_target_index = normalize_text(target[index_col-1] if index_col and index_col <= len(target) else "").upper()
        if ((hub_col and not columns.get("sukabumi_template") and actual_target_hub != actual_hub)
                or (index_col and actual_index and actual_target_index != actual_index)):
            raise RuntimeError("Unit OLT hasil sharing berubah")
        from openpyxl.formula.translate import Translator
        if any(col > len(target_formula) or target_formula[col-1] != Translator(
                formula, origin=rowcol_to_a1(source_row, col)).translate_formula(rowcol_to_a1(target_row, col))
               for col, formula in prior_formulas.items()):
            raise RuntimeError("Formula template sharing FDT berubah; perlu rekonsiliasi")
        if columns.get("legacy_template"):
            if normalize_text(target[3] if len(target) > 3 else "") != dependency:
                raise RuntimeError("Dependensi literal FDT legacy tidak tersalin")
            for col in range(1, min(last_col, len(physical)) + 1):
                if col in prior_formulas or col in (site_col, code_col, segment_col):
                    continue
                if normalize_text(physical[col - 1]) != normalize_text(target[col - 1] if col <= len(target) else ""):
                    raise RuntimeError("Hasil formula legacy berbeda dari FDT sumber")
        actual_source = after_values[source_row - 1] if source_row <= len(after_values) else []
        actual_source_formula = after_formulas[source_row - 1] if source_row <= len(after_formulas) else []
        if physical != actual_source or physical_formulas != actual_source_formula:
            raise RuntimeError("Baris FDT asli berubah saat sharing; perlu rekonsiliasi")
        if not fdt_coding_db.set_record_source_metadata(db_path, record_id, source_book, source_tab, target_row):
            raise RuntimeError("Metadata baris sharing tidak tersimpan")
        logger.info("Sharing FDT record %d from %d -> %s row %d", record_id, source_id, source_tab, target_row)
        return True
    except Exception as exc:
        logger.exception("Sharing FDT record %s gagal (write_started=%s)", record_id, write_started)
        raise SharedFdtSyncError(str(exc), reconciliation_required=write_started) from exc


@synchronized_sync_operation
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


@synchronized_sync_operation
def sync_outbound_delete_records(record_snapshots: dict, db_path: str | Path | None = None) -> dict:
    """Batch delete with delay between records to avoid 429 quota."""
    results: dict = {}
    for _idx, (record_id, snapshot) in enumerate(record_snapshots.items()):
        if _idx > 0:
            time.sleep(5)
        results[record_id] = sync_outbound_delete_record(record_id, db_path=db_path, record_snapshot=snapshot)
    return results

@synchronized_sync_operation
def sync_outbound_update_record(record_id: int, db_path: str | Path | None = None, record_snapshot: dict | None = None) -> bool:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    try:
        record = fdt_coding_db.get_record(db_path, record_id)
        identity = record_snapshot or record
        if record is None:
            logger.warning("Outbound sync: record %d tidak ditemukan.", record_id)
            return False
        if is_inbound_read_only(record) or is_inbound_read_only(identity):
            logger.warning("Outbound sync update: record %d Tangerang read-only, diblok.", record_id)
            return False

        with fdt_coding_db.read_connect(db_path) as check:
            fdt_coding_db.reject_tangerang_write(record, check)
            fdt_coding_db.reject_tangerang_write(identity, check)
        old_spreadsheet_id = normalize_text(identity.get("source_spreadsheet_id"))
        old_sheet_name = str(identity.get("sheet_name") or "")
        old_source_row = int(identity.get("source_row", 0) or 0)
        if not old_spreadsheet_id or not old_sheet_name.strip() or old_source_row < 1:
            logger.info("Outbound sync: record %d tidak punya source metadata, skip.", record_id)
            return False

        if record_requires_sheet_move(record, identity):
            return _move_record_between_sheets(record_id, record, identity, db_path)

        gs_client = _build_gspread_client(old_spreadsheet_id)
        spreadsheet = gs_client.open_by_key(old_spreadsheet_id)
        ws_title = _find_case_insensitive_sheet(spreadsheet, old_sheet_name) or old_sheet_name
        worksheet = spreadsheet.worksheet(ws_title)
        layout = _find_header_layout_for_sheet(worksheet)
        if not layout:
            logger.warning("Outbound sync: header tidak ditemukan di sheet '%s'.", old_sheet_name)
            return False
        header_row, columns = layout
        data_row = old_source_row
        if data_row <= header_row:
            logger.warning("Outbound sync: source_row %d <= header_row %d, skip.", data_row, header_row)
            return False
        values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")
        if data_row > len(values) or not _record_matches_sheet_row(values[data_row - 1], columns, identity):
            logger.error(
                "Outbound sync: source identity mismatch; record %d sheet '%s' row %d not updated.",
                record_id, ws_title, data_row,
            )
            return False
        _outbound_update_row(worksheet, header_row, columns, data_row, record)
        logger.info("Outbound sync: record %d -> sheet '%s' row %d.", record_id, ws_title, data_row)
        return True

    except gspread.exceptions.WorksheetNotFound:
        logger.warning("Outbound sync: worksheet '%s' tidak ditemukan.", locals().get("old_sheet_name", ""))
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
    if not row_fdt and not fdt_col:
        suffix_match = re.search(r"\.([0-9]{3})$", row_code)
        row_fdt = format_excel_identifier(suffix_match.group(1)) if suffix_match else ""
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

def _clear_record_row(worksheet: gspread.Worksheet, columns: dict[str, int], row_number: int) -> None:
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Spreadsheet Tangerang read-only; outbound delete diblok")
    if columns.get("legacy_template"):
        # Legacy rows carry formula columns A,D,F:J. Clear user-managed B/C/E only.
        updates = [
            {"range": rowcol_to_a1(row_number, column), "values": [[""]]}
            for column in (columns.get("site", 0), columns.get("fdt_code", 0), columns.get("segment", 0))
            if column > 0
        ]
        if updates:
            worksheet.batch_update(updates, value_input_option="RAW")
        return
    if _is_sukabumi_worksheet(worksheet) or _is_tangerang_worksheet(worksheet):
        updates = [
            {"range": rowcol_to_a1(row_number, column), "values": [[""]]}
            for column in _sukabumi_managed_columns(columns)
        ]
        if updates:
            worksheet.batch_update(updates, value_input_option="RAW")
        return
    worksheet.delete_rows(row_number)


def _clear_record_row_in_place(
    worksheet: gspread.Worksheet,
    columns: dict[str, int],
    row_number: int,
    width: int = 0,
    formula_values: Sequence[object] | None = None,
) -> None:
    """Blank data cells without shifting rows or destroying template formulas."""
    if _is_tangerang_worksheet(worksheet):
        raise ValueError("Spreadsheet Tangerang read-only; clear row diblok")
    if columns.get("legacy_template") or _is_sukabumi_worksheet(worksheet):
        _clear_record_row(worksheet, columns, row_number)
        return
    mapped_columns = {
        int(value)
        for key, value in columns.items()
        if key in {
            "site", "fdt_no", "fdt_code", "index", "cluster",
            "olt_initial", "hub_id", "olt_hostname",
        }
        and value and int(value) > 0
    }
    if width > 0:
        mapped_columns.update(range(1, width + 1))
    formula_columns = {
        index
        for index, value in enumerate(formula_values or (), start=1)
        if isinstance(value, str) and value.startswith("=")
    }
    updates = [
        {
            "range": rowcol_to_a1(row_number, column),
            "values": [[""]],
        }
        for column in sorted(mapped_columns)
        if column not in formula_columns
    ]
    if updates:
        worksheet.batch_update(updates, value_input_option="RAW")


def _clear_record_rows(worksheet: gspread.Worksheet, columns: dict[str, int], record: dict, preferred_row: int = 0) -> list[int]:
    if _is_tangerang_worksheet(worksheet) or is_inbound_read_only(record):
        raise ValueError("Spreadsheet Tangerang read-only; outbound delete diblok")
    values = worksheet.get_values(value_render_option="FORMATTED_VALUE", major_dimension="ROWS")

    # Prefer source_row only when the row content still matches the record.
    if preferred_row > 0 and preferred_row <= len(values):
        preferred_values = values[preferred_row - 1]
        if _record_matches_sheet_row(preferred_values, columns, record):
            _clear_record_row(worksheet, columns, preferred_row)
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
        _clear_record_row(worksheet, columns, matches[0])
        return matches
    if len(matches) > 1:
        logger.error(
            "Outbound sync delete: ambiguous matches for record %s in sheet '%s': rows %s. No rows cleared.",
            record.get("id", ""),
            record.get("sheet_name", ""),
            matches,
        )
    return []

@synchronized_sync_operation
def sync_outbound_delete_record(record_id: int, db_path: str | Path | None = None, record_snapshot: dict | None = None) -> bool:
    from config import Config

    db_path = Path(db_path or Config.FDT_CODING_DB_PATH)
    try:
        stored = fdt_coding_db.get_record(db_path, record_id)
        if is_inbound_read_only(stored) or is_inbound_read_only(record_snapshot):
            return False
        record = record_snapshot or stored
        if record is None:
            return False
        if is_inbound_read_only(record):
            logger.warning("Outbound sync delete: record %d Tangerang read-only, diblok.", record_id)
            return False

        with fdt_coding_db.read_connect(db_path) as check:
            fdt_coding_db.reject_tangerang_write(stored, check)
            fdt_coding_db.reject_tangerang_write(record, check)
        spreadsheet_id = normalize_text(record.get("source_spreadsheet_id"))
        sheet_name = str(record.get("sheet_name") or "")
        source_row = int(record.get("source_row", 0))
        if not spreadsheet_id or not sheet_name.strip() or source_row < 1:
            return False

        last_exc = None
        for _attempt in range(3):
            try:
                gs_client = _build_gspread_client(spreadsheet_id)
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
    results = sync_all_incremental(sources=sources, force="--force" in sys.argv)
    elapsed = time.time() - start

    changed_total = sum(v.get("changed", 0) for v in results.values())
    logger.info("Sync complete in %.1fs: %d records changed.", elapsed, changed_total)
    for name, counts in results.items():
        logger.info("  %s: %d changed, %d skipped", name, counts.get("changed", 0), counts.get("skipped", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())


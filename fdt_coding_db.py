from __future__ import annotations

import re
import sqlite3
from contextlib import nullcontext, suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from kmz_standardizer import FdtCodingRecord, format_excel_identifier, normalize_text, parse_fdt_coding_workbook

SCHEMA = """
CREATE TABLE IF NOT EXISTS fdt_coding_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 olt_initial TEXT NOT NULL DEFAULT '',
 hub_id TEXT NOT NULL DEFAULT '',
 olt_name_initial TEXT NOT NULL DEFAULT '',
 olt_hostname TEXT NOT NULL DEFAULT '',
 site_name TEXT NOT NULL,
 area TEXT NOT NULL DEFAULT '',
 fdt_order INTEGER,
 fdt_id TEXT NOT NULL DEFAULT '',
 fdt_code TEXT NOT NULL,
 index_code TEXT NOT NULL DEFAULT '',
 cluster_initial TEXT NOT NULL DEFAULT '',
 source_spreadsheet_id TEXT NOT NULL DEFAULT '',
 sheet_name TEXT NOT NULL DEFAULT '',
 source_row INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fdt_coding_olt ON fdt_coding_records(olt_initial);
CREATE INDEX IF NOT EXISTS idx_fdt_coding_site ON fdt_coding_records(site_name);
CREATE INDEX IF NOT EXISTS idx_fdt_coding_fdt_code ON fdt_coding_records(fdt_code);
CREATE TABLE IF NOT EXISTS fdt_coding_olts (
    olt_initial TEXT PRIMARY KEY,
    hub_id TEXT NOT NULL DEFAULT '',
    olt_name_initial TEXT NOT NULL DEFAULT '',
    olt_hostname TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fdt_coding_olt_catalog (
    olt_code TEXT PRIMARY KEY,
    olt_initial TEXT NOT NULL DEFAULT '',
    olt_name_initial TEXT NOT NULL DEFAULT '',
    olt_hostname TEXT NOT NULL DEFAULT '',
    olt_vendor TEXT NOT NULL DEFAULT '',
    olt_area TEXT NOT NULL DEFAULT '',
    source_row INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fdt_olt_catalog_name ON fdt_coding_olt_catalog(olt_initial);
"""

SOURCE_ROW_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fdt_coding_source_row ON fdt_coding_records(
    source_spreadsheet_id, sheet_name, source_row
) WHERE source_spreadsheet_id <> '' AND source_row > 0
"""

MANUAL_IDENTITY_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fdt_coding_manual_identity ON fdt_coding_records(
    olt_initial, site_name, fdt_id, fdt_code, index_code
) WHERE source_spreadsheet_id = ''
"""

OLT_DB_PATH = Path(__file__).resolve().parent / "data" / "OLT.db"
LEGACY_FDT_SPREADSHEET_ID = "1PNnmDDFgmks0bEkGF4jF8c3Vlv9-hzF3YushyP9xQWs"
SUKABUMI_FDT_SPREADSHEET_ID = "1jBoLTe0psl5G8PoQTLXzRUdS9kpQq7NWah-69Z4CWhQ"
TANGERANG_FDT_SPREADSHEET_ID = "1I5RR2Ow631aRgm9oOBfi5rhOMU-fnMOKGCUXWLSi6U4"
TANGERANG_RETIRED_SPREADSHEET_ID = "1a89l5TmTlZiICSSvvRwCB6lONDOCQgpd"
TANGERANG_PROTECTED_SOURCE_IDS = frozenset({
    TANGERANG_FDT_SPREADSHEET_ID,
    TANGERANG_RETIRED_SPREADSHEET_ID,
})
TANGERANG_FDT_TAB_NAMES = frozenset({
    # Protect the complete workbook identity, including helper tabs.
    "Create Email sending",
    "Tangerang",
    "SMT",
    "CODING POLE MAIN FEEDER",
    "OLT CIPUTAT",
    "OLT PAMULANG",
    "OLT JOMBANG",
    "OLT SMART BSD",
    "OLT JOMBANG RAYA 02",
    "OLT CURUG",
    "OLT SERPONG UTARA",
    "Mini OLT Paragon Village",
    "OLT KELAPA DUA",
    "OLT PASAR KEMIS",
    "OLT RAJEG",
    "OLT TIGA RAKSA",
    "OLT SAWAH LAMA",
    "OLT CILEDUG",
    "OLT MODERNLAND",
    "OLT GUNUNG SINDUR",
    "OLT CISOKA",
    "OLT SAWANGAN",
    "OLT ATMA ASNAWI",
    "OLT BANTAR PANJANG",
    "OLT CISAUK",
    "OLT PARUNG PANJANG",
    "OLT SEPATAN",
    "OLT TELUK NAGA",
    "OLT TENJO",
    "OLT LIPPO KARAWACI UTARA",
    "OLT LIPPO KARAWACI SENTRAL",
    "OLT LIPPO KARAWACI BARAT",
    "OLT PARK SERPONG",
})
TANGERANG_PROTECTED_OLT_KEYS = frozenset(
    normalize_text(name).upper() for name in TANGERANG_FDT_TAB_NAMES
)
def is_tangerang_read_only_record(payload: dict | sqlite3.Row | None) -> bool:
    """Identify all immutable ownership signals for the inbound-only source."""
    data = dict(payload or {})
    source_id = normalize_text(data.get("source_spreadsheet_id"))
    area = normalize_text(data.get("area")).upper()
    hub_id = normalize_text(data.get("hub_id")).upper()
    sheet_name = normalize_text(data.get("sheet_name")).upper()
    olt_initial = normalize_text(data.get("olt_initial")).upper()
    return (
        source_id in TANGERANG_PROTECTED_SOURCE_IDS
        or area == "TANGERANG"
        or hub_id.startswith("TNG.")
        or sheet_name in TANGERANG_PROTECTED_OLT_KEYS
        or olt_initial in TANGERANG_PROTECTED_OLT_KEYS
    )


def reject_tangerang_write(payload: dict | sqlite3.Row | None) -> None:
    if is_tangerang_read_only_record(payload):
        raise ValueError("Area Tangerang read-only; perubahan operator diblok")


def _require_unprotected_source(source_spreadsheet_id: str) -> str:
    """Generic helpers never receive protected write authority.

    Tangerang reconciliation is implemented only inside the validated top-level
    sync transaction; no importable token or low-level bypass exists.
    """
    source_id = normalize_text(source_spreadsheet_id)
    if source_id == TANGERANG_RETIRED_SPREADSHEET_ID:
        raise ValueError("Source Tangerang retired; akses diblok")
    if source_id == TANGERANG_FDT_SPREADSHEET_ID:
        raise ValueError("Area Tangerang read-only; hanya inbound sync terotorisasi")
    return source_id


def _selected_rows(
    conn: sqlite3.Connection,
    where_sql: str,
    params: Iterable[object],
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM fdt_coding_records WHERE {where_sql}",
        list(params),
    ).fetchall()


def _reject_selected_protected_rows(
    conn: sqlite3.Connection,
    where_sql: str,
    params: Iterable[object],
) -> list[sqlite3.Row]:
    rows = _selected_rows(conn, where_sql, params)
    for row in rows:
        reject_tangerang_write(row)
        _reject_pending_sharing(row)
    return rows


def _reject_pending_sharing(row: sqlite3.Row) -> None:
    if int(row['source_row'] or 0) < 0:
        raise ValueError(f"Sharing FDT pending record {row['id']}; perlu rekonsiliasi sebelum mutasi")


def read_connect(db_path: str | Path) -> sqlite3.Connection:
    """Open query helpers read-only; a missing DB gets an ephemeral empty schema."""
    path = Path(db_path)
    if path.exists():
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def attach_olt_catalog_db(conn: sqlite3.Connection) -> None:
    if not OLT_DB_PATH.exists():
        return
    with suppress(sqlite3.OperationalError):
        read_only_uri = f"file:{OLT_DB_PATH.resolve()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS olt_db", (read_only_uri,))


def olt_catalog_source(conn: sqlite3.Connection) -> tuple[str, str]:
    attach_olt_catalog_db(conn)
    attached = conn.execute("PRAGMA database_list").fetchall()
    if any(row[1] == "olt_db" for row in attached):
        exists = conn.execute(
            "SELECT 1 FROM olt_db.sqlite_master WHERE type = 'table' AND name = 'olt_catalog'"
        ).fetchone()
        if exists:
            return "olt_db.olt_catalog", "olt_name"
    return "fdt_coding_olt_catalog", "olt_name_initial"


FIELDS = (
    "olt_initial",
    "hub_id",
    "olt_name_initial",
    "olt_hostname",
    "site_name",
    "area",
    "fdt_order",
    "fdt_id",
    "fdt_code",
    "index_code",
    "cluster_initial",
    "source_spreadsheet_id",
    "sheet_name",
    "source_row",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Serialize ownership checks with their following mutation."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def migrate_identity_indexes(conn: sqlite3.Connection) -> None:
    """Atomically replace global logical uniqueness with physical row identity."""
    index_names = {
        row[1]
        for row in conn.execute("PRAGMA index_list(fdt_coding_records)").fetchall()
    }
    if not (
        "idx_fdt_coding_unique" in index_names
        or "idx_fdt_coding_identity" in index_names
        or "idx_fdt_coding_source_row" not in index_names
        or "idx_fdt_coding_manual_identity" not in index_names
    ):
        return

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        invalid = conn.execute(
            """
            SELECT id FROM fdt_coding_records
            WHERE source_spreadsheet_id <> ''
              AND (TRIM(sheet_name) = '' OR source_row <= 0)
            LIMIT 1
            """
        ).fetchone()
        if invalid:
            raise ValueError(f"Source locator FDT invalid pada record {invalid['id']}")
        conn.execute(SOURCE_ROW_INDEX_SQL)
        conn.execute(MANUAL_IDENTITY_INDEX_SQL)
        conn.execute("DROP INDEX IF EXISTS idx_fdt_coding_unique")
        conn.execute("DROP INDEX IF EXISTS idx_fdt_coding_identity")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fdt_coding_records)").fetchall()}
    if "olt_name_initial" not in columns:
        conn.execute("ALTER TABLE fdt_coding_records ADD COLUMN olt_name_initial TEXT NOT NULL DEFAULT ''")
    if "source_spreadsheet_id" not in columns:
        conn.execute("ALTER TABLE fdt_coding_records ADD COLUMN source_spreadsheet_id TEXT NOT NULL DEFAULT ''")
    if "olt_hostname" not in columns:
        conn.execute("ALTER TABLE fdt_coding_records ADD COLUMN olt_hostname TEXT NOT NULL DEFAULT ''")
    if "area" not in columns:
        conn.execute("ALTER TABLE fdt_coding_records ADD COLUMN area TEXT NOT NULL DEFAULT ''")
    migrate_identity_indexes(conn)
    olt_columns = {row[1] for row in conn.execute("PRAGMA table_info(fdt_coding_olts)").fetchall()}
    if "olt_hostname" not in olt_columns:
        conn.execute("ALTER TABLE fdt_coding_olts ADD COLUMN olt_hostname TEXT NOT NULL DEFAULT ''")
    if "area" not in olt_columns:
        conn.execute("ALTER TABLE fdt_coding_olts ADD COLUMN area TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn



def is_valid_olt_code(value: object) -> bool:
    text = normalize_text(value).upper()
    return bool(re.fullmatch(r"[A-Z]{2,}\.\d{3}\.\d{2,4}(?:\.\d+)?", text))

def initial_from_olt_hostname(value: object) -> str:
    text = normalize_text(value).upper()
    match = re.search(r"^[A-Z]{2,4}-([A-Z0-9]{2,6})-OLT\d+", text)
    return match.group(1) if match else ""

def comparable_olt_name(value: object) -> str:
    text = normalize_text(value).upper()
    text = re.sub(r"^OLT\s+", "", text)
    text = re.sub(r"^MINI\s+OLT\s+", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\b(?:USED|NOT\s+USED|MASUK\s+BEKASI)\b", "", text)
    text = re.sub(r"\b\d+\s*$", "", text)
    return re.sub(r"[^A-Z0-9]+", "", text)

def normalize_record_payload(payload: dict) -> dict:
    fdt_order_value = payload.get("fdt_order")
    try:
        fdt_order = int(fdt_order_value) if str(fdt_order_value or "").strip() else None
    except (TypeError, ValueError):
        fdt_order = None
    source_row_value = payload.get("source_row", payload.get("row_number"))
    try:
        source_row = int(source_row_value) if str(source_row_value or "").strip() else 0
    except (TypeError, ValueError):
        source_row = 0
    return {
        "olt_initial": normalize_text(payload.get("olt_initial")).upper(),
        "hub_id": normalize_text(payload.get("hub_id")),
        "olt_name_initial": normalize_text(payload.get("olt_name_initial")),
        "olt_hostname": normalize_text(payload.get("olt_hostname")),
        "site_name": normalize_text(payload.get("site_name")).upper(),
        "area": normalize_text(payload.get("area")),
        "fdt_order": fdt_order,
        "fdt_id": format_excel_identifier(payload.get("fdt_id")),
        "fdt_code": normalize_text(payload.get("fdt_code")).upper(),
        "index_code": normalize_text(payload.get("index_code")),
        "cluster_initial": normalize_text(payload.get("cluster_initial")),
        "source_spreadsheet_id": normalize_text(payload.get("source_spreadsheet_id")),
        # Worksheet title is physical source identity. Google Sheets allows
        # significant trailing spaces, so preserve it byte-for-byte.
        "sheet_name": str(payload.get("sheet_name") or ""),
        "source_row": source_row,
    }



def validate_payload(payload: dict) -> None:
    if not payload.get("site_name"):
        raise ValueError("site_name wajib diisi")
    if not payload.get("fdt_code"):
        raise ValueError("fdt_code wajib diisi")
    if payload.get("source_spreadsheet_id") and (
        not str(payload.get("sheet_name") or "").strip()
        or int(payload.get("source_row") or 0) < 1
    ):
        raise ValueError("source Sheet wajib punya sheet_name dan source_row > 0")


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def row_to_record(row: sqlite3.Row) -> FdtCodingRecord:
    return FdtCodingRecord(
        sheet_name=row["sheet_name"] or "Database",
        row_number=int(row["source_row"] or row["id"] or 0),
        site_name=row["site_name"] or "",
        area=row["area"] or "",
        fdt_order=row["fdt_order"],
        fdt_id=row["fdt_id"] or "",
        fdt_code=row["fdt_code"] or "",
        index_code=row["index_code"] or "",
        cluster_initial=row["cluster_initial"] or "",
        olt_initial=row["olt_name_initial"] or row["olt_initial"] or "",
        hub_id=row["hub_id"] or "",
        olt_hostname=row["olt_hostname"] or "",
        olt_name_initial=row["olt_name_initial"] or "",
    )


def count_records(db_path: str | Path) -> int:
    with read_connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM fdt_coding_records").fetchone()[0])


def count_records_by_source(db_path: str | Path, source_spreadsheet_id: str) -> int:
    """Count records that originated from a specific source spreadsheet.

    Used by the inbound sync to decide whether the DB already holds a
    given spreadsheet's data. If the DB is empty for that source, a full
    fetch is forced regardless of the cheap modifiedTime gate (so a fresh
    deploy / empty DB always gets populated)."""
    with read_connect(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM fdt_coding_records WHERE source_spreadsheet_id = ?",
                (normalize_text(source_spreadsheet_id),),
            ).fetchone()[0]
        )


def list_olts(db_path: str | Path, area: str | None = None) -> list[dict]:
    """Return one parent row per OLT, preserving its OLT ID/hostname units."""
    area_filter = normalize_text(area).strip()
    with read_connect(db_path) as conn:
        catalog_table, catalog_name_col = olt_catalog_source(conn)
        record_where = "WHERE TRIM(olt_initial) <> ''"
        catalog_where = "WHERE TRIM(olt_initial) <> ''"
        record_params: list[str] = []
        catalog_params: list[str] = []
        if area_filter:
            record_where += " AND UPPER(TRIM(COALESCE(area, ''))) = UPPER(?)"
            catalog_where += " AND UPPER(TRIM(COALESCE(olt_area, ''))) = UPPER(?)"
            record_params.append(area_filter)
            catalog_params.append(area_filter)

        # Coding records show what is in use; the OLT catalog adds every valid
        # unit/hostname pair, including units not used by an FDT row yet.
        record_rows = conn.execute(
            f"""
            SELECT
                TRIM(olt_initial) AS olt_initial,
                TRIM(hub_id) AS olt_code,
                TRIM(olt_hostname) AS olt_hostname,
                TRIM(olt_name_initial) AS olt_name_initial,
                TRIM(area) AS area,
                MIN(TRIM(source_spreadsheet_id)) AS source_spreadsheet_id,
                COUNT(DISTINCT id) AS total
            FROM fdt_coding_records
            {record_where}
            GROUP BY TRIM(olt_initial), TRIM(hub_id), TRIM(olt_hostname), TRIM(olt_name_initial), TRIM(area)
            """,
            record_params,
        ).fetchall()
        catalog_rows = conn.execute(
            f"""
            SELECT
                TRIM(olt_initial) AS olt_initial,
                TRIM(olt_code) AS olt_code,
                TRIM(olt_hostname) AS olt_hostname,
                TRIM({catalog_name_col}) AS olt_name_initial,
                TRIM(olt_area) AS area,
                '' AS source_spreadsheet_id,
                0 AS total
            FROM {catalog_table}
            {catalog_where}
            """,
            catalog_params,
        ).fetchall()
        # Manually added OLTs are not guaranteed to be in OLT.db yet.
        manual_where = "WHERE TRIM(olt_initial) <> ''"
        manual_params: list[str] = []
        if area_filter:
            manual_where += " AND UPPER(TRIM(COALESCE(area, ''))) = UPPER(?)"
            manual_params.append(area_filter)
        manual_rows = conn.execute(
            f"""
            SELECT
                TRIM(olt_initial) AS olt_initial,
                TRIM(hub_id) AS olt_code,
                TRIM(olt_hostname) AS olt_hostname,
                TRIM(olt_name_initial) AS olt_name_initial,
                TRIM(area) AS area,
                '' AS source_spreadsheet_id,
                0 AS total
            FROM fdt_coding_olts
            {manual_where}
            """,
            manual_params,
        ).fetchall()

    parents: dict[str, dict] = {}
    for row in [*record_rows, *catalog_rows, *manual_rows]:
        item = dict(row)
        olt_initial = normalize_text(item.get("olt_initial")).upper()
        if not olt_initial:
            continue
        parent = parents.setdefault(
            olt_initial,
            {
                "olt_initial": olt_initial,
                "total": 0,
                "area_values": set(),
                "source_values": set(),
                "units": {},
            },
        )
        total = int(item.get("total") or 0)
        parent["total"] += total
        if item.get("area"):
            parent["area_values"].add(normalize_text(item["area"]))
        if item.get("source_spreadsheet_id"):
            parent["source_values"].add(normalize_text(item["source_spreadsheet_id"]))

        unit = {
            "olt_code": normalize_text(item.get("olt_code")).upper(),
            "olt_hostname": normalize_text(item.get("olt_hostname")).upper(),
            "olt_name_initial": normalize_text(item.get("olt_name_initial")).upper(),
            "area": normalize_text(item.get("area")),
            "total": total,
        }
        # Same OLT code is one unit. Catalog fills hostname/initial when older
        # coding records have blanks; divergent nonblank hostname stays visible.
        key = (unit["olt_code"], unit["olt_hostname"] or unit["olt_name_initial"])
        existing = parent["units"].get(key)
        if existing:
            existing["total"] += total
            for field in ("olt_hostname", "olt_name_initial", "area"):
                if not existing[field] and unit[field]:
                    existing[field] = unit[field]
        else:
            parent["units"][key] = unit

    result: list[dict] = []
    for parent in parents.values():
        options = sorted(
            parent["units"].values(),
            key=lambda unit: (-int(unit["total"] or 0), unit["olt_code"], unit["olt_hostname"]),
        )
        primary = options[0] if options else {"olt_code": "", "olt_hostname": "", "olt_name_initial": ""}
        result.append(
            {
                "olt_initial": parent["olt_initial"],
                "total": parent["total"],
                "hub_id": primary["olt_code"],
                "olt_hostname": primary["olt_hostname"],
                "olt_name_initial": primary["olt_name_initial"],
                "area": sorted(parent["area_values"])[0] if parent["area_values"] else "",
                "source_spreadsheet_id": sorted(parent["source_values"])[0] if parent["source_values"] else "",
                "options": options,
            }
        )
    return sorted(result, key=lambda item: item["olt_initial"])

def list_olt_catalog(conn_or_db_path) -> list[dict]:
    close_conn = False
    if isinstance(conn_or_db_path, sqlite3.Connection):
        conn = conn_or_db_path
    else:
        conn = read_connect(conn_or_db_path)
        close_conn = True
    try:
        catalog_table, catalog_name_col = olt_catalog_source(conn)
        rows = conn.execute(
            f"""
            SELECT olt_initial, olt_code, {catalog_name_col} AS olt_name_initial, olt_hostname, olt_vendor, olt_area, source_row
            FROM {catalog_table}
            ORDER BY olt_initial, olt_code
            """
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        if close_conn:
            conn.close()

def upsert_olt_catalog_units(db_path: str | Path, units: Iterable[dict]) -> int:
    """Store source-verified units in local catalog for Area-filtered form lists."""
    values = list(units)
    if not values:
        return 0
    for unit in values:
        reject_tangerang_write({
            "area": unit.get("olt_area"),
            "hub_id": unit.get("olt_code"),
            "olt_initial": unit.get("olt_initial"),
        })
    now = utc_now()
    with connect(db_path) as conn:
        begin_immediate(conn)
        for unit in values:
            unit_code = normalize_text(unit.get("olt_code"))
            unit_parent = normalize_text(unit.get("olt_initial"))
            existing = conn.execute(
                "SELECT * FROM fdt_coding_olt_catalog WHERE UPPER(TRIM(olt_code)) = UPPER(?) LIMIT 1",
                (unit_code,),
            ).fetchone()
            existing_parent = normalize_text(existing["olt_initial"]) if existing is not None else ""
            if existing is not None:
                reject_tangerang_write({
                    "area": existing["olt_area"],
                    "hub_id": existing["olt_code"],
                    "olt_initial": existing_parent,
                })
            # Catalog ownership is also inherited from every stored coding row
            # that uses the unit or either parent. Inspect full rows under the
            # same write lock before changing the shared catalog tuple.
            _reject_selected_protected_rows(
                conn,
                """UPPER(TRIM(hub_id)) = UPPER(?)
                   OR UPPER(TRIM(olt_initial)) IN (UPPER(?), UPPER(?))""",
                (unit_code, unit_parent, existing_parent),
            )
            conn.execute(
                """
                INSERT INTO fdt_coding_olt_catalog
                (olt_code, olt_initial, olt_name_initial, olt_hostname, olt_vendor, olt_area, source_row, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?, 0, ?, ?)
                ON CONFLICT(olt_code) DO UPDATE SET
                    olt_initial=excluded.olt_initial,
                    olt_name_initial=excluded.olt_name_initial,
                    olt_hostname=excluded.olt_hostname,
                    olt_area=excluded.olt_area,
                    updated_at=excluded.updated_at
                """,
                (
                    normalize_text(unit.get("olt_code")).upper(),
                    normalize_text(unit.get("olt_initial")).upper(),
                    normalize_text(unit.get("olt_name_initial")).upper(),
                    normalize_text(unit.get("olt_hostname")).upper(),
                    normalize_text(unit.get("olt_area")),
                    now,
                    now,
                ),
            )
    return len(values)


OLT_AREA_PREFIXES = {
    "Bekasi": {"BKS"},
    "Jakarta": {"JKT"},
    "Sukabumi": {"SKB", "SMI"},
    "Tangerang": {"TNG"},
    "Indramayu": {"IDM"},
    "Kuningan": {"KNG"},
}


def prepare_olt(db_path: str | Path, payload: dict) -> dict:
    """Normalize and validate one manual OLT without mutating the database."""
    reject_tangerang_write(payload)
    olt_initial = normalize_text(payload.get("olt_initial")).upper()
    hub_id = normalize_text(payload.get("hub_id")).upper()
    olt_name_initial = normalize_text(payload.get("olt_name_initial")).upper()
    olt_hostname = normalize_text(payload.get("olt_hostname")).upper()
    area_input = normalize_text(payload.get("area"))
    area = next((name for name in OLT_AREA_PREFIXES if name.upper() == area_input.upper()), "")
    if not area:
        raise ValueError("Area tujuan wajib dipilih")
    if not olt_initial:
        raise ValueError("OLT Name wajib diisi")
    if not hub_id:
        raise ValueError("OLT ID wajib diisi")
    if not is_valid_olt_code(hub_id):
        raise ValueError("Format OLT ID tidak valid")
    if not olt_name_initial:
        raise ValueError("Inisial name OLT wajib diisi")
    prefix = hub_id.split(".", 1)[0]
    if prefix not in OLT_AREA_PREFIXES[area]:
        allowed = "/".join(sorted(OLT_AREA_PREFIXES[area]))
        raise ValueError(f"OLT ID {hub_id} tidak sesuai Area {area}; gunakan prefix {allowed}")

    with connect(db_path) as conn:
        existing_manual = conn.execute(
            "SELECT 1 FROM fdt_coding_olts WHERE UPPER(TRIM(olt_initial)) = UPPER(?) LIMIT 1",
            (olt_initial,),
        ).fetchone()
        if existing_manual:
            raise ValueError(f"OLT {olt_initial} sudah terdaftar")
        catalog_table, catalog_name_col = olt_catalog_source(conn)
        existing_unit = conn.execute(
            f"""
            SELECT TRIM(olt_initial) AS olt_initial,
                   TRIM(olt_area) AS area,
                   TRIM(olt_hostname) AS olt_hostname,
                   TRIM({catalog_name_col}) AS olt_name_initial
            FROM {catalog_table}
            WHERE UPPER(TRIM(olt_code)) = UPPER(?)
            LIMIT 1
            """,
            (hub_id,),
        ).fetchone()
    if existing_unit:
        existing_area = normalize_text(existing_unit["area"])
        existing_parent = normalize_text(existing_unit["olt_initial"]).upper()
        if existing_area and existing_area.upper() != area.upper():
            raise ValueError(f"OLT ID {hub_id} sudah terdaftar di Area {existing_area}")
        if existing_parent and comparable_olt_name(existing_parent) != comparable_olt_name(olt_initial):
            raise ValueError(f"OLT ID {hub_id} sudah terdaftar untuk {existing_parent}")

    return {
        "olt_initial": olt_initial,
        "hub_id": hub_id,
        "olt_name_initial": olt_name_initial,
        "olt_hostname": olt_hostname,
        "area": area,
        "total": 0,
    }


def create_olt(db_path: str | Path, payload: dict) -> dict:
    data = prepare_olt(db_path, payload)
    now = utc_now()
    with connect(db_path) as conn:
        begin_immediate(conn)
        # Revalidate under the write lock: prepare_olt() is intentionally usable
        # as a read-only preview, so its earlier checks cannot authorize this write.
        existing_manual = conn.execute(
            "SELECT * FROM fdt_coding_olts WHERE UPPER(TRIM(olt_initial)) = UPPER(?) LIMIT 1",
            (data["olt_initial"],),
        ).fetchone()
        if existing_manual is not None:
            raise ValueError(f"OLT {data['olt_initial']} sudah terdaftar")
        catalog_table, catalog_name_col = olt_catalog_source(conn)
        existing_unit = conn.execute(
            f"""
            SELECT TRIM(olt_initial) AS olt_initial,
                   TRIM(olt_area) AS area,
                   TRIM(olt_code) AS hub_id,
                   TRIM(olt_hostname) AS olt_hostname,
                   TRIM({catalog_name_col}) AS olt_name_initial
            FROM {catalog_table}
            WHERE UPPER(TRIM(olt_code)) = UPPER(?)
            LIMIT 1
            """,
            (data["hub_id"],),
        ).fetchone()
        existing_parent = ""
        if existing_unit is not None:
            existing_payload = row_to_dict(existing_unit)
            reject_tangerang_write(existing_payload)
            existing_area = normalize_text(existing_unit["area"])
            existing_parent = normalize_text(existing_unit["olt_initial"]).upper()
            if existing_area and existing_area.upper() != data["area"].upper():
                raise ValueError(f"OLT ID {data['hub_id']} sudah terdaftar di Area {existing_area}")
            if existing_parent and comparable_olt_name(existing_parent) != comparable_olt_name(data["olt_initial"]):
                raise ValueError(f"OLT ID {data['hub_id']} sudah terdaftar untuk {existing_parent}")
        _reject_selected_protected_rows(
            conn,
            """UPPER(TRIM(hub_id)) = UPPER(?)
               OR UPPER(TRIM(olt_initial)) IN (UPPER(?), UPPER(?))""",
            (data["hub_id"], data["olt_initial"], existing_parent),
        )
        conn.execute(
            """
            INSERT INTO fdt_coding_olts (olt_initial, hub_id, olt_name_initial, olt_hostname, area, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(olt_initial) DO UPDATE SET
                hub_id=excluded.hub_id,
                olt_name_initial=excluded.olt_name_initial,
                olt_hostname=excluded.olt_hostname,
                area=excluded.area,
                updated_at=excluded.updated_at
            """,
            (
                data["olt_initial"], data["hub_id"], data["olt_name_initial"],
                data["olt_hostname"], data["area"], now, now,
            ),
        )
        conn.execute(
            """
            INSERT INTO fdt_coding_olt_catalog
            (olt_code, olt_initial, olt_name_initial, olt_hostname, olt_vendor, olt_area, source_row, created_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, 0, ?, ?)
            ON CONFLICT(olt_code) DO UPDATE SET
                olt_initial=excluded.olt_initial,
                olt_name_initial=excluded.olt_name_initial,
                olt_hostname=excluded.olt_hostname,
                olt_area=excluded.olt_area,
                updated_at=excluded.updated_at
            """,
            (
                data["hub_id"], data["olt_initial"], data["olt_name_initial"],
                data["olt_hostname"], data["area"], now, now,
            ),
        )
    return data


def delete_olt(db_path: str | Path, olt_initial: str, force: bool = False) -> bool:
    olt = normalize_text(olt_initial).upper()
    if not olt:
        raise ValueError("OLT Name wajib diisi")
    if olt in TANGERANG_PROTECTED_OLT_KEYS:
        raise ValueError("Area Tangerang read-only; hapus OLT diblok")
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        protected_titles = sorted(TANGERANG_PROTECTED_OLT_KEYS)
        title_placeholders = ",".join("?" for _ in protected_titles)
        tangerang_owner = conn.execute(
            f"""
            SELECT 1 FROM fdt_coding_records
            WHERE UPPER(TRIM(olt_initial)) = ?
              AND (
                    source_spreadsheet_id IN (?, ?)
                    OR UPPER(TRIM(area)) = 'TANGERANG'
                    OR UPPER(TRIM(hub_id)) LIKE 'TNG.%'
                    OR UPPER(TRIM(sheet_name)) IN ({title_placeholders})
                    OR UPPER(TRIM(olt_initial)) IN ({title_placeholders})
              )
            UNION ALL
            SELECT 1 FROM fdt_coding_olts
            WHERE UPPER(TRIM(olt_initial)) = ?
              AND (UPPER(TRIM(area)) = 'TANGERANG' OR UPPER(TRIM(hub_id)) LIKE 'TNG.%')
            UNION ALL
            SELECT 1 FROM fdt_coding_olt_catalog
            WHERE UPPER(TRIM(olt_initial)) = ?
              AND (UPPER(TRIM(olt_area)) = 'TANGERANG' OR UPPER(TRIM(olt_code)) LIKE 'TNG.%')
            LIMIT 1
            """,
            [
                olt,
                TANGERANG_FDT_SPREADSHEET_ID,
                TANGERANG_RETIRED_SPREADSHEET_ID,
                *protected_titles,
                *protected_titles,
                olt,
                olt,
            ],
        ).fetchone()
        if tangerang_owner:
            raise ValueError("Area Tangerang read-only; hapus OLT diblok")
        record_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fdt_coding_records WHERE olt_initial = ?",
                (olt,),
            ).fetchone()[0]
        )
        if record_count and not force:
            raise ValueError(f"OLT masih punya {record_count} record FDT. Hapus record FDT dulu sebelum hapus OLT.")
        if force:
            pending = conn.execute("SELECT * FROM fdt_coding_records WHERE olt_initial = ? AND source_row < 0 LIMIT 1", (olt,)).fetchone()
            if pending:
                _reject_pending_sharing(pending)
            conn.execute("DELETE FROM fdt_coding_records WHERE olt_initial = ?", (olt,))
        cursor = conn.execute("DELETE FROM fdt_coding_olts WHERE olt_initial = ?", (olt,))
        catalog_cursor = conn.execute("DELETE FROM fdt_coding_olt_catalog WHERE olt_initial = ?", (olt,))
    return (cursor.rowcount > 0) or (catalog_cursor.rowcount > 0)



def record_filters(olt_initial: str = "", query: str = "", area: str = "") -> tuple[str, list[object]]:
    clauses = []
    params: list[object] = []
    olt = normalize_text(olt_initial).upper()
    if olt and olt != "ALL":
        if olt == "(NO OLT)":
            clauses.append("olt_initial = ''")
        else:
            clauses.append("olt_initial = ?")
            params.append(olt)
    area_param = normalize_text(area) if area else None
    if area_param:
        clauses.append("area = ?")
        params.append(area_param)
    q = normalize_text(query)
    if q:
        like = f"%{q}%"
        clauses.append("(site_name LIKE ? OR fdt_code LIKE ? OR cluster_initial LIKE ? OR hub_id LIKE ? OR olt_name_initial LIKE ? OR fdt_id LIKE ? OR index_code LIKE ?)")
        params.extend([like] * 7)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params

def count_matching_records(db_path: str | Path, olt_initial: str = "", query: str = "", area: str = "") -> int:
    where, params = record_filters(olt_initial, query, area=area)
    with read_connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM fdt_coding_records{where}", params).fetchone()[0])

def list_records(db_path: str | Path, olt_initial: str = "", query: str = "", area: str = "", limit: int = 250, offset: int = 0) -> list[dict]:
    where, params = record_filters(olt_initial, query, area=area)
    params.extend([max(int(limit), 1), max(int(offset), 0)])
    with read_connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM fdt_coding_records
            {where}
            ORDER BY COALESCE(fdt_order, 9999), id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def all_records(db_path: str | Path | sqlite3.Connection) -> list[FdtCodingRecord]:
    if isinstance(db_path, sqlite3.Connection):
        rows = db_path.execute(
            "SELECT * FROM fdt_coding_records ORDER BY olt_initial, site_name, COALESCE(fdt_order, 9999), fdt_code"
        ).fetchall()
        return [row_to_record(row) for row in rows]
    with read_connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM fdt_coding_records ORDER BY olt_initial, site_name, COALESCE(fdt_order, 9999), fdt_code"
        ).fetchall()
    return [row_to_record(row) for row in rows]


def _upsert_record(conn: sqlite3.Connection, payload: dict) -> int:
    data = normalize_record_payload(payload)
    reject_tangerang_write(data)
    validate_payload(data)
    begin_immediate(conn)
    now = utc_now()
    existing = find_existing_record(conn, data)
    if existing:
        reject_tangerang_write(existing)
        assignments = ", ".join(f"{field} = ?" for field in FIELDS) + ", updated_at = ?"
        conn.execute(
            f"UPDATE fdt_coding_records SET {assignments} WHERE id = ?",
            [data[field] for field in FIELDS] + [now, int(existing["id"])],
        )
        return int(existing["id"])
    columns = ", ".join((*FIELDS, "created_at", "updated_at"))
    placeholders = ", ".join("?" for _ in (*FIELDS, "created_at", "updated_at"))
    values = [data[field] for field in FIELDS] + [now, now]
    cursor = conn.execute(
        f"INSERT INTO fdt_coding_records ({columns}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def upsert_record(conn: sqlite3.Connection, payload: dict) -> int:
    """Operator/import upsert. Tangerang is writable only via replace_records_by_source()."""
    data = normalize_record_payload(payload)
    reject_tangerang_write(data)
    return _upsert_record(conn, data)


def find_existing_record(conn: sqlite3.Connection, data: dict) -> sqlite3.Row | None:
    if data.get("source_spreadsheet_id") and data.get("source_row"):
        row = conn.execute(
            """
            SELECT * FROM fdt_coding_records
            WHERE source_spreadsheet_id = ?
              AND sheet_name = ?
              AND source_row = ?
            LIMIT 1
            """,
            (data["source_spreadsheet_id"], data["sheet_name"], data["source_row"]),
        ).fetchone()
        return row

    # Manual records have no physical source row; dedupe them logically.
    identity = normalize_record_identity(data)
    row = conn.execute(
        """
        SELECT * FROM fdt_coding_records
        WHERE olt_initial = ?
          AND site_name = ?
          AND fdt_id = ?
          AND fdt_code = ?
          AND index_code = ?
          AND source_spreadsheet_id = ''
        ORDER BY id
        LIMIT 1
        """,
        identity,
    ).fetchone()
    if row:
        return row

    return row

def normalize_record_identity(data: dict) -> tuple[str, str, str, str, str]:
    return (
        normalize_text(data.get("olt_initial")).upper(),
        normalize_text(data.get("site_name")).upper(),
        format_excel_identifier(data.get("fdt_id")),
        normalize_text(data.get("fdt_code")).upper(),
        normalize_text(data.get("index_code")),
    )



def upsert_source_record(conn: sqlite3.Connection, payload: dict) -> int:
    data = normalize_record_payload(payload)
    validate_payload(data)
    return upsert_record(conn, data)

def parse_fdt_id_number(value: str) -> int | None:
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(r"(\d+)$", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None

def next_available_fdt_ids(conn: sqlite3.Connection, olt_initial: str, count: int) -> list[int]:
    rows = conn.execute(
        "SELECT fdt_id FROM fdt_coding_records WHERE olt_initial = ?",
        (normalize_text(olt_initial).upper(),),
    ).fetchall()
    used = {number for row in rows if (number := parse_fdt_id_number(row["fdt_id"] or "")) is not None}
    block_size = max(int(count), 1)
    candidate = 1
    while True:
        block = list(range(candidate, candidate + block_size))
        if all(number not in used for number in block):
            return block
        candidate += 1


def available_fdt_ids(db_path: str | Path, olt_initial: str = "", count: int = 1) -> list[str]:
    with read_connect(db_path) as conn:
        return [f"{number:03d}" for number in next_available_fdt_ids(conn, olt_initial, max(int(count), 1))]

def _share_source_is_physically_consistent(row: dict | sqlite3.Row) -> bool:
    """Fail closed when a catalog parent/unit contradicts physical source evidence."""
    source = dict(row)
    sheet = str(source.get("sheet_name") or "")
    parent = str(source.get("olt_initial") or "")
    hub = normalize_text(source.get("hub_id")).upper()
    if not sheet.strip() or not hub:
        return False
    if normalize_text(source.get("area")) == "Sukabumi" and normalize_text(source.get("fdt_code")).upper() != (
            f"{normalize_text(source.get('cluster_initial')).upper()}."
            f"{normalize_text(source.get('fdt_id')).upper()}"):
        return False  # Source's literal historical code cannot be rebuilt from writable inputs.
    if source.get("source_spreadsheet_id") != LEGACY_FDT_SPREADSHEET_ID:
        full_index = normalize_text(source.get("index_code")).upper()
        match = re.match(r"^([A-Z]{2,}\.\d{3}\.\d{4})\.", full_index)
        if match and match.group(1) != hub:
            return False
        # Catalog may canonicalize the parent away from the physical tab.
        # A mismatched title needs explicit unit evidence, not a name guess.
        if comparable_olt_name(sheet) != comparable_olt_name(parent) and not match:
            return False
    return True


def _share_source_catalog_pair_matches(conn: sqlite3.Connection, row: dict | sqlite3.Row) -> bool:
    """Do not offer a sourced FDT under a conflicting canonical unit hostname."""
    hub = normalize_text(row['hub_id']).upper()
    hostname = normalize_text(row['olt_hostname']).upper()
    if not hub:
        return False
    table, _ = olt_catalog_source(conn)
    try:
        catalog_rows = conn.execute(
            f"SELECT olt_initial, olt_area, olt_hostname FROM {table} WHERE UPPER(TRIM(olt_code))=?",
            (hub,),
        ).fetchall()
    except sqlite3.OperationalError:
        # A partial authoritative catalog cannot validate Sharing identity.
        return False
    if catalog_rows:
        parent = normalize_text(row['olt_initial']).upper()
        area = normalize_text(row['area']).upper()
        for item in catalog_rows:
            listed_parent = normalize_text(item['olt_initial']).upper()
            listed_area = normalize_text(item['olt_area']).upper()
            listed_host = normalize_text(item['olt_hostname']).upper()
            if (listed_parent and listed_parent != parent) or (listed_area and listed_area != area):
                return False
            if table.startswith('olt_db.') and (not listed_parent or not listed_area):
                return False
            if listed_host != hostname:
                return False
        return True
    known = {normalize_text(item[0]).upper() for item in conn.execute(
        "SELECT olt_hostname FROM fdt_coding_olts WHERE UPPER(TRIM(hub_id))=? AND TRIM(olt_hostname)<>''",
        (hub,),
    )}
    return not known or hostname in known


def list_shareable_fdt(db_path: str | Path, area: str, olt_initial: str, query: str = "") -> list[dict]:
    """Search only physical FDT rows owned by the selected area and parent OLT."""
    area = normalize_text(area)
    olt_initial = normalize_text(olt_initial).upper()
    if not area or not olt_initial:
        return []
    with read_connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, area, olt_initial, hub_id, olt_hostname, site_name, fdt_id,
                      fdt_code, cluster_initial, index_code, source_spreadsheet_id, sheet_name, source_row
               FROM fdt_coding_records
               WHERE area = ? AND olt_initial = ?
                 AND TRIM(fdt_id) <> '' AND TRIM(fdt_code) <> ''
                 AND UPPER(TRIM(fdt_code)) <> 'SHARING'
                 AND source_spreadsheet_id <> '' AND source_row > 0
               ORDER BY fdt_code, id""",
            (area, olt_initial),
        ).fetchall()
        wanted = normalize_text(query).upper()
        result = []
        for row in rows:
            item = row_to_dict(row)
            if (is_tangerang_read_only_record(item) or not _share_source_is_physically_consistent(item)
                    or not _share_source_catalog_pair_matches(conn, item)):
                continue
            if wanted and not any(wanted in normalize_text(item[field]).upper() for field in
                                  ("fdt_code", "fdt_id", "site_name", "hub_id")):
                continue
            result.append({field: item[field] for field in
                           ("id", "hub_id", "olt_hostname", "site_name", "fdt_id", "fdt_code", "sheet_name", "source_row")})
        return result


def create_records(db_path: str | Path, payload: dict) -> list[dict]:
    if is_tangerang_read_only_record(payload):
        raise ValueError(
            "Tambah Coding baru Area Tangerang belum didukung; "
            "source Tangerang read-only."
        )
    total_value = payload.get("total_fdt", payload.get("fdt_order", 1))
    try:
        total_fdt = max(int(total_value or 1), 1)
    except (TypeError, ValueError):
        total_fdt = 1
    base_payload = dict(payload)
    cluster_name = normalize_text(base_payload.get("site_name")).upper()
    shared_id = base_payload.get("shared_fdt_id")
    if not cluster_name:
        raise ValueError("cluster_name wajib diisi")
    if shared_id is not None:
        if not isinstance(shared_id, int) or isinstance(shared_id, bool) or shared_id < 1:
            raise ValueError("Pilih FDT sharing yang valid")
        if total_fdt != 1:
            raise ValueError("Sharing FDT hanya menerima satu FDT per cluster")
        with connect(db_path) as conn:
            # ATTACH read-only OLT.db before BEGIN IMMEDIATE; inside the write
            # transaction SQLite can reject ATTACH and silently fall back.
            if OLT_DB_PATH.exists() and OLT_DB_PATH.stat().st_size:
                table, _ = olt_catalog_source(conn)
                if table != 'olt_db.olt_catalog':
                    raise ValueError('Katalog OLT eksternal tidak dapat diverifikasi')
            begin_immediate(conn)
            source = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (shared_id,)).fetchone()
            if (source is None or not source["fdt_id"] or not source["fdt_code"]
                    or source["fdt_code"].upper() == "SHARING" or not source["source_spreadsheet_id"]
                    or not source["sheet_name"].strip() or source["source_row"] < 1):
                raise ValueError("FDT sharing sumber tidak ditemukan di spreadsheet")
            reject_tangerang_write(source)
            if (not _share_source_is_physically_consistent(source)
                    or not _share_source_catalog_pair_matches(conn, source)):
                raise ValueError("Unit/OLT fisik spreadsheet tidak sesuai FDT yang dipilih")
            area = normalize_text(base_payload.get("area"))
            olt = normalize_text(base_payload.get("olt_initial")).upper()
            hub = normalize_text(base_payload.get("hub_id")).upper()
            hostname = normalize_text(base_payload.get("olt_hostname")).upper()
            if not area or not olt or not hub or (area, olt, hub, hostname) != (
                source["area"], source["olt_initial"], normalize_text(source["hub_id"]).upper(),
                normalize_text(source["olt_hostname"]).upper()
            ):
                raise ValueError("FDT sharing harus dari Area, OLT, dan Unit OLT yang sama")
            if conn.execute("""SELECT 1 FROM fdt_coding_records WHERE area = ? AND olt_initial = ?
                               AND site_name = ? AND fdt_code = ? LIMIT 1""",
                            (area, olt, cluster_name, source["fdt_code"])).fetchone():
                raise ValueError("Cluster ini sudah memakai FDT sharing yang dipilih")
            data = normalize_record_payload({
                **base_payload, "site_name": cluster_name,
                "fdt_order": source["fdt_order"], "fdt_id": source["fdt_id"], "fdt_code": source["fdt_code"],
                "cluster_initial": source["cluster_initial"], "index_code": source["index_code"],
                "olt_hostname": source["olt_hostname"], "olt_name_initial": source["olt_name_initial"],
                "source_spreadsheet_id": "", "sheet_name": source["sheet_name"], "source_row": 0,
            })
            validate_payload(data)
            # Manual identity is unique across Area; never let an upsert replace
            # an existing row in a different Area (or an unresolved attempt).
            if find_existing_record(conn, data):
                raise ValueError("Identitas FDT manual sudah dipakai Area lain")
            record_id = upsert_record(conn, data)
            return [row_to_dict(conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (record_id,)).fetchone())]
    fdt_prefix = normalize_text(base_payload.get("cluster_initial") or base_payload.get("fdt_code")).upper()
    if not fdt_prefix:
        raise ValueError("fdt_code wajib diisi")
    with connect(db_path) as conn:
        begin_immediate(conn)  # Reserve the allocated block across concurrent creates.
        # For single record, use user-provided fdt_id if present
        user_fdt_id = format_excel_identifier(base_payload.get("fdt_id"))
        if total_fdt == 1 and user_fdt_id:
            fdt_numbers = [int(user_fdt_id)]
        else:
            fdt_numbers = next_available_fdt_ids(conn, base_payload.get("olt_initial", ""), total_fdt)

        now = (datetime.now(timezone.utc) + timedelta(milliseconds=1)).isoformat(timespec="microseconds")
        created: list[dict] = []
        for index, fdt_number in enumerate(fdt_numbers, start=1):
            prefix = f"{fdt_prefix}-{index}" if total_fdt > 1 else fdt_prefix
            data = normalize_record_payload({
                **base_payload,
                "olt_name_initial": normalize_text(base_payload.get("olt_name_initial")),
                "site_name": f"{cluster_name} - FDT {index}" if total_fdt > 1 else cluster_name,
                "fdt_order": index,
                "fdt_id": f"{fdt_number:03d}",
                "fdt_code": f"{prefix}.{fdt_number:03d}",
                # Legacy Indramayu/Kuningan stores SEGMENT in index_code.
                # Normal templates submit an empty value, unchanged behavior.
                "index_code": normalize_text(base_payload.get("index_code")),
                "cluster_initial": prefix,
            })
            validate_payload(data)
            rec_id = upsert_record(conn, data)
            row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (rec_id,)).fetchone()
            created.append(row_to_dict(row))
        return created

def create_record(db_path: str | Path, payload: dict) -> dict:
    reject_tangerang_write(payload)
    data = normalize_record_payload(payload)
    validate_payload(data)
    now = utc_now()
    with connect(db_path) as conn:
        rec_id = upsert_record(conn, data)
        row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (rec_id,)).fetchone()
    return row_to_dict(row)


def update_record(db_path: str | Path, record_id: int, payload: dict) -> dict:
    with connect(db_path) as conn:
        begin_immediate(conn)
        existing = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
        if existing is None:
            raise ValueError("Record tidak ditemukan")
        reject_tangerang_write(existing)
        adjusted_payload = {**dict(existing), **dict(payload)}
        # Physical identity and Segment are source-owned, never browser-owned.
        for field in ("source_spreadsheet_id", "sheet_name", "source_row", "index_code"):
            adjusted_payload[field] = existing[field]
        reject_tangerang_write(adjusted_payload)
        fdt_prefix = normalize_text(adjusted_payload.get("cluster_initial") or adjusted_payload.get("fdt_code")).upper()
        fdt_id = format_excel_identifier(adjusted_payload.get("fdt_id"))
        if fdt_prefix and fdt_id:
            adjusted_payload["fdt_code"] = f"{fdt_prefix}.{fdt_id}"
            adjusted_payload["cluster_initial"] = fdt_prefix
            adjusted_payload["index_code"] = normalize_text(adjusted_payload.get("index_code"))
        data = normalize_record_payload(adjusted_payload)
        validate_payload(data)
        assignments = ", ".join(f"{field} = ?" for field in FIELDS) + ", updated_at = ?"
        conn.execute(
            f"UPDATE fdt_coding_records SET {assignments} WHERE id = ?",
            [data[field] for field in FIELDS] + [utc_now(), int(record_id)],
        )
        row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
    return row_to_dict(row)


def delete_record(db_path: str | Path, record_id: int) -> bool:
    with connect(db_path) as conn:
        begin_immediate(conn)
        existing = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
        if existing is None:
            return False
        reject_tangerang_write(existing)
        _reject_pending_sharing(existing)
        cursor = conn.execute("DELETE FROM fdt_coding_records WHERE id = ?", (int(record_id),))
        return cursor.rowcount > 0



def base_cluster_name(site_name: str) -> str:
    return re.sub(r"\s+-\s+FDT\s+\d+\s*$", "", normalize_text(site_name), flags=re.IGNORECASE)

def get_records_by_ids(conn: sqlite3.Connection, record_ids: Iterable[int]) -> list[sqlite3.Row]:
    ids = [int(record_id) for record_id in record_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(f"SELECT * FROM fdt_coding_records WHERE id IN ({placeholders}) ORDER BY COALESCE(fdt_order, 9999), fdt_id, id", ids).fetchall()

def delete_records(db_path: str | Path, record_ids: Iterable[int]) -> int:
    ids = [int(record_id) for record_id in record_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect(db_path) as conn:
        begin_immediate(conn)
        rows = conn.execute(f"SELECT * FROM fdt_coding_records WHERE id IN ({placeholders})", ids).fetchall()
        if len(rows) != len(ids):
            raise ValueError("Sebagian record tidak ditemukan")
        for row in rows:
            reject_tangerang_write(row)
            _reject_pending_sharing(row)
        cursor = conn.execute(f"DELETE FROM fdt_coding_records WHERE id IN ({placeholders})", ids)
        return int(cursor.rowcount or 0)

def update_records(db_path: str | Path, record_ids: Iterable[int], payload: dict) -> list[dict]:
    ids = [int(record_id) for record_id in record_ids]
    if not ids:
        raise ValueError("Pilih record dulu")
    cluster_name = normalize_text(payload.get("site_name")).upper()
    fdt_prefix = normalize_text(payload.get("cluster_initial") or payload.get("fdt_code")).upper()
    raw_fdt_ids = payload.get("fdt_ids") or []
    if isinstance(raw_fdt_ids, str):
        raw_fdt_ids = re.split(r"[\n,;]+", raw_fdt_ids)
    fdt_ids = [format_excel_identifier(value) for value in raw_fdt_ids if normalize_text(value)]
    if not cluster_name:
        raise ValueError("cluster_name wajib diisi")
    if not fdt_prefix:
        raise ValueError("fdt_code wajib diisi")
    with connect(db_path) as conn:
        begin_immediate(conn)
        rows = get_records_by_ids(conn, ids)
        if len(rows) != len(ids):
            raise ValueError("Sebagian record tidak ditemukan")
        for row in rows:
            reject_tangerang_write(row)
            reject_tangerang_write({**dict(row), **dict(payload)})
        clusters = {base_cluster_name(row["site_name"] or "").upper() for row in rows}
        if len(clusters) > 1:
            raise ValueError("Edit multiple hanya bisa untuk Cluster Name yang sama")
        now = utc_now()
        updated: list[dict] = []
        multiple = len(rows) > 1
        # Sort rows by fdt_order so fdt_ids assigned correctly per FDT sequence
        rows_sorted = sorted(rows, key=lambda r: int(r["fdt_order"] or 999))
        suffixes = [re.fullmatch(r"(.+)-(\d+)", normalize_text(row["cluster_initial"])) for row in rows_sorted]
        numbered_group = multiple and all(
            match and match.group(2) == str(row["fdt_order"] or 0)
            for match, row in zip(suffixes, rows_sorted)
        ) and len({match.group(1) for match in suffixes}) == 1 and len({row["fdt_order"] for row in rows_sorted}) == len(rows_sorted)
        requested_suffix = re.fullmatch(r"(.+)-(\d+)", fdt_prefix)
        group_prefix = requested_suffix.group(1) if numbered_group and requested_suffix and any(
            int(requested_suffix.group(2)) == int(row["fdt_order"] or 0) for row in rows_sorted
        ) else fdt_prefix
        for idx, row in enumerate(rows_sorted, start=1):
            fdt_order = int(row["fdt_order"] or idx)
            fdt_id = fdt_ids[idx - 1] if idx <= len(fdt_ids) else format_excel_identifier(row["fdt_id"])
            row_prefix = (row["cluster_initial"] if fdt_prefix == rows_sorted[0]["cluster_initial"]
                          else f"{group_prefix}-{fdt_order}" if numbered_group else fdt_prefix)
            data = normalize_record_payload({
                **dict(row),
                **payload,
                # Bulk logical edits never change the physical source target.
                "source_spreadsheet_id": row["source_spreadsheet_id"],
                "sheet_name": row["sheet_name"],
                "source_row": row["source_row"],
                "site_name": f"{cluster_name} - FDT {fdt_order}" if multiple else cluster_name,
                "fdt_order": fdt_order,
                "fdt_id": fdt_id,
                "fdt_code": f"{row_prefix}.{fdt_id}",
                # Segment is source-owned and no longer editable from the UI.
                "index_code": row["index_code"],
                "cluster_initial": row_prefix,
            })
            validate_payload(data)
            assignments = ", ".join(f"{field} = ?" for field in FIELDS) + ", updated_at = ?"
            conn.execute(
                f"UPDATE fdt_coding_records SET {assignments} WHERE id = ?",
                [data[field] for field in FIELDS] + [now, int(row["id"])],
            )
            updated_row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(row["id"]),)).fetchone()
            updated.append(row_to_dict(updated_row))
        return updated

def get_record(db_path: str | Path, record_id: int) -> dict | None:
    with read_connect(db_path) as conn:
        row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
        return row_to_dict(row) if row else None


def restore_record_snapshot(db_path: str | Path, snapshot: dict) -> dict:
    """Restore one existing non-Tangerang record after a failed outbound write."""
    record_id = int((snapshot or {}).get("id", 0) or 0)
    if record_id < 1:
        raise ValueError("Snapshot record FDT invalid")
    data = normalize_record_payload(snapshot)
    reject_tangerang_write(data)
    validate_payload(data)
    with connect(db_path) as conn:
        begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM fdt_coding_records WHERE id = ?", (record_id,)
        ).fetchone()
        if existing is None:
            raise ValueError("Record rollback FDT tidak ditemukan")
        reject_tangerang_write(existing)
        assignments = ", ".join(f"{field} = ?" for field in FIELDS)
        conn.execute(
            f"UPDATE fdt_coding_records SET {assignments}, created_at = ?, updated_at = ? WHERE id = ?",
            [
                *[data[field] for field in FIELDS],
                str(snapshot.get("created_at") or existing["created_at"]),
                str(snapshot.get("updated_at") or existing["updated_at"]),
                record_id,
            ],
        )
        row = conn.execute(
            "SELECT * FROM fdt_coding_records WHERE id = ?", (record_id,)
        ).fetchone()
        return row_to_dict(row)


def set_record_source_metadata(
    db_path: str | Path,
    record_id: int,
    source_spreadsheet_id: str,
    sheet_name: str,
    source_row: int,
) -> dict | None:
    with connect(db_path) as conn:
        begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)
        ).fetchone()
        if existing is None:
            return None
        reject_tangerang_write(existing)
        reject_tangerang_write({
            "source_spreadsheet_id": source_spreadsheet_id,
            "sheet_name": sheet_name,
        })
        book, tab, row_number = normalize_text(source_spreadsheet_id), str(sheet_name or ""), int(source_row)
        if book and row_number:
            occupied = conn.execute("""SELECT id FROM fdt_coding_records
                WHERE source_spreadsheet_id=? AND sheet_name=? AND source_row IN (?, ?)
                  AND id<>? LIMIT 1""", (book, tab, row_number, -row_number, int(record_id))).fetchone()
            if occupied:
                raise ValueError("Locator FDT sudah terpakai atau pending reservasi")
        conn.execute(
            """
            UPDATE fdt_coding_records
            SET source_spreadsheet_id = ?, sheet_name = ?, source_row = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                normalize_text(source_spreadsheet_id),
                str(sheet_name or ""),
                int(source_row),
                utc_now(),
                int(record_id),
            ),
        )
        row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
        return row_to_dict(row) if row else None

def record_to_db_payload(record: FdtCodingRecord, source_spreadsheet_id: str = "") -> dict:
    payload = asdict(record)
    source_id = normalize_text(source_spreadsheet_id)
    # Legacy Indramayu/Kuningan tabs contain multiple parent OLTs. Preserve
    # parent derived from each row hub; regular sheets retain tab grouping.
    if source_id == LEGACY_FDT_SPREADSHEET_ID:
        payload["olt_initial"] = record.olt_initial
    else:
        payload["olt_initial"] = record.sheet_name
    payload["source_spreadsheet_id"] = source_id
    payload["olt_hostname"] = normalize_text(getattr(record, "olt_hostname", "")).upper()
    payload["olt_name_initial"] = normalize_text(getattr(record, "olt_name_initial", "")).upper()
    return payload


def replace_from_records(db_path: str | Path, records: Iterable[FdtCodingRecord], source_spreadsheet_id: str = "") -> int:
    source_id = normalize_text(source_spreadsheet_id)
    reject_tangerang_write({"source_spreadsheet_id": source_id})
    payloads = [record_to_db_payload(record, source_spreadsheet_id=source_id) for record in records]
    for payload in payloads:
        reject_tangerang_write(payload)
    with connect(db_path) as conn:
        # Lock before the ownership check so a concurrent inbound commit cannot
        # insert protected rows between SELECT and the full-table DELETE.
        conn.execute("BEGIN IMMEDIATE")
        protected_titles = sorted(TANGERANG_PROTECTED_OLT_KEYS)
        title_placeholders = ",".join("?" for _ in protected_titles)
        protected = conn.execute(
            f"""
            SELECT 1 FROM fdt_coding_records
            WHERE source_spreadsheet_id IN (?, ?)
               OR UPPER(TRIM(area)) = 'TANGERANG'
               OR UPPER(TRIM(hub_id)) LIKE 'TNG.%'
               OR UPPER(TRIM(sheet_name)) IN ({title_placeholders})
               OR UPPER(TRIM(olt_initial)) IN ({title_placeholders})
            LIMIT 1
            """,
            [
                TANGERANG_FDT_SPREADSHEET_ID,
                TANGERANG_RETIRED_SPREADSHEET_ID,
                *protected_titles,
                *protected_titles,
            ],
        ).fetchone()
        if protected:
            raise ValueError("Full replace diblok: Area Tangerang read-only")
        pending = conn.execute("SELECT * FROM fdt_coding_records WHERE source_row < 0 LIMIT 1").fetchone()
        if pending:
            _reject_pending_sharing(pending)
        conn.execute("DELETE FROM fdt_coding_records")
        for payload in payloads:
            upsert_record(conn, payload)
        return len(payloads)


def import_workbook(db_path: str | Path, workbook_path: str | Path, replace: bool = True, source_spreadsheet_id: str = "") -> int:
    records = parse_fdt_coding_workbook(workbook_path)
    if replace:
        return replace_from_records(db_path, records, source_spreadsheet_id=source_spreadsheet_id)
    with connect(db_path) as conn:
        count = 0
        for record in records:
            upsert_record(conn, record_to_db_payload(record, source_spreadsheet_id=source_spreadsheet_id))
            count += 1
        return count


def delete_records_by_source(
    db_path: str | Path,
    source_spreadsheet_id: str,
    olt_initial: str,
) -> int:
    source_id = _require_unprotected_source(source_spreadsheet_id)
    with connect(db_path) as conn:
        begin_immediate(conn)
        _reject_selected_protected_rows(
            conn,
            "source_spreadsheet_id = ? AND olt_initial = ?",
            (source_id, olt_initial),
        )
        cursor = conn.execute(
            "DELETE FROM fdt_coding_records WHERE source_spreadsheet_id = ? AND olt_initial = ?",
            (source_id, olt_initial),
        )
        return int(cursor.rowcount or 0)


def delete_source_records_not_in_sheets(
    db_path: str | Path,
    source_spreadsheet_id: str,
    active_sheet_names: Iterable[str],
    conn: sqlite3.Connection | None = None,
) -> int:
    """Delete rows whose physical worksheet no longer exists."""
    source_id = _require_unprotected_source(source_spreadsheet_id)
    names = sorted({str(name) for name in active_sheet_names if str(name)})
    with connect(db_path) if conn is None else nullcontext(conn) as conn:
        begin_immediate(conn)
        if not names:
            where_sql = "source_spreadsheet_id = ?"
            params: list[object] = [source_id]
        else:
            placeholders = ",".join("?" for _ in names)
            where_sql = f"source_spreadsheet_id = ? AND sheet_name NOT IN ({placeholders})"
            params = [source_id, *names]
        _reject_selected_protected_rows(conn, where_sql, params)
        pending = conn.execute(
            f"SELECT id, sheet_name, source_row FROM fdt_coding_records WHERE {where_sql} AND source_row < 0 LIMIT 1",
            params,
        ).fetchone()
        if pending:
            raise ValueError(f"Sharing FDT pending record {pending['id']} at {pending['sheet_name']} row {-pending['source_row']}; perlu rekonsiliasi")
        cursor = conn.execute(
            f"DELETE FROM fdt_coding_records WHERE {where_sql}",
            params,
        )
        return int(cursor.rowcount or 0)


def delete_records_by_spreadsheet_ids(
    db_path: str | Path,
    source_spreadsheet_ids: Iterable[str],
    conn: sqlite3.Connection | None = None,
) -> int:
    ids = sorted({normalize_text(value) for value in source_spreadsheet_ids if normalize_text(value)})
    if not ids:
        return 0
    for source_id in ids:
        _require_unprotected_source(source_id)
    placeholders = ",".join("?" for _ in ids)
    with connect(db_path) if conn is None else nullcontext(conn) as conn:
        begin_immediate(conn)
        _reject_selected_protected_rows(
            conn,
            f"source_spreadsheet_id IN ({placeholders})",
            ids,
        )
        cursor = conn.execute(
            f"DELETE FROM fdt_coding_records WHERE source_spreadsheet_id IN ({placeholders})",
            ids,
        )
        return int(cursor.rowcount or 0)


def apply_catalog_to_payload(conn: sqlite3.Connection, payload: dict) -> dict:
    payload = dict(payload)
    hub_id = normalize_text(payload.get("hub_id")).upper()
    olt_initial = normalize_text(payload.get("olt_initial")).upper()
    olt_hostname = normalize_text(payload.get("olt_hostname")).upper()
    row = None
    exact_catalog_unit = False
    catalog_table, catalog_name_col = olt_catalog_source(conn)
    select_sql = f"SELECT olt_code, olt_initial, {catalog_name_col} AS olt_name_initial, olt_hostname FROM {catalog_table}"
    if is_valid_olt_code(hub_id):
        row = conn.execute(
            f"""
            {select_sql}
            WHERE olt_code = ?
            """,
            (hub_id,),
        ).fetchone()
        if row is not None:
            catalog_hostname = normalize_text(row["olt_hostname"]).upper()
            # A source tab can be stale/wrong. An exact OLT ID plus matching
            # hostname is authoritative: catalog parent wins over tab title.
            exact_catalog_unit = bool(not olt_hostname or not catalog_hostname or olt_hostname == catalog_hostname)
    if row is None and olt_initial:
        row = conn.execute(
            f"""
            {select_sql}
            WHERE olt_initial = ?
            ORDER BY olt_code
            LIMIT 1
            """,
            (olt_initial,),
        ).fetchone()
    if row is None and olt_initial:
        target_key = comparable_olt_name(olt_initial)
        for candidate in conn.execute(
            f"""
            {select_sql}
            WHERE olt_initial <> '' OR {catalog_name_col} <> ''
            ORDER BY olt_code
            """
        ):
            if comparable_olt_name(candidate["olt_initial"]) == target_key or comparable_olt_name(candidate["olt_name_initial"]) == target_key:
                row = candidate
                break
    if row is None:
        if not is_valid_olt_code(hub_id):
            payload["hub_id"] = ""
        return payload
    if exact_catalog_unit and normalize_text(row["olt_initial"]):
        payload["olt_initial"] = normalize_text(row["olt_initial"]).upper()
    payload["hub_id"] = payload.get("hub_id", "") or row["olt_code"] or ""
    payload["olt_hostname"] = payload.get("olt_hostname", "") or row["olt_hostname"] or ""
    hostname_initial = initial_from_olt_hostname(row["olt_hostname"])
    payload["olt_name_initial"] = hostname_initial or row["olt_name_initial"] or payload.get("olt_name_initial", "")
    return payload

def replace_records_by_source(
    db_path: str | Path,
    records: Iterable[FdtCodingRecord],
    source_spreadsheet_id: str,
    source_sheet_name: str,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Replace one ordinary inbound source sheet after source validation."""
    source_id = _require_unprotected_source(source_spreadsheet_id)
    source_sheet = str(source_sheet_name or "")
    reject_tangerang_write({"source_spreadsheet_id": source_id, "sheet_name": source_sheet})
    record_list = list(records)

    payloads: list[dict] = []
    source_rows: set[tuple[str, str, int]] = set()
    with connect(db_path) if conn is None else nullcontext(conn) as conn:
        begin_immediate(conn)
        for record in record_list:
            payload = record_to_db_payload(record, source_spreadsheet_id=source_id)
            if source_id != SUKABUMI_FDT_SPREADSHEET_ID:
                payload = apply_catalog_to_payload(conn, payload)
            payload = normalize_record_payload(payload)
            validate_payload(payload)
            source_key = (
                payload["source_spreadsheet_id"],
                payload["sheet_name"],
                int(payload["source_row"]),
            )
            if source_key in source_rows:
                raise ValueError(
                    "Duplicate physical FDT source locator: "
                    f"{source_key[0]} / {source_key[1]} / row {source_key[2]}"
                )
            source_rows.add(source_key)
            payloads.append(payload)

        # Pending sharing rows reserve -physical_row before any Google write.
        # Replay may replace that row only when the physical row is complete and
        # matches exactly; otherwise stop before deleting/importing anything.
        pending = conn.execute("""SELECT * FROM fdt_coding_records
            WHERE source_spreadsheet_id=? AND sheet_name=? AND source_row<0""",
            (source_id, source_sheet)).fetchall()
        mismatched_title = conn.execute("""SELECT id, sheet_name FROM fdt_coding_records
            WHERE source_spreadsheet_id=? AND source_row<0
              AND UPPER(TRIM(sheet_name))=UPPER(TRIM(?)) AND sheet_name<>? LIMIT 1""",
            (source_id, source_sheet, source_sheet)).fetchone()
        if mismatched_title:
            raise ValueError(f"Sharing FDT pending record {mismatched_title['id']} has ambiguous physical worksheet title")
        incoming = {int(payload['source_row']): payload for payload in payloads}
        retained_ids: dict[int, sqlite3.Row] = {}
        for row in pending:
            physical = -int(row['source_row'])
            payload = incoming.get(physical)
            identity = ('area', 'olt_initial', 'site_name', 'fdt_id', 'fdt_code',
                        'hub_id', 'olt_hostname', 'index_code', 'cluster_initial')
            if not payload or any(normalize_text(row[field]).upper() != normalize_text(payload[field]).upper()
                                  for field in identity) or physical in retained_ids:
                raise ValueError(f"Sharing FDT pending record {row['id']} at {source_sheet} row {physical}; "
                                 "perlu rekonsiliasi sebelum inbound")
            retained_ids[physical] = row
        where_sql = """
            source_spreadsheet_id = ?
            AND (
                UPPER(TRIM(sheet_name)) = UPPER(TRIM(?))
                OR (TRIM(sheet_name) = '' AND UPPER(TRIM(olt_initial)) = UPPER(?))
            )
        """
        params: list[object] = [source_id, source_sheet, source_sheet]
        retained = {int(row['id']) for row in retained_ids.values()}
        for selected in _selected_rows(conn, where_sql, params):
            reject_tangerang_write(selected)
            if int(selected['id']) not in retained:
                _reject_pending_sharing(selected)
        for physical, pending_row in retained_ids.items():
            occupied = conn.execute("""SELECT id FROM fdt_coding_records
                WHERE source_spreadsheet_id=? AND sheet_name=? AND source_row=?""",
                (source_id, source_sheet, physical)).fetchone()
            if occupied:
                raise ValueError(f"Locator sharing pending {pending_row['id']} sudah ditempati {occupied['id']}")
        if retained_ids:
            exclusions = ','.join('?' for _ in retained_ids)
            conn.execute(f"DELETE FROM fdt_coding_records WHERE {where_sql} AND id NOT IN ({exclusions})",
                         [*params, *(int(row['id']) for row in retained_ids.values())])
            for physical, pending_row in retained_ids.items():
                conn.execute("UPDATE fdt_coding_records SET source_row=? WHERE id=? AND source_row=?",
                             (physical, int(pending_row['id']), -physical))
        else:
            conn.execute(f"DELETE FROM fdt_coding_records WHERE {where_sql}", params)
        for payload in payloads:
            _upsert_record(conn, payload)
        return len(payloads)


from __future__ import annotations

import re
import sqlite3
from contextlib import suppress
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_fdt_coding_unique ON fdt_coding_records(
    olt_initial, site_name, fdt_order, fdt_code
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fdt_coding_identity ON fdt_coding_records(
    olt_initial, site_name, fdt_id, fdt_code, index_code
);
CREATE TABLE IF NOT EXISTS fdt_coding_olts (
    olt_initial TEXT PRIMARY KEY,
    hub_id TEXT NOT NULL DEFAULT '',
    olt_name_initial TEXT NOT NULL DEFAULT '',
    olt_hostname TEXT NOT NULL DEFAULT '',
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

OLT_DB_PATH = Path(__file__).resolve().parent / "data" / "OLT.db"
LEGACY_FDT_SPREADSHEET_ID = "1PNnmDDFgmks0bEkGF4jF8c3Vlv9-hzF3YushyP9xQWs"


def attach_olt_catalog_db(conn: sqlite3.Connection) -> None:
    if not OLT_DB_PATH.exists():
        return
    with suppress(sqlite3.OperationalError):
        conn.execute("ATTACH DATABASE ? AS olt_db", (str(OLT_DB_PATH),))


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
    olt_columns = {row[1] for row in conn.execute("PRAGMA table_info(fdt_coding_olts)").fetchall()}
    if "olt_hostname" not in olt_columns:
        conn.execute("ALTER TABLE fdt_coding_olts ADD COLUMN olt_hostname TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        INSERT OR IGNORE INTO fdt_coding_olts (olt_initial, hub_id, olt_name_initial, olt_hostname, created_at, updated_at)
        SELECT
            olt_initial,
            COALESCE(MIN(NULLIF(hub_id, '')), ''),
            COALESCE(MIN(NULLIF(olt_name_initial, '')), ''),
            COALESCE(MIN(NULLIF(olt_hostname, '')), ''),
            ?,
            ?
        FROM fdt_coding_records
        WHERE olt_initial <> ''
        GROUP BY olt_initial
        """,
        (utc_now(), utc_now()),
    )
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
        "sheet_name": normalize_text(payload.get("sheet_name")),
        "source_row": source_row,
    }



def validate_payload(payload: dict) -> None:
    if not payload.get("site_name"):
        raise ValueError("site_name wajib diisi")
    if not payload.get("fdt_code"):
        raise ValueError("fdt_code wajib diisi")


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
    with connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM fdt_coding_records").fetchone()[0])


def count_records_by_source(db_path: str | Path, source_spreadsheet_id: str) -> int:
    """Count records that originated from a specific source spreadsheet.

    Used by the inbound sync to decide whether the DB already holds a
    given spreadsheet's data. If the DB is empty for that source, a full
    fetch is forced regardless of the cheap modifiedTime gate (so a fresh
    deploy / empty DB always gets populated)."""
    with connect(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM fdt_coding_records WHERE source_spreadsheet_id = ?",
                (normalize_text(source_spreadsheet_id),),
            ).fetchone()[0]
        )


def list_olts(db_path: str | Path, area: str | None = None) -> list[dict]:
    """Return one parent row per OLT, preserving its OLT ID/hostname units."""
    area_filter = normalize_text(area).strip()
    with connect(db_path) as conn:
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
        manual_rows = []
        if not area_filter:
            manual_rows = conn.execute(
                """
                SELECT
                    TRIM(olt_initial) AS olt_initial,
                    TRIM(hub_id) AS olt_code,
                    TRIM(olt_hostname) AS olt_hostname,
                    TRIM(olt_name_initial) AS olt_name_initial,
                    '' AS area,
                    '' AS source_spreadsheet_id,
                    0 AS total
                FROM fdt_coding_olts
                WHERE TRIM(olt_initial) <> ''
                """
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
        conn = connect(conn_or_db_path)
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
    now = utc_now()
    with connect(db_path) as conn:
        for unit in values:
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


def create_olt(db_path: str | Path, payload: dict) -> dict:
    olt_initial = normalize_text(payload.get("olt_initial")).upper()
    hub_id = normalize_text(payload.get("hub_id"))
    olt_name_initial = normalize_text(payload.get("olt_name_initial"))
    olt_hostname = normalize_text(payload.get("olt_hostname"))
    if not olt_initial:
        raise ValueError("OLT Name wajib diisi")
    if not hub_id:
        raise ValueError("OLT ID wajib diisi")
    if not olt_name_initial:
        raise ValueError("Inisial name OLT wajib diisi")
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO fdt_coding_olts (olt_initial, hub_id, olt_name_initial, olt_hostname, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(olt_initial) DO UPDATE SET
                hub_id=excluded.hub_id,
                olt_name_initial=excluded.olt_name_initial,
                olt_hostname=excluded.olt_hostname,
                updated_at=excluded.updated_at
            """,
            (olt_initial, hub_id, olt_name_initial, olt_hostname, now, now),
        )
        if hub_id:
            conn.execute(
                """
                INSERT INTO fdt_coding_olt_catalog
                (olt_code, olt_initial, olt_name_initial, olt_hostname, olt_vendor, olt_area, source_row, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', '', 0, ?, ?)
                ON CONFLICT(olt_code) DO UPDATE SET
                    olt_initial=excluded.olt_initial,
                    olt_name_initial=excluded.olt_name_initial,
                    olt_hostname=excluded.olt_hostname,
                    updated_at=excluded.updated_at
                """,
                (hub_id, olt_initial, olt_name_initial, olt_hostname, now, now),
            )
    return {"olt_initial": olt_initial, "hub_id": hub_id, "olt_name_initial": olt_name_initial, "olt_hostname": olt_hostname, "total": 0}

def delete_olt(db_path: str | Path, olt_initial: str, force: bool = False) -> bool:
    olt = normalize_text(olt_initial).upper()
    if not olt:
        raise ValueError("OLT Name wajib diisi")
    with connect(db_path) as conn:
        record_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fdt_coding_records WHERE olt_initial = ?",
                (olt,),
            ).fetchone()[0]
        )
        if record_count and not force:
            raise ValueError(f"OLT masih punya {record_count} record FDT. Hapus record FDT dulu sebelum hapus OLT.")
        if force:
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
    with connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM fdt_coding_records{where}", params).fetchone()[0])

def list_records(db_path: str | Path, olt_initial: str = "", query: str = "", area: str = "", limit: int = 250, offset: int = 0) -> list[dict]:
    where, params = record_filters(olt_initial, query, area=area)
    params.extend([max(int(limit), 1), max(int(offset), 0)])
    with connect(db_path) as conn:
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


def all_records(db_path: str | Path) -> list[FdtCodingRecord]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM fdt_coding_records ORDER BY olt_initial, site_name, COALESCE(fdt_order, 9999), fdt_code"
        ).fetchall()
    return [row_to_record(row) for row in rows]


def upsert_record(conn: sqlite3.Connection, payload: dict) -> int:
    data = normalize_record_payload(payload)
    validate_payload(data)
    now = utc_now()
    existing = find_existing_record(conn, data)
    if existing:
        assignments = ", ".join(f"{field} = ?" for field in FIELDS) + ", updated_at = ?"
        conn.execute(
            f"UPDATE fdt_coding_records SET {assignments} WHERE id = ?",
            [data[field] for field in FIELDS] + [now, int(existing["id"])],
        )
        return int(existing["id"])
    columns = ", ".join((*FIELDS, "created_at", "updated_at"))
    placeholders = ", ".join("?" for _ in (*FIELDS, "created_at", "updated_at"))
    update_set = ", ".join(f"{field}=excluded.{field}" for field in FIELDS) + ", updated_at=excluded.updated_at"
    values = [data[field] for field in FIELDS] + [now, now]
    cursor = conn.execute(
        f"""
        INSERT INTO fdt_coding_records ({columns}) VALUES ({placeholders})
        ON CONFLICT(olt_initial, site_name, fdt_id, fdt_code, index_code)
        DO UPDATE SET {update_set}
        """,
        values,
    )
    return int(cursor.lastrowid or 0)

def find_existing_record(conn: sqlite3.Connection, data: dict) -> sqlite3.Row | None:
    if data.get("source_spreadsheet_id") and data.get("source_row"):
        row = conn.execute(
            """
            SELECT id FROM fdt_coding_records
            WHERE source_spreadsheet_id = ?
              AND sheet_name = ?
              AND olt_initial = ?
              AND source_row = ?
            LIMIT 1
            """,
            (data["source_spreadsheet_id"], data["sheet_name"], data["olt_initial"], data["source_row"]),
        ).fetchone()
        if row:
            return row
    # Match DB identity unique index: idx_fdt_coding_identity
    identity = normalize_record_identity(data)
    row = conn.execute(
        """
        SELECT id FROM fdt_coding_records
        WHERE olt_initial = ?
          AND site_name = ?
          AND fdt_id = ?
          AND fdt_code = ?
          AND index_code = ?
        ORDER BY id
        LIMIT 1
        """,
        identity,
    ).fetchone()
    if row:
        return row

    # Match legacy unique index: idx_fdt_coding_unique
    # Without this, create/edit can fail with UNIQUE constraint when fdt_order matches.
    if data.get("fdt_order") is not None:
        row = conn.execute(
            """
            SELECT id FROM fdt_coding_records
            WHERE olt_initial = ?
              AND site_name = ?
              AND fdt_order = ?
              AND fdt_code = ?
            ORDER BY id
            LIMIT 1
            """,
            (
                normalize_text(data.get("olt_initial")).upper(),
                normalize_text(data.get("site_name")).upper(),
                int(data.get("fdt_order")),
                normalize_text(data.get("fdt_code")).upper(),
            ),
        ).fetchone()
        if row:
            return row

    return None

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
    with connect(db_path) as conn:
        return [f"{number:03d}" for number in next_available_fdt_ids(conn, olt_initial, max(int(count), 1))]

def create_records(db_path: str | Path, payload: dict) -> list[dict]:
    total_value = payload.get("total_fdt", payload.get("fdt_order", 1))
    try:
        total_fdt = max(int(total_value or 1), 1)
    except (TypeError, ValueError):
        total_fdt = 1
    base_payload = dict(payload)
    cluster_name = normalize_text(base_payload.get("site_name")).upper()
    fdt_prefix = normalize_text(base_payload.get("cluster_initial") or base_payload.get("fdt_code")).upper()
    if not cluster_name:
        raise ValueError("cluster_name wajib diisi")
    if not fdt_prefix:
        raise ValueError("fdt_code wajib diisi")
    with connect(db_path) as conn:
        # For single record, use user-provided fdt_id if present
        user_fdt_id = format_excel_identifier(base_payload.get("fdt_id"))
        if total_fdt == 1 and user_fdt_id:
            fdt_numbers = [int(user_fdt_id)]
        else:
            fdt_numbers = next_available_fdt_ids(conn, base_payload.get("olt_initial", ""), total_fdt)

        now = (datetime.now(timezone.utc) + timedelta(milliseconds=1)).isoformat(timespec="microseconds")
        created: list[dict] = []
        for index, fdt_number in enumerate(fdt_numbers, start=1):
            data = normalize_record_payload({
                **base_payload,
                "olt_name_initial": normalize_text(base_payload.get("olt_name_initial")),
                "site_name": f"{cluster_name} - FDT {index}" if total_fdt > 1 else cluster_name,
                "fdt_order": index,
                "fdt_id": f"{fdt_number:03d}",
                "fdt_code": f"{fdt_prefix}.{fdt_number:03d}",
                # Legacy Indramayu/Kuningan stores SEGMENT in index_code.
                # Normal templates submit an empty value, unchanged behavior.
                "index_code": normalize_text(base_payload.get("index_code")),
                "cluster_initial": fdt_prefix,
            })
            validate_payload(data)
            rec_id = upsert_record(conn, data)
            row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (rec_id,)).fetchone()
            created.append(row_to_dict(row))
        return created

def create_record(db_path: str | Path, payload: dict) -> dict:
    data = normalize_record_payload(payload)
    validate_payload(data)
    now = utc_now()
    with connect(db_path) as conn:
        rec_id = upsert_record(conn, data)
        row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (rec_id,)).fetchone()
    return row_to_dict(row)


def update_record(db_path: str | Path, record_id: int, payload: dict) -> dict:
    with connect(db_path) as conn:
        existing = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
        if existing is None:
            raise ValueError("Record tidak ditemukan")
        adjusted_payload = {**dict(existing), **dict(payload)}
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
        rows = get_records_by_ids(conn, ids)
        if len(rows) != len(ids):
            raise ValueError("Sebagian record tidak ditemukan")
        clusters = {base_cluster_name(row["site_name"] or "").upper() for row in rows}
        if len(clusters) > 1:
            raise ValueError("Edit multiple hanya bisa untuk Cluster Name yang sama")
        now = utc_now()
        updated: list[dict] = []
        multiple = len(rows) > 1
        # Sort rows by fdt_order so fdt_ids assigned correctly per FDT sequence
        rows_sorted = sorted(rows, key=lambda r: int(r.get("fdt_order") or 999))
        for idx, row in enumerate(rows_sorted, start=1):
            fdt_order = int(row.get("fdt_order") or idx)
            fdt_id = fdt_ids[idx - 1] if idx <= len(fdt_ids) else format_excel_identifier(row["fdt_id"])
            data = normalize_record_payload({
                **dict(row),
                **payload,
                "site_name": f"{cluster_name} - FDT {fdt_order}" if multiple else cluster_name,
                "fdt_order": fdt_order,
                "fdt_id": fdt_id,
                "fdt_code": f"{fdt_prefix}.{fdt_id}",
                "index_code": normalize_text(payload.get("index_code") or row["index_code"]),
                "cluster_initial": fdt_prefix,
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
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM fdt_coding_records WHERE id = ?", (int(record_id),)).fetchone()
        return row_to_dict(row) if row else None

def set_record_source_metadata(
    db_path: str | Path,
    record_id: int,
    source_spreadsheet_id: str,
    sheet_name: str,
    source_row: int,
) -> dict | None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE fdt_coding_records
            SET source_spreadsheet_id = ?, sheet_name = ?, source_row = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                normalize_text(source_spreadsheet_id),
                normalize_text(sheet_name),
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
    with connect(db_path) as conn:
        conn.execute("DELETE FROM fdt_coding_records")
        count = 0
        for record in records:
            upsert_record(conn, record_to_db_payload(record, source_spreadsheet_id=source_spreadsheet_id))
            count += 1
        return count


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


def delete_records_by_source(db_path: str | Path, source_spreadsheet_id: str, olt_initial: str) -> int:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM fdt_coding_records WHERE source_spreadsheet_id = ? AND olt_initial = ?",
            (source_spreadsheet_id, olt_initial),
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
) -> int:
    """Replace one source sheet after catalog canonicalization.

    The source sheet title can differ from canonical OLT parent. Delete by
    immutable source ID + sheet name so resync cannot leave stale rows under a
    previous parent name.
    """
    record_list = list(records)
    with connect(db_path) as conn:
        conn.execute(
            """
            DELETE FROM fdt_coding_records
            WHERE source_spreadsheet_id = ?
              AND (
                    UPPER(TRIM(sheet_name)) = UPPER(?)
                    OR (
                        TRIM(sheet_name) = ''
                        AND UPPER(TRIM(olt_initial)) = UPPER(?)
                    )
              )
            """,
            (source_spreadsheet_id, source_sheet_name, source_sheet_name),
        )

        deduped: dict[tuple[str, str, str, str, str], dict] = {}
        for record in record_list:
            payload = record_to_db_payload(record, source_spreadsheet_id=source_spreadsheet_id)
            payload = apply_catalog_to_payload(conn, payload)
            payload = normalize_record_payload(payload)
            validate_payload(payload)
            deduped[normalize_record_identity(payload)] = payload

        for payload in deduped.values():
            upsert_record(conn, payload)
        return len(deduped)


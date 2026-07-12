from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS coreman_opm_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_name TEXT NOT NULL,
    normalized_cluster_name TEXT NOT NULL,
    raw_message TEXT NOT NULL DEFAULT '',
    source_chat_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_sender TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    detected_fdt_count INTEGER NOT NULL DEFAULT 0,
    detected_fdt_labels_json TEXT NOT NULL DEFAULT '[]',
    matched_fdt_records_json TEXT NOT NULL DEFAULT '[]',
    selected_olt_name TEXT NOT NULL DEFAULT '',
    selected_segment_name TEXT NOT NULL DEFAULT '',
    waiting_mancore INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coreman_opm_batches_cluster ON coreman_opm_batches(normalized_cluster_name);
CREATE INDEX IF NOT EXISTS idx_coreman_opm_batches_status ON coreman_opm_batches(status);

CREATE TABLE IF NOT EXISTS coreman_cluster_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_name TEXT NOT NULL,
    normalized_alias_name TEXT NOT NULL UNIQUE,
    canonical_cluster_name TEXT NOT NULL,
    normalized_canonical_name TEXT NOT NULL,
    fdt_code TEXT NOT NULL DEFAULT '',
    olt_name TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coreman_aliases_canonical ON coreman_cluster_aliases(normalized_canonical_name);

CREATE TABLE IF NOT EXISTS coreman_sheet_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    olt_name TEXT NOT NULL,
    segment_name TEXT NOT NULL,
    sheet_name TEXT NOT NULL UNIQUE,
    spreadsheet_id TEXT NOT NULL DEFAULT '',
    template_sheet_name TEXT NOT NULL DEFAULT '',
    sheet_gid TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    last_synced_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coreman_sheet_registry_olt_segment ON coreman_sheet_registry(olt_name, segment_name);

CREATE TABLE IF NOT EXISTS coreman_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER,
    event_type TEXT NOT NULL,
    actor_name TEXT NOT NULL DEFAULT '',
    source_chat_id TEXT NOT NULL DEFAULT '',
    message_text TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES coreman_opm_batches(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_coreman_audit_logs_batch ON coreman_audit_logs(batch_id);
CREATE INDEX IF NOT EXISTS idx_coreman_audit_logs_event ON coreman_audit_logs(event_type);
"""


def utc_ts() -> float:
    return time.time()


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().upper().split())


def json_loads(value: str, default):
    try:
        return json.loads(value or "")
    except Exception:
        return default


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("detected_fdt_labels_json", "matched_fdt_records_json", "payload_json"):
        if key in data:
            parsed_key = key.removesuffix("_json")
            data[parsed_key] = json_loads(data[key], [] if "records" in key or "labels" in key else {})
    return data


def create_opm_batch(db_path: str | Path, payload: dict) -> dict:
    now = utc_ts()
    cluster_name = str(payload.get("cluster_name") or "").strip()
    normalized = normalize_text(cluster_name)
    detected_fdt_labels = payload.get("detected_fdt_labels") or []
    matched_fdt_records = payload.get("matched_fdt_records") or []
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO coreman_opm_batches (
                cluster_name, normalized_cluster_name, raw_message, source_chat_id, source_message_id,
                source_sender, status, detected_fdt_count, detected_fdt_labels_json, matched_fdt_records_json,
                selected_olt_name, selected_segment_name, waiting_mancore, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cluster_name,
                normalized,
                str(payload.get("raw_message") or ""),
                str(payload.get("source_chat_id") or ""),
                str(payload.get("source_message_id") or ""),
                str(payload.get("source_sender") or ""),
                str(payload.get("status") or "pending"),
                int(payload.get("detected_fdt_count") or len(detected_fdt_labels)),
                json.dumps(detected_fdt_labels),
                json.dumps(matched_fdt_records),
                str(payload.get("selected_olt_name") or ""),
                str(payload.get("selected_segment_name") or ""),
                1 if payload.get("waiting_mancore", True) else 0,
                str(payload.get("last_error") or ""),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM coreman_opm_batches WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return row_to_dict(row) or {}


def update_opm_batch(db_path: str | Path, batch_id: int, payload: dict) -> dict:
    current = get_opm_batch(db_path, batch_id)
    if not current:
        raise ValueError("Batch tidak ditemukan")
    merged = {**current, **payload}
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE coreman_opm_batches
            SET cluster_name = ?, normalized_cluster_name = ?, raw_message = ?, source_chat_id = ?, source_message_id = ?,
                source_sender = ?, status = ?, detected_fdt_count = ?, detected_fdt_labels_json = ?, matched_fdt_records_json = ?,
                selected_olt_name = ?, selected_segment_name = ?, waiting_mancore = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(merged.get("cluster_name") or "").strip(),
                normalize_text(merged.get("cluster_name")),
                str(merged.get("raw_message") or ""),
                str(merged.get("source_chat_id") or ""),
                str(merged.get("source_message_id") or ""),
                str(merged.get("source_sender") or ""),
                str(merged.get("status") or "pending"),
                int(merged.get("detected_fdt_count") or 0),
                json.dumps(merged.get("detected_fdt_labels") or []),
                json.dumps(merged.get("matched_fdt_records") or []),
                str(merged.get("selected_olt_name") or ""),
                str(merged.get("selected_segment_name") or ""),
                1 if merged.get("waiting_mancore") else 0,
                str(merged.get("last_error") or ""),
                utc_ts(),
                int(batch_id),
            ),
        )
        row = conn.execute("SELECT * FROM coreman_opm_batches WHERE id = ?", (int(batch_id),)).fetchone()
        return row_to_dict(row) or {}


def get_opm_batch(db_path: str | Path, batch_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM coreman_opm_batches WHERE id = ?", (int(batch_id),)).fetchone()
        return row_to_dict(row)


def list_opm_batches(db_path: str | Path, status: str = "", limit: int = 100) -> list[dict]:
    params: list[object] = []
    where = ""
    if status and status.upper() != "ALL":
        where = "WHERE status = ?"
        params.append(status)
    params.append(max(1, int(limit)))
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM coreman_opm_batches {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [row_to_dict(row) or {} for row in rows]


def create_alias(db_path: str | Path, payload: dict) -> dict:
    alias_name = str(payload.get("alias_name") or "").strip()
    canonical = str(payload.get("canonical_cluster_name") or "").strip()
    if not alias_name or not canonical:
        raise ValueError("Alias dan nama cluster wajib diisi")
    now = utc_ts()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO coreman_cluster_aliases (
                alias_name, normalized_alias_name, canonical_cluster_name, normalized_canonical_name,
                fdt_code, olt_name, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_alias_name) DO UPDATE SET
                alias_name=excluded.alias_name,
                canonical_cluster_name=excluded.canonical_cluster_name,
                normalized_canonical_name=excluded.normalized_canonical_name,
                fdt_code=excluded.fdt_code,
                olt_name=excluded.olt_name,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                alias_name,
                normalize_text(alias_name),
                canonical,
                normalize_text(canonical),
                str(payload.get("fdt_code") or ""),
                str(payload.get("olt_name") or ""),
                str(payload.get("notes") or ""),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM coreman_cluster_aliases WHERE normalized_alias_name = ?", (normalize_text(alias_name),)).fetchone()
        return row_to_dict(row) or {}


def list_aliases(db_path: str | Path, query: str = "") -> list[dict]:
    q = normalize_text(query)
    with connect(db_path) as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM coreman_cluster_aliases WHERE normalized_alias_name LIKE ? OR normalized_canonical_name LIKE ? ORDER BY alias_name",
                (like, like),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM coreman_cluster_aliases ORDER BY alias_name").fetchall()
        return [row_to_dict(row) or {} for row in rows]


def delete_alias(db_path: str | Path, alias_id: int) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM coreman_cluster_aliases WHERE id = ?", (int(alias_id),))
        return bool(cursor.rowcount)


def upsert_sheet(db_path: str | Path, payload: dict) -> dict:
    olt_name = str(payload.get("olt_name") or "").strip()
    segment_name = str(payload.get("segment_name") or "").strip()
    if not olt_name or not segment_name:
        raise ValueError("OLT dan segment wajib diisi")
    sheet_name = str(payload.get("sheet_name") or f"{olt_name} - SEG {segment_name}").strip()
    now = utc_ts()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO coreman_sheet_registry (
                olt_name, segment_name, sheet_name, spreadsheet_id, template_sheet_name,
                sheet_gid, row_count, status, last_synced_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sheet_name) DO UPDATE SET
                olt_name=excluded.olt_name,
                segment_name=excluded.segment_name,
                spreadsheet_id=excluded.spreadsheet_id,
                template_sheet_name=excluded.template_sheet_name,
                sheet_gid=excluded.sheet_gid,
                row_count=excluded.row_count,
                status=excluded.status,
                last_synced_at=excluded.last_synced_at,
                updated_at=excluded.updated_at
            """,
            (
                olt_name, segment_name, sheet_name,
                str(payload.get("spreadsheet_id") or ""),
                str(payload.get("template_sheet_name") or ""),
                str(payload.get("sheet_gid") or ""),
                int(payload.get("row_count") or 0),
                str(payload.get("status") or "active"),
                payload.get("last_synced_at") if payload.get("last_synced_at") is not None else None,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM coreman_sheet_registry WHERE sheet_name = ?", (sheet_name,)).fetchone()
        return row_to_dict(row) or {}


def list_sheets(db_path: str | Path, olt_name: str = "") -> list[dict]:
    with connect(db_path) as conn:
        if olt_name:
            rows = conn.execute("SELECT * FROM coreman_sheet_registry WHERE olt_name = ? ORDER BY olt_name, segment_name", (olt_name,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM coreman_sheet_registry ORDER BY olt_name, segment_name").fetchall()
        return [row_to_dict(row) or {} for row in rows]


def add_audit_log(db_path: str | Path, payload: dict) -> dict:
    now = utc_ts()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO coreman_audit_logs (batch_id, event_type, actor_name, source_chat_id, message_text, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload.get("batch_id"),
                str(payload.get("event_type") or "info"),
                str(payload.get("actor_name") or ""),
                str(payload.get("source_chat_id") or ""),
                str(payload.get("message_text") or ""),
                json.dumps(payload.get("payload") or {}),
                now,
            ),
        )
        row = conn.execute("SELECT * FROM coreman_audit_logs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return row_to_dict(row) or {}


def list_audit_logs(db_path: str | Path, limit: int = 200) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM coreman_audit_logs ORDER BY created_at DESC, id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        return [row_to_dict(row) or {} for row in rows]


def reset_operational_data(db_path: str | Path, include_aliases: bool = False, include_sheets: bool = False) -> dict:
    with connect(db_path) as conn:
        deleted_batches = conn.execute("DELETE FROM coreman_opm_batches").rowcount or 0
        deleted_logs = conn.execute("DELETE FROM coreman_audit_logs").rowcount or 0
        deleted_aliases = conn.execute("DELETE FROM coreman_cluster_aliases").rowcount or 0 if include_aliases else 0
        deleted_sheets = conn.execute("DELETE FROM coreman_sheet_registry").rowcount or 0 if include_sheets else 0
        sequence_names = ["coreman_opm_batches", "coreman_audit_logs"]
        if include_aliases:
            sequence_names.append("coreman_cluster_aliases")
        if include_sheets:
            sequence_names.append("coreman_sheet_registry")
        conn.executemany("DELETE FROM sqlite_sequence WHERE name = ?", [(name,) for name in sequence_names])
    return {
        "batches": int(deleted_batches),
        "logs": int(deleted_logs),
        "aliases": int(deleted_aliases),
        "sheets": int(deleted_sheets),
    }


def dashboard_summary(db_path: str | Path) -> dict:
    with connect(db_path) as conn:
        total_batches = int(conn.execute("SELECT COUNT(*) FROM coreman_opm_batches").fetchone()[0])
        pending_batches = int(conn.execute("SELECT COUNT(*) FROM coreman_opm_batches WHERE status IN ('pending','queued','waiting_confirmation','waiting_mancore')").fetchone()[0])
        total_aliases = int(conn.execute("SELECT COUNT(*) FROM coreman_cluster_aliases").fetchone()[0])
        total_sheets = int(conn.execute("SELECT COUNT(*) FROM coreman_sheet_registry").fetchone()[0])
        total_logs = int(conn.execute("SELECT COUNT(*) FROM coreman_audit_logs").fetchone()[0])
    return {
        "total_batches": total_batches,
        "pending_batches": pending_batches,
        "total_aliases": total_aliases,
        "total_sheets": total_sheets,
        "total_logs": total_logs,
    }


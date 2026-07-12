"""Region database lookup - province, city, district, subdistrict for Indonesia."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_REGION_DB_PATH = Path(os.getenv("REGION_DB_PATH", "/app/data/region.db"))
_LOCAL = threading.local()
_WARNING_LOCK = threading.Lock()
_WARNED_MESSAGES: set[str] = set()


def _warn_once(message: str, *args) -> None:
    rendered = message % args
    with _WARNING_LOCK:
        if rendered in _WARNED_MESSAGES:
            return
        _WARNED_MESSAGES.add(rendered)
    LOGGER.warning(message, *args)


def _region_key(value: str) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    for prefix in ("kabupaten ", "kab. ", "kab ", "kota "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    # Official datasets are inconsistent about internal spaces (for example
    # "Medansatria" versus the commonly entered "Medan Satria").
    return "".join(character for character in text if character.isalnum())


def region_key(value: str) -> str:
    """Return the normalized lookup key used by the region database."""
    return _region_key(value)


def database_ready() -> bool:
    db_path = _REGION_DB_PATH
    if not db_path.is_file():
        db_path = Path(os.getenv("BASE_DIR", "/opt/eva")) / "data" / "region.db"
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        return False
    try:
        with sqlite3.connect(str(db_path)) as conn:
            return conn.execute("SELECT 1 FROM regions LIMIT 1").fetchone() is not None
    except (OSError, sqlite3.Error):
        return False


def _get_conn() -> sqlite3.Connection | None:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        db_path = _REGION_DB_PATH
        if not db_path.is_file():
            db_path = Path(os.getenv("BASE_DIR", "/opt/eva")) / "data" / "region.db"
        if not db_path.is_file():
            _warn_once("Region DB not found at %s", db_path)
            return None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            _LOCAL.conn = conn
        except Exception as exc:
            _warn_once("Failed to open region DB: %s", exc)
            return None
    return conn


def province_for_city(city: str) -> str:
    conn = _get_conn()
    if conn is None:
        return "Lainnya"
    row = conn.execute("SELECT DISTINCT province FROM regions WHERE city = ? COLLATE NOCASE LIMIT 1", (city,)).fetchone()
    if not row:
        row = conn.execute("SELECT DISTINCT province FROM regions WHERE city_key = ? LIMIT 1", (_region_key(city),)).fetchone()
    return row["province"] if row else "Lainnya"


def city_for_kecamatan(district: str) -> str:
    conn = _get_conn()
    if conn is None:
        return "Lainnya"
    row = conn.execute("SELECT DISTINCT city FROM regions WHERE district = ? COLLATE NOCASE AND city != '' LIMIT 1", (district,)).fetchone()
    if not row:
        row = conn.execute("SELECT DISTINCT city FROM regions WHERE district_key = ? AND city != '' LIMIT 1", (_region_key(district),)).fetchone()
    return row["city"] if row else "Lainnya"


def subdistrict_for_district(district: str) -> list[str]:
    conn = _get_conn()
    if conn is None:
        return []
    rows = conn.execute("SELECT DISTINCT subdistrict FROM regions WHERE district = ? COLLATE NOCASE AND subdistrict != '' ORDER BY subdistrict", (district,)).fetchall()
    if not rows:
        rows = conn.execute("SELECT DISTINCT subdistrict FROM regions WHERE district_key = ? AND subdistrict != '' ORDER BY subdistrict", (_region_key(district),)).fetchall()
    return [r["subdistrict"] for r in rows]


def resolve_admin_path(district: str = "", subdistrict: str = "", city: str = "") -> dict[str, str]:
    """Look up full admin path (province/city/district/subdistrict) for a district or subdistrict."""
    conn = _get_conn()
    if conn is None:
        return {}

    def fetch(use_keys: bool) -> dict[str, str]:
        suffix = "_key" if use_keys else ""
        normalize = _region_key if use_keys else str
        base_filters: list[str] = []
        base_params: list[str] = []
        if city:
            base_filters.append(f"city{suffix} = ?" + ("" if use_keys else " COLLATE NOCASE"))
            base_params.append(normalize(city))
        if district:
            base_filters.append(f"district{suffix} = ?" + ("" if use_keys else " COLLATE NOCASE"))
            base_params.append(normalize(district))

        if subdistrict:
            filters = [*base_filters, f"subdistrict{suffix} = ?" + ("" if use_keys else " COLLATE NOCASE"), "subdistrict != ''"]
            row = conn.execute(
                f"SELECT province, city, district, subdistrict FROM regions WHERE {' AND '.join(filters)} LIMIT 1",
                (*base_params, normalize(subdistrict)),
            ).fetchone()
            if row:
                return dict(row)

        if district:
            filters = [*base_filters, "district != ''"]
            row = conn.execute(
                f"SELECT province, city, district, subdistrict FROM regions WHERE {' AND '.join(filters)} ORDER BY subdistrict LIMIT 1",
                base_params,
            ).fetchone()
            if row:
                return dict(row)

        if city:
            filters = [f"city{suffix} = ?" + ("" if use_keys else " COLLATE NOCASE")]
            row = conn.execute(
                f"SELECT province, city, district, subdistrict FROM regions WHERE {' AND '.join(filters)} ORDER BY city, district, subdistrict LIMIT 1",
                (normalize(city),),
            ).fetchone()
            if row:
                return dict(row)
        return {}

    return fetch(False) or fetch(True)


def resolve_from_nominatim(geo_result: dict) -> dict:
    """Fill missing admin fields from region DB after Nominatim geocoding."""
    result = dict(geo_result or {})
    if _get_conn() is None:
        return result

    district = result.get("district", "")
    sub_district = result.get("sub_district", "")
    city = result.get("regency_city", "") or result.get("city", "")
    resolved = resolve_admin_path(district=district, subdistrict=sub_district, city=city)

    if resolved:
        if not sub_district and resolved.get("subdistrict"):
            result["sub_district"] = resolved["subdistrict"]
            result["subdistrict"] = resolved["subdistrict"]
        if not city and resolved.get("city"):
            result["regency_city"] = resolved["city"]
            result["city"] = resolved["city"]
        if not result.get("province") and resolved.get("province"):
            result["province"] = resolved["province"]

    return result

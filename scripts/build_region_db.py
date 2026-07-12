#!/usr/bin/env python3
"""Build EVA's SQLite region database from the current Kemendagri dataset."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_SOURCE_URL = "https://raw.githubusercontent.com/cahyadsn/wilayah/master/db/wilayah.sql"
ROW_PATTERN = re.compile(r"\('(?P<code>[^']+)','(?P<name>(?:''|[^'])*)'\)")


def region_key(value: str) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    for prefix in ("kabupaten ", "kab. ", "kab ", "kota "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return "".join(character for character in text if character.isalnum())


def parse_source(payload: bytes) -> dict[str, str]:
    text = payload.decode("utf-8", "replace")
    rows = {
        match.group("code"): match.group("name").replace("''", "'").strip()
        for match in ROW_PATTERN.finditer(text)
    }
    if len(rows) < 80_000:
        raise ValueError(f"Dataset wilayah tidak lengkap: hanya {len(rows)} baris")
    return rows


def hierarchy_rows(names: dict[str, str]):
    districts = sorted(code for code in names if len(code.split(".")) == 3)
    villages_by_district: dict[str, list[str]] = {code: [] for code in districts}
    for code in names:
        parts = code.split(".")
        if len(parts) == 4:
            district_code = ".".join(parts[:3])
            if district_code in villages_by_district:
                villages_by_district[district_code].append(code)

    for district_code in districts:
        parts = district_code.split(".")
        province_code = parts[0]
        city_code = ".".join(parts[:2])
        province = names.get(province_code, "")
        city = names.get(city_code, "")
        district = names[district_code]
        village_codes = sorted(villages_by_district[district_code]) or [""]
        for village_code in village_codes:
            subdistrict = names.get(village_code, "")
            yield (
                province,
                city,
                district,
                subdistrict,
                province_code,
                city_code,
                district_code,
                village_code,
                region_key(city),
                region_key(district),
                region_key(subdistrict),
            )


def write_database(output_path: Path, names: dict[str, str], source_url: str, source_sha256: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="region-", suffix=".db", dir=output_path.parent)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with sqlite3.connect(temporary_path) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE regions (
                    province TEXT NOT NULL,
                    city TEXT NOT NULL,
                    district TEXT NOT NULL,
                    subdistrict TEXT NOT NULL,
                    province_code TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    district_code TEXT NOT NULL,
                    subdistrict_code TEXT NOT NULL,
                    city_key TEXT NOT NULL,
                    district_key TEXT NOT NULL,
                    subdistrict_key TEXT NOT NULL
                );
                CREATE TABLE region_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            conn.executemany(
                """INSERT INTO regions (
                    province, city, district, subdistrict,
                    province_code, city_code, district_code, subdistrict_code,
                    city_key, district_key, subdistrict_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                hierarchy_rows(names),
            )
            conn.executemany(
                "INSERT INTO region_metadata (key, value) VALUES (?, ?)",
                [
                    ("source_url", source_url),
                    ("source_sha256", source_sha256),
                    ("generated_at", datetime.now(timezone.utc).isoformat()),
                ],
            )
            conn.executescript(
                """
                CREATE INDEX idx_regions_city ON regions(city COLLATE NOCASE);
                CREATE INDEX idx_regions_district ON regions(district COLLATE NOCASE);
                CREATE INDEX idx_regions_subdistrict ON regions(subdistrict COLLATE NOCASE);
                CREATE INDEX idx_regions_city_key ON regions(city_key);
                CREATE INDEX idx_regions_district_key ON regions(district_key);
                CREATE INDEX idx_regions_subdistrict_key ON regions(subdistrict_key);
                ANALYZE;
                """
            )
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
            if integrity != "ok" or count < 75_000:
                raise ValueError(f"Region DB invalid: integrity={integrity}, rows={count}")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--output", type=Path, default=Path("data/region.db"))
    args = parser.parse_args()

    request = Request(args.source_url, headers={"User-Agent": "EVA-region-db-builder/1.0"})
    with urlopen(request, timeout=120) as response:
        payload = response.read()
    source_sha256 = hashlib.sha256(payload).hexdigest()
    names = parse_source(payload)
    write_database(args.output, names, args.source_url, source_sha256)
    with sqlite3.connect(args.output) as conn:
        count = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
    print(f"region_db={args.output} rows={count} source_sha256={source_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

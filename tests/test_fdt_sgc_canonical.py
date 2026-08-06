from pathlib import Path

import fdt_coding_db
from kmz_standardizer import FdtCodingRecord


def _isolated_catalog(db_path: Path):
    original = fdt_coding_db.OLT_DB_PATH
    fdt_coding_db.OLT_DB_PATH = db_path.parent / "missing-olt.db"
    return original


def _insert_sgc_catalog(conn):
    conn.execute(
        """
        INSERT INTO fdt_coding_olt_catalog
        (olt_code, olt_initial, olt_name_initial, olt_hostname, olt_vendor, olt_area, source_row, created_at, updated_at)
        VALUES ('BKS.100.2601', 'OLT SEGARA CITY', 'SGC', 'BKS-SGC-OLT1-FH', '', 'BEKASI', 0, 'now', 'now')
        """
    )


def test_exact_sgc_unit_canonicalizes_wrong_sheet_parent(tmp_path: Path):
    db_path = tmp_path / "fdt.db"
    original = _isolated_catalog(db_path)
    try:
        with fdt_coding_db.connect(db_path) as conn:
            _insert_sgc_catalog(conn)
            payload = fdt_coding_db.apply_catalog_to_payload(conn, {
                "olt_initial": "OLT NORTH BEKASI",
                "hub_id": "BKS.100.2601",
                "olt_hostname": "BKS-SGC-OLT1-FH",
                "olt_name_initial": "SGC",
            })
    finally:
        fdt_coding_db.OLT_DB_PATH = original

    assert payload["olt_initial"] == "OLT SEGARA CITY"
    assert payload["hub_id"] == "BKS.100.2601"
    assert payload["olt_hostname"] == "BKS-SGC-OLT1-FH"
    assert payload["olt_name_initial"] == "SGC"


def test_resync_removes_old_sheet_rows_after_catalog_canonicalization(tmp_path: Path):
    db_path = tmp_path / "fdt.db"
    original = _isolated_catalog(db_path)
    source_id = "source-bekasi"
    sheet_name = "OLT North Bekasi"
    try:
        with fdt_coding_db.connect(db_path) as conn:
            _insert_sgc_catalog(conn)
            conn.execute(
                """
                INSERT INTO fdt_coding_records
                (olt_initial, hub_id, olt_name_initial, olt_hostname, site_name, area, fdt_order, fdt_id, fdt_code, index_code, cluster_initial, source_spreadsheet_id, sheet_name, source_row, created_at, updated_at)
                VALUES ('OLT NORTH BEKASI', 'BKS.100.2601', 'SGC', 'BKS-SGC-OLT1-FH', 'OLD SGC SITE', 'Bekasi', 1, '001', 'OLD.001', '', 'OLD', ?, ?, 30, 'now', 'now')
                """,
                (source_id, sheet_name),
            )
            conn.commit()

        record = FdtCodingRecord(
            sheet_name=sheet_name,
            row_number=30,
            site_name="NEW SGC SITE",
            area="Bekasi",
            fdt_order=1,
            fdt_id="002",
            fdt_code="NEW.002",
            index_code="",
            cluster_initial="NEW",
            olt_initial="OLT NORTH BEKASI",
            hub_id="BKS.100.2601",
            olt_hostname="BKS-SGC-OLT1-FH",
            olt_name_initial="SGC",
        )
        fdt_coding_db.replace_records_by_source(db_path, [record], source_id, sheet_name.upper())

        with fdt_coding_db.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT olt_initial, site_name FROM fdt_coding_records ORDER BY id"
            ).fetchall()
    finally:
        fdt_coding_db.OLT_DB_PATH = original

    assert [(row["olt_initial"], row["site_name"]) for row in rows] == [
        ("OLT SEGARA CITY", "NEW SGC SITE")
    ]

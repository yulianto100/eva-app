from pathlib import Path

import fdt_coding_db


def test_list_olts_groups_parent_and_preserves_all_hostname_units(tmp_path: Path):
    db_path = tmp_path / "fdt.db"
    # Isolate test catalog from production OLT.db attached by connect().
    original_catalog = fdt_coding_db.OLT_DB_PATH
    fdt_coding_db.OLT_DB_PATH = tmp_path / "missing-olt.db"
    try:
        with fdt_coding_db.connect(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO fdt_coding_olt_catalog
                (olt_code, olt_initial, olt_name_initial, olt_hostname, olt_vendor, olt_area, source_row, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', 'BEKASI', 0, 'now', 'now')
                """,
                [
                    ("BKS.100.0301", "OLT NORTH BEKASI", "NBK", "BKS-NBK-OLT1-HW"),
                    ("BKS.100.0302", "OLT NORTH BEKASI", "NBK", "BKS-NBK-OLT2-HW"),
                ],
            )
            conn.commit()

        olts = fdt_coding_db.list_olts(db_path, area="Bekasi")
    finally:
        fdt_coding_db.OLT_DB_PATH = original_catalog

    assert len(olts) == 1
    assert olts[0]["olt_initial"] == "OLT NORTH BEKASI"
    assert [(unit["olt_code"], unit["olt_hostname"]) for unit in olts[0]["options"]] == [
        ("BKS.100.0301", "BKS-NBK-OLT1-HW"),
        ("BKS.100.0302", "BKS-NBK-OLT2-HW"),
    ]
    assert olts[0]["hub_id"] == "BKS.100.0301"
    assert olts[0]["olt_hostname"] == "BKS-NBK-OLT1-HW"


def test_summary_template_keeps_selected_unit_pairing():
    template = (Path(__file__).resolve().parents[1] / "templates" / "fdt_coding.html").read_text()
    assert "const unit=currentUnitOption();" in template
    assert "els.fields.hubId.value = unit ? (unit.olt_code || '')" in template
    assert "els.fields.oltHostname.value = unit ? (unit.olt_hostname || '')" in template
    assert "new URLSearchParams({area:els.area.value||''})" in template

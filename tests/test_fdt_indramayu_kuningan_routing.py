import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdt_sync_service

SOURCE_ID = "1PNnmDDFgmks0bEkGF4jF8c3Vlv9-hzF3YushyP9xQWs"


def parse(sheet_name, rows):
    return fdt_sync_service._parse_sheet_data(SOURCE_ID, sheet_name, rows)


def test_legacy_idm_sheet_routes_each_base_hub_to_its_canonical_olt():
    rows = [
        ["NO", "SITE", "FDT ID", "x", "SEGMENT", "OLT CODE", "FAT CODE"],
        ["001", "PERUM TAMAN", "PSR.001", "PSR", "S01", "IDM.100.0101.PSR.001", "=FORMULA()"],
        ["002", "PERUM BARU", "PSR.002", "PSR", "S01", "IDM.100.0102.PSR.002", "=FORMULA()"],
        ["003", "PERUM TIGA", "PSR.003", "PSR", "S01", "IDM.100.0103.PSR.003", "=FORMULA()"],
    ]
    records = parse("OLT INDRAMAYU", rows)
    assert [(r.hub_id, r.olt_initial, r.area, r.fdt_code) for r in records] == [
        ("IDM.100.0101", "OLT INDRAMAYU", "Indramayu", "PSR.001"),
        ("IDM.100.0102", "OLT INDRAMAYU 02", "Indramayu", "PSR.002"),
        ("IDM.100.0103", "OLT INDRAMAYU 03", "Indramayu", "PSR.003"),
    ]


def test_source_rows_with_same_parent_and_row_number_stay_separate_per_tab():
    import tempfile
    import fdt_coding_db
    from kmz_standardizer import FdtCodingRecord

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "fdt.db"
        source = SOURCE_ID
        first = FdtCodingRecord(
            sheet_name="OLT INDRAMAYU", row_number=2, site_name="SITE SATU", fdt_order=1,
            fdt_id="001", fdt_code="PSR.001", index_code="S01", cluster_initial="PSR",
            area="Indramayu", olt_initial="OLT INDRAMAYU", hub_id="IDM.100.0101",
            olt_hostname="IDM-IDM-OLT1-FH", olt_name_initial="IDM",
        )
        second = FdtCodingRecord(
            sheet_name="OLT LOHBENER", row_number=2, site_name="SITE DUA", fdt_order=1,
            fdt_id="001", fdt_code="BUL.001", index_code="S01", cluster_initial="BUL",
            area="Indramayu", olt_initial="OLT INDRAMAYU", hub_id="IDM.100.0101",
            olt_hostname="IDM-IDM-OLT1-FH", olt_name_initial="IDM",
        )
        assert fdt_coding_db.replace_records_by_source(db_path, [first], source, "OLT INDRAMAYU") == 1
        assert fdt_coding_db.replace_records_by_source(db_path, [second], source, "OLT LOHBENER") == 1
        rows = fdt_coding_db.list_records(db_path, area="Indramayu", limit=10)
        assert len(rows) == 2
        assert {(row["sheet_name"], row["site_name"]) for row in rows} == {
            ("OLT INDRAMAYU", "SITE SATU"),
            ("OLT LOHBENER", "SITE DUA"),
        }


def test_legacy_kuningan_tab_imports_kng_but_skips_cwg():
    rows = [
        ["NO", "SITE", "FDT ID", "x", "SEGMENT", "OLT CODE"],
        ["001", "SITE KNG", "KNG.001", "KNG", "S01", "KNG.100.0101.KNG.001"],
        ["002", "SITE CWG", "CWG.001", "CWG", "S01", "CWG.100.0201.CWG.001"],
        ["003", "SITE CBG", "CBG.001", "CBG", "S01", "KNG.100.0201.CBG.001"],
    ]
    records = parse("KUNINGAN", rows)
    assert [(r.hub_id, r.olt_initial, r.area) for r in records] == [
        ("KNG.100.0101", "OLT KUNINGAN", "Kuningan"),
        ("KNG.100.0201", "OLT CIAWIGEBANG", "Kuningan"),
    ]


def test_legacy_parser_keeps_sleman_three_segment_code_and_skips_delete_marker():
    rows = [
        ["NO", "SITE", "FDT ID", "x", "SEGMENT", "OLT CODE"],
        ["025", "SLEMAN SITE", "SLEMAN.01.025", "SLEMAN", "S01", "IDM.100.0201.SLEMAN.01.025"],
        ["", "", "delete", "", "", ""],
    ]
    records = parse("OLT LOHBENER", rows)
    assert len(records) == 1
    assert records[0].fdt_code == "SLEMAN.01.025"
    assert records[0].fdt_id == "025"
    assert records[0].olt_initial == "OLT LOHBENER"
    assert records[0].area == "Indramayu"


def test_legacy_source_accepts_only_declared_tabs():
    assert fdt_sync_service._should_sync_sheet(SOURCE_ID, "OLT INDRAMAYU") is True
    assert fdt_sync_service._should_sync_sheet(SOURCE_ID, "OLT LOHBENER") is True
    assert fdt_sync_service._should_sync_sheet(SOURCE_ID, "KUNINGAN") is True
    assert fdt_sync_service._should_sync_sheet(SOURCE_ID, "RANDOM") is False


def test_indramayu_and_kuningan_form_area_options_are_declared():
    template = (Path(__file__).resolve().parents[1] / "templates" / "fdt_coding.html").read_text()
    for area in ("Indramayu", "Kuningan"):
        assert f'<option value="{area}">{area}</option>' in template
    assert 'id="legacySegmentField"' in template
    assert 'function setLegacyTemplateMode()' in template
    assert 'index_code: els.fields.indexCode.value' in template


def test_regular_source_area_still_uses_source_label():
    assert fdt_sync_service.extract_sheet_area("OLT RANDOM", "Coding FTTH - Jakarta Area Update 2021") == "Jakarta"


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("FDT_INDRAMAYU_KUNINGAN_ROUTING_TESTS_PASS")

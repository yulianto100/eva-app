import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdt_coding_db


def test_legacy_area_preserves_segment_on_create_and_update():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fdt.db"
        created = fdt_coding_db.create_records(db_path, {
            "area": "Indramayu",
            "olt_initial": "OLT INDRAMAYU",
            "hub_id": "IDM.100.0101",
            "olt_hostname": "IDM-IDM-OLT1-FH",
            "olt_name_initial": "IDM",
            "site_name": "SITE BARU",
            "fdt_id": "155",
            "fdt_order": "1",
            "total_fdt": "1",
            "fdt_code": "PSR",
            "cluster_initial": "PSR",
            "index_code": "S02",
        })[0]
        assert created["index_code"] == "S02"
        updated = fdt_coding_db.update_record(db_path, created["id"], {
            "site_name": "SITE EDIT",
            "fdt_id": "156",
            "cluster_initial": "PSE",
        })
        assert updated["index_code"] == "S02"
        assert updated["fdt_code"] == "PSE.156"


if __name__ == "__main__":
    test_legacy_area_preserves_segment_on_create_and_update()
    print("FDT_LEGACY_SEGMENT_TESTS_PASS")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdt_sync_service


class FakeWorksheet:
    def __init__(self):
        self.values = [
            ["NO", "SITE", "FDT ID", "x", "SEGMENT", "OLT CODE", "FAT"],
            ["001", "SITE SATU", "PSR.001", "=FORMULA_D2", "S01", "=FORMULA_F2", "=FORMULA_G2"],
        ]
        self.updates = []
        self.deleted_rows = []

    def get_values(self, **_kwargs):
        return self.values

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)

    def delete_rows(self, row):
        self.deleted_rows.append(row)


def test_legacy_delete_clears_user_cells_without_deleting_formula_row():
    worksheet = FakeWorksheet()
    record = {
        "id": 99,
        "site_name": "SITE SATU",
        "fdt_id": "001",
        "fdt_code": "PSR.001",
        "index_code": "S01",
        "sheet_name": "OLT INDRAMAYU",
    }
    columns = {
        "legacy_template": 1,
        "fdt_no": 1,
        "site": 2,
        "fdt_code": 3,
        "segment": 5,
    }

    cleared = fdt_sync_service._clear_record_rows(worksheet, columns, record, preferred_row=2)

    assert cleared == [2]
    assert worksheet.deleted_rows == []
    assert [(item["range"], item["values"]) for item in worksheet.updates] == [
        ("B2", [[""]]),
        ("C2", [[""]]),
        ("E2", [[""]]),
    ]


if __name__ == "__main__":
    test_legacy_delete_clears_user_cells_without_deleting_formula_row()
    print("FDT_LEGACY_DELETE_TESTS_PASS")
